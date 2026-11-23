#!/usr/bin/env python3
"""Install, update and roll back PiRadio releases.

Layout on the Pi:
    /opt/piradio/releases/<version>/   one folder per installed version
    /opt/piradio/current  -> releases/<version>   (what runs)
    /opt/piradio/previous -> releases/<version>   (for rollback)

An update is a .tar.gz/.zip of the `piradio` folder. It is verified
(MANIFEST.sha256), the transmitter is compiled, the Python code is
import-checked, and only then the `current` link is switched. The new
version must report healthy within ~90 s, otherwise it is rolled back
automatically (also if it crashes 3 times in a row).

CLI (used by install.sh and the systemd launcher):
    updater.py deploy <folder|archive> [--no-pending]
    updater.py rollback [reason]
    updater.py launch-check
    updater.py info
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile

BASE = os.environ.get("PIRADIO_BASE", "/opt/piradio")
STATE = os.environ.get("PIRADIO_DATA", "/var/lib/piradio")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+([.\-+][0-9A-Za-z.\-]+)?$")
MAX_UNPACKED = 300 * 1024 * 1024
MAX_MEMBERS = 5000
NOT_IN_MANIFEST = {"MANIFEST.sha256", "piradio.conf", "README.txt"}   # user-editable
JUNK = (".DS_Store", "__MACOSX", "__pycache__", "Thumbs.db")
MAX_CRASHES = 3


class UpdateError(Exception):
    pass


# ---------------------------------------------------------------- helpers
def _p(*parts):
    return os.path.join(*parts)


def read_version(folder):
    try:
        with open(_p(folder, "VERSION"), encoding="utf-8") as f:
            v = f.read().strip()
    except OSError:
        return None
    return v if VERSION_RE.match(v) else None


def _real(link):
    return os.path.realpath(link) if os.path.lexists(link) and os.path.isdir(link) else None


def current_dir(base=BASE):
    return _real(_p(base, "current"))


def previous_dir(base=BASE):
    p = _real(_p(base, "previous"))
    return p if p and p != current_dir(base) else None


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _rm(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def switch_link(link, target):
    tmp = link + ".new"
    if os.path.lexists(tmp):
        os.remove(tmp)
    os.symlink(target, tmp)
    os.replace(tmp, link)        # atomic


def info(base=BASE, state=STATE):
    cur, prev = current_dir(base), previous_dir(base)
    return {
        "version": read_version(cur) if cur else None,
        "previous": read_version(prev) if prev else None,
        "pending": _read_json(_p(state, "update-pending.json")),
        "result": _read_json(_p(state, "update-result.json")),
    }


def _is_junk(rel):
    parts = rel.replace("\\", "/").split("/")
    return any(p in JUNK or p.startswith("._") for p in parts)


# ---------------------------------------------------------------- unpack
def safe_extract(archive, dest):
    """Extract a .tar(.gz) or .zip without allowing paths outside `dest`."""
    os.makedirs(dest, exist_ok=True)
    total = 0

    def target_for(name):
        name = name.replace("\\", "/")
        if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
            raise UpdateError("ملف التحديث فيه مسار غير مسموح: %s" % name)
        parts = [p for p in name.split("/") if p not in ("", ".")]
        if any(p == ".." for p in parts):
            raise UpdateError("ملف التحديث فيه مسار غير مسموح: %s" % name)
        return _p(dest, *parts) if parts else None

    if tarfile.is_tarfile(archive):
        with tarfile.open(archive, "r:*") as tf:
            members = tf.getmembers()
            if len(members) > MAX_MEMBERS:
                raise UpdateError("ملف التحديث فيه ملفات كثيرة كثير")
            for m in members:
                if _is_junk(m.name):
                    continue
                out = target_for(m.name)
                if out is None:
                    continue
                if m.isdir():
                    os.makedirs(out, exist_ok=True)
                elif m.isfile():
                    total += m.size
                    if total > MAX_UNPACKED:
                        raise UpdateError("ملف التحديث كبير كثير")
                    os.makedirs(os.path.dirname(out), exist_ok=True)
                    with tf.extractfile(m) as src, open(out, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    os.chmod(out, 0o755 if m.mode & 0o111 else 0o644)
                # symlinks, devices, hard links: ignored on purpose
    elif zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as zf:
            infos = zf.infolist()
            if len(infos) > MAX_MEMBERS:
                raise UpdateError("ملف التحديث فيه ملفات كثيرة كثير")
            for zi in infos:
                if _is_junk(zi.filename):
                    continue
                out = target_for(zi.filename)
                if out is None:
                    continue
                if zi.is_dir():
                    os.makedirs(out, exist_ok=True)
                    continue
                total += zi.file_size
                if total > MAX_UNPACKED:
                    raise UpdateError("ملف التحديث كبير كثير")
                os.makedirs(os.path.dirname(out), exist_ok=True)
                with zf.open(zi) as src, open(out, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                os.chmod(out, 0o755 if out.endswith(".sh") else 0o644)
    else:
        raise UpdateError("هذا مش ملف تحديث (لازم يكون .tar.gz أو .zip)")
    return find_root(dest)


def find_root(folder):
    """The package root is the folder that has VERSION and app/main.py."""
    for cand in [folder] + [_p(folder, d) for d in sorted(os.listdir(folder))]:
        if os.path.isfile(_p(cand, "VERSION")) and os.path.isfile(_p(cand, "app", "main.py")):
            return cand
    raise UpdateError("ملف التحديث ناقص: ما لقيت VERSION و app/main.py")


# ---------------------------------------------------------------- manifest
def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            h.update(block)
    return h.hexdigest()


def package_files(root):
    out = []
    for d, dirs, files in os.walk(root):
        dirs[:] = sorted(x for x in dirs if not _is_junk(x) and x not in ("bin", "tests", "dist"))
        for n in sorted(files):
            rel = os.path.relpath(_p(d, n), root).replace(os.sep, "/")
            if _is_junk(rel) or rel in NOT_IN_MANIFEST:
                continue
            out.append(rel)
    return out


def write_manifest(root):
    lines = ["%s  %s" % (file_sha256(_p(root, rel)), rel) for rel in package_files(root)]
    with open(_p(root, "MANIFEST.sha256"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return len(lines)


def verify_manifest(root):
    path = _p(root, "MANIFEST.sha256")
    if not os.path.isfile(path):
        raise UpdateError("ملف التحديث ما فيه MANIFEST.sha256 (مش ملف رسمي)")
    bad = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line.strip():
                continue
            digest, rel = line.split("  ", 1)
            fp = _p(root, *rel.split("/"))
            if not os.path.isfile(fp) or file_sha256(fp) != digest:
                bad.append(rel)
    if bad:
        raise UpdateError("ملفات ناقصة أو خربانة: %s" % ", ".join(bad[:5]))


# ---------------------------------------------------------------- build
def _src_hash(root):
    h = hashlib.sha256()
    src = _p(root, "pifmrds")
    for n in sorted(os.listdir(src)):
        if n.endswith((".c", ".h")):
            h.update(n.encode())
            with open(_p(src, n), "rb") as f:
                h.update(f.read())
    with open(_p(root, "system", "build-pifmrds.sh"), "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def build_transmitter(root, log, base=BASE):
    out_dir = _p(root, "bin")
    os.makedirs(out_dir, exist_ok=True)
    out = _p(out_dir, "pi_fm_rds")
    want = _src_hash(root)
    cur = current_dir(base)
    if cur and cur != root:
        old = _p(cur, "bin")
        try:
            with open(_p(old, ".src-hash")) as f:
                same = f.read().strip() == want
        except OSError:
            same = False
        if same and os.path.isfile(_p(old, "pi_fm_rds")):
            shutil.copy2(_p(old, "pi_fm_rds"), out)
            log("transmitter source unchanged: reused the compiled program")
            with open(_p(out_dir, ".src-hash"), "w") as f:
                f.write(want)
            return
    fake = os.environ.get("PIRADIO_FAKE_TX")          # test suite only
    if fake:
        shutil.copy2(fake, out)
        log("test mode: copied %s" % fake)
    else:
        log("compiling the FM transmitter (about a minute)...")
        r = subprocess.run(["bash", _p(root, "system", "build-pifmrds.sh"),
                            _p(root, "pifmrds"), out],
                           capture_output=True, text=True, timeout=900)
        for line in (r.stdout + r.stderr).splitlines()[-15:]:
            log("  " + line)
        if r.returncode != 0 or not os.path.isfile(out):
            raise UpdateError("بناء برنامج البث فشل (التفاصيل بالسجل)")
    with open(_p(out_dir, ".src-hash"), "w") as f:
        f.write(want)


def import_check(root, log):
    app = _p(root, "app")
    code = ("import sys; sys.path.insert(0, %r); "
            "import settings, rdstext, library, engine, system, radio, server, updater; "
            "print('ok')" % app)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       timeout=120, cwd=app, env=dict(os.environ, PIRADIO_DEV="1"))
    if r.returncode != 0 or "ok" not in r.stdout:
        tail = (r.stderr or r.stdout).strip().splitlines()[-3:]
        raise UpdateError("كود النسخة الجديدة فيه خطأ: %s" % " | ".join(tail))
    log("python code check passed")


# ---------------------------------------------------------------- deploy
def deploy(src, base=BASE, state=STATE, pending=True, log=print):
    """Install `src` (folder or archive). Returns the installed version."""
    work = None
    try:
        if os.path.isfile(src):
            work = tempfile.mkdtemp(prefix="piradio-update-", dir=state if os.path.isdir(state) else None)
            log("unpacking %s" % os.path.basename(src))
            root = safe_extract(src, work)
        else:
            root = find_root(src)
        version = read_version(root)
        if not version:
            raise UpdateError("رقم الإصدار (VERSION) مش صحيح")
        cur = current_dir(base)
        cur_v = read_version(cur) if cur else None
        log("installing version %s (current: %s)" % (version, cur_v or "none"))
        verify_manifest(root)
        log("all files verified (MANIFEST.sha256)")

        releases = _p(base, "releases")
        os.makedirs(releases, exist_ok=True)
        incoming = _p(releases, ".incoming")
        shutil.rmtree(incoming, ignore_errors=True)
        shutil.copytree(root, incoming, ignore=shutil.ignore_patterns("._*", ".DS_Store",
                                                                      "__pycache__", "bin"))
        # keep the user's piradio.conf from the running version
        if cur and os.path.isfile(_p(cur, "piradio.conf")) and pending:
            shutil.copy2(_p(cur, "piradio.conf"), _p(incoming, "piradio.conf"))
        try:
            build_transmitter(incoming, log, base)
            import_check(incoming, log)
        except Exception:
            shutil.rmtree(incoming, ignore_errors=True)
            raise
        hook = _p(incoming, "system", "post-update.sh")
        if pending and os.path.isfile(hook):
            log("running post-update.sh")
            r = subprocess.run(["bash", hook], capture_output=True, text=True, timeout=600,
                               env=dict(os.environ, PIRADIO_NEW=incoming, PIRADIO_OLD=cur or ""))
            for line in (r.stdout + r.stderr).splitlines()[-10:]:
                log("  " + line)
            if r.returncode != 0:
                raise UpdateError("سكربت ما بعد التحديث فشل")

        dest, n = _p(releases, version), 2
        while os.path.exists(dest):
            if dest == cur:
                dest = _p(releases, "%s-r%d" % (version, n))
                n += 1
            else:
                shutil.rmtree(dest)
        os.rename(incoming, dest)
        if cur:
            switch_link(_p(base, "previous"), cur)
        switch_link(_p(base, "current"), dest)
        if pending:
            _write_json(_p(state, "update-pending.json"),
                        {"version": version, "from": cur_v, "time": time.time()})
            _rm(_p(state, "update-attempts"))
        _prune(base)
        log("version %s installed" % version)
        return version
    finally:
        if work:
            shutil.rmtree(work, ignore_errors=True)


def _prune(base):
    keep = {current_dir(base), previous_dir(base)}
    rel = _p(base, "releases")
    for n in os.listdir(rel):
        p = os.path.realpath(_p(rel, n))
        if p not in keep and not n.startswith("."):
            shutil.rmtree(p, ignore_errors=True)


# ---------------------------------------------------------------- health
def confirm(state=STATE, base=BASE):
    """Called by the new version once it is running fine."""
    pend = _read_json(_p(state, "update-pending.json"))
    if not pend:
        return False
    _write_json(_p(state, "update-result.json"), {
        "result": "ok", "version": read_version(current_dir(base)), "from": pend.get("from"),
        "time": time.time()})
    _rm(_p(state, "update-pending.json"))
    _rm(_p(state, "update-attempts"))
    return True


def rollback(reason="", base=BASE, state=STATE):
    cur, prev = current_dir(base), previous_dir(base)
    _rm(_p(state, "update-pending.json"))
    _rm(_p(state, "update-attempts"))
    if not prev:
        return False
    switch_link(_p(base, "current"), prev)
    switch_link(_p(base, "previous"), cur)
    _write_json(_p(state, "update-result.json"), {
        "result": "rolled_back", "from": read_version(cur), "to": read_version(prev),
        "reason": reason, "time": time.time()})
    return True


def launch_check(base=BASE, state=STATE):
    """Run by the systemd launcher before every start of the service."""
    if not os.path.isfile(_p(state, "update-pending.json")):
        return
    try:
        with open(_p(state, "update-attempts")) as f:
            n = int(f.read().strip() or 0) + 1
    except (OSError, ValueError):
        n = 1
    with open(_p(state, "update-attempts"), "w") as f:
        f.write(str(n))
    if n > MAX_CRASHES:
        rollback("النسخة الجديدة وقفت %d مرات عند التشغيل" % (n - 1), base, state)


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    cmd = argv[1]
    try:
        if cmd == "deploy" and len(argv) >= 3:
            deploy(argv[2], pending="--no-pending" not in argv)
        elif cmd == "rollback":
            print("rolled back" if rollback(" ".join(argv[2:]) or "manual") else "nothing to roll back")
        elif cmd == "launch-check":
            launch_check()
        elif cmd == "info":
            print(json.dumps(info(), ensure_ascii=False, indent=1))
        elif cmd == "manifest" and len(argv) >= 3:
            print("%d files" % write_manifest(argv[2]))
        else:
            print(__doc__)
            return 2
    except UpdateError as e:
        print("ERROR: %s" % e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
