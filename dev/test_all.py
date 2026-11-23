#!/usr/bin/env python3
"""End-to-end tests for PiRadio (run on a computer, no Raspberry Pi needed).

The real PiFmRds audio + RDS code runs inside dev/pi_fm_rds_sim (hardware
replaced by a real-time clock). The server is installed with the same
updater the Pi uses, then driven through its HTTP API.

usage: test_all.py <scratch-dir> <sim-binary> <python-with-flask+requests>
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import tarfile
import time
import zipfile

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "piradio")
WORK, SIM, PY = sys.argv[1], sys.argv[2], sys.argv[3]
PORT = 8766
URL = "http://127.0.0.1:%d" % PORT
BASE = os.path.join(WORK, "opt")
DATA = os.path.join(WORK, "var")
RUN = os.path.join(WORK, "run")
BOOT = os.path.join(WORK, "boot-music")
LOG = os.path.join(WORK, "server.log")
SONGS = os.path.join(WORK, "songs")

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
             PIRADIO_HTTP_PORT=str(PORT), PIRADIO_HTTPS_PORT="0", PIRADIO_BOOT_MUSIC=BOOT,
             PIRADIO_FAKE_TX=SIM, TMPDIR=os.path.join(DATA, "tmp"))
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
    v = updater.deploy(PKG, BASE, DATA, pending=False, log=lambda m: None)
    rel = os.path.realpath(os.path.join(BASE, "current"))
    check("installer deploys version 1.0.0", v == "1.0.0" and os.path.isfile(os.path.join(rel, "bin", "pi_fm_rds")), v)

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
    check("version 1.0.0 reported", st and st["version"] == "1.0.0", st and st.get("version"))
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
    check("non-audio upload rejected with Arabic message", code == 400 and "صوت" in j["error"], j)
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
    pkg101 = build_package("1.0.1")
    code, j = upload_update(pkg101)
    check("update 1.0.1 installs", code == 200 and j.get("version") == "1.0.1", j)
    check("unchanged transmitter source is reused (no recompile)", any("reused" in l for l in j.get("log", [])), j.get("log"))
    stop_server()
    start_server()
    check("running 1.0.1 after restart", state()["version"] == "1.0.1", state().get("version"))
    v = wait_until(lambda: (lambda i: i if i["result"] and i["result"].get("result") == "ok" else None)(api("GET", "/api/version")[1]), 25)
    check("1.0.1 confirmed healthy, previous = 1.0.0", v and v["version"] == "1.0.1" and v["previous"] == "1.0.0", api("GET", "/api/version")[1])

    def syntax_error(root):
        with open(os.path.join(root, "app", "server.py"), "a") as f:
            f.write("\ndef broken(:\n")
    code, j = upload_update(build_package("1.0.2", mutate=syntax_error))
    check("update with broken code is refused, nothing changes", code == 400 and "خطأ" in j["error"] and state()["version"] == "1.0.1", j)
    code, j = upload_update(build_package("1.0.2", tamper=True))
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
    code, j = upload_update(build_package("1.0.3", mutate=bad_transmitter))
    check("update 1.0.3 (bad at runtime) installs", code == 200, j)
    stop_server()
    start_server()
    check("1.0.3 running", state()["version"] == "1.0.3", state().get("version"))
    print("   (waiting up to 100 s for the automatic health check...)", flush=True)
    v = wait_until(lambda: (lambda i: i if i["result"] and i["result"].get("result") == "rolled_back" else None)(updater.info(BASE, DATA)), 110, 2)
    check("unhealthy 1.0.3 rolled back automatically to 1.0.1", v and v["version"] == "1.0.1", updater.info(BASE, DATA))
    stop_server()
    start_server()
    check("after rollback the radio runs 1.0.1 and is on air",
          wait_until(lambda: state()["version"] == "1.0.1" and state()["tx"]["status"] == "on_air", 10), state())

    # crash-loop guard (piradio-launch runs launch-check before each start)
    stop_server()
    upd = build_package("1.0.4")
    updater.deploy(upd, BASE, DATA, pending=True, log=lambda m: None)
    for _ in range(4):
        updater.launch_check(BASE, DATA)
    i = updater.info(BASE, DATA)
    check("crashing 3 times after an update rolls back", i["version"] == "1.0.1" and i["result"]["result"] == "rolled_back", i)

    start_server()
    code, j = api("POST", "/api/update/rollback", json={})
    i = updater.info(BASE, DATA)
    check("manual rollback button switches versions", code == 200 and i["version"] == "1.0.4" and i["previous"] == "1.0.1", (code, j, i))
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
