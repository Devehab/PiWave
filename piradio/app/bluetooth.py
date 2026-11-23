"""Bluetooth receiver: the Pi shows up on the phone as a speaker ("PiRadio")
and whatever the phone plays goes out on FM.

    phone --A2DP--> bluetoothd + BlueALSA --> ffmpeg (ALSA capture with the
    same volume / pre-emphasis / limiter as the songs) --> LiveSource --> player

Pairing is only possible while "pairing mode" is switched on from the web
page (the adapter is not pairable otherwise, and BlueZ switches it off by
itself after 3 minutes). Phones that paired before reconnect on their own.

Everything goes through `busctl` (part of systemd, always installed): one
GetManagedObjects call returns the adapter, the devices, the A2DP transports
and the phone's now-playing info. The tests use a fake busctl.
"""
import collections
import json
import logging
import os
import shlex
import shutil
import subprocess
import threading
import time

from engine import FRAME, RATE, LiveSource, audio_filters, peak_of

log = logging.getLogger("piradio.bt")

BLUEZ = "org.bluez"
ADAPTER = "org.bluez.Adapter1"
DEVICE = "org.bluez.Device1"
TRANSPORT = "org.bluez.MediaTransport1"
MEDIA_PLAYER = "org.bluez.MediaPlayer1"

PAIRING_SECONDS = 180
LOUD = 0.004             # peak above this counts as real sound (about -48 dBFS)
TAKEOVER_AFTER = 1.0     # s of sound before the phone interrupts a playing song
RELEASE_AFTER = 6.0      # s of silence before the song library gets the air back
UNSUPPRESS_AFTER = 2.0   # phone paused this long -> its next "play" takes the air again
REFRESH = 3.0            # s between BlueZ polls
PREROLL = 0.3            # s of phone audio kept so the start of a sound is not cut


class BTError(Exception):
    pass


def unwrap(v):
    """busctl --json values -> plain Python (variants are {"type": .., "data": ..})."""
    if isinstance(v, dict):
        if set(v) == {"type", "data"}:
            return unwrap(v["data"])
        return {k: unwrap(x) for k, x in v.items()}
    if isinstance(v, list):
        return [unwrap(x) for x in v]
    return v


def mac_of(path):
    """/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF -> AA:BB:CC:DD:EE:FF"""
    for part in path.split("/"):
        if part.startswith("dev_"):
            return part[4:].replace("_", ":")
    return ""


FRIENDLY = (
    ("not ready", "البلوتوث مش جاهز"),
    ("does not exist", "الجهاز مش موجود"),
    ("in progress", "في عملية شغالة، استنى ثواني"),
    ("page-timeout", "التلفون مش قريب أو البلوتوث عليه مطفي"),
    ("timeout", "التلفون ما رد. تأكد إنه البلوتوث عليه شغال وقريب"),
    ("profile-unavailable", "التلفون رفض الاتصال. افتح البلوتوث بالتلفون واختار PiRadio"),
    ("not running", "خدمة البلوتوث مش شغالة"),
    ("was not provided", "خدمة البلوتوث مش شغالة"),
)


def friendly(err):
    low = str(err).lower()
    for key, msg in FRIENDLY:
        if key in low:
            return msg
    return str(err) or "خطأ بالبلوتوث"


class BlueZ:
    """Tiny BlueZ client on top of busctl."""

    def __init__(self, busctl="busctl"):
        self.busctl = busctl

    def _run(self, args, timeout=8):
        try:
            r = subprocess.run([self.busctl] + args, capture_output=True, text=True,
                               timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as e:
            return 1, "", str(e)
        return r.returncode, r.stdout, (r.stderr or "").strip()

    def objects(self):
        rc, out, err = self._run(["--json=short", "call", BLUEZ, "/",
                                  "org.freedesktop.DBus.ObjectManager", "GetManagedObjects"])
        if rc != 0:
            raise BTError(err or "bluetoothd is not running")
        data = json.loads(out)
        d = data.get("data") if isinstance(data, dict) else None
        if isinstance(d, list):
            d = d[0] if d else {}
        return unwrap(d or {})

    def set(self, path, iface, prop, sig, value):
        if sig == "b":
            value = "true" if value else "false"
        rc, _, err = self._run(["set-property", BLUEZ, path, iface, prop, sig, str(value)])
        if rc != 0:
            raise BTError(err)

    def call(self, path, iface, method, *args, timeout=10):
        rc, _, err = self._run(["--timeout=%d" % timeout, "call", BLUEZ, path, iface, method]
                               + [str(a) for a in args], timeout=timeout + 5)
        if rc != 0:
            raise BTError(err)


class BluetoothManager:
    def __init__(self, player, settings, ffmpeg="ffmpeg", errlog="/tmp/piradio-bt.log",
                 on_live=None, setup_script=None):
        self.player = player
        self.settings = settings
        self.ffmpeg = ffmpeg
        self.errlog = errlog
        self.on_live = on_live or (lambda: None)
        self.setup_script = setup_script
        self.bus = BlueZ(os.environ.get("PIRADIO_BUSCTL", "busctl"))
        self.installed = self._detect_installed()
        self.available = False
        self.error = ""
        self.pairing_until = 0.0
        self.just_paired = None            # (name, wall time)
        self.live = None                   # LiveSource while the phone is on air
        self.suppressed = False            # user chose the song library over the phone
        self.install_state = None          # {"running", "ok", "log"} for the page
        self._lock = threading.RLock()
        self._objs = {}
        self._adapter = None
        self._refreshed = 0.0
        self._known_paired = None
        self._configured = False
        self._cap = None                   # running capture: proc, mac, name
        self._cap_retry = 0.0
        self._ring = collections.deque()
        self._ring_bytes = 0
        self._last_audio = 0.0
        self._last_loud = 0.0
        self._loud_since = None
        self._started = time.monotonic()
        self._next_reconnect = self._started + 8
        self._reconnecting = False
        self._stop = threading.Event()
        self._wake = threading.Event()

    @staticmethod
    def _detect_installed():
        if os.environ.get("PIRADIO_BT_INSTALLED") == "1":
            return True
        return bool(shutil.which("bluealsa") or shutil.which("bluealsad")) and \
            bool(shutil.which("busctl"))

    # ------------------------------------------------------------------ thread
    def start(self):
        threading.Thread(target=self._loop, daemon=True, name="bluetooth").start()

    def shutdown(self):
        self._stop.set()
        self._wake.set()
        self._stop_capture(resume=False)

    def poke(self):
        self._refreshed = 0.0
        self._wake.set()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:   # never let the loop die
                log.exception("bluetooth loop error")
            self._wake.wait(0.5)
            self._wake.clear()

    def _tick(self):
        if not self.installed:
            return
        now = time.monotonic()
        if now - self._refreshed >= REFRESH:
            self.refresh()
        if not self.available:
            self._stop_capture()
            return
        if not self._configured:
            self._configure()
        if self.pairing_until and now > self.pairing_until:
            try:
                self.set_pairing(False)
            except BTError as e:
                log.warning("could not end pairing mode: %s", e)
        dev = self._audio_device()
        enabled = self.settings.get("bt_enabled")
        if not enabled or dev is None:
            self._stop_capture()
        elif self._cap is None or self._cap["mac"] != dev["mac"]:
            if now >= self._cap_retry:
                self._start_capture(dev)
        self._arbitrate(now)
        self._auto_reconnect(now, dev)

    # ------------------------------------------------------------------ BlueZ state
    def refresh(self):
        try:
            objs, err = self.bus.objects(), ""
        except (BTError, ValueError) as e:
            objs, err = {}, str(e)
        with self._lock:
            self._objs = objs
            self._refreshed = time.monotonic()
            self._adapter = next((p for p in sorted(objs) if ADAPTER in objs[p]), None)
            was = self.available
            self.available = self._adapter is not None
            self.error = "" if self.available else friendly(err) if err else \
                "ما لقيت قطعة البلوتوث بالجهاز"
        if was and not self.available:
            self._configured = False
        self._check_pairings()

    def _adapter_props(self):
        with self._lock:
            return dict(self._objs.get(self._adapter, {}).get(ADAPTER, {})) if self._adapter else {}

    def devices(self):
        with self._lock:
            objs = dict(self._objs)
        transports = {}
        players = {}
        for path, ifs in objs.items():
            if TRANSPORT in ifs:
                t = ifs[TRANSPORT]
                transports[t.get("Device") or path.rsplit("/", 2)[0]] = t.get("State", "")
            if MEDIA_PLAYER in ifs:
                players[ifs[MEDIA_PLAYER].get("Device") or path.rsplit("/", 1)[0]] = path
        out = []
        for path, ifs in sorted(objs.items()):
            d = ifs.get(DEVICE)
            if d is None or (self._adapter and not path.startswith(self._adapter + "/")):
                continue
            mac = d.get("Address") or mac_of(path)
            out.append({
                "path": path, "mac": mac,
                "name": d.get("Alias") or d.get("Name") or mac,
                "paired": bool(d.get("Paired") or d.get("Bonded")),
                "connected": bool(d.get("Connected")),
                "trusted": bool(d.get("Trusted")),
                "icon": d.get("Icon", ""),
                "audio": path in transports,
                "playing": transports.get(path) == "active",
                "player": players.get(path),
            })
        return out

    def _device(self, mac):
        for d in self.devices():
            if d["mac"].upper() == str(mac).upper():
                return d
        raise BTError("الجهاز مش موجود")

    def _audio_device(self):
        """The connected phone to take audio from (one with an A2DP stream first)."""
        conn = [d for d in self.devices() if d["connected"]]
        conn.sort(key=lambda d: (not d["audio"], not d["playing"]))
        return conn[0] if conn and conn[0]["audio"] else None

    def _configure(self):
        """Once per boot: name, power, and not discoverable/pairable."""
        self._configured = True
        ad = self._adapter
        s = self.settings.all()
        for prop, sig, val in (("Alias", "s", s["bt_name"]), ("Powered", "b", s["bt_enabled"]),
                               ("Discoverable", "b", False), ("Pairable", "b", False)):
            try:
                self.bus.set(ad, ADAPTER, prop, sig, val)
            except BTError as e:
                log.warning("bluetooth %s: %s", prop, e)
        self.poke()

    def _check_pairings(self):
        devs = self.devices()
        paired = {d["path"] for d in devs if d["paired"]}
        new = [] if self._known_paired is None else \
            [d for d in devs if d["paired"] and d["path"] not in self._known_paired]
        self._known_paired = paired
        for d in devs:
            if d["paired"] and not d["trusted"]:
                try:                       # trusted = reconnects without asking
                    self.bus.set(d["path"], DEVICE, "Trusted", "b", True)
                except BTError as e:
                    log.warning("could not trust %s: %s", d["name"], e)
        for d in new:
            log.info("paired with %s (%s)", d["name"], d["mac"])
            self.just_paired = (d["name"], time.time())
            if self.pairing_until:
                try:
                    self.set_pairing(False)
                except BTError:
                    pass
        conn = next((d for d in devs if d["connected"] and d["paired"]), None)
        if conn and self.settings.get("bt_last") != conn["mac"]:
            try:
                self.settings.update(bt_last=conn["mac"])
            except (OSError, ValueError):
                pass

    # ------------------------------------------------------------------ actions
    def _need_adapter(self):
        if not self.installed:
            raise BTError("البلوتوث مش مثبت على الجهاز")
        if not self.available:
            self.refresh()
            if not self.available:
                raise BTError(self.error or "البلوتوث مش جاهز")
        return self._adapter

    def set_pairing(self, on):
        ad = self._need_adapter()
        if on:
            if not self.settings.get("bt_enabled"):
                raise BTError("شغّل البلوتوث أول")
            self.bus.set(ad, ADAPTER, "Powered", "b", True)
            self.bus.set(ad, ADAPTER, "PairableTimeout", "u", PAIRING_SECONDS)
            self.bus.set(ad, ADAPTER, "DiscoverableTimeout", "u", PAIRING_SECONDS)
            self.bus.set(ad, ADAPTER, "Pairable", "b", True)
            self.bus.set(ad, ADAPTER, "Discoverable", "b", True)
            self.pairing_until = time.monotonic() + PAIRING_SECONDS
            self.just_paired = None
            log.info("pairing mode on for %d s", PAIRING_SECONDS)
        else:
            self.pairing_until = 0.0
            self.bus.set(ad, ADAPTER, "Discoverable", "b", False)
            self.bus.set(ad, ADAPTER, "Pairable", "b", False)
        self.poke()

    def remove(self, mac):
        ad = self._need_adapter()
        d = self._device(mac)
        if self._cap and self._cap["mac"] == d["mac"]:
            self._stop_capture()
        self.bus.call(ad, ADAPTER, "RemoveDevice", "o", d["path"])
        log.info("unpaired %s", d["name"])
        self.poke()

    def disconnect(self, mac):
        self._need_adapter()
        d = self._device(mac)
        self.bus.call(d["path"], DEVICE, "Disconnect", timeout=10)
        self.poke()

    def connect(self, mac):
        """Ask a paired phone to connect (runs in the background)."""
        self._need_adapter()
        d = self._device(mac)

        def go():
            try:
                self.bus.call(d["path"], DEVICE, "Connect", timeout=25)
            except BTError as e:
                log.info("connect %s failed: %s", d["name"], e)
                self.error = "ما قدرت أتصل بـ %s: %s" % (d["name"], friendly(e))
            self.poke()
        threading.Thread(target=go, daemon=True, name="bt-connect").start()

    def apply_enabled(self):
        on = self.settings.get("bt_enabled")
        if not on:
            self.pairing_until = 0.0
            self._stop_capture()
        if self.available:
            try:
                self.bus.set(self._adapter, ADAPTER, "Powered", "b", on)
            except BTError as e:
                raise BTError(friendly(e))
        self.poke()

    def apply_name(self):
        if self.available:
            try:
                self.bus.set(self._adapter, ADAPTER, "Alias", "s", self.settings.get("bt_name"))
            except BTError as e:
                raise BTError(friendly(e))
        self.poke()

    def media(self, action):
        """Play / pause / next / previous on the phone (AVRCP)."""
        method = {"play": "Play", "pause": "Pause", "next": "Next", "prev": "Previous"}.get(action)
        if not method:
            raise BTError("أمر مش معروف")
        self._need_adapter()
        dev = self._audio_device() or next((d for d in self.devices() if d["connected"]), None)
        if not dev or not dev["player"]:
            raise BTError("التلفون ما بيسمح بالتحكم من هون. تحكم من التلفون نفسه")
        self.bus.call(dev["player"], MEDIA_PLAYER, method, timeout=5)
        self.poke()

    def suppress(self):
        """The user picked the song library: the phone stops taking the air
        until it pauses and plays again."""
        live = self.live
        if live is not None:
            self.live = None
            self.player.end_live("bt", live, resume=False)
        if live is not None or time.monotonic() - self._last_loud < UNSUPPRESS_AFTER:
            self.suppressed = True

    def restart_capture(self):
        """Volume / pre-emphasis changed: rebuild the ffmpeg filter."""
        cap = self._cap
        if cap is not None:
            self._kill(cap)
            self._cap_retry = 0.0
            self._wake.set()

    def install(self):
        """Install the Bluetooth packages (needs internet: home Wi-Fi)."""
        if self.install_state and self.install_state.get("running"):
            raise BTError("التثبيت شغال هلأ")
        script = self.setup_script
        if not script or not os.path.isfile(script):
            raise BTError("سكربت التثبيت مش موجود")
        self.install_state = {"running": True, "ok": None, "log": ""}

        def go():
            try:
                r = subprocess.run(["bash", script, "--install"], capture_output=True, text=True,
                                   timeout=1800)
                out = (r.stdout + r.stderr).strip().splitlines()
                ok = r.returncode == 0
                msg = "\n".join(out[-15:])
                if r.returncode == 3:
                    msg = "ما في إنترنت. وصّل الجهاز بواي فاي البيت (الإعدادات ← اتصل بواي فاي البيت) وجرّب مرة ثانية."
            except (OSError, subprocess.TimeoutExpired) as e:
                ok, msg = False, str(e)
            self.install_state = {"running": False, "ok": ok, "log": msg}
            self.installed = self._detect_installed()
            self._configured = False
            self.poke()
        threading.Thread(target=go, daemon=True, name="bt-install").start()

    # ------------------------------------------------------------------ audio
    def capture_cmd(self, mac):
        override = os.environ.get("PIRADIO_BT_INPUT")
        if override:
            src = shlex.split(override.replace("{mac}", mac))
        else:
            src = ["-f", "alsa", "-i", "bluealsa:DEV=%s,PROFILE=a2dp" % mac]
        return ([self.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
                 "-fflags", "nobuffer"] + src +
                ["-af", audio_filters(self.player.volume, self.player.preemphasis),
                 "-ac", "2", "-ar", str(RATE), "-f", "s16le", "-acodec", "pcm_s16le",
                 "-flush_packets", "1", "pipe:1"])

    def _start_capture(self, dev):
        self._stop_capture(resume=False)
        try:
            err = open(self.errlog, "wb")
            proc = subprocess.Popen(self.capture_cmd(dev["mac"]), stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=err, close_fds=True)
        except OSError as e:
            self.error = "ما قدرت أشغّل ffmpeg للبلوتوث: %s" % e
            self._cap_retry = time.monotonic() + 15
            return
        cap = {"proc": proc, "mac": dev["mac"], "name": dev["name"], "err": err,
               "started": time.monotonic()}
        with self._lock:
            self._cap = cap
            self._ring.clear()
            self._ring_bytes = 0
        log.info("receiving Bluetooth audio from %s (%s)", dev["name"], dev["mac"])
        threading.Thread(target=self._read_capture, args=(cap,), daemon=True,
                         name="bt-audio").start()

    def _read_capture(self, cap):
        fd = cap["proc"].stdout.fileno()
        rest = b""
        keep = int(PREROLL * RATE) * FRAME
        while True:
            try:
                d = os.read(fd, 16384)
            except OSError:
                break
            if not d:
                break
            now = time.monotonic()
            self._last_audio = now
            if peak_of(d) > LOUD:
                self._last_loud = now
                if self._loud_since is None:
                    self._loud_since = now
            data = rest + d
            cut = len(data) - len(data) % FRAME
            data, rest = data[:cut], data[cut:]
            live = self.live
            if live is not None:
                live.push(data)
            else:
                with self._lock:
                    self._ring.append(data)
                    self._ring_bytes += len(data)
                    while self._ring_bytes > keep and len(self._ring) > 1:
                        self._ring_bytes -= len(self._ring.popleft())
        rc = cap["proc"].wait()
        try:
            cap["err"].close()
        except OSError:
            pass
        with self._lock:
            mine = self._cap is cap
            if mine:
                self._cap = None
                # ran a while (e.g. the stream closed on pause): reopen fast so the
                # next song isn't cut; failed right away: don't hammer it
                ran = time.monotonic() - cap["started"]
                self._cap_retry = time.monotonic() + (1 if ran > 10 else 3)
        if mine:
            log.info("Bluetooth audio from %s stopped (rc=%s)", cap["name"], rc)
            self._end_live(resume=True)
            self._wake.set()

    @staticmethod
    def _kill(cap):
        p = cap["proc"]
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(3)
            except subprocess.TimeoutExpired:
                p.kill()

    def _stop_capture(self, resume=True):
        with self._lock:
            cap, self._cap = self._cap, None
        if cap is not None:
            self._kill(cap)
        self._end_live(resume=resume)
        self._loud_since = None

    def _end_live(self, resume=True):
        live, self.live = self.live, None
        if live is not None:
            self.player.end_live("bt", live, resume=resume)

    def _arbitrate(self, now):
        if self._cap is None or now - self._last_loud > 1.0:
            self._loud_since = None
        live = self.live
        if live is not None:
            if live.closed:                  # taken off air by someone else
                self.live = None
            elif now - self._last_loud > RELEASE_AFTER:
                log.info("phone quiet for %.0f s: back to the song library", RELEASE_AFTER)
                self._end_live(resume=True)
            return
        if self.suppressed:
            if now - self._last_loud > UNSUPPRESS_AFTER:
                self.suppressed = False
            return
        if not self.settings.get("bt_takeover") or self._cap is None or self._loud_since is None:
            return
        snap = self.player.snapshot()
        busy = snap["state"] == "playing" or snap["live"] is not None
        if busy and now - self._loud_since < TAKEOVER_AFTER:
            return                           # a short notification "ding" doesn't stop the song
        src = LiveSource("bt", label=self._cap["name"], target=0.25, max_delay=1.0)
        with self._lock:
            src.push(b"".join(self._ring))
            self._ring.clear()
            self._ring_bytes = 0
        self.live = src
        self.player.begin_live(src)
        self.on_live()
        log.info("Bluetooth audio from %s is on air", self._cap["name"])

    def _auto_reconnect(self, now, dev):
        """Right after boot, call the last phone (like a car stereo does)."""
        if dev is not None or self._reconnecting or now < self._next_reconnect \
                or now - self._started > 240 or not self.settings.get("bt_enabled"):
            return
        devs = [d for d in self.devices() if d["paired"] and not d["connected"]]
        if any(d["connected"] for d in self.devices()) or not devs:
            return
        last = self.settings.get("bt_last")
        devs.sort(key=lambda d: d["mac"] != last)
        target = devs[0]
        self._next_reconnect = now + 30
        self._reconnecting = True

        def go():
            try:
                self.bus.call(target["path"], DEVICE, "Connect", timeout=20)
                log.info("reconnected to %s", target["name"])
            except BTError:
                pass
            finally:
                self._reconnecting = False
                self.poke()
        threading.Thread(target=go, daemon=True, name="bt-reconnect").start()

    # ------------------------------------------------------------------ for the page
    def track(self):
        """What the phone is playing (AVRCP), if it tells us."""
        dev = self._audio_device()
        if not dev or not dev["player"]:
            return None
        with self._lock:
            p = self._objs.get(dev["player"], {}).get(MEDIA_PLAYER, {})
        t = p.get("Track") or {}
        title = (t.get("Title") or "").strip()
        if not title:
            return None
        return {"title": title, "artist": (t.get("Artist") or "").strip() or None,
                "status": p.get("Status", "")}

    def summary(self):
        """Cheap snapshot for /api/state (no BlueZ call)."""
        now = time.monotonic()
        cap = self._cap
        return {
            "installed": self.installed, "available": self.available,
            "enabled": self.settings.get("bt_enabled"),
            "takeover": self.settings.get("bt_takeover"),
            "connected": cap["name"] if cap else next(
                (d["name"] for d in self.devices() if d["connected"]), None),
            "streaming": cap is not None and now - self._last_loud < 2.0,
            "on_air": self.live is not None,
            "suppressed": self.suppressed,
            "pairing_left": max(0, round(self.pairing_until - now)) if self.pairing_until else 0,
            "track": self.track() if cap else None,
        }

    def status(self):
        if self.installed and time.monotonic() - self._refreshed > 1.5:
            self.refresh()
        ad = self._adapter_props()
        jp = self.just_paired
        return dict(
            self.summary(),
            name=ad.get("Alias") or self.settings.get("bt_name"),
            powered=bool(ad.get("Powered")),
            discoverable=bool(ad.get("Discoverable")),
            error=self.error,
            devices=[{k: d[k] for k in ("mac", "name", "paired", "connected", "icon", "audio",
                                        "playing")}
                     for d in self.devices() if d["paired"] or d["connected"]],
            just_paired=jp[0] if jp and time.time() - jp[1] < 30 else None,
            install=self.install_state,
        )
