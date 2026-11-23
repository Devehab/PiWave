"""RadioApp: wires settings, library, transmitter, player and feeder together."""
import logging
import os
import secrets
import threading
import time

import system
from bluetooth import BluetoothManager, BTError
from engine import Feeder, LiveSource, Player, Transmitter, TONE_ID, mono_to_stereo
from library import Library
from rdstext import ps_segments, to_rds
from settings import Settings

log = logging.getLogger("piradio")

BOOT_MUSIC_DIRS = ["/boot/firmware/piradio-music", "/boot/piradio-music"]
RELEASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PTT_IDLE = 3.0          # s without microphone audio -> the phone went away, end live
PTT_MAX = 15 * 60       # one push-to-talk session at most 15 minutes


class LiveEnded(Exception):
    """The push-to-talk session is over (replaced, timed out or finished)."""


class RadioApp:
    def __init__(self, data_dir, tx_binary, ffmpeg="ffmpeg", ffprobe="ffprobe",
                 run_dir="/run/piradio", boot_music_dirs=None):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        os.makedirs(run_dir, exist_ok=True)
        self.boot_music_dirs = BOOT_MUSIC_DIRS if boot_music_dirs is None else boot_music_dirs
        self.settings = Settings(os.path.join(data_dir, "settings.json"))
        self.library = Library(os.path.join(data_dir, "music"),
                               os.path.join(data_dir, "library.json"), ffprobe)
        self.tx = Transmitter(tx_binary, os.path.join(run_dir, "rds_ctl"))
        self.player = Player(self.library, ffmpeg, os.path.join(run_dir, "ffmpeg.log"))
        self.feeder = Feeder(self.tx, self.player)
        self.bt = BluetoothManager(self.player, self.settings, ffmpeg,
                                   errlog=os.path.join(run_dir, "bt-ffmpeg.log"),
                                   on_live=self._wanted_on_air,
                                   setup_script=os.path.join(RELEASE_DIR, "system",
                                                             "setup-bluetooth.sh"))
        self._ptt = None
        self._ptt_lock = threading.Lock()
        s = self.settings.all()
        self.tx.params.update(freq=s["frequency"], ps=s["station_name"], pi=s["pi_code"],
                              ppm=s["ppm"], rt=to_rds(s["radiotext"], 64))
        self.player.volume = s["volume"]
        self.player.preemphasis = s["preemphasis"]
        self.player.shuffle = s["shuffle"]
        self.player.repeat = s["repeat"]
        self._stop = threading.Event()
        self._last_seq = -1
        self._last_save = 0.0
        self._ps_segs, self._ps_idx, self._ps_next = [], 0, 0.0
        self._ps_shown = None
        self.imported = 0

    # ------------------------------------------------------------------ start/stop
    def start(self, background=True):
        """Carrier first (radio comes on air fast), then the library, then autoplay."""
        self.feeder.start()
        self.bt.start()
        threading.Thread(target=self._housekeeping, daemon=True, name="housekeeping").start()
        if self.settings.get("transmit_on_boot"):
            self.tx.turn_on()
        if background:
            threading.Thread(target=self._boot_library, daemon=True, name="boot-library").start()
        else:
            self._boot_library()

    def _boot_library(self):
        for d in self.boot_music_dirs:
            try:
                self.imported += self.library.import_folder(d)
            except OSError as e:
                log.warning("import from %s failed: %s", d, e)
        self.library.scan()
        s = self.settings.all()
        untouched = self.player.state == "stopped" and self.player.change_seq == 0
        if s["autoplay"] and untouched and self.library.ids():
            last = s["last_track"]
            if last and self.library.get(last):
                self.player.play(last, s["last_position"])
            else:
                self.player.play()
        log.info("PiRadio ready (%d songs, %.2f MHz, %d imported from SD card)",
                 len(self.library.ids()), s["frequency"], self.imported)

    def shutdown(self):
        self._stop.set()
        self._save_position(force=True)
        self.bt.shutdown()
        self.feeder.stop()
        self.player.shutdown()
        self.tx.shutdown()

    # ------------------------------------------------------------------ background
    def _current_rt(self):
        s = self.settings.all()
        snap = self.player.snapshot()
        live = snap["live"]
        if live and live["kind"] == "ptt":
            return to_rds("LIVE - " + (s["radiotext"] or s["station_name"]), 64)
        if live and live["kind"] == "bt":
            bt = self.bt.track()
            if s["rds_song_text"] and bt:
                text = bt["title"] if not bt["artist"] else "%s - %s" % (bt["artist"], bt["title"])
                rt = to_rds(text, 64)
                if rt:
                    return rt
            return to_rds(s["radiotext"], 64) or " "
        t = snap["track"]
        if s["rds_song_text"] and t and snap["state"] == "playing" and t["id"] != TONE_ID:
            text = t["title"] if not t.get("artist") else "%s - %s" % (t["artist"], t["title"])
            rt = to_rds(text, 64)
            if rt:
                return rt
        return to_rds(s["radiotext"], 64) or " "

    def _save_position(self, force=False):
        snap = self.player.snapshot()
        t = snap["track"]
        if not t or t["id"] == TONE_ID:
            return
        now = time.monotonic()
        if force or now - self._last_save > 30:
            self._last_save = now
            try:
                self.settings.update(last_track=t["id"], last_position=snap["position"])
            except (OSError, ValueError) as e:
                log.warning("could not save position: %s", e)

    def _housekeeping(self):
        while not self._stop.wait(0.5):
            try:
                seq = self.player.change_seq
                if seq != self._last_seq:
                    self._last_seq = seq
                    self._save_position(force=True)
                    self._ps_segs, self._ps_idx, self._ps_next = [], 0, 0.0
                elif self.player.state == "playing":
                    self._save_position()
                self.tx.configure(rt=self._current_rt())   # only sent when it changed
                self._scroll_ps()
                self._ptt_watchdog()
            except Exception:   # never let the background loop die
                log.exception("housekeeping error")

    def _scroll_ps(self):
        """Optionally alternate the station name with the song name (8 chars at a time)."""
        s = self.settings.all()
        station = s["station_name"]
        snap = self.player.snapshot()
        t = snap["track"]
        want_scroll = s["ps_scroll"] and snap["state"] == "playing" and t and t["id"] != TONE_ID \
            and not snap["live"]
        now = time.monotonic()
        if not want_scroll:
            if self._ps_shown not in (None, station):
                self.tx.set_ps(station)
            self._ps_shown = station
            return
        if not self._ps_segs:
            text = t["title"] if not t.get("artist") else "%s %s" % (t["artist"], t["title"])
            self._ps_segs = [station] + ps_segments(text)
            self._ps_idx, self._ps_next = 0, 0.0
        if now >= self._ps_next:
            seg = self._ps_segs[self._ps_idx % len(self._ps_segs)]
            self.tx.set_ps(seg)
            self._ps_shown = seg
            self._ps_next = now + (4.0 if self._ps_idx % len(self._ps_segs) == 0 else 2.5)
            self._ps_idx += 1

    # ------------------------------------------------------------------ actions
    def set_frequency(self, freq):
        self.settings.update(frequency=freq)
        self.tx.configure(freq=self.settings.get("frequency"))

    def set_transmitter(self, on):
        if on:
            self.tx.turn_on()
        else:
            self.player.pause()
            self.tx.turn_off()

    def update_settings(self, values):
        allowed = {"station_name", "radiotext", "rds_song_text", "ps_scroll", "pi_code", "ppm",
                   "preemphasis", "transmit_on_boot", "autoplay", "bt_enabled", "bt_takeover",
                   "bt_name", "captive_mode", "language"}
        values = {k: v for k, v in values.items() if k in allowed}
        changed = self.settings.update(**values)
        s = self.settings.all()
        if {"station_name", "ps_scroll"} & changed.keys():
            self._ps_segs = []
            self._ps_shown = None
            self.tx.configure(ps=s["station_name"])
        if {"pi_code", "ppm"} & changed.keys():
            self.tx.configure(pi=s["pi_code"], ppm=s["ppm"])
        if {"radiotext", "rds_song_text"} & changed.keys():
            self.tx.configure(rt=self._current_rt())
        if "preemphasis" in changed:
            self.player.set_audio(preemphasis=s["preemphasis"])
            self.bt.restart_capture()
        try:
            if "bt_enabled" in changed:
                self.bt.apply_enabled()
            if "bt_name" in changed:
                self.bt.apply_name()
        except BTError as e:
            raise ValueError(str(e))
        return changed

    def set_volume(self, volume):
        self.settings.update(volume=volume)
        self.player.set_audio(volume=self.settings.get("volume"))
        self.bt.restart_capture()

    def set_modes(self, shuffle=None, repeat=None):
        vals = {}
        if shuffle is not None:
            vals["shuffle"] = shuffle
        if repeat is not None:
            vals["repeat"] = repeat
        self.settings.update(**vals)
        s = self.settings.all()
        self.player.set_modes(shuffle=s["shuffle"] if shuffle is not None else None,
                              repeat=s["repeat"] if repeat is not None else None)

    # ------------------------------------------------------------------ live audio
    def _wanted_on_air(self):
        if not self.tx.wanted:
            self.tx.turn_on()

    def library_takeover(self):
        """Before a song-library action: the library gets the air back from the
        phone. While someone is talking on the microphone, songs must wait."""
        if self.player.live_kind() == "ptt":
            raise ValueError("في حدا عم يحكي على الهوا هلأ. استنى يخلص")
        self.bt.suppress()

    def ptt_start(self, client=""):
        src = LiveSource("ptt", label="مايك مباشر", target=0.25, max_delay=0.8)
        src.sid = secrets.token_urlsafe(9)
        src.client = client
        with self._ptt_lock:
            self._ptt = src            # last one to press the button wins
        self._wanted_on_air()
        self.player.begin_live(src)
        log.info("push-to-talk on air (%s)", client or "?")
        return src.sid

    def _ptt_session(self, sid):
        src = self._ptt
        if src is None or src.sid != sid or src.closed:
            raise LiveEnded()
        return src

    def ptt_push(self, sid, pcm_mono):
        """44.1 kHz signed 16-bit mono from the browser."""
        self._ptt_session(sid).push(mono_to_stereo(pcm_mono))

    def ptt_end(self, sid):
        try:
            self._ptt_session(sid).finish()
            log.info("push-to-talk finished")
        except LiveEnded:
            pass

    def _ptt_watchdog(self):
        src = self._ptt
        if src is None or src.closed:
            return
        now = time.monotonic()
        if now - src.last_push > PTT_IDLE or now - src.created > PTT_MAX:
            log.info("push-to-talk ended (no audio from the phone)")
            src.finish()

    def delete_track(self, tid):
        ok = self.library.delete(tid)
        self.player.forget(tid)
        if self.settings.get("last_track") == tid:
            self.settings.update(last_track=None, last_position=0)
        return ok

    def signal_check(self):
        tx = self.tx.snapshot()
        snap = self.player.snapshot()
        gpio = system.gpio4_function() if tx["status"] in ("on_air", "starting") else None
        recent = time.monotonic() - self.feeder.last_write < 2.0
        live = snap["live"]
        return {
            "transmitter": tx["status"] == "on_air",
            "gpio": gpio,                     # 'GPCLK0' = clock really goes out of pin 7
            "pin_ok": gpio in ("GPCLK0", "DEV"),
            "audio_flowing": recent,
            "level": round(self.feeder.level, 3),
            "playing": snap["state"] == "playing" or bool(live),
            "live": live and live["kind"],
            "frequency": tx["frequency"],
            "error": tx["error"],
            "log": list(self.tx.output)[-12:],
        }

    def state(self):
        s = self.settings.all()
        return {
            "tx": self.tx.snapshot(),
            "player": self.player.snapshot(),
            "level": round(self.feeder.level, 3),
            "settings": {k: s[k] for k in (
                "frequency", "station_name", "radiotext", "rds_song_text", "ps_scroll",
                "pi_code", "ppm", "volume", "preemphasis", "transmit_on_boot", "autoplay",
                "shuffle", "repeat", "bt_enabled", "bt_takeover", "bt_name", "captive_mode",
                "language")},
            "rds": {"ps": self._ps_shown or s["station_name"], "rt": self.tx.params.get("rt", "")},
            "library": {"version": self.library.version, "count": len(self.library.ids()),
                        "scanning": self.library.scanning, "imported": self.imported},
            "bt": self.bt.summary(),
            "time": time.time(),
        }
