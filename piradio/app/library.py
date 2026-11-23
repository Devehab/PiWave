"""Music library: files in a folder + a small JSON cache of their metadata."""
import hashlib
import json
import logging
import os
import shutil
import subprocess
import threading
import time
import unicodedata

log = logging.getLogger("piradio.library")

AUDIO_EXT = {
    ".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".oga", ".opus", ".wma",
    ".mp4", ".webm", ".aif", ".aiff", ".amr", ".3gp", ".mka", ".m4b", ".mpga",
    ".audio",
}


def track_id(filename):
    return hashlib.sha1(filename.encode("utf-8")).hexdigest()[:12]


def safe_filename(name):
    """Keep a readable (also Arabic) name but make it safe as a file name."""
    name = unicodedata.normalize("NFC", os.path.basename(str(name or "")))
    name = "".join(c for c in name if c not in '/\\\x00' and ord(c) >= 32)
    name = name.strip().lstrip(".").strip()
    stem, ext = os.path.splitext(name)
    stem = stem[:120].strip() or "song"
    ext = ext.lower()[:8]
    return stem + ext


def is_junk(name):
    # macOS writes "._name" AppleDouble files on FAT cards; also skip hidden files
    return name.startswith(".") or name in ("Thumbs.db", "desktop.ini")


def probe(path, ffprobe="ffprobe"):
    """Return (has_audio, duration, title, artist) using ffprobe."""
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-print_format", "json", "-show_format",
             "-show_streams", path],
            capture_output=True, timeout=60, check=False).stdout
        info = json.loads(out or b"{}")
    except (OSError, ValueError, subprocess.TimeoutExpired) as e:
        log.warning("ffprobe failed for %s: %s", path, e)
        return False, None, None, None
    streams = info.get("streams") or []
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    fmt = info.get("format") or {}
    try:
        duration = float(fmt.get("duration"))
    except (TypeError, ValueError):
        duration = None
    tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
    for s in streams:  # ogg/opus keep tags on the stream
        for k, v in (s.get("tags") or {}).items():
            tags.setdefault(k.lower(), v)
    title = (tags.get("title") or "").strip() or None
    artist = (tags.get("artist") or tags.get("album_artist") or "").strip() or None
    return has_audio, duration, title, artist


class Library:
    def __init__(self, music_dir, index_path, ffprobe="ffprobe"):
        self.music_dir = music_dir
        self.index_path = index_path
        self.ffprobe = ffprobe
        self._lock = threading.RLock()
        self._tracks = {}          # id -> dict
        self.version = 0
        self.scanning = False
        self._scan_thread = None
        os.makedirs(music_dir, exist_ok=True)
        self._load_index()
        self._cleanup_partials()

    # ---------- persistence ----------
    def _load_index(self):
        try:
            with open(self.index_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for t in data.get("tracks", []):
                if "file" in t:
                    t["id"] = track_id(t["file"])
                    self._tracks[t["id"]] = t
        except (OSError, ValueError):
            pass

    def _save_index_locked(self):
        tmp = self.index_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"tracks": list(self._tracks.values())}, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.index_path)

    def _cleanup_partials(self):
        for n in os.listdir(self.music_dir):
            if n.startswith(".upload-") or n.startswith("._") or n.endswith(".importing"):
                try:
                    os.remove(os.path.join(self.music_dir, n))
                except OSError:
                    pass

    # ---------- queries ----------
    def get(self, tid):
        with self._lock:
            t = self._tracks.get(tid)
            return dict(t) if t else None

    def path(self, tid):
        t = self.get(tid)
        return os.path.join(self.music_dir, t["file"]) if t else None

    def ordered(self):
        with self._lock:
            ts = [dict(t) for t in self._tracks.values() if t.get("ok", True)]
        ts.sort(key=lambda t: (t.get("added", 0), t["file"].lower()))
        return ts

    def bad(self):
        with self._lock:
            return [dict(t) for t in self._tracks.values() if not t.get("ok", True)]

    def ids(self):
        return [t["id"] for t in self.ordered()]

    def display_title(self, t):
        if not t:
            return ""
        return t.get("title") or os.path.splitext(t["file"])[0]

    # ---------- scanning ----------
    def scan_async(self):
        with self._lock:
            if self._scan_thread and self._scan_thread.is_alive():
                return
            self._scan_thread = threading.Thread(target=self.scan, daemon=True, name="library-scan")
            self._scan_thread.start()

    def scan(self):
        """Sync the index with the folder; probe new/changed files."""
        self.scanning = True
        try:
            on_disk = {}
            for n in os.listdir(self.music_dir):
                p = os.path.join(self.music_dir, n)
                if is_junk(n) or not os.path.isfile(p):
                    continue
                st = os.stat(p)
                on_disk[n] = st
            with self._lock:
                removed = [tid for tid, t in self._tracks.items() if t["file"] not in on_disk]
                for tid in removed:
                    del self._tracks[tid]
                todo = []
                for n, st in on_disk.items():
                    tid = track_id(n)
                    t = self._tracks.get(tid)
                    if t and t.get("size") == st.st_size and t.get("mtime") == int(st.st_mtime):
                        continue
                    if not t and os.path.splitext(n)[1].lower() not in AUDIO_EXT:
                        continue  # unknown extension dropped in by hand: ignore
                    self._tracks[tid] = {
                        "id": tid, "file": n, "title": None, "artist": None,
                        "duration": None, "size": st.st_size, "mtime": int(st.st_mtime),
                        "added": (t or {}).get("added", st.st_mtime), "ok": True,
                    }
                    todo.append(tid)
                if removed or todo:
                    self.version += 1
            for tid in todo:
                with self._lock:
                    t = self._tracks.get(tid)
                    if not t:
                        continue
                    p = os.path.join(self.music_dir, t["file"])
                has_audio, dur, title, artist = probe(p, self.ffprobe)
                with self._lock:
                    t = self._tracks.get(tid)
                    if t:
                        t.update(ok=has_audio, duration=dur, title=title, artist=artist)
                        self.version += 1
            with self._lock:
                if removed or todo:
                    self._save_index_locked()
        except OSError as e:
            log.error("scan failed: %s", e)
        finally:
            self.scanning = False

    # ---------- changes ----------
    def _unique_name_locked(self, name):
        stem, ext = os.path.splitext(name)
        cand, i = name, 2
        while os.path.exists(os.path.join(self.music_dir, cand)):
            cand = "%s (%d)%s" % (stem, i, ext)
            i += 1
        return cand

    def add_file(self, tmp_path, original_name):
        """Move an uploaded temp file into the library. Returns the track or raises ValueError."""
        has_audio, dur, title, artist = probe(tmp_path, self.ffprobe)
        if not has_audio:
            os.remove(tmp_path)
            raise ValueError("هذا الملف مش ملف صوت يمكن تشغيله")
        name = safe_filename(original_name)
        if os.path.splitext(name)[1].lower() not in AUDIO_EXT:
            name += ".audio"   # ffmpeg detects the real format from the content
        with self._lock:
            name = self._unique_name_locked(name)
            dst = os.path.join(self.music_dir, name)
            os.replace(tmp_path, dst)
            st = os.stat(dst)
            tid = track_id(name)
            self._tracks[tid] = {
                "id": tid, "file": name, "title": title, "artist": artist,
                "duration": dur, "size": st.st_size, "mtime": int(st.st_mtime),
                "added": time.time(), "ok": True,
            }
            self.version += 1
            self._save_index_locked()
            return dict(self._tracks[tid])

    def import_folder(self, folder):
        """Move audio files from e.g. the SD card boot partition into the library."""
        if not os.path.isdir(folder):
            return 0
        moved = 0
        for n in sorted(os.listdir(folder)):
            src = os.path.join(folder, n)
            if not os.path.isfile(src):
                continue
            if n.startswith("._"):
                try:
                    os.remove(src)
                except OSError:
                    pass
                continue
            if is_junk(n) or os.path.splitext(n)[1].lower() not in AUDIO_EXT:
                continue
            with self._lock:
                dst = os.path.join(self.music_dir, self._unique_name_locked(safe_filename(n)))
            try:
                tmp = dst + ".importing"
                shutil.copyfile(src, tmp)
                os.replace(tmp, dst)
                os.remove(src)
                moved += 1
                log.info("imported %s", n)
            except OSError as e:
                log.error("import of %s failed: %s", n, e)
        return moved

    def delete(self, tid):
        with self._lock:
            t = self._tracks.pop(tid, None)
            if not t:
                return False
            try:
                os.remove(os.path.join(self.music_dir, t["file"]))
            except FileNotFoundError:
                pass
            self.version += 1
            self._save_index_locked()
            return True
