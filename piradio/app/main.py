#!/usr/bin/env python3
"""PiRadio entry point: starts the radio engine and the web servers."""
import logging
import os
import signal
import ssl
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from werkzeug.serving import make_server  # noqa: E402

import system  # noqa: E402
import updater  # noqa: E402
from radio import RadioApp  # noqa: E402
from server import create_app  # noqa: E402

log = logging.getLogger("piradio")
RELEASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def watch_update_health(radio, started):
    """After an update: confirm it once it broadcasts fine, else roll back."""
    while time.monotonic() - started < 90:
        time.sleep(3)
        tx = radio.tx.snapshot()
        healthy = tx["status"] == "on_air" if radio.tx.wanted else True
        if healthy and time.monotonic() - started > 15:
            updater.confirm()
            log.info("update confirmed healthy")
            return
    log.error("new version is not healthy after 90 s: rolling back")
    if updater.rollback("النسخة الجديدة ما قدرت تبث خلال 90 ثانية"):
        system.restart_service()


def env(name, default):
    return os.environ.get(name, default)


def main():
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    if os.environ.get("TMPDIR"):
        os.makedirs(os.environ["TMPDIR"], exist_ok=True)
    boot_dirs = env("PIRADIO_BOOT_MUSIC", "/boot/firmware/piradio-music:/boot/piradio-music")
    radio = RadioApp(
        data_dir=env("PIRADIO_DATA", "/var/lib/piradio"),
        tx_binary=env("PIRADIO_TX", os.path.join(RELEASE_DIR, "bin", "pi_fm_rds")),
        ffmpeg=env("PIRADIO_FFMPEG", "ffmpeg"),
        ffprobe=env("PIRADIO_FFPROBE", "ffprobe"),
        run_dir=env("PIRADIO_RUN", "/run/piradio"),
        boot_music_dirs=[d for d in boot_dirs.split(":") if d],
    )
    app = create_app(radio)

    servers = []
    http_port = int(env("PIRADIO_HTTP_PORT", "80"))
    servers.append(make_server("0.0.0.0", http_port, app, threaded=True))
    cert = env("PIRADIO_CERT", "/etc/piradio/cert.pem")
    key = env("PIRADIO_KEY", "/etc/piradio/key.pem")
    https_port = int(env("PIRADIO_HTTPS_PORT", "443"))
    if https_port and os.path.exists(cert) and os.path.exists(key):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        servers.append(make_server("0.0.0.0", https_port, app, threaded=True, ssl_context=ctx))
    else:
        log.info("HTTPS disabled (no certificate): voice recording needs it")

    radio.start()
    if updater.info()["pending"]:
        threading.Thread(target=watch_update_health, args=(radio, time.monotonic()),
                         daemon=True, name="update-health").start()
    for s in servers:
        threading.Thread(target=s.serve_forever, daemon=True, name="http").start()
    log.info("web UI on http://0.0.0.0:%d%s", http_port,
             " and https://0.0.0.0:%d" % https_port if len(servers) > 1 else "")

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    while not stop.wait(1):
        pass
    log.info("stopping")
    radio.shutdown()
    for s in servers:
        s.shutdown()


if __name__ == "__main__":
    main()
