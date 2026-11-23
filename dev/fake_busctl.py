#!/usr/bin/env python3
"""Test double for `busctl` talking to BlueZ (PiRadio test suite only).

Keeps a fake BlueZ object tree in the JSON file named by $PIRADIO_FAKE_BT and
answers the busctl commands PiRadio uses:

  busctl --json=short call org.bluez / org.freedesktop.DBus.ObjectManager GetManagedObjects
  busctl set-property org.bluez <path> <iface> <prop> <sig> <value>
  busctl --timeout=N call org.bluez <path> <iface> <method> [o <path>]

Values are stored the way busctl prints them: {"type": "<sig>", "data": <value>}.
The test edits the same file to pretend a phone paired, connected or played.
"""
import fcntl
import json
import os
import sys

STATE = os.environ["PIRADIO_FAKE_BT"]


class Locked:
    def __enter__(self):
        self.f = open(STATE + ".lock", "w")
        fcntl.flock(self.f, fcntl.LOCK_EX)
        try:
            with open(STATE) as f:
                self.st = json.load(f)
        except (OSError, ValueError):
            self.st = {"objects": {}, "calls": []}
        return self.st

    def __exit__(self, *exc):
        if exc[0] is None:
            tmp = STATE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.st, f, indent=1)
            os.replace(tmp, STATE)
        fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()


def v(sig, data):
    return {"type": sig, "data": data}


def parse(sig, raw):
    if sig == "b":
        return raw.lower() in ("true", "yes", "1", "on")
    if sig in ("u", "i", "q", "n", "t", "x", "y"):
        return int(raw)
    return raw


def fail(msg):
    print(msg, file=sys.stderr)
    sys.exit(1)


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        fail("usage")
    cmd = args[0]
    with Locked() as st:
        objs = st.setdefault("objects", {})
        st.setdefault("calls", []).append(args)
        if st.get("down"):
            fail("Failed to get managed objects: The name org.bluez was not provided by any .service files")
        if cmd == "call" and args[3:5] == ["org.freedesktop.DBus.ObjectManager", "GetManagedObjects"]:
            st["calls"].pop()               # polls would flood the log
            print(json.dumps({"type": "a{oa{sa{sv}}}", "data": [objs]}))
            return
        if cmd == "set-property":
            _, _, path, iface, prop, sig, raw = args[:7]
            if path not in objs or iface not in objs[path]:
                fail("Failed to set property %s on interface %s: Unknown object" % (prop, iface))
            objs[path][iface][prop] = v(sig, parse(sig, raw))
            return
        if cmd == "call":
            _, _, path, iface, method = args[:5]
            rest = args[5:]
            if path not in objs:
                fail("Call failed: Method \"%s\" with signature \"\" on interface \"%s\" doesn't exist" % (method, iface))
            if method == "RemoveDevice":
                dev = rest[1]
                if dev not in objs:
                    fail("Call failed: Does Not Exist")
                for p in [p for p in objs if p == dev or p.startswith(dev + "/")]:
                    del objs[p]
                return
            if method == "Connect":
                if st.get("phone_away"):
                    fail("Call failed: br-connection-page-timeout")
                objs[path]["org.bluez.Device1"]["Connected"] = v("b", True)
                objs[path + "/sep1/fd0"] = {"org.bluez.MediaTransport1": {
                    "Device": v("o", path), "State": v("s", "idle")}}
                return
            if method == "Disconnect":
                objs[path]["org.bluez.Device1"]["Connected"] = v("b", False)
                for p in [p for p in objs if p.startswith(path + "/")]:
                    del objs[p]
                return
            if iface == "org.bluez.MediaPlayer1":
                objs[path][iface]["Status"] = v("s", {"Play": "playing", "Pause": "paused"}.get(method, "playing"))
                st.setdefault("media", []).append(method)
                return
        fail("fake busctl: unsupported %s" % " ".join(args))


if __name__ == "__main__":
    main(sys.argv[1:])
