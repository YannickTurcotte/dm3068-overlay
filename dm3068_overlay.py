# -*- coding: utf-8 -*-
"""
DM3068 Overlay v4 — live bench readout for the Rigol DM3068, in a floating
always-on-top window: big red digits on black, cyan OVERLOAD banner, scrolling
graph — the kind of inset you see in repair videos, wired to YOUR meter.

Features:
  - live reading (≈ 1–28 samples/s depending on function) with SI prefixes
  - 11 remotely switchable functions; badge follows panel changes too
  - ⚙ settings dialog: IP address + language (English default, French
    available); persisted to dm3068_overlay.json next to the script
  - ▶/⏸ start-stop (series kept), 🧹 Clear (wipe series/stats/graph)
  - scrolling 600-point graph, auto-scaled, min/avg/max stats, measured rate
  - speed − / +: MAX → ÷4 → ÷10 → 1/5 s (polling cadence; the hardware
    ceiling is fixed per function: DCV ≈ 2.7 sps, CONT ≈ 28 sps, DIOD ≈ 25
    sps, ACV/ACI ≈ 1 sps, FREQ/PER ≈ 1 sps — this firmware rejects
    RATE/NPLC/ZERO/APER)
  - ▶/■ CSV live logging: rows written in real time to logs/*.csv,
    "● REC n" counter in the stats line while recording
  - 🔔 threshold alarm with beep, 📷 window snapshot (Pillow)
  - start with graph open: python dm3068_overlay.py --graph

Protocol: raw SCPI over TCP port 5555 (no dependencies required).
Close: window X or Escape.
"""
import csv
import datetime
import json
import os
import queue
import socket
import sys
import threading
import time
import tkinter as tk
from collections import deque
from tkinter import simpledialog, messagebox

try:
    import winsound
except ImportError:
    winsound = None
try:
    from PIL import ImageGrab
except ImportError:
    ImageGrab = None

DEFAULT_IP = "192.168.2.218"
PORT = 5555
OVERLOAD = 9.9e37          # value returned when out of range
HISTORY = 600              # points kept for the graph
CAPTURES_DIR = "captures"
LOGS_DIR = "logs"
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "dm3068_overlay.json")

# function code returned by :FUNCtion?  ->  (MEAS command, base unit)
FUNCS = {
    "DCV":   (":MEAS:VOLT:DC?",  "V"),
    "ACV":   (":MEAS:VOLT:AC?",  "V"),
    "DCI":   (":MEAS:CURR:DC?",  "A"),
    "ACI":   (":MEAS:CURR:AC?",  "A"),
    "2WR":   (":MEAS:RES?",      "Ω"),
    "4WR":   (":MEAS:FRES?",     "Ω"),
    "CONT":  (":MEAS:CONT?",     "Ω"),
    "FREQ":  (":MEAS:FREQ?",     "Hz"),
    "PERI":  (":MEAS:PER?",      "s"),
    "DIODE": (":MEAS:DIOD?",     "V"),
    "CAP":   (":MEAS:CAP?",      "F"),
}

# (extra sleep in s, label) — polling cadence on top of the hardware ceiling
SPEEDS = [(0.0, "MAX"), (1.0, "÷4"), (3.0, "÷10"), (10.0, "1/5s")]

BUTTONS = [
    ("V⎓", "DCV"), ("V~", "ACV"), ("A⎓", "DCI"), ("A~", "ACI"),
    ("Ω", "2WR"), ("Ω4", "4WR"), ("CONT", "CONT"), ("Hz", "FREQ"),
    ("s", "PERI"), ("DIOD", "DIODE"), ("F", "CAP"),
]

# ---------------------------------------------------------------- i18n
T = {
    "en": {
        "funcs": {"DCV": "DC volts", "ACV": "AC volts", "DCI": "DC current",
                  "ACI": "AC current", "2WR": "Resistance",
                  "4WR": "4-wire resistance", "CONT": "Continuity",
                  "FREQ": "Frequency", "PERI": "Period",
                  "DIODE": "Diode test", "CAP": "Capacitance"},
        "connecting": "connecting…", "lost": "● CONNECTION LOST",
        "stopped": "⏸ acquisition stopped",
        "alarm": "🔔 ALARM: threshold exceeded",
        "csv_done": "📄 logging stopped — {rows} rows written → {path}",
        "snap_done": "📷 snapshot → {path}",
        "unknown": "unknown func {f}",
        "sps": "sps", "pts": "pts",
        "avg": "avg",
        "clear": "Clear",
        "alarm_title": "Alarm",
        "alarm_prompt": "Alarm threshold in {unit} (absolute value):",
        "set_title": "Settings", "set_ip": "IP address:",
        "set_lang": "Language:", "ok": "OK", "cancel": "Cancel",
        "bad_ip": "Invalid IP address.",
        "waiting": "waiting for points…",
    },
    "fr": {
        "funcs": {"DCV": "V continu", "ACV": "V alternatif", "DCI": "A continu",
                  "ACI": "A alternatif", "2WR": "Résistance",
                  "4WR": "Résistance 4 fils", "CONT": "Continuité",
                  "FREQ": "Fréquence", "PERI": "Période",
                  "DIODE": "Test diode", "CAP": "Capacité"},
        "connecting": "connexion…", "lost": "● PERTE LIAISON",
        "stopped": "⏸ acquisition arrêtée",
        "alarm": "🔔 ALARME : seuil dépassé",
        "csv_done": "📄 enregistrement arrêté — {rows} lignes écrites → {path}",
        "snap_done": "📷 capture → {path}",
        "unknown": "fct inconnue {f}",
        "sps": "éch/s", "pts": "éch",
        "avg": "moy",
        "clear": "Vider",
        "alarm_title": "Alarme",
        "alarm_prompt": "Seuil d'alarme en {unit} (valeur absolue) :",
        "set_title": "Configuration", "set_ip": "Adresse IP :",
        "set_lang": "Langue :", "ok": "OK", "cancel": "Annuler",
        "bad_ip": "Adresse IP invalide.",
        "waiting": "en attente de points…",
    },
}
LANG_CHOICES = [("English", "en"), ("Français", "fr")]


def load_config():
    cfg = {"ip": DEFAULT_IP, "lang": "en"}
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update({k: v for k, v in json.load(f).items()
                        if k in ("ip", "lang")})
    except (OSError, ValueError):
        pass
    return cfg


def fmt(val, func):
    """-> (digits, prefixed unit)  e.g. -1.839 mV ; 60.006 Hz."""
    if func == "DIODE":
        return f"{val:.3f}", "V"
    for p, sym in ((1e9, "G"), (1e6, "M"), (1e3, "k"),
                   (1.0, ""), (1e-3, "m"), (1e-6, "µ"), (1e-9, "n")):
        if abs(val) >= p:
            return f"{val / p:.3f}", f"{sym}{FUNCS[func][1]}"
    return f"{val:.3f}", FUNCS[func][1]


class Meter(threading.Thread):
    """Reader thread: raw SCPI, adjustable cadence, start/stop. Messages are
    tagged with a generation id so stale messages from a replaced thread
    (e.g. after an IP change) are ignored by the UI."""

    def __init__(self, out_q, ip, gen):
        super().__init__(daemon=True)
        self.out_q = out_q
        self.ip = ip
        self.gen = gen
        self.stop_evt = threading.Event()
        self.desired_func = None
        self.speed_idx = 0
        self.running = True           # ▶/⏸ acquisition
        self.sock = None
        self.func = None

    def put(self, kind, *payload):
        self.out_q.put((self.gen, kind, *payload))

    def set_speed(self, idx):
        self.speed_idx = idx % len(SPEEDS)

    def shutdown(self):
        self.stop_evt.set()
        if self.sock is not None:
            try:
                self.sock.close()      # unblocks any pending recv
            except OSError:
                pass

    def _read_line(self):
        self.sock.settimeout(4)
        buf = b""
        while b"\n" not in buf:
            d = self.sock.recv(4096)
            if not d:
                raise ConnectionError("link closed by instrument")
            buf += d
        return buf.decode(errors="replace").strip()

    def _ask(self, cmd):
        self.sock.sendall(cmd.encode() + b"\n")
        return self._read_line()

    def _connect(self):
        self.sock = socket.create_connection((self.ip, PORT), timeout=2)
        self.sock.settimeout(4)
        for _ in range(12):                       # drain the error queue
            if self._ask(":SYSTem:ERRor?").startswith("0,"):
                break
        self.func = self._ask(":FUNCtion?")

    def run(self):
        was_running = True
        since_funcq = 0
        while not self.stop_evt.is_set():
            try:
                if self.sock is None:
                    self._connect()
                    self.put("status", f"● {self.ip}")
                    self.put("func", self.func)

                # function button click: MEAS switches AND measures; when
                # stopped the reading is discarded so the series stays frozen
                if self.desired_func is not None:
                    self._ask(FUNCS[self.desired_func][0])
                    self.func = self.desired_func
                    self.desired_func = None
                    since_funcq = 0
                    self.put("func", self.func)

                if not self.running:
                    if was_running:
                        self.put("stopped")
                        was_running = False
                    self.stop_evt.wait(0.3)
                    continue
                was_running = True

                # periodic re-sync with the meter's physical button
                if since_funcq >= 10:
                    self.func = self._ask(":FUNCtion?")
                    since_funcq = 0
                    self.put("func", self.func)
                since_funcq += 1

                entry = FUNCS.get(self.func)
                if entry is None:
                    self.put("reading", self.func, None, "unknown")
                    self.stop_evt.wait(1.0)
                    continue
                raw = self._ask(entry[0])
                try:
                    val = float(raw)
                except ValueError:
                    self.put("reading", self.func, None, raw)
                    continue
                self.put("reading", self.func, val, None, time.time())

                extra = SPEEDS[self.speed_idx][0]
                if extra:
                    self.stop_evt.wait(extra)
            except Exception:
                if self.stop_evt.is_set():
                    break
                self.put("status", "LOST")
                if self.sock is not None:
                    try:
                        self.sock.close()
                    except OSError:
                        pass
                self.sock = None
                self.stop_evt.wait(1.5)


class Overlay(tk.Tk):
    def __init__(self):
        super().__init__()
        cfg = load_config()
        self.lang = cfg["lang"] if cfg["lang"] in ("en", "fr") else "en"
        self.ip = cfg["ip"]
        self.T = T[self.lang]

        self.title(f"DM3068 — {self.ip}")
        self.configure(bg="black")
        self.attributes("-topmost", True)
        self.protocol("WM_DELETE_WINDOW", self._quit)
        self.bind("<Escape>", lambda e: self._quit())

        self.series = deque(maxlen=HISTORY)     # (timestamp, value | None=OL)
        self.dirty = False
        self.func = None
        self.alarm_threshold = None
        self.last_beep = 0.0
        self.gen = 0
        self.log_f = None            # fichier CSV d'enregistrement temps réel
        self.log_count = 0
        self.log_name = None

        top = tk.Frame(self, bg="black")
        top.pack(fill="x", padx=10, pady=(6, 0))
        self.func_lbl = tk.Label(top, text="—", font=("Consolas", 13, "bold"),
                                 fg="#ffd400", bg="black")
        self.func_lbl.pack(side="left")
        self.stat_lbl = tk.Label(top, text=self.T["connecting"],
                                 font=("Consolas", 11), fg="#888888", bg="black")
        self.stat_lbl.pack(side="right")

        mid = tk.Frame(self, bg="black")
        mid.pack(pady=(2, 0))
        self.val_lbl = tk.Label(mid, text="- - - - -", font=("Consolas", 54, "bold"),
                                fg="#ff1a1a", bg="black")
        self.val_lbl.pack(side="left")
        self.unit_lbl = tk.Label(mid, text="", font=("Consolas", 30, "bold"),
                                 fg="#ff1a1a", bg="black")
        self.unit_lbl.pack(side="left", padx=(12, 0))

        self.ol_lbl = tk.Label(self, text="", font=("Consolas", 16, "bold"),
                               fg="#00ffff", bg="black")
        self.ol_lbl.pack()

        self.stats_lbl = tk.Label(self, text="", font=("Consolas", 11),
                                  fg="#9aa0a6", bg="black")
        self.stats_lbl.pack(pady=(0, 4))

        self.canvas = tk.Canvas(self, width=560, height=170, bg="#0a0a0a",
                                highlightthickness=1, highlightbackground="#333333")
        self.graph_on = False

        # --- function buttons ---
        btns = tk.Frame(self, bg="black")
        btns.pack(fill="x", padx=8)
        for label, code in BUTTONS:
            b = tk.Button(btns, text=label, width=5, bd=0,
                          font=("Consolas", 11, "bold"),
                          fg="#dddddd", bg="#1a1a1a", activebackground="#333333",
                          activeforeground="#ffffff",
                          command=lambda c=code: self.switch_to(c))
            b.pack(side="left", padx=2)

        # --- control buttons ---
        def ctl_btn(parent, text, cb, fg="#dddddd", width=3):
            return tk.Button(parent, text=text, width=width, bd=0,
                             font=("Consolas", 11, "bold"),
                             fg=fg, bg="#1a1a1a", activebackground="#333333",
                             activeforeground="#ffffff", command=cb)

        ctl = tk.Frame(self, bg="black")
        ctl.pack(fill="x", padx=8, pady=(6, 8))
        self.speed_lbl = tk.Label(ctl, text="⏱ MAX", font=("Consolas", 11, "bold"),
                                  fg="#66d9ef", bg="black", width=9, anchor="w")
        self.speed_lbl.pack(side="left", padx=(2, 4))
        ctl_btn(ctl, "−", lambda: self.bump_speed(-1), fg="#66d9ef").pack(side="left", padx=2)
        ctl_btn(ctl, "+", lambda: self.bump_speed(+1), fg="#66d9ef").pack(side="left", padx=2)

        self.run_btn = ctl_btn(ctl, "⏸", self.toggle_run, fg="#ffaa00", width=4)
        self.run_btn.pack(side="left", padx=(8, 2))
        self.clear_btn = ctl_btn(ctl, self.T["clear"], self.clear_series,
                                 fg="#dddddd", width=7)
        self.clear_btn.pack(side="left", padx=2)
        self.graph_btn = ctl_btn(ctl, "📈", self.toggle_graph, fg="#a6e22e", width=4)
        self.graph_btn.pack(side="left", padx=2)

        self.csv_btn = ctl_btn(ctl, "▶ CSV", self.toggle_log, fg="#a6e22e", width=8)
        self.csv_btn.pack(side="left", padx=2)

        self.alarm_btn = ctl_btn(ctl, "🔔", self.set_alarm, fg="#ff8080", width=4)
        self.alarm_btn.pack(side="left", padx=2)

        if ImageGrab is not None:
            ctl_btn(ctl, "📷", self.snapshot, fg="#aaaaaa", width=4).pack(side="left", padx=2)
        ctl_btn(ctl, "⚙", self.open_settings, fg="#bbbbbb", width=4).pack(side="left", padx=2)

        self.q = queue.Queue()
        self.meter = Meter(self.q, self.ip, self.gen)
        self.meter.start()
        self.after(70, self.poll)
        self.after(140, self.redraw)

        if "--graph" in sys.argv:                # start with graph open
            self.toggle_graph()

    # ---------------- settings ----------------

    def open_settings(self):
        dlg = tk.Toplevel(self)
        dlg.title(self.T["set_title"])
        dlg.configure(bg="black")
        dlg.resizable(False, False)
        dlg.transient(self)
        dlg.grab_set()
        dlg.attributes("-topmost", True)

        tk.Label(dlg, text=self.T["set_ip"], font=("Consolas", 11, "bold"),
                 fg="#dddddd", bg="black").grid(row=0, column=0, sticky="w",
                                                padx=10, pady=(12, 4))
        ip_var = tk.StringVar(value=self.ip)
        tk.Entry(dlg, textvariable=ip_var, width=18, bd=0,
                 font=("Consolas", 12), fg="#ff1a1a", bg="#111111",
                 insertbackground="#ff1a1a").grid(row=1, column=0, sticky="we",
                                                  padx=10, pady=(0, 8))

        tk.Label(dlg, text=self.T["set_lang"], font=("Consolas", 11, "bold"),
                 fg="#dddddd", bg="black").grid(row=2, column=0, sticky="w",
                                                padx=10, pady=(4, 4))
        # two explicit buttons instead of a dropdown (option menus are
        # unreliable under the modal grab of the dialog)
        chosen = {"lang": self.lang}
        lang_btns = {}

        def pick(code):
            chosen["lang"] = code
            for lbl, c in LANG_CHOICES:
                b = lang_btns[c]
                b.config(bg="#881111" if c == code else "#1a1a1a",
                         fg="#ffffff" if c == code else "#dddddd")

        langrow = tk.Frame(dlg, bg="black")
        langrow.grid(row=3, column=0, sticky="w", padx=10, pady=(0, 12))
        for lbl, c in LANG_CHOICES:
            b = tk.Button(langrow, text=lbl, width=10, bd=0,
                          font=("Consolas", 11, "bold"),
                          fg="#ffffff" if c == self.lang else "#dddddd",
                          bg="#881111" if c == self.lang else "#1a1a1a",
                          activebackground="#333333",
                          activeforeground="#ffffff",
                          command=lambda c=c: pick(c))
            b.pack(side="left", padx=(0, 6))
            lang_btns[c] = b

        def on_ok():
            ip = ip_var.get().strip()
            try:
                socket.inet_aton(ip)
            except OSError:
                messagebox.showwarning(self.T["set_title"], self.T["bad_ip"],
                                       parent=dlg)
                return
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump({"ip": ip, "lang": chosen["lang"]}, f)
            dlg.destroy()
            self.apply_settings(ip, chosen["lang"])

        row = tk.Frame(dlg, bg="black")
        row.grid(row=4, column=0, sticky="e", padx=10, pady=(0, 12))
        tk.Button(row, text=self.T["cancel"], width=8, bd=0,
                  font=("Consolas", 11, "bold"), fg="#dddddd", bg="#1a1a1a",
                  activebackground="#333333",
                  command=dlg.destroy).pack(side="left", padx=4)
        tk.Button(row, text=self.T["ok"], width=8, bd=0,
                  font=("Consolas", 11, "bold"), fg="#a6e22e", bg="#1a1a1a",
                  activebackground="#333333", command=on_ok).pack(side="left")

    def apply_settings(self, ip, lang):
        lang_changed = lang != self.lang
        ip_changed = ip != self.ip

        if lang_changed:
            self.lang = lang
            self.T = T[lang]
            self.clear_btn.config(text=self.T["clear"])
            self.apply_func(self.func)           # refresh the badge wording

        if ip_changed:
            self.ip = ip
            self.gen += 1
            self._log_stop()                     # never mix meters in one file
            self.meter.shutdown()                # kill the old thread promptly
            self.series.clear()
            self.dirty = True
            self.func = None
            self.func_lbl.config(text="—")
            self.title(f"DM3068 — {self.ip}")
            self.stat_lbl.config(text=self.T["connecting"], fg="#888888")
            self.ol_lbl.config(text="")
            self.meter = Meter(self.q, self.ip, self.gen)
            self.meter.start()

    # ---------------- interactions ----------------

    def switch_to(self, code):
        self.meter.desired_func = code

    def bump_speed(self, direction):
        self.meter.set_speed(self.meter.speed_idx + direction)
        self.speed_lbl.config(text=f"⏱ {SPEEDS[self.meter.speed_idx][1]}")

    def toggle_run(self):
        self.meter.running = not self.meter.running
        self.run_btn.config(text="▶" if not self.meter.running else "⏸")

    def clear_series(self):
        self.series.clear()
        self.dirty = True
        self.update_stats()

    def toggle_graph(self):
        self.graph_on = not self.graph_on
        if self.graph_on:
            self.canvas.pack(fill="x", padx=8, pady=(0, 8))
        else:
            self.canvas.pack_forget()

    def set_alarm(self):
        if self.alarm_threshold is not None:      # disarm
            self.alarm_threshold = None
            self.alarm_btn.config(text="🔔", fg="#ff8080", bg="#1a1a1a")
            self.ol_lbl.config(text="")
            return
        unit = FUNCS[self.func][1] if self.func in FUNCS else "?"
        th = simpledialog.askfloat(
            self.T["alarm_title"], self.T["alarm_prompt"].format(unit=unit),
            parent=self, minvalue=0.0, initialvalue=1.0)
        if th is None:
            return
        self.alarm_threshold = th
        self.alarm_btn.config(text="🔔 ON", fg="#ffffff", bg="#881111")

    def toggle_log(self):
        if self.log_f is None:                      # ▶ start recording
            os.makedirs(LOGS_DIR, exist_ok=True)
            self.log_name = f"{LOGS_DIR}/dm3068_{datetime.datetime.now():%Y%m%d_%H%M%S}.csv"
            unit = FUNCS[self.func][1] if self.func in FUNCS else "?"
            self.log_f = open(self.log_name, "w", newline="", encoding="utf-8-sig")
            csv.writer(self.log_f, delimiter=";").writerow(
                ["timestamp", "epoch_s", "function", f"value ({unit})"])
            self.log_count = 0
            self.csv_btn.config(text="■ CSV", fg="#ff5555", bg="#401010",
                                activebackground="#662020")
            self.stats_lbl.config(fg="#ff8888")
        else:                                       # ■ stop recording
            self._log_stop()
            self.ol_lbl.config(text=self.T["csv_done"].format(
                rows=self.log_count, path=self.log_name), fg="#a6e22e")
            self.after(6000, lambda: self.ol_lbl.config(text=""))

    def _log_stop(self):
        if self.log_f is not None:
            try:
                self.log_f.close()
            except OSError:
                pass
        self.log_f = None
        self.csv_btn.config(text="▶ CSV", fg="#a6e22e", bg="#1a1a1a",
                            activebackground="#333333")
        self.stats_lbl.config(fg="#9aa0a6")

    def _quit(self):
        if self.log_f is not None:
            self._log_stop()
        self.meter.shutdown()
        self.destroy()

    def snapshot(self):
        os.makedirs(CAPTURES_DIR, exist_ok=True)
        x, y = self.winfo_rootx(), self.winfo_rooty()
        w, h = self.winfo_width(), self.winfo_height()
        img = ImageGrab.grab(bbox=(x, y, x + w, y + h))
        name = f"{CAPTURES_DIR}/snapshot_{datetime.datetime.now():%Y%m%d_%H%M%S}.png"
        img.save(name)
        self.ol_lbl.config(text=self.T["snap_done"].format(path=name), fg="#a6e22e")
        self.after(3000, lambda: self.ol_lbl.config(text=""))

    # ---------------- UI loops ----------------

    def apply_func(self, code):
        """Refresh the function badge; wipe the series ONLY on a real change."""
        changed = code != self.func
        self.func = code
        if code in FUNCS:
            self.func_lbl.config(text=f"{code}  ({self.T['funcs'][code]})")
        else:
            self.func_lbl.config(text=str(code) if code else "—")
        if changed and code is not None:
            self.series.clear()
            self.dirty = True

    def poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                if msg[0] != self.gen:            # stale message from old meter
                    continue
                kind = msg[1]
                if kind == "status":
                    txt, lost = msg[2], msg[2] == "LOST"
                    self.stat_lbl.config(
                        text=self.T["lost"] if lost else txt,
                        fg="#ff4040" if lost else "#33cc33")
                elif kind == "func":
                    self.apply_func(msg[2])
                elif kind == "stopped":
                    self.ol_lbl.config(text=self.T["stopped"], fg="#ffaa00")
                elif kind == "reading":
                    _, _, func, val, err, ts = msg
                    self.apply_func(func)
                    if err is not None:
                        self.val_lbl.config(text="!  ERR")
                        self.unit_lbl.config(text="")
                        self.ol_lbl.config(
                            text=self.T["unknown"].format(f=func)
                            if err == "unknown" else str(err), fg="#ff8080")
                    elif val >= OVERLOAD:
                        self.val_lbl.config(text="- - - -")
                        self.unit_lbl.config(text="")
                        self.ol_lbl.config(text="OVERLOAD", fg="#00ffff")
                        self.series.append((ts, None))
                        self.dirty = True
                    else:
                        digits, unit = fmt(val, func)
                        self.val_lbl.config(text=digits)
                        self.unit_lbl.config(text=unit)
                        self.ol_lbl.config(text="")
                        self.series.append((ts, val))
                        self.dirty = True
                    # live CSV logging (real readings, overloads kept)
                    if self.log_f is not None:
                        stamp = datetime.datetime.fromtimestamp(
                            ts).isoformat(timespec="milliseconds")
                        if err is not None:
                            value = "ERROR"
                        elif val >= OVERLOAD:
                            value = "OVERLOAD"
                        else:
                            value = repr(val)
                        csv.writer(self.log_f, delimiter=";").writerow(
                            [stamp, f"{ts:.3f}", func, value])
                        self.log_f.flush()
                        self.log_count += 1

                    # alarm (real readings only)
                    if (self.alarm_threshold is not None and err is None
                            and val < OVERLOAD and abs(val) >= self.alarm_threshold):
                        self.ol_lbl.config(text=self.T["alarm"], fg="#ff4000")
                        if winsound and time.time() - self.last_beep > 0.8:
                            self.last_beep = time.time()
                            threading.Thread(
                                target=winsound.Beep, args=(1200, 150),
                                daemon=True).start()
        except queue.Empty:
            pass
        self.after(70, self.poll)

    def redraw(self):
        if self.graph_on and self.dirty:
            self.dirty = False
            self.draw()
        elif not self.graph_on:
            self.update_stats()
        self.after(140, self.redraw)

    def update_stats(self):
        vals = [v for _, v in self.series if v is not None]
        hz = self.measured_hz()
        rec = f"   ·   ● REC {self.log_count}" if self.log_f is not None else ""
        if not vals:
            self.stats_lbl.config(text=(f"({hz})" if hz else "") + rec)
            return
        lo, hi = min(vals), max(vals)
        avg = sum(vals) / len(vals)
        f3 = (lambda v: " ".join(fmt(v, self.func))
              if self.func in FUNCS else "?")
        rec = f"   ·   ● REC {self.log_count}" if self.log_f is not None else ""
        self.stats_lbl.config(
            text=f"min {f3(lo)}   ·   {self.T['avg']} {f3(avg)}   ·   "
                 f"max {f3(hi)}   ·   {hz}{rec}")

    def measured_hz(self):
        pts = [t for t, v in self.series if v is not None]
        if len(pts) >= 2:
            dt = pts[-1] - pts[0]
            if dt > 0:
                return f"{(len(pts) - 1) / dt:.1f} {self.T['sps']}"
        return ""

    def draw(self):
        self.update_stats()
        c = self.canvas
        c.delete("all")
        w, h = int(c["width"]), int(c["height"])
        for frac in (0.25, 0.5, 0.75):                      # horizontal grid
            y = int(h * frac)
            c.create_line(0, y, w, y, fill="#222222")
        pts = list(self.series)
        vals = [v for _, v in pts if v is not None]
        c.create_text(8, 8, anchor="nw",
                      text=f"{self.func or '?'}   {self.measured_hz()}   "
                           f"[{len(pts)} {self.T['pts']}]",
                      fill="#888888", font=("Consolas", 10))
        if len(vals) < 2:
            c.create_text(w // 2, h // 2, text=self.T["waiting"],
                          fill="#555555", font=("Consolas", 12))
            return
        lo, hi = min(vals), max(vals)
        if hi - lo < 1e-30:
            lo, hi = lo - 1, hi + 1
        pad = (hi - lo) * 0.1
        lo, hi = lo - pad, hi + pad

        def xy(i, v):
            x = i * (w - 1) / max(len(pts) - 1, 1)
            y = h - 4 - (v - lo) / (hi - lo) * (h - 20)
            return x, y

        # continuous segments only (None = OVERLOAD cuts the trace)
        seg = []
        for i, (_, v) in enumerate(pts):
            if v is None:
                if len(seg) >= 2:
                    c.create_line(seg, fill="#ff1a1a", width=1)
                seg = []
            else:
                seg.extend(xy(i, v))
        if len(seg) >= 2:
            c.create_line(seg, fill="#ff1a1a", width=1)

        f3 = (lambda v: " ".join(fmt(v, self.func))
              if self.func in FUNCS else f"{v:.3g}")
        c.create_text(w - 8, 8, anchor="ne", text=f3(hi),
                      fill="#666666", font=("Consolas", 10))
        c.create_text(w - 8, h - 8, anchor="se", text=f3(lo),
                      fill="#666666", font=("Consolas", 10))


if __name__ == "__main__":
    Overlay().mainloop()
