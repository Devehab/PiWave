"""HTTP API + static web UI (Flask)."""
import ipaddress
import json
import logging
import os
import socket
import tempfile
import threading
import time

from flask import Flask, Request, jsonify, redirect, request, send_from_directory

import captive
import system
from i18n import tr
import updater
from bluetooth import BTError
from radio import LiveEnded
from settings import SettingsError

log = logging.getLogger("piradio.web")
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
VERSION = updater.read_version(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))) or "?"


def create_app(radio):
    upload_dir = radio.library.music_dir

    class UploadRequest(Request):
        # Stream uploads straight into the music folder (one write to the SD
        # card, no RAM-backed /tmp on a 512 MB Pi).
        def _get_file_stream(self, total_content_length, content_type, filename=None,
                             content_length=None):
            return tempfile.NamedTemporaryFile("wb+", dir=upload_dir, prefix=".upload-",
                                               delete=False)

    app = Flask(__name__, static_folder=None)
    app.request_class = UploadRequest
    app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024 * 1024
    if hasattr(app, "json"):
        app.json.ensure_ascii = False
    else:
        app.config["JSON_AS_ASCII"] = False

    my_names = {"localhost", system.AP_IP, "piradio", "piradio.local",
                socket.gethostname().lower(), socket.gethostname().lower() + ".local"}
    welcomed = captive.Welcomed(os.path.join(radio.data_dir, "welcomed.json"))
    captive_on = not system.DEV or os.environ.get("PIRADIO_CAPTIVE") == "1"

    def body():
        return request.get_json(silent=True) or {}

    def T(text):
        """Message in the page's language (messages are written in Arabic)."""
        return tr(text, radio.settings.get("language"))

    def page(name):
        """HTML page in the chosen language from the first paint (no flash)."""
        with open(os.path.join(STATIC, name), encoding="utf-8") as f:
            html = f.read()
        if radio.settings.get("language") == "ar":
            html = html.replace('<html lang="en" dir="ltr">', '<html lang="ar" dir="rtl">', 1)
        return html

    def ok(**extra):
        return jsonify(dict(ok=True, **extra))

    @app.errorhandler(SettingsError)
    @app.errorhandler(ValueError)
    @app.errorhandler(BTError)
    def bad_request(e):
        return jsonify(ok=False, error=T(str(e))), 400

    @app.errorhandler(LiveEnded)
    def live_ended(e):
        return jsonify(ok=False, error=T("انتهى البث المباشر"), ended=True), 410

    @app.errorhandler(413)
    def too_big(e):
        return jsonify(ok=False, error=T("الملف كبير كثير (الحد 1GB)")), 413

    def client():
        return system.client_id(request.remote_addr)

    # Captive portal: phones probe e.g. captive.apple.com when they join the
    # Wi-Fi. New phones are sent to the welcome page (it pops up by itself);
    # phones that tapped "Done" get the answer they expect, so it stays closed.
    @app.before_request
    def captive_redirect():
        host = (request.host or "").split(":")[0].strip("[]").lower()
        if not host or host in my_names:
            return None
        try:
            ipaddress.ip_address(host)
            return None
        except ValueError:
            pass
        if not captive_on:
            return None
        if radio.settings.get("captive_mode") == "once" and client() in welcomed:
            answer = captive.probe_answer(request.path)
            if answer:
                body_, code, mime = answer
                r = app.response_class(body_, status=code, mimetype=mime)
                r.headers["Cache-Control"] = "no-store"
                return r
            return redirect("http://%s/" % system.AP_IP, code=302)
        r = redirect("http://%s/welcome" % system.AP_IP, code=302)
        r.headers["Cache-Control"] = "no-store"
        return r

    # ---------------- static ----------------
    @app.get("/")
    def index():
        r = app.response_class(page("index.html"), mimetype="text/html")
        r.headers["Cache-Control"] = "no-cache"
        return r

    @app.get("/welcome")
    def welcome_page():
        r = app.response_class(page("welcome.html"), mimetype="text/html")
        r.headers["Cache-Control"] = "no-store"
        return r

    @app.get("/api/captive")
    def captive_state():
        return jsonify(welcomed=client() in welcomed, mode=radio.settings.get("captive_mode"),
                       count=len(welcomed), url="http://%s" % system.AP_IP)

    @app.post("/api/captive/ack")
    def captive_ack():
        welcomed.add(client())
        log.info("welcome page done for %s", client())
        return ok()

    @app.post("/api/captive/reset")
    def captive_reset():
        return ok(cleared=welcomed.clear())

    @app.get("/static/<path:name>")
    def static_files(name):
        r = send_from_directory(STATIC, name)
        r.headers["Cache-Control"] = "no-cache"
        return r

    # ---------------- state ----------------
    @app.get("/api/state")
    def state():
        st = radio.state()
        st["version"] = VERSION
        st["tx"]["error"] = T(st["tx"]["error"])
        p = st["player"]
        p["error"] = T(p["error"])
        if p["track"] and p["track"]["id"] == "__tone__":
            p["track"]["title"] = T(p["track"]["title"])
        if p["live"]:
            p["live"]["label"] = T(p["live"]["label"])
        return jsonify(st)

    @app.get("/api/library")
    def library():
        return jsonify(version=radio.library.version, tracks=[
            {"id": t["id"], "title": radio.library.display_title(t), "artist": t.get("artist"),
             "duration": t.get("duration"), "size": t.get("size"), "file": t["file"]}
            for t in radio.library.ordered()],
            bad=[{"id": t["id"], "file": t["file"]} for t in radio.library.bad()])

    @app.get("/api/system")
    def sysinfo():
        info = system.stats(radio.data_dir)
        info["wifi"] = system.hotspot_get()
        info["wifi_mode"] = system.wifi_mode()
        return jsonify(info)

    @app.get("/api/check")
    def check():
        c = radio.signal_check()
        c["error"] = T(c["error"])
        return jsonify(c)

    # ---------------- transmitter ----------------
    @app.post("/api/frequency")
    def frequency():
        radio.set_frequency(body().get("frequency"))
        return ok(frequency=radio.settings.get("frequency"))

    @app.post("/api/transmitter")
    def transmitter():
        radio.set_transmitter(bool(body().get("on")))
        return ok()

    @app.post("/api/settings")
    def settings():
        changed = radio.update_settings(body())
        return ok(changed=list(changed))

    # ---------------- player ----------------
    @app.post("/api/player/<action>")
    def player(action):
        p, b = radio.player, body()
        if action in ("play", "resume", "toggle", "next", "prev", "seek", "stop", "tone"):
            radio.library_takeover()      # the songs get the air back from the phone
        if action in ("play", "resume", "toggle", "next", "prev") and not radio.tx.wanted:
            radio.set_transmitter(True)   # pressing play means "I want to hear it"
        if action == "play":
            if not p.play(b.get("id"), float(b.get("position") or 0)):
                raise ValueError(p.error or "ما قدرت أشغّل")
        elif action == "pause":
            p.pause()
        elif action == "resume":
            p.resume()
        elif action == "toggle":
            if not p.toggle():
                raise ValueError(p.error or "ما في أغاني للتشغيل")
        elif action == "stop":
            p.stop()
        elif action == "next":
            p.next()
        elif action == "prev":
            p.prev()
        elif action == "seek":
            p.seek(float(b.get("position") or 0))
        elif action == "volume":
            radio.set_volume(b.get("volume"))
        elif action == "modes":
            radio.set_modes(shuffle=b.get("shuffle"), repeat=b.get("repeat"))
        elif action == "tone":
            if not radio.tx.wanted:
                radio.set_transmitter(True)
            p.play_tone(20)
        else:
            return jsonify(ok=False, error="unknown action"), 404
        return ok(player=p.snapshot())

    # ---------------- library ----------------
    @app.post("/api/upload")
    def upload():
        f = request.files.get("file")
        if f is None:
            raise ValueError("ما وصل ملف")
        stream = f.stream
        tmp = getattr(stream, "name", None)
        try:
            stream.flush()
            stream.close()
            if not tmp or not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
                raise ValueError("الملف فاضي")
            track = radio.library.add_file(tmp, f.filename or "song")
        except Exception:
            if tmp and os.path.exists(tmp):
                os.remove(tmp)
            raise
        if request.form.get("play") == "1":
            radio.library_takeover()
            if not radio.tx.wanted:
                radio.set_transmitter(True)
            radio.player.play(track["id"])
        log.info("uploaded %s", track["file"])
        return ok(track={"id": track["id"], "title": radio.library.display_title(track)})

    @app.delete("/api/library/<tid>")
    def delete(tid):
        if not radio.delete_track(tid):
            return jsonify(ok=False, error=T("الأغنية مش موجودة")), 404
        return ok()

    # ---------------- Bluetooth ----------------
    def bt_state():
        s = radio.bt.status()
        s["error"] = T(s["error"])
        if s.get("install") and s["install"].get("log"):
            s["install"] = dict(s["install"], log=T(s["install"]["log"]))
        return s

    @app.get("/api/bluetooth")
    def bt_status():
        return jsonify(bt_state())

    @app.post("/api/bluetooth/<action>")
    def bt_action(action):
        bt, b = radio.bt, body()
        if action == "pairing":
            bt.set_pairing(bool(b.get("on")))
        elif action == "connect":
            bt.connect(b.get("mac"))
        elif action == "disconnect":
            bt.disconnect(b.get("mac"))
        elif action == "remove":
            bt.remove(b.get("mac"))
        elif action == "media":
            bt.media(b.get("command"))
        elif action == "install":
            bt.install()
        else:
            return jsonify(ok=False, error="unknown action"), 404
        return ok(bluetooth=bt_state())

    # ---------------- push-to-talk (live microphone) ----------------
    @app.post("/api/ptt/start")
    def ptt_start():
        return ok(sid=radio.ptt_start(client()), rate=44100)

    @app.post("/api/ptt/<sid>")
    def ptt_audio(sid):
        data = request.get_data(cache=False)
        if len(data) > 1024 * 1024:
            raise ValueError("قطعة صوت كبيرة كثير")
        radio.ptt_push(sid, data)
        return ok()

    @app.post("/api/ptt/<sid>/end")
    def ptt_end(sid):
        radio.ptt_end(sid)
        return ok()

    # ---------------- updates ----------------
    update_lock = threading.Lock()

    def uploaded_file():
        f = request.files.get("file")
        if f is None:
            raise ValueError("ما وصل ملف")
        tmp = getattr(f.stream, "name", None)
        f.stream.flush()
        f.stream.close()
        return f, tmp

    @app.get("/api/version")
    def version():
        info = dict(updater.info(), running=VERSION)
        if info.get("result") and info["result"].get("reason"):
            info["result"] = dict(info["result"], reason=T(info["result"]["reason"]))
        return jsonify(info)

    @app.post("/api/update")
    def update():
        if not update_lock.acquire(blocking=False):
            raise ValueError("في تحديث شغال هلأ، استنى يخلص")
        tmp = None
        try:
            f, tmp = uploaded_file()
            lines = []
            try:
                v = updater.deploy(tmp, pending=True, log=lines.append)
            except updater.UpdateError as e:
                log.error("update failed: %s", e)
                return jsonify(ok=False, error=T(str(e)), log=lines), 400
            log.info("update to %s installed, restarting", v)
            system.restart_service()
            return ok(version=v, previous=VERSION, log=lines)
        finally:
            if tmp and os.path.exists(tmp):
                os.remove(tmp)
            update_lock.release()

    @app.post("/api/update/rollback")
    def do_rollback():
        if not updater.rollback("رجوع يدوي من الصفحة"):
            raise ValueError("ما في إصدار سابق للرجوع إله")
        system.restart_service()
        return ok()

    @app.get("/api/logs")
    def logs():
        parts = [
            "PiRadio %s  —  %s" % (VERSION, time.strftime("%Y-%m-%d %H:%M:%S")),
            "== version ==", json.dumps(updater.info(), ensure_ascii=False, indent=1),
            "== state ==", json.dumps(radio.state(), ensure_ascii=False, indent=1),
            "== signal check ==", json.dumps(radio.signal_check(), ensure_ascii=False, indent=1),
            "== system ==", json.dumps(system.stats(radio.data_dir), ensure_ascii=False, indent=1),
            "== transmitter output ==", "\n".join(radio.tx.output),
            "== journal ==", system.journal(),
        ]
        r = app.response_class("\n".join(parts) + "\n", mimetype="text/plain; charset=utf-8")
        r.headers["Content-Disposition"] = "attachment; filename=piradio-log-%s.txt" % \
            time.strftime("%Y%m%d-%H%M")
        return r

    # ---------------- system ----------------
    @app.post("/api/wifi")
    def wifi():
        b = body()
        system.hotspot_set(b.get("ssid"), b.get("password"))
        return ok()

    @app.post("/api/wifi/home")
    def wifi_home():
        system.wifi_join_home()
        return ok()

    @app.post("/api/system/<action>")
    def power(action):
        if action == "reboot":
            radio.shutdown()
            system.reboot()
        elif action == "shutdown":
            radio.shutdown()
            system.poweroff()
        else:
            return jsonify(ok=False, error="unknown action"), 404
        return ok()

    return app
