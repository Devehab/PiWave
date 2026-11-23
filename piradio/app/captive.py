"""Welcome page ("captive portal") that a phone shows once.

When a phone joins the PiRadio Wi-Fi it checks a well-known address (for
example captive.apple.com) to see if the network has internet. Every name
resolves to the Pi, so at first the phone gets redirected to /welcome and
opens it in a pop-up. When the user taps "Done" the phone is remembered and
from then on gets the exact answer it expects ("Success", HTTP 204 ...), so
the pop-up closes and does not come back.
"""
import json
import os
import threading
import time

APPLE_SUCCESS = "<HTML><HEAD><TITLE>Success</TITLE></HEAD><BODY>Success</BODY></HTML>"
FIREFOX_CANONICAL = ('<meta http-equiv="refresh" '
                     'content="0;url=https://support.mozilla.org/kb/captive-portal"/>')
MAX_CLIENTS = 300


def probe_answer(path):
    """(body, status, mimetype) a phone expects from a connectivity check, or None."""
    p = path.lower().rstrip("/")
    if p.endswith("generate_204") or p.endswith("gen_204"):
        return "", 204, "text/plain"
    if p.endswith("hotspot-detect.html") or p.endswith("/library/test/success.html"):
        return APPLE_SUCCESS, 200, "text/html"
    if p.endswith("ncsi.txt"):
        return "Microsoft NCSI", 200, "text/plain"
    if p.endswith("connecttest.txt"):
        return "Microsoft Connect Test", 200, "text/plain"
    if p.endswith("success.txt"):
        return "success\n", 200, "text/plain"
    if p.endswith("canonical.html"):
        return FIREFOX_CANONICAL, 200, "text/html"
    return None


class Welcomed:
    """Phones that already tapped "Done" (stored on the SD card)."""

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._data = {}
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                self._data = {str(k): float(v) for k, v in d.items()}
        except (OSError, ValueError, TypeError):
            pass

    def _save_locked(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    def __contains__(self, client):
        with self._lock:
            return client in self._data

    def add(self, client):
        with self._lock:
            self._data[client] = time.time()
            if len(self._data) > MAX_CLIENTS:
                for k in sorted(self._data, key=self._data.get)[:len(self._data) - MAX_CLIENTS]:
                    del self._data[k]
            self._save_locked()

    def clear(self):
        with self._lock:
            n = len(self._data)
            self._data = {}
            self._save_locked()
            return n

    def __len__(self):
        with self._lock:
            return len(self._data)
