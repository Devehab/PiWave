#!/usr/bin/env python3
"""End-to-end tests for PiRadio (run on a computer, no Raspberry Pi needed).

The real PiFmRds audio + RDS code runs inside dev/pi_fm_rds_sim (hardware
replaced by a real-time clock). The server is installed with the same
updater the Pi uses, then driven through its HTTP API.

usage: test_all.py <scratch-dir> <sim-binary> <python-with-flask+requests>
"""
import fcntl
import json
import math
import os
import shutil
import signal
import struct
import subprocess
import sys
import tarfile
import threading
import time
import zipfile

import requests
import socket
import urllib3

urllib3.disable_warnings()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "piradio")
WORK, SIM, PY = sys.argv[1], sys.argv[2], sys.argv[3]
PORT = 8766
URL = "http://127.0.0.1:%d" % PORT
SPORT = 8767
SURL = "https://127.0.0.1:%d" % SPORT
BASE = os.path.join(WORK, "opt")
DATA = os.path.join(WORK, "var")
RUN = os.path.join(WORK, "run")
BOOT = os.path.join(WORK, "boot-music")
LOG = os.path.join(WORK, "server.log")
SONGS = os.path.join(WORK, "songs")
FAKE_BT = os.path.join(WORK, "fakebt.json")
CERT = os.path.join(WORK, "cert.pem")
KEY = os.path.join(WORK, "key.pem")
BT_FIFO = os.path.join(WORK, "phone-audio.fifo")
VERSION = open(os.path.join(PKG, "VERSION")).read().strip()
MAJ, MIN, PATCH = (int(x) for x in VERSION.split("."))


def ver(n):
    """A version newer than the one being tested (for update tests)."""
    return "%d.%d.%d" % (MAJ, MIN, PATCH + n)

sys.path.insert(0, os.path.join(PKG, "app"))
os.environ.update(PIRADIO_BASE=BASE, PIRADIO_DATA=DATA, PIRADIO_FAKE_TX=SIM)
import updater  # noqa: E402

results = []
server = None


def check(name, cond, info=""):
    results.append((name, bool(cond), info))
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else "   -> %s" % info), flush=True)


def wait_until(fn, timeout=10.0, step=0.25):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        try:
            last = fn()
            if last:
                return last
        except Exception as e:   # server restarting etc.
            last = e
        time.sleep(step)
    return None


def api(method, path, **kw):
    r = requests.request(method, URL + path, timeout=200, **kw)
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, r.text


def state():
    return api("GET", "/api/state")[1]


def log_text():
    with open(LOG, encoding="utf-8", errors="replace") as f:
        return f.read()


def sim_lines(since):
    return [l for l in log_text()[since:].splitlines() if "[pi_fm_rds]" in l]


def env():
    e = dict(os.environ, PIRADIO_DEV="1", PIRADIO_BASE=BASE, PIRADIO_DATA=DATA, PIRADIO_RUN=RUN,
             PIRADIO_HTTP_PORT=str(PORT), PIRADIO_HTTPS_PORT=str(SPORT), PIRADIO_BOOT_MUSIC=BOOT,
             PIRADIO_CERT=CERT, PIRADIO_KEY=KEY,
             PIRADIO_FAKE_TX=SIM, TMPDIR=os.path.join(DATA, "tmp"),
             PIRADIO_BUSCTL=os.path.join(ROOT, "dev", "fake_busctl.py"), PIRADIO_FAKE_BT=FAKE_BT,
             PIRADIO_BT_INSTALLED="1", PIRADIO_CAPTIVE="1",
             PIRADIO_BT_INPUT="-f s16le -ar 44100 -ac 2 -i " + BT_FIFO)
    e.pop("PIRADIO_TX", None)  # must default to <current release>/bin/pi_fm_rds
    return e


def start_server():
    """What systemd + piradio-launch do on the Pi."""
    global server
    subprocess.run([PY, os.path.join(PKG, "app", "updater.py"), "launch-check"], env=env(), check=False)
    rel = os.path.realpath(os.path.join(BASE, "current"))
    logf = open(LOG, "a")
    server = subprocess.Popen([PY, os.path.join(rel, "app", "main.py")], env=env(), stdout=logf,
                              stderr=subprocess.STDOUT)
    ok = wait_until(lambda: requests.get(URL + "/api/state", timeout=2).ok, 20)
    return ok


def stop_server():
    global server
    if server and server.poll() is None:
        server.send_signal(signal.SIGTERM)
        try:
            server.wait(15)
        except subprocess.TimeoutExpired:
            server.kill()
    server = None


def make_song(name, freq, secs, title=None, artist=None, extra=()):
    path = os.path.join(SONGS, name)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
           "-i", "sine=frequency=%d:duration=%d" % (freq, secs), "-af", "volume=6", "-ac", "2"]
    if title:
        cmd += ["-metadata", "title=" + title]
    if artist:
        cmd += ["-metadata", "artist=" + artist]
    subprocess.run(cmd + list(extra) + [path], check=True)
    return path


def build_package(version, mutate=None, tamper=False):
    stage = os.path.join(WORK, "stage-" + version)
    shutil.rmtree(stage, ignore_errors=True)
    shutil.copytree(PKG, os.path.join(stage, "piradio"),
                    ignore=shutil.ignore_patterns("__pycache__", ".DS_Store", "._*"))
    root = os.path.join(stage, "piradio")
    with open(os.path.join(root, "VERSION"), "w") as f:
        f.write(version + "\n")
    if mutate:
        mutate(root)
    updater.write_manifest(root)
    if tamper:
        with open(os.path.join(root, "app", "rdstext.py"), "a") as f:
            f.write("\n# tampered after signing\n")
    out = os.path.join(WORK, "piradio-update-%s.tar.gz" % version)
    with tarfile.open(out, "w:gz") as tf:
        tf.add(root, arcname="piradio")
    return out


# ---------------------------------------------------------------- fake phone
def v(sig, data):
    return {"type": sig, "data": data}


def bt_edit(fn):
    """Change the fake BlueZ state the same (locked) way fake_busctl.py does."""
    with open(FAKE_BT + ".lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        with open(FAKE_BT) as f:
            st = json.load(f)
        fn(st)
        with open(FAKE_BT + ".tmp", "w") as f:
            json.dump(st, f, indent=1)
        os.replace(FAKE_BT + ".tmp", FAKE_BT)
        fcntl.flock(lk, fcntl.LOCK_UN)


def bt_state():
    with open(FAKE_BT) as f:
        return json.load(f)


ADAPTER = "/org/bluez/hci0"
PHONE_MAC = "AA:BB:CC:DD:EE:02"
PHONE = ADAPTER + "/dev_" + PHONE_MAC.replace(":", "_")


def bt_init():
    st = {"objects": {
        ADAPTER: {"org.bluez.Adapter1": {
            "Address": v("s", "B8:27:EB:00:00:01"), "Alias": v("s", "raspberrypi"),
            "Powered": v("b", True), "Discoverable": v("b", False), "Pairable": v("b", True),
            "DiscoverableTimeout": v("u", 0), "PairableTimeout": v("u", 0)}},
        ADAPTER + "/dev_AA_BB_CC_DD_EE_01": {"org.bluez.Device1": {
            "Address": v("s", "AA:BB:CC:DD:EE:01"), "Alias": v("s", "Old Phone"),
            "Paired": v("b", True), "Trusted": v("b", True), "Connected": v("b", False)}}},
        "calls": [], "phone_away": True}
    with open(FAKE_BT, "w") as f:
        json.dump(st, f)
    if not os.path.exists(BT_FIFO):
        os.mkfifo(BT_FIFO)


def phone_pairs_and_connects(st):
    o = st["objects"]
    o[PHONE] = {"org.bluez.Device1": {"Address": v("s", PHONE_MAC), "Alias": v("s", "Galaxy S24"),
                                      "Paired": v("b", True), "Trusted": v("b", False),
                                      "Connected": v("b", True), "Icon": v("s", "phone")}}
    o[PHONE + "/sep1/fd0"] = {"org.bluez.MediaTransport1": {"Device": v("o", PHONE), "State": v("s", "active")}}
    o[PHONE + "/player0"] = {"org.bluez.MediaPlayer1": {
        "Device": v("o", PHONE), "Status": v("s", "playing"),
        "Track": v("a{sv}", {"Title": v("s", "Ya Habibi"), "Artist": v("s", "Test Artist")})}}


def phone_disconnects(st):
    o = st["objects"]
    o[PHONE]["org.bluez.Device1"]["Connected"] = v("b", False)
    for k in [k for k in o if k.startswith(PHONE + "/")]:
        del o[k]


class PhoneAudio(threading.Thread):
    """Plays a tone into the Bluetooth audio pipe in real time (like BlueALSA
    would); .playing can be switched off to pretend the phone paused."""

    def __init__(self):
        super().__init__(daemon=True)
        self.playing = threading.Event()
        self.playing.set()
        self.stop = threading.Event()
        self.written = 0

    def run(self):
        frames = 2048
        chunk = b"".join(struct.pack("<hh", int(9000 * math.sin(2 * math.pi * 660 * i / 44100)),
                                     int(9000 * math.sin(2 * math.pi * 660 * i / 44100)))
                         for i in range(frames))
        silence = bytes(len(chunk))
        try:
            fd = os.open(BT_FIFO, os.O_WRONLY)     # waits for PiRadio's ffmpeg
        except OSError:
            return
        t = time.time()
        try:
            while not self.stop.is_set():
                if self.playing.is_set():
                    os.write(fd, chunk)
                    self.written += len(chunk)
                t += frames / 44100.0
                time.sleep(max(0, t - time.time()))
        except OSError:
            pass                                    # PiRadio closed the stream (phone disconnected)
        finally:
            os.close(fd)
        del silence


def tone_mono(seconds, freq=440, amp=12000):
    n = int(44100 * seconds)
    return b"".join(struct.pack("<h", int(amp * math.sin(2 * math.pi * freq * i / 44100))) for i in range(n))


def upload_update(path):
    with open(path, "rb") as f:
        return api("POST", "/api/update", files={"file": (os.path.basename(path), f)})


# =====================================================================
def main():
    shutil.rmtree(WORK, ignore_errors=True)
    for d in (WORK, SONGS, BOOT):
        os.makedirs(d, exist_ok=True)
    open(LOG, "w").close()

    print("== install (same code path as install.sh)")
    bt_init()
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "30",
                    "-subj", "/CN=PiRadio", "-keyout", KEY, "-out", CERT],
                   check=True, capture_output=True)
    v = updater.deploy(PKG, BASE, DATA, pending=False, log=lambda m: None)
    rel = os.path.realpath(os.path.join(BASE, "current"))
    check("installer deploys version %s" % VERSION, v == VERSION and os.path.isfile(os.path.join(rel, "bin", "pi_fm_rds")), v)

    print("== songs copied onto the SD card")
    make_song("first.mp3", 330, 6, "First Song", "Band A")
    make_song("عربي.mp3", 550, 5, "تملي معاك", "عمرو دياب")
    for n in ("first.mp3", "عربي.mp3"):
        shutil.copy(os.path.join(SONGS, n), BOOT)
    with open(os.path.join(BOOT, "._first.mp3"), "w") as f:
        f.write("macOS junk")
    with open(os.path.join(BOOT, "broken.mp3"), "w") as f:
        f.write("not audio")

    print("== boot")
    check("server starts", start_server(), log_text()[-500:])
    st = wait_until(lambda: (lambda s: s if s["tx"]["status"] == "on_air" and s["player"]["state"] == "playing" else None)(state()), 15)
    check("transmitter on air at boot", st and st["tx"]["status"] == "on_air", st)
    check("autoplay starts the first song", st and st["player"]["track"]["title"] == "First Song", st and st["player"])
    lib = api("GET", "/api/library")[1]
    check("2 good songs imported from the card", len(lib["tracks"]) == 2, lib)
    check("broken file listed as bad", [b["file"] for b in lib["bad"]] == ["broken.mp3"], lib.get("bad"))
    check("SD-card folder emptied, ._ junk removed", os.listdir(BOOT) == [], os.listdir(BOOT))
    check("version %s reported" % VERSION, st and st["version"] == VERSION, st and st.get("version"))
    mark = len(log_text())
    time.sleep(3)
    lines = sim_lines(mark)
    check("real audio reaches the transmitter", sum("audio=1" in l for l in lines) >= 2, lines[-4:])
    check("song name sent as RadioText", 'RT set to: "Band A - First Song"' in log_text(), "")

    print("== playback")
    wait_until(lambda: state()["player"]["track"]["title"] == "تملي معاك", 10)
    check("auto-advance to next song", state()["player"]["track"]["title"] == "تملي معاك", state()["player"])
    check("Arabic title converted to Arabizi for RDS",
          wait_until(lambda: 'RT set to: "3mrw Dyab - Tmly M3ak"' in log_text(), 4), "")
    api("POST", "/api/player/pause", json={})
    p1 = state()["player"]["position"]
    mark = len(log_text())
    time.sleep(2.5)
    s = state()["player"]
    check("pause keeps position", s["state"] == "paused" and abs(s["position"] - p1) < 0.2, s)
    check("silence while paused (carrier stays on)",
          any("audio=0" in l for l in sim_lines(mark)) and state()["tx"]["status"] == "on_air", sim_lines(mark)[-3:])
    api("POST", "/api/player/resume", json={})
    time.sleep(1.5)
    check("resume continues", state()["player"]["state"] == "playing" and state()["player"]["position"] > p1, state()["player"])
    first_id = next(t["id"] for t in lib["tracks"] if t["title"] == "First Song")
    api("POST", "/api/player/play", json={"id": first_id})
    api("POST", "/api/player/seek", json={"position": 3})
    time.sleep(0.6)
    pos = state()["player"]["position"]
    check("seek to 3 s", 2.9 <= pos <= 4.5, pos)
    code, _ = api("POST", "/api/player/volume", json={"volume": 40})
    check("volume change", code == 200 and state()["player"]["volume"] == 40, code)
    api("POST", "/api/player/modes", json={"repeat": "one"})
    wait_until(lambda: state()["player"]["position"] < 1.0, 6)
    check("repeat-one replays the same song", state()["player"]["track"]["title"] == "First Song", state()["player"])
    api("POST", "/api/player/modes", json={"repeat": "off"})
    api("POST", "/api/player/play", json={"id": next(t["id"] for t in lib["tracks"] if t["title"] == "تملي معاك")})
    st = wait_until(lambda: (lambda s: s if s["player"]["state"] == "stopped" else None)(state()), 9)
    check("repeat-off stops after the last song", st is not None, state()["player"])
    api("POST", "/api/player/modes", json={"repeat": "all"})
    api("POST", "/api/player/volume", json={"volume": 100})

    print("== uploads")
    make_song("upload.m4a", 700, 4, extra=("-c:a", "aac"))
    with open(os.path.join(SONGS, "upload.m4a"), "rb") as f:
        code, j = api("POST", "/api/upload", files={"file": ("أغنيتي الجديدة.m4a", f, "audio/mp4")}, data={"play": "1"})
    check("upload m4a with Arabic file name + play now", code == 200 and j["ok"], j)
    time.sleep(1)
    s = state()["player"]
    check("uploaded song is playing", s["state"] == "playing" and s["track"]["title"] == "أغنيتي الجديدة", s)
    code, j = api("POST", "/api/upload", files={"file": ("notes.mp3", b"hello world", "audio/mpeg")})
    check("non-audio upload rejected with a clear message", code == 400 and "not an audio file" in j["error"], j)
    leftovers = [n for n in os.listdir(os.path.join(DATA, "music")) if n.startswith(".upload-")]
    check("no temp files left after uploads", leftovers == [], leftovers)
    # a voice recording as browsers produce it (webm/opus)
    rec = os.path.join(SONGS, "rec.webm")
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "sine=frequency=500:duration=3", "-af", "volume=6", "-c:a", "libopus", rec], check=True)
    with open(rec, "rb") as f:
        code, j = api("POST", "/api/upload", files={"file": ("تسجيل 10-30.webm", f, "audio/webm")}, data={"play": "1"})
    time.sleep(1)
    check("voice recording (webm/opus) uploads and plays", code == 200 and state()["player"]["track"]["title"] == "تسجيل 10-30", (code, j, state()["player"]))

    print("== delete while playing")
    tid = state()["player"]["track"]["id"]
    code, _ = api("DELETE", "/api/library/" + tid)
    time.sleep(1)
    s = state()["player"]
    check("deleting the playing song moves to the next one", code == 200 and s["track"] and s["track"]["id"] != tid and s["state"] == "playing", s)

    print("== frequency")
    mark = len(log_text())
    code, j = api("POST", "/api/frequency", json={"frequency": 88.1})
    st = wait_until(lambda: (lambda s: s if s["tx"]["status"] == "on_air" and s["tx"]["frequency"] == 88.1 else None)(state()), 8)
    check("change to 88.1 MHz restarts on the new frequency", st and "Starting to transmit on 88.1 MHz." in log_text()[mark:], sim_lines(mark)[:8])
    time.sleep(2.5)
    check("audio continues after the frequency change", any("audio=1" in l for l in sim_lines(mark)), sim_lines(mark)[-3:])
    code, j = api("POST", "/api/frequency", json={"frequency": 120})
    check("invalid frequency rejected", code == 400 and "108" in j["error"], j)

    print("== RDS")
    mark = len(log_text())
    code, j = api("POST", "/api/settings", json={"station_name": "MY CAR"})
    check("station name changes live", wait_until(lambda: 'PS set to: "MY CAR"' in log_text()[mark:], 3), sim_lines(mark))
    code, j = api("POST", "/api/settings", json={"station_name": "TOO LONG NAME"})
    check("station name > 8 chars rejected", code == 400, j)
    mark = len(log_text())
    api("POST", "/api/settings", json={"ps_scroll": True})
    time.sleep(7)
    ps = [l for l in log_text()[mark:].splitlines() if "PS set to" in l]
    check("scrolling station name shows the song name", len(set(ps)) >= 2, ps)
    api("POST", "/api/settings", json={"ps_scroll": False})

    print("== transmitter on/off")
    api("POST", "/api/transmitter", json={"on": False})
    time.sleep(1)
    s = state()
    check("transmitter off pauses music", s["tx"]["status"] == "off" and s["player"]["state"] == "paused", s["tx"])
    api("POST", "/api/player/toggle", json={})
    st = wait_until(lambda: (lambda s: s if s["tx"]["status"] == "on_air" else None)(state()), 6)
    check("pressing play turns the transmitter back on", st and st["player"]["state"] == "playing", state())

    print("== crash recovery")
    pid = subprocess.run(["pgrep", "-f", "bin/pi_fm_rds"], capture_output=True, text=True).stdout.split()
    if pid:
        os.kill(int(pid[0]), signal.SIGKILL)
    time.sleep(1)
    check("transmitter crash detected", state()["tx"]["status"] in ("error", "starting"), state()["tx"])
    st = wait_until(lambda: (lambda s: s if s["tx"]["status"] == "on_air" else None)(state()), 10)
    check("transmitter restarts by itself", st is not None, state()["tx"])
    mark = len(log_text())
    time.sleep(2.5)
    check("audio flows again after the crash", any("audio=1" in l for l in sim_lines(mark)), sim_lines(mark)[-3:])

    print("== test tone + signal check")
    before = state()["player"]["track"]["id"]
    api("POST", "/api/player/tone", json={})
    time.sleep(1)
    check("test tone plays", state()["player"]["track"]["id"] == "__tone__", state()["player"])
    levels = []
    for _ in range(8):
        c = api("GET", "/api/check")[1]
        levels.append(c["level"])
        time.sleep(0.25)
    check("signal check: transmitter + pin + audio OK",
          c["transmitter"] and c["pin_ok"] and c["audio_flowing"] and c["playing"] and max(levels) > 0.3, (c, levels))
    st = wait_until(lambda: (lambda s: s if s["player"]["track"] and s["player"]["track"]["id"] == before else None)(state()), 25)
    check("after the tone the previous song resumes", st is not None, state()["player"])

    print("== language")
    page = requests.get(URL + "/", timeout=5).text
    code, j = api("POST", "/api/frequency", json={"frequency": 150})
    check("English by default (page and messages)", '<html lang="en" dir="ltr">' in page and "between 76.0 and 108.0 MHz" in j["error"], (page[:80], j))
    code, j = api("POST", "/api/settings", json={"language": "ar"})
    page = requests.get(URL + "/", timeout=5).text
    welcome = requests.get(URL + "/welcome", timeout=5).text
    code2, j2 = api("POST", "/api/frequency", json={"frequency": 150})
    check("switch to Arabic: right-to-left page and Arabic messages",
          code == 200 and '<html lang="ar" dir="rtl">' in page and '<html lang="ar" dir="rtl">' in welcome and "ميغاهيرتز" in j2["error"],
          (code, page[:80], j2))
    code, j = api("POST", "/api/settings", json={"language": "fr"})
    check("unknown language refused", code == 400, j)
    api("POST", "/api/settings", json={"language": "en"})
    check("back to English", '<html lang="en" dir="ltr">' in requests.get(URL + "/", timeout=5).text, "")

    print("== welcome page (captive portal)")
    probe = lambda host, path: requests.get(URL + path, headers={"Host": host}, allow_redirects=False, timeout=5)
    r = probe("captive.apple.com", "/hotspot-detect.html")
    check("new phone is sent to the welcome page", r.status_code == 302 and r.headers.get("Location", "").endswith("/welcome"), (r.status_code, r.headers.get("Location")))
    r = requests.get(URL + "/welcome", timeout=5)
    check("welcome page loads", r.status_code == 200 and "Done, got it" in r.text, r.status_code)
    code, j = api("POST", "/api/captive/ack")
    r1 = probe("captive.apple.com", "/hotspot-detect.html")
    r2 = probe("connectivitycheck.gstatic.com", "/generate_204")
    r3 = probe("www.msftconnecttest.com", "/connecttest.txt")
    check("after «Done» the phone gets the normal answers (no pop-up)",
          code == 200 and r1.status_code == 200 and "Success" in r1.text and r2.status_code == 204 and r3.text == "Microsoft Connect Test",
          (code, r1.status_code, r2.status_code, r3.text[:40]))
    check("other web addresses still lead to the radio page", probe("example.com", "/").headers.get("Location") == "http://192.168.4.1/", "")
    api("POST", "/api/settings", json={"captive_mode": "always"})
    check("mode «every time» shows the welcome page again", probe("captive.apple.com", "/hotspot-detect.html").status_code == 302, "")
    api("POST", "/api/settings", json={"captive_mode": "once"})
    code, j = api("POST", "/api/captive/reset")
    check("reset: every phone sees the welcome page again", code == 200 and probe("captive.apple.com", "/hotspot-detect.html").status_code == 302, j)
    api("POST", "/api/captive/ack")

    print("== secure page (needed for the microphone)")
    r = requests.get(SURL + "/api/state", verify=False, timeout=5)
    check("https page works", r.status_code == 200 and r.json()["version"] == VERSION, r.status_code)
    idle = [socket.create_connection(("127.0.0.1", SPORT)) for _ in range(3)]   # browsers pre-open spare connections
    try:
        t = time.time()
        r = requests.get(SURL + "/api/state", verify=False, timeout=5)
        ok_ = r.status_code == 200
    except requests.RequestException as e:
        ok_, r = False, e
    check("https keeps answering while silent connections are open", ok_ and time.time() - t < 3, r)
    for c in idle:
        c.close()

    print("== push-to-talk (live microphone)")
    before = state()["player"]
    code, j = api("POST", "/api/ptt/start", json={})
    sid = j.get("sid") if isinstance(j, dict) else None
    check("talk button starts a live session", code == 200 and sid, j)
    mark = len(log_text())
    pcm = tone_mono(3.0)
    step = 4410 * 2                     # 100 ms like the browser
    t0 = time.time()
    for i in range(0, len(pcm), step):
        api("POST", "/api/ptt/" + sid, data=pcm[i:i + step], headers={"Content-Type": "application/octet-stream"})
        if i == step * 15:
            mid = state()
            code_busy, j_busy = api("POST", "/api/player/next", json={})
        time.sleep(max(0, t0 + (i + step) / 88200.0 - time.time()))
    check("voice is on air, the song waits", mid["player"]["live"] and mid["player"]["live"]["kind"] == "ptt" and mid["player"]["state"] == "paused", mid["player"])
    check("song buttons wait while someone talks", code_busy == 400 and "talking" in j_busy.get("error", ""), (code_busy, j_busy))
    check("car display says LIVE while talking", wait_until(lambda: 'RT set to: "LIVE - ' in log_text()[mark:], 3), "")
    check("microphone audio reaches the transmitter", any("audio=1" in l for l in sim_lines(mark)), sim_lines(mark)[-3:])
    api("POST", "/api/ptt/%s/end" % sid, json={})
    st = wait_until(lambda: (lambda s: s if not s["player"]["live"] and s["player"]["state"] == "playing" else None)(state()), 5)
    check("after talking the song continues by itself", st and st["player"]["track"]["id"] == before["track"]["id"], state()["player"])
    code, j = api("POST", "/api/ptt/" + sid, data=pcm[:step])
    check("audio after the end is refused (410)", code == 410, (code, j))
    code, j = api("POST", "/api/ptt/start", json={})
    api("POST", "/api/ptt/" + j["sid"], data=pcm[:step * 5])
    st = wait_until(lambda: (lambda s: s if not s["player"]["live"] and s["player"]["state"] == "playing" else None)(state()), 8)
    check("phone vanishes mid-talk: live ends by itself", st is not None, state()["player"])

    print("== Bluetooth")
    b = api("GET", "/api/bluetooth")[1]
    a = bt_state()["objects"][ADAPTER]["org.bluez.Adapter1"]
    check("Bluetooth ready, shows up as «PiRadio»", b["available"] and b["name"] == "PiRadio" and a["Alias"]["data"] == "PiRadio", b)
    check("not pairable unless pairing mode is on", a["Pairable"]["data"] is False and a["Discoverable"]["data"] is False, a)
    check("paired phones are listed", [d["name"] for d in b["devices"]] == ["Old Phone"], b["devices"])
    code, j = api("POST", "/api/bluetooth/pairing", json={"on": True})
    a = bt_state()["objects"][ADAPTER]["org.bluez.Adapter1"]
    check("pairing mode on: discoverable + pairable, BlueZ turns it off after 3 min too",
          code == 200 and j["bluetooth"]["pairing_left"] > 170 and a["Discoverable"]["data"] and a["Pairable"]["data"]
          and a["DiscoverableTimeout"]["data"] == 180, (code, a))
    phone = PhoneAudio()
    phone.start()
    bt_edit(phone_pairs_and_connects)
    b = wait_until(lambda: (lambda b: b if b["just_paired"] else None)(api("GET", "/api/bluetooth")[1]), 8)
    a = bt_state()["objects"][ADAPTER]["org.bluez.Adapter1"]
    check("new phone paired: pairing mode ends by itself", b and b["just_paired"] == "Galaxy S24" and not b["pairing_left"] and a["Discoverable"]["data"] is False, b)
    check("new phone is trusted (reconnects without asking)", bt_state()["objects"][PHONE]["org.bluez.Device1"]["Trusted"]["data"] is True, "")
    mark = len(log_text())
    st = wait_until(lambda: (lambda s: s if s["player"]["live"] and s["player"]["live"]["kind"] == "bt" else None)(state()), 10)
    check("phone music takes the air from the songs", st and st["player"]["state"] == "paused" and st["bt"]["on_air"], st and (st["player"], st["bt"]))
    check("phone's song name on the car display", wait_until(lambda: 'RT set to: "Test Artist - Ya Habibi"' in log_text()[mark:], 4), "")
    time.sleep(2)
    check("phone audio reaches the transmitter", sum("audio=1" in l for l in sim_lines(mark)) >= 2 and phone.written > 0, sim_lines(mark)[-3:])
    code, j = api("POST", "/api/bluetooth/media", json={"command": "pause"})
    check("phone controls from the page (AVRCP)", code == 200 and bt_state().get("media", [])[-1:] == ["Pause"], (code, j))
    code, j = api("POST", "/api/player/play", json={})
    time.sleep(0.8)
    s = state()
    check("pressing play on the page brings the songs back", code == 200 and s["player"]["state"] == "playing" and not s["player"]["live"] and s["bt"]["suppressed"], s["bt"])
    time.sleep(2)
    check("…and the phone doesn't grab it back while it keeps playing", not state()["player"]["live"], state()["bt"])
    phone.playing.clear()                                   # phone paused
    time.sleep(3)
    phone.playing.set()                                     # and plays again
    st = wait_until(lambda: (lambda s: s if s["player"]["live"] else None)(state()), 6)
    check("phone pauses and plays again: it takes the air again", st is not None, state()["bt"])
    phone.playing.clear()
    st = wait_until(lambda: (lambda s: s if not s["player"]["live"] and s["player"]["state"] == "playing" else None)(state()), 12)
    check("phone quiet for a few seconds: songs continue", st is not None, state()["player"])
    phone.playing.set()
    wait_until(lambda: state()["player"]["live"], 6)
    bt_edit(phone_disconnects)
    st = wait_until(lambda: (lambda s: s if not s["player"]["live"] and not s["bt"]["connected"] else None)(state()), 8)
    check("phone disconnects: songs continue, Bluetooth audio stops", st and st["player"]["state"] == "playing", state()["bt"])
    phone.stop.set()
    code, j = api("POST", "/api/bluetooth/remove", json={"mac": PHONE_MAC})
    check("unpair from the page", code == 200 and PHONE not in bt_state()["objects"], (code, j))
    code, j = api("POST", "/api/settings", json={"bt_name": "x" * 40})
    check("too long Bluetooth name refused", code == 400, j)
    code, j = api("POST", "/api/settings", json={"bt_name": "Car Radio"})
    a = bt_state()["objects"][ADAPTER]["org.bluez.Adapter1"]
    check("Bluetooth name change", code == 200 and a["Alias"]["data"] == "Car Radio", a["Alias"])
    code, j = api("POST", "/api/settings", json={"bt_enabled": False})
    a = bt_state()["objects"][ADAPTER]["org.bluez.Adapter1"]
    check("Bluetooth off from the page", code == 200 and a["Powered"]["data"] is False and api("GET", "/api/state")[1]["bt"]["enabled"] is False, a["Powered"])
    api("POST", "/api/settings", json={"bt_enabled": True})

    print("== persistence across restart (car switched off and on)")
    api("POST", "/api/frequency", json={"frequency": 107.9})
    time.sleep(1)
    tid = state()["player"]["track"]["id"]
    stop_server()
    check("clean shutdown stops the carrier", "Terminating: cleanly deactivated" in log_text()[-3000:], log_text()[-400:])
    start_server()
    st = wait_until(lambda: (lambda s: s if s["player"]["state"] == "playing" else None)(state()), 10)
    check("after reboot: same frequency + same song resumes", st and st["tx"]["frequency"] == 107.9 and st["player"]["track"]["id"] == tid, st and (st["tx"], st["player"]))

    print("== updates")
    pkg101 = build_package(ver(1))
    code, j = upload_update(pkg101)
    check("update %s installs" % ver(1), code == 200 and j.get("version") == ver(1), j)
    check("unchanged transmitter source is reused (no recompile)", any("reused" in l for l in j.get("log", [])), j.get("log"))
    stop_server()
    start_server()
    check("running %s after restart" % ver(1), state()["version"] == ver(1), state().get("version"))
    v = wait_until(lambda: (lambda i: i if i["result"] and i["result"].get("result") == "ok" else None)(api("GET", "/api/version")[1]), 25)
    check("%s confirmed healthy, previous = %s" % (ver(1), VERSION), v and v["version"] == ver(1) and v["previous"] == VERSION, api("GET", "/api/version")[1])

    def syntax_error(root):
        with open(os.path.join(root, "app", "server.py"), "a") as f:
            f.write("\ndef broken(:\n")
    code, j = upload_update(build_package(ver(2), mutate=syntax_error))
    check("update with broken code is refused, nothing changes", code == 400 and "error" in j["error"] and state()["version"] == ver(1), j)
    code, j = upload_update(build_package(ver(2), tamper=True))
    check("tampered/corrupt update refused", code == 400 and "rdstext.py" in j["error"], j)
    junk = os.path.join(WORK, "junk.tar.gz")
    with open(junk, "wb") as f:
        f.write(b"this is not an archive")
    code, j = upload_update(junk)
    check("non-archive refused", code == 400, j)
    evil = os.path.join(WORK, "evil.zip")
    with zipfile.ZipFile(evil, "w") as z:
        z.writestr("../../escape.txt", "x")
    code, j = upload_update(evil)
    check("path-traversal archive refused", code == 400 and not os.path.exists(os.path.join(DATA, "..", "escape.txt")), j)

    def bad_transmitter(root):   # installs fine, but cannot go on air
        p = os.path.join(root, "app", "engine.py")
        s = open(p, encoding="utf-8").read().replace('"%.2f" % p["freq"]', '"999"')
        open(p, "w", encoding="utf-8").write(s)
    code, j = upload_update(build_package(ver(3), mutate=bad_transmitter))
    check("update %s (bad at runtime) installs" % ver(3), code == 200, j)
    stop_server()
    start_server()
    check("%s running" % ver(3), state()["version"] == ver(3), state().get("version"))
    print("   (waiting up to 100 s for the automatic health check...)", flush=True)
    v = wait_until(lambda: (lambda i: i if i["result"] and i["result"].get("result") == "rolled_back" else None)(updater.info(BASE, DATA)), 110, 2)
    check("unhealthy %s rolled back automatically to %s" % (ver(3), ver(1)), v and v["version"] == ver(1), updater.info(BASE, DATA))
    stop_server()
    start_server()
    check("after rollback the radio runs %s and is on air" % ver(1),
          wait_until(lambda: state()["version"] == ver(1) and state()["tx"]["status"] == "on_air", 10), state())

    # crash-loop guard (piradio-launch runs launch-check before each start)
    stop_server()
    upd = build_package(ver(4))
    updater.deploy(upd, BASE, DATA, pending=True, log=lambda m: None)
    for _ in range(4):
        updater.launch_check(BASE, DATA)
    i = updater.info(BASE, DATA)
    check("crashing 3 times after an update rolls back", i["version"] == ver(1) and i["result"]["result"] == "rolled_back", i)

    start_server()
    code, j = api("POST", "/api/update/rollback", json={})
    i = updater.info(BASE, DATA)
    check("manual rollback button switches versions", code == 200 and i["version"] == ver(4) and i["previous"] == ver(1), (code, j, i))
    releases = sorted(n for n in os.listdir(os.path.join(BASE, "releases")) if not n.startswith("."))
    check("old releases pruned (keeps current + previous)", len(releases) <= 2, releases)
    code, text = api("GET", "/api/logs")
    check("log download works", code == 200 and "PiRadio" in text and "transmitter output" in text, str(text)[:200])

    stop_server()
    errors = [l for l in log_text().splitlines() if "Traceback" in l]
    check("no Python tracebacks in the server log", not errors, errors[:5])

    passed = sum(1 for _, ok, _ in results if ok)
    print("\n%d/%d checks passed" % (passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        stop_server()
