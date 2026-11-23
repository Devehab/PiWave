"""Persistent settings stored as JSON, written atomically (safe on power loss)."""
import json
import os
import re
import threading

DEFAULTS = {
    "frequency": 107.9,          # MHz
    "station_name": "PI RADIO",  # RDS PS, max 8 chars (shown on the car display)
    "radiotext": "Pi Radio",     # RDS RT when no song is playing
    "rds_song_text": True,       # put the song name in RadioText
    "ps_scroll": False,          # also scroll the song name in the station-name field
    "pi_code": "E2A1",           # RDS PI code (4 hex digits)
    "ppm": 0.0,                  # crystal correction, rarely needed
    "volume": 100,               # 0..100 %
    "preemphasis": "50",         # "50" (Europe/Middle East), "75" (Americas), "off"
    "transmit_on_boot": True,    # start the carrier as soon as the Pi boots
    "autoplay": True,            # resume the last song on boot
    "shuffle": False,
    "repeat": "all",             # "off" | "all" | "one"
    "last_track": None,
    "last_position": 0.0,
    "bt_enabled": True,          # the Pi is a Bluetooth speaker for the phone
    "bt_takeover": True,         # phone audio takes the air automatically
    "bt_name": "PiRadio",        # name the phone shows in its Bluetooth list
    "bt_last": None,             # last connected phone (reconnected after boot)
    "captive_mode": "once",      # welcome page: "once" per phone, or "always"
    "language": "en",            # web page language: "en" or "ar"
}

PS_RE = re.compile(r"^[ -~]{1,8}$")      # printable ASCII, 1..8 chars
PI_RE = re.compile(r"^[0-9A-Fa-f]{4}$")


class SettingsError(ValueError):
    pass


def parse_frequency(value):
    try:
        f = round(float(value), 2)
    except (TypeError, ValueError):
        raise SettingsError("التردد لازم يكون رقم، مثل 107.9")
    if not 76.0 <= f <= 108.0:
        raise SettingsError("التردد لازم يكون بين 76.0 و 108.0 ميغاهيرتز")
    return f


def validate(key, value):
    """Return a cleaned value for `key` or raise SettingsError."""
    if key == "frequency":
        return parse_frequency(value)
    if key == "station_name":
        v = str(value).strip()
        if not PS_RE.match(v):
            raise SettingsError("اسم المحطة: من 1 إلى 8 أحرف إنجليزية/أرقام فقط")
        return v
    if key == "radiotext":
        v = " ".join(str(value).split())
        if len(v) > 64:
            raise SettingsError("النص لازم يكون 64 حرف أو أقل")
        return v
    if key in ("rds_song_text", "ps_scroll", "transmit_on_boot", "autoplay", "shuffle",
               "bt_enabled", "bt_takeover"):
        return bool(value)
    if key == "pi_code":
        v = str(value).strip().upper()
        if not PI_RE.match(v) or v == "0000":
            raise SettingsError("رمز PI لازم يكون 4 خانات hex مثل E2A1")
        return v
    if key == "ppm":
        try:
            v = float(value)
        except (TypeError, ValueError):
            raise SettingsError("قيمة ppm غير صحيحة")
        if not -500 <= v <= 500:
            raise SettingsError("قيمة ppm لازم تكون بين -500 و 500")
        return v
    if key == "volume":
        try:
            v = int(round(float(value)))
        except (TypeError, ValueError):
            raise SettingsError("قيمة الصوت غير صحيحة")
        return max(0, min(100, v))
    if key == "preemphasis":
        v = str(value)
        if v not in ("50", "75", "off"):
            raise SettingsError("قيمة pre-emphasis غير صحيحة")
        return v
    if key == "repeat":
        v = str(value)
        if v not in ("off", "all", "one"):
            raise SettingsError("وضع التكرار غير صحيح")
        return v
    if key == "bt_name":
        v = " ".join(str(value).split())
        if not 1 <= len(v) <= 30 or any(ord(c) < 32 for c in v):
            raise SettingsError("اسم البلوتوث: من 1 إلى 30 حرف")
        return v
    if key == "bt_last":
        return None if value is None else str(value).upper()[:17]
    if key == "captive_mode":
        v = str(value)
        if v not in ("once", "always"):
            raise SettingsError("قيمة صفحة الترحيب غير صحيحة")
        return v
    if key == "language":
        v = str(value)
        if v not in ("en", "ar"):
            raise SettingsError("اللغة لازم تكون en أو ar")
        return v
    if key == "last_track":
        return None if value is None else str(value)
    if key == "last_position":
        return max(0.0, float(value or 0))
    raise SettingsError("إعداد غير معروف: %s" % key)


class Settings:
    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._data = dict(DEFAULTS)
        self._load()

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError):
            return
        for k, v in raw.items():
            if k in DEFAULTS:
                try:
                    self._data[k] = validate(k, v)
                except (SettingsError, TypeError, ValueError):
                    pass

    def _save_locked(self):
        tmp = self.path + ".tmp"
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    def get(self, key):
        with self._lock:
            return self._data[key]

    def all(self):
        with self._lock:
            return dict(self._data)

    def update(self, **values):
        """Validate and persist several values; returns dict of changed keys."""
        cleaned = {k: validate(k, v) for k, v in values.items()}
        with self._lock:
            changed = {k: v for k, v in cleaned.items() if self._data.get(k) != v}
            if changed:
                self._data.update(changed)
                self._save_locked()
            return changed
