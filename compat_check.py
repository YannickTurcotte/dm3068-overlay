# -*- coding: utf-8 -*-
"""
Compatibility probe for the DM3068 Overlay — also useful for DM3058 or other
Rigol DMMs of the same family.

Connects with raw SCPI on TCP 5555 (same channel the old Java web-control
applet used) and prints a PASS/FAIL report.

Usage:
    python compat_check.py            # default IP 192.168.2.218
    python compat_check.py 10.0.0.5   # your meter's IP
"""
import socket
import sys

IP = sys.argv[1] if len(sys.argv) > 1 else "192.168.2.218"
PORT = 5555

ok = True


def section(name, fn):
    global ok
    try:
        result = fn()
        print(f"[PASS] {name}: {result}")
        return result
    except Exception as e:
        ok = False
        print(f"[FAIL] {name}: {type(e).__name__} {e}")
        return None


def ask(s, cmd, timeout=3.0):
    s.settimeout(timeout)
    s.sendall(cmd.encode() + b"\n")
    buf = b""
    while b"\n" not in buf:
        d = s.recv(4096)
        if not d:
            raise ConnectionError("link closed by instrument")
        buf += d
    return buf.decode(errors="replace").strip()


def main():
    print(f"Probing {IP}:{PORT} ...\n")
    try:
        s = socket.create_connection((IP, PORT), timeout=3)
    except OSError as e:
        print(f"[FAIL] TCP connection: {e}")
        sys.exit(1)

    section("Identity (*IDN?)", lambda: ask(s, "*IDN?"))
    section("Current function (:FUNCtion?)", lambda: ask(s, ":FUNCtion?"))

    def meas():
        r = ask(s, ":MEAS:VOLT:DC?")
        float(r)                       # raises ValueError if not numeric
        return r
    section("Measurement, numeric (:MEAS:VOLT:DC?)", meas)

    section("Error queue (:SYSTem:ERRor?)", lambda: ask(s, ":SYSTem:ERRor?"))
    s.close()

    print("\n---")
    if ok:
        print("All checks passed: the DM3068 Overlay should work on this meter.")
        print("Run it with this IP, and please report the result on the forum")
        print("thread or as a GitHub issue (mention your model & firmware).")
    else:
        print("Some checks failed — this firmware may differ. Report the output")
        print("above on the forum thread or as a GitHub issue.")
    sys.exit(0 if ok else 1)


main()
