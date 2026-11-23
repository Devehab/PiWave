"""Real-time engine: transmitter supervisor, decoder, player and feeder.

Data flow (one continuous stream, so the car never hears static between songs):

    ffmpeg (song -> raw PCM) --> Player.read_chunk() --> Feeder thread
        --> pipe --> pi_fm_rds -audio -raw  (FM + RDS on GPIO4)

When nothing is playing the feeder sends silence, so the carrier and the
station name stay on the car radio.
"""
import collections
import logging
from array import array
import os
import random
import subprocess
import threading
import time

RATE = 44100
FRAME = 4                      # stereo, signed 16-bit
BPS = RATE * FRAME             # bytes per second
CHUNK = 2048 * FRAME           # ~46 ms per write
SILENCE = bytes(CHUNK)
STALL_TIMEOUT = 8.0            # seconds without decoder output -> skip track
TONE_ID = "__tone__"

log = logging.getLogger("piradio.engine")


# --------------------------------------------------------------------------
# Transmitter
# --------------------------------------------------------------------------
class Transmitter:
    """Runs and supervises the pi_fm_rds process."""

    def __init__(self, binary, ctl_path):
        self.binary = binary
        self.ctl_path = ctl_path
        self.params = {"freq": 107.9, "ps": "PI RADIO", "rt": "", "pi": "E2A1", "ppm": 0.0}
        self.status = "off"            # off | starting | on_air | error
        self.error = ""
        self.generation = 0
        self.output = collections.deque(maxlen=80)
        self._lock = threading.RLock()
        self._proc = None
        self._sink = None              # (generation, write fd) not yet taken by feeder
        self._ctl_fd = None
        self._want_on = False
        self._started_at = 0.0
        self._failures = 0
        self._retry_at = 0.0
        self._wake = threading.Event()
        threading.Thread(target=self._supervise, daemon=True, name="tx-supervisor").start()

    # ---- public API ----
    @property
    def wanted(self):
        return self._want_on

    def turn_on(self):
        with self._lock:
            self._want_on = True
            self._failures = 0
            self._retry_at = 0.0
            if self._proc is None:
                self._spawn_locked()

    def turn_off(self):
        with self._lock:
            self._want_on = False
            self._stop_locked()
            self.status = "off"
            self.error = ""

    def configure(self, **params):
        """Change freq/pi/ppm (restarts the carrier) or ps/rt (live via RDS pipe)."""
        with self._lock:
            restart = any(k in ("freq", "pi", "ppm") and self.params.get(k) != v
                          for k, v in params.items())
            live = {k: v for k, v in params.items()
                    if k in ("ps", "rt") and self.params.get(k) != v}
            self.params.update(params)
            if restart and self._proc is not None:
                log.info("restarting transmitter for %s", params)
                self._stop_locked()
                self._spawn_locked()
            elif live:
                for k, v in live.items():
                    self._send_ctl_locked("%s %s" % (k.upper(), v))

    def set_ps(self, ps):
        """Temporarily show text in the station-name field (scrolling PS)."""
        with self._lock:
            self._send_ctl_locked("PS %s" % ps)

    def take_sink(self):
        with self._lock:
            s, self._sink = self._sink, None
            return s

    def poke(self):
        self._wake.set()

    def snapshot(self):
        with self._lock:
            up = time.monotonic() - self._started_at if self._proc else 0
            return {
                "on": self._want_on, "status": self.status, "error": self.error,
                "frequency": self.params["freq"], "uptime": round(up),
                "retry_in": max(0, round(self._retry_at - time.monotonic()))
                if self.status == "error" else 0,
            }

    def shutdown(self):
        with self._lock:
            self._want_on = False
            self._stop_locked()

    # ---- internals ----
    def _cmd(self):
        p = self.params
        cmd = [self.binary, "-freq", "%.2f" % p["freq"], "-audio", "-raw",
               "-pi", p["pi"], "-ps", p["ps"] or " ", "-rt", p["rt"] or " ",
               "-ctl", self.ctl_path]
        if p.get("ppm"):
            cmd += ["-ppm", "%.2f" % p["ppm"]]
        return cmd

    def _spawn_locked(self):
        try:
            os.makedirs(os.path.dirname(self.ctl_path), exist_ok=True)
            try:
                os.unlink(self.ctl_path)
            except FileNotFoundError:
                pass
            os.mkfifo(self.ctl_path, 0o600)
            if self._ctl_fd is not None:
                os.close(self._ctl_fd)
            # Opening read+write never blocks and keeps the FIFO open for the
            # transmitter, so it starts immediately and never sees EOF.
            self._ctl_fd = os.open(self.ctl_path, os.O_RDWR | os.O_NONBLOCK)
        except OSError as e:
            self._fail_locked("ما قدرت أجهز قناة التحكم: %s" % e)
            return
        r, w = os.pipe()
        try:
            proc = subprocess.Popen(self._cmd(), stdin=r, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, close_fds=True)
        except OSError as e:
            os.close(r)
            os.close(w)
            self._fail_locked("ما قدرت أشغّل برنامج البث (%s): %s" % (self.binary, e))
            return
        os.close(r)
        self._close_sink_locked()
        self.generation += 1
        self._sink = (self.generation, w)
        self._proc = proc
        self._started_at = time.monotonic()
        self.status = "starting"
        log.info("transmitter started: %s", " ".join(self._cmd()))
        threading.Thread(target=self._read_output, args=(proc,), daemon=True,
                         name="tx-output").start()
        self._wake.set()

    def _read_output(self, proc):
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                self.output.append(line)
                log.info("[pi_fm_rds] %s", line)
        proc.stdout.close()
        self._wake.set()

    def _stop_locked(self):
        p, self._proc = self._proc, None
        if p is not None and p.poll() is None:
            p.terminate()      # pi_fm_rds traps SIGTERM and shuts the DMA down cleanly
            try:
                p.wait(4)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(2)
        self._close_sink_locked()

    def _close_sink_locked(self):
        if self._sink is not None:
            try:
                os.close(self._sink[1])
            except OSError:
                pass
            self._sink = None

    def _send_ctl_locked(self, line):
        if self._ctl_fd is None or self._proc is None:
            return
        try:
            os.write(self._ctl_fd, (line.replace("\n", " ") + "\n").encode("utf-8"))
        except OSError:
            pass

    def _fail_locked(self, message):
        self._failures += 1
        delay = min(30, 2 ** min(self._failures, 5))
        self._retry_at = time.monotonic() + delay
        self.status = "error"
        self.error = message
        log.error("%s (retry in %ss)", message, delay)

    def _explain(self, rc):
        text = "\n".join(self.output)
        if "/dev/mem" in text:
            return "برنامج البث محتاج صلاحيات root (/dev/mem)"
        if "mailbox" in text.lower():
            return "مشكلة بالاتصال مع معالج الريسبيري (mailbox)"
        if "Could not allocate memory" in text:
            return "ما في ذاكرة كافية للبث"
        last = [l for l in self.output if not l.startswith("Terminating")]
        return "برنامج البث وقف (رمز %s): %s" % (rc, last[-1] if last else "بدون رسالة")

    def _supervise(self):
        while True:
            self._wake.wait(0.5)
            self._wake.clear()
            with self._lock:
                p = self._proc
                now = time.monotonic()
                if p is not None and p.poll() is not None:
                    self._proc = None
                    self._close_sink_locked()
                    if self._want_on:
                        self._fail_locked(self._explain(p.returncode))
                    else:
                        self.status = "off"
                if self._want_on and self._proc is None and now >= self._retry_at:
                    self._spawn_locked()
                if self._proc is not None and self.status == "starting" \
                        and now - self._started_at > 1.5:
                    self.status = "on_air"
                    self.error = ""
                if self.status == "on_air" and now - self._started_at > 30:
                    self._failures = 0


# --------------------------------------------------------------------------
# Decoder: one ffmpeg process turning a file into raw PCM
# --------------------------------------------------------------------------
def audio_filters(volume, preemphasis):
    f = ["aresample=%d" % RATE, "aformat=sample_fmts=fltp:channel_layouts=stereo"]
    if volume != 100:
        f.append("volume=%.3f" % (max(0, volume) / 100.0))
    if preemphasis in ("50", "75"):
        # FM receivers cut treble (de-emphasis); boost it first so music sounds clear.
        f.append("aemphasis=type=%sfm:mode=production" % preemphasis)
    f.append("alimiter=limit=0.95:level=disabled")   # never over-modulate
    return ",".join(f)


def decoder_cmd(ffmpeg, path, start, volume, preemphasis):
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error"]
    if start > 0.05:
        cmd += ["-ss", "%.2f" % start]
    cmd += ["-i", path, "-map", "0:a:0", "-vn", "-sn", "-dn",
            "-af", audio_filters(volume, preemphasis),
            "-ac", "2", "-ar", str(RATE), "-f", "s16le", "-acodec", "pcm_s16le", "pipe:1"]
    return cmd


# Do-Mi-Sol chime, repeated every 1.5 s with only a short pause: easy to hear in a car.
TONE_EXPR = ("0.6*sin(2*PI*t*if(lt(mod(t,1.5),0.35),523.25,if(lt(mod(t,1.5),0.7),659.25,783.99)))"
             "*(0.55+0.45*exp(-3*mod(mod(t,1.5),0.35)))*lt(mod(t,1.5),1.3)")


def tone_cmd(ffmpeg, seconds, volume, preemphasis):
    return [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
            "-f", "lavfi", "-i", "aevalsrc='%s':s=%d:d=%d" % (TONE_EXPR, RATE, seconds),
            "-af", audio_filters(volume, preemphasis),
            "-ac", "2", "-ar", str(RATE), "-f", "s16le", "-acodec", "pcm_s16le", "pipe:1"]


class Decoder:
    def __init__(self, cmd, errlog):
        self._err = open(errlog, "wb")
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=self._err, close_fds=True)
        self._fd = self.proc.stdout.fileno()
        os.set_blocking(self._fd, False)
        self._buf = bytearray()
        self.eof = False
        self.errlog = errlog

    def read(self, n):
        """Non-blocking: return up to n bytes (whole frames) that are ready now."""
        while len(self._buf) < n and not self.eof:
            try:
                d = os.read(self._fd, max(n - len(self._buf), 16384))
            except BlockingIOError:
                break
            except OSError:
                self.eof = True
                break
            if not d:
                self.eof = True
                break
            self._buf += d
        take = min(n, len(self._buf))
        take -= take % FRAME
        out = bytes(self._buf[:take])
        del self._buf[:take]
        if self.eof and len(self._buf) < FRAME:
            self._buf.clear()
        return out

    @property
    def finished(self):
        return self.eof and not self._buf

    def close(self):
        if self.proc.poll() is None:
            self.proc.kill()
        try:
            self.proc.wait(3)
        except subprocess.TimeoutExpired:
            pass
        try:
            self.proc.stdout.close()
        except OSError:
            pass
        self._err.close()
        return self.proc.returncode

    def error_text(self):
        try:
            with open(self.errlog, "r", encoding="utf-8", errors="replace") as f:
                lines = [l.strip() for l in f.read().splitlines() if l.strip()]
            return lines[-1] if lines else ""
        except OSError:
            return ""


# --------------------------------------------------------------------------
# Player
# --------------------------------------------------------------------------
class Player:
    def __init__(self, library, ffmpeg="ffmpeg", errlog="/tmp/piradio-ffmpeg.log"):
        self.library = library
        self.ffmpeg = ffmpeg
        self.errlog = errlog
        self.state = "stopped"          # stopped | playing | paused
        self.track_id = None
        self.volume = 100
        self.preemphasis = "50"
        self.shuffle = False
        self.repeat = "all"
        self.error = ""
        self.change_seq = 0             # bumps on every track/state change
        self._lock = threading.RLock()
        self._decoder = None
        self._start_pos = 0.0
        self._bytes = 0
        self._last_data = 0.0
        self._queue = []
        self._queue_version = -1
        self._fail_streak = 0
        self._tone_prev = None

    # ---- helpers ----
    def _changed(self):
        self.change_seq += 1

    def position(self):
        with self._lock:
            return self._start_pos + self._bytes / float(BPS)

    def _close_decoder_locked(self):
        rc = None
        if self._decoder is not None:
            rc = self._decoder.close()
            self._decoder = None
        return rc

    def _sync_queue_locked(self):
        if self._queue_version == self.library.version and self._queue:
            return
        ids = self.library.ids()
        if self.shuffle:
            known = [i for i in self._queue if i in set(ids)]
            new = [i for i in ids if i not in set(known)]
            random.shuffle(new)
            if not known:
                if self.track_id in new:
                    new.remove(self.track_id)
                    new.insert(0, self.track_id)
                self._queue = new
            else:
                cur = known.index(self.track_id) + 1 if self.track_id in known else len(known)
                rest = known[cur:] + new
                random.shuffle(rest)
                self._queue = known[:cur] + rest
        else:
            self._queue = ids
        self._queue_version = self.library.version

    def _start_locked(self, tid, pos=0.0):
        path = self.library.path(tid)
        if not path or not os.path.exists(path):
            self.error = "الأغنية مش موجودة"
            return False
        self._close_decoder_locked()
        self._tone_prev = None
        try:
            self._decoder = Decoder(decoder_cmd(self.ffmpeg, path, pos, self.volume,
                                                self.preemphasis), self.errlog)
        except OSError as e:
            self.error = "ما قدرت أشغّل ffmpeg: %s" % e
            self.state = "stopped"
            self._changed()
            return False
        self.track_id = tid
        self._start_pos = max(0.0, pos)
        self._bytes = 0
        self._last_data = time.monotonic()
        self.state = "playing"
        self.error = ""
        self._changed()
        return True

    def _end_tone_locked(self):
        prev, self._tone_prev = self._tone_prev, None
        self.track_id = None
        self.state = "stopped"
        if prev and prev[0]:
            tid, pos, state = prev
            if state == "playing":
                self._start_locked(tid, pos)
                return
            self.track_id, self._start_pos, self._bytes = tid, pos, 0
            self.state = "paused" if state == "paused" else "stopped"
        self._changed()

    def _advance_locked(self, step=1, auto=False):
        self._sync_queue_locked()
        if not self._queue:
            self._close_decoder_locked()
            self.state = "stopped"
            self._changed()
            return
        if auto and self.repeat == "one" and self.track_id in self._queue:
            self._start_locked(self.track_id, 0)
            return
        i = self._queue.index(self.track_id) if self.track_id in self._queue else -1
        j = i + step
        if j >= len(self._queue):
            if auto and self.repeat == "off":
                self._close_decoder_locked()
                self.state = "stopped"
                self._start_pos, self._bytes = 0.0, 0
                self._changed()
                return
            if self.shuffle:
                random.shuffle(self._queue)
            j = 0
        elif j < 0:
            j = len(self._queue) - 1
        self._start_locked(self._queue[j], 0)

    def _on_decoder_end_locked(self, stalled=False):
        dec = self._decoder
        produced = self._bytes
        err = dec.error_text() if dec else ""
        rc = self._close_decoder_locked()
        if self.track_id == TONE_ID:
            self._end_tone_locked()
            return
        failed = stalled or (rc not in (0, None) and produced < BPS)
        if failed:
            t = self.library.get(self.track_id)
            name = self.library.display_title(t)
            self.error = "تعذّر تشغيل «%s»%s" % (name, (": " + err) if err else "")
            log.warning("track failed: %s rc=%s %s", name, rc, err)
            self._fail_streak += 1
            if self._fail_streak >= max(1, len(self._queue)):
                self.state = "stopped"
                self._changed()
                return
        else:
            self._fail_streak = 0
        self._advance_locked(1, auto=True)

    # ---- called by the feeder thread, ~22 times a second ----
    def read_chunk(self):
        with self._lock:
            if self.state != "playing" or self._decoder is None:
                return SILENCE
            data = self._decoder.read(CHUNK)
            if data:
                self._bytes += len(data)
                self._last_data = time.monotonic()
                if len(data) < CHUNK:
                    data += bytes(CHUNK - len(data))
                return data
            if self._decoder.finished:
                self._on_decoder_end_locked()
            elif time.monotonic() - self._last_data > STALL_TIMEOUT:
                self._on_decoder_end_locked(stalled=True)
            return SILENCE

    # ---- controls (called from web requests) ----
    def play(self, tid=None, pos=0.0):
        with self._lock:
            self._fail_streak = 0
            self._sync_queue_locked()
            if tid is None:
                if self.state == "paused" and self.track_id:
                    return self.resume()
                if self.state == "playing":
                    return True
                tid = self.track_id if self.track_id in self._queue else \
                    (self._queue[0] if self._queue else None)
                pos = self._start_pos if tid == self.track_id else 0.0
            if tid is None:
                self.error = "ما في أغاني. ارفع أغاني أول."
                return False
            return self._start_locked(tid, pos)

    def pause(self):
        with self._lock:
            if self.state == "playing":
                self.state = "paused"
                self._changed()

    def resume(self):
        with self._lock:
            if self.state == "paused":
                if self._decoder is not None:
                    self.state = "playing"
                    self._last_data = time.monotonic()
                    self._changed()
                    return True
                if self.track_id:
                    return self._start_locked(self.track_id, self._start_pos)
            elif self.state == "stopped":
                return self.play()
            return self.state == "playing"

    def toggle(self):
        with self._lock:
            if self.state == "playing":
                self.pause()
                return True
            return self.resume() if self.state == "paused" else self.play()

    def stop(self):
        with self._lock:
            self._close_decoder_locked()
            if self.track_id == TONE_ID:
                self.track_id = None
                self._tone_prev = None
            self.state = "stopped"
            self._start_pos, self._bytes = 0.0, 0
            self._changed()

    def next(self):
        with self._lock:
            self._fail_streak = 0
            if self.track_id == TONE_ID:
                self._end_tone_locked()
            self._advance_locked(1)

    def prev(self):
        with self._lock:
            self._fail_streak = 0
            if self.track_id == TONE_ID:
                self._end_tone_locked()
            if self.position() > 4 and self.track_id:
                self._start_locked(self.track_id, 0)
            else:
                self._advance_locked(-1)

    def seek(self, pos):
        with self._lock:
            if not self.track_id or self.track_id == TONE_ID:
                return
            pos = max(0.0, float(pos))
            t = self.library.get(self.track_id)
            if t and t.get("duration"):
                pos = min(pos, max(0.0, t["duration"] - 1))
            if self.state == "playing":
                self._start_locked(self.track_id, pos)
            else:
                self._close_decoder_locked()
                self._start_pos, self._bytes = pos, 0
                self._changed()

    def set_audio(self, volume=None, preemphasis=None):
        """Volume/pre-emphasis live in the ffmpeg filter: restart it at the same spot."""
        with self._lock:
            if volume is not None:
                self.volume = volume
            if preemphasis is not None:
                self.preemphasis = preemphasis
            if self.state == "playing" and self.track_id and self.track_id != TONE_ID:
                self._start_locked(self.track_id, self.position())
            elif self.state == "paused":
                pos = self.position()
                self._close_decoder_locked()
                self._start_pos, self._bytes = pos, 0

    def set_modes(self, shuffle=None, repeat=None):
        with self._lock:
            if shuffle is not None and shuffle != self.shuffle:
                self.shuffle = shuffle
                self._queue = []
                self._queue_version = -1
            if repeat is not None:
                self.repeat = repeat
            self._changed()

    def play_tone(self, seconds=20):
        with self._lock:
            if self.track_id != TONE_ID:
                self._tone_prev = (self.track_id, self.position(), self.state) \
                    if self.track_id else None
            prev = self._tone_prev
            self._close_decoder_locked()
            try:
                self._decoder = Decoder(tone_cmd(self.ffmpeg, seconds, self.volume,
                                                 self.preemphasis), self.errlog)
            except OSError as e:
                self.error = "ما قدرت أشغّل ffmpeg: %s" % e
                return False
            self._tone_prev = prev
            self.track_id = TONE_ID
            self._start_pos, self._bytes = 0.0, 0
            self._last_data = time.monotonic()
            self.state = "playing"
            self.error = ""
            self._changed()
            return True

    def forget(self, tid):
        """A track was deleted from the library (call after Library.delete)."""
        with self._lock:
            self._queue_version = -1
            if self.track_id != tid:
                return
            was_playing = self.state == "playing"
            nxt = None
            if tid in self._queue:   # continue with the song that came after it
                i = self._queue.index(tid)
                rest = self._queue[i + 1:] + self._queue[:i]
                nxt = next((x for x in rest if self.library.get(x)), None)
            self._close_decoder_locked()
            self.track_id = None
            self.state = "stopped"
            self._start_pos, self._bytes = 0.0, 0
            if was_playing and nxt:
                self._start_locked(nxt, 0)
            self._changed()

    def snapshot(self):
        with self._lock:
            if self.track_id == TONE_ID:
                track = {"id": TONE_ID, "title": "نغمة اختبار", "artist": None, "duration": 20}
            else:
                t = self.library.get(self.track_id) if self.track_id else None
                track = None if not t else {
                    "id": t["id"], "title": self.library.display_title(t),
                    "artist": t.get("artist"), "duration": t.get("duration")}
            return {
                "state": self.state, "track": track,
                "position": round(self.position(), 1),
                "volume": self.volume, "shuffle": self.shuffle, "repeat": self.repeat,
                "error": self.error, "seq": self.change_seq,
            }

    def shutdown(self):
        with self._lock:
            self._close_decoder_locked()


# --------------------------------------------------------------------------
# Feeder: pushes PCM into the transmitter at exactly real-time speed
# --------------------------------------------------------------------------
class Feeder(threading.Thread):
    def __init__(self, transmitter, player):
        super().__init__(daemon=True, name="audio-feeder")
        self.tx = transmitter
        self.player = player
        self.level = 0.0           # peak level of the audio going out (0..1)
        self.last_write = 0.0      # monotonic time of the last successful write
        self._stop_evt = threading.Event()

    def stop(self):
        self._stop_evt.set()

    def run(self):
        sink = None
        while not self._stop_evt.is_set():
            if sink is not None and sink[0] != self.tx.generation:
                os.close(sink[1])
                sink = None
            if sink is None:
                sink = self.tx.take_sink()
                if sink is None:
                    time.sleep(0.05)
                    continue
            data = self.player.read_chunk()
            if data is SILENCE:
                peak = 0.0
            else:
                a = array("h")
                a.frombytes(data)
                peak = max(max(a), -min(a)) / 32768.0
            self.level = max(peak, self.level * 0.8)
            try:
                view = memoryview(data)
                while view:
                    # Blocks while the pipe is full: pi_fm_rds consumes audio at
                    # exactly real-time speed, which paces the whole player.
                    n = os.write(sink[1], view)
                    view = view[n:]
                self.last_write = time.monotonic()
            except OSError:
                try:
                    os.close(sink[1])
                except OSError:
                    pass
                sink = None
                self.tx.poke()
        if sink is not None:
            os.close(sink[1])
