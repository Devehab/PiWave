"""Operating-system helpers: Wi-Fi hotspot, power, health, GPIO check."""
import os
import re
import shutil
import subprocess
import threading
import time

DEV = os.environ.get("PIRADIO_DEV") == "1"
HOTSPOT_CON = "PiRadio-Hotspot"
AP_IP = os.environ.get("PIRADIO_AP_IP", "192.168.4.1")

_dev_wifi = {"ssid": "PiRadio", "password": "piradio123"}


def _run(cmd, timeout=10):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, "", str(e)


def _later(delay, cmd):
    def go():
        time.sleep(delay)
        subprocess.run(cmd)
    threading.Thread(target=go, daemon=True).start()


# ---------------- Wi-Fi hotspot ----------------
def hotspot_get():
    if DEV:
        return dict(_dev_wifi, ip=AP_IP)
    _, ssid, _ = _run(["nmcli", "-g", "802-11-wireless.ssid", "connection", "show", HOTSPOT_CON])
    _, psk, _ = _run(["nmcli", "--show-secrets", "-g", "802-11-wireless-security.psk",
                      "connection", "show", HOTSPOT_CON])
    return {"ssid": ssid.replace("\\:", ":"), "password": psk.replace("\\:", ":"), "ip": AP_IP}


def hotspot_set(ssid, password):
    ssid = (ssid or "").strip()
    password = password or ""
    if not 1 <= len(ssid.encode("utf-8")) <= 32:
        raise ValueError("اسم الشبكة لازم يكون من 1 إلى 32 حرف")
    if not 8 <= len(password) <= 63 or not all(" " <= c <= "~" for c in password):
        raise ValueError("كلمة السر لازم تكون من 8 إلى 63 حرف إنجليزي/رقم")
    if DEV:
        _dev_wifi.update(ssid=ssid, password=password)
        return
    rc, _, err = _run(["nmcli", "connection", "modify", HOTSPOT_CON,
                       "802-11-wireless.ssid", ssid, "802-11-wireless-security.psk", password])
    if rc != 0:
        raise ValueError("ما قدرت أغيّر الشبكة: %s" % err)
    # restart the hotspot after the HTTP response has been sent
    _later(2, ["nmcli", "connection", "up", HOTSPOT_CON])


def wifi_join_home():
    """Leave the hotspot and join the saved home Wi-Fi until the next reboot."""
    if not DEV:
        _later(2, ["/usr/local/sbin/piradio-wifi", "home"])


def wifi_mode():
    """'hotspot', 'home' (joined another network) or 'unknown'."""
    if DEV:
        return "hotspot"
    _, out, _ = _run(["nmcli", "-g", "GENERAL.CONNECTION", "device", "show", "wlan0"])
    if out == HOTSPOT_CON:
        return "hotspot"
    return "home" if out else "unknown"


# ---------------- power ----------------
def reboot():
    if not DEV:
        _later(1.5, ["systemctl", "reboot"])


def poweroff():
    if not DEV:
        _later(1.5, ["systemctl", "poweroff"])


def restart_service():
    if not DEV:
        _later(2, ["systemctl", "--no-block", "restart", "piradio"])


def journal(lines=400):
    if DEV or not shutil.which("journalctl"):
        return "(journal not available)"
    _, out, err = _run(["journalctl", "-u", "piradio", "-n", str(lines), "--no-pager",
                        "-o", "short-iso"], timeout=15)
    return out or err


# ---------------- health ----------------
def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def stats(data_dir):
    t = _read("/sys/class/thermal/thermal_zone0/temp")
    temp = round(int(t) / 1000.0, 1) if t and t.lstrip("-").isdigit() else None
    under_now = under_past = None
    if shutil.which("vcgencmd") and not DEV:
        _, out, _ = _run(["vcgencmd", "get_throttled"], timeout=3)
        m = re.search(r"0x([0-9a-fA-F]+)", out)
        if m:
            v = int(m.group(1), 16)
            under_now, under_past = bool(v & 0x1), bool(v & 0x10000)
    try:
        du = shutil.disk_usage(data_dir)
        disk = {"free": du.free, "total": du.total}
    except OSError:
        disk = None
    up = _read("/proc/uptime")
    return {
        "temp": temp, "undervoltage": under_now, "undervoltage_past": under_past,
        "disk": disk, "uptime": int(float(up.split()[0])) if up else None, "dev": DEV,
    }


def gpio4_function():
    """Return what GPIO4 (pin 7) is doing, e.g. 'GPCLK0' while transmitting.

    This reads the real pin configuration, so 'GPCLK0' proves the radio
    clock is being routed out of pin 7.
    """
    if DEV:
        return "DEV"
    for cmd in (["pinctrl", "get", "4"], ["raspi-gpio", "get", "4"]):
        if shutil.which(cmd[0]):
            rc, out, _ = _run(cmd, timeout=3)
            if rc == 0 and out:
                m = re.search(r"GPCLK0", out)
                if m:
                    return "GPCLK0"
                m = re.search(r"func=(\w+)", out) or re.search(r"=\s*(\w+)\s*$", out)
                return m.group(1) if m else out
    return None


def client_id(ip):
    """Stable id of a phone on the hotspot: its Wi-Fi MAC address (phones keep
    one private address per network), or the IP when the MAC is unknown."""
    try:
        with open("/proc/net/arp") as f:
            for line in f.read().splitlines()[1:]:
                cols = line.split()
                if len(cols) >= 4 and cols[0] == ip and cols[3] != "00:00:00:00:00:00":
                    return "mac:" + cols[3].lower()
    except OSError:
        pass
    return "ip:" + str(ip)
