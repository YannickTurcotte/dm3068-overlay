# -*- coding: utf-8 -*-
"""
Overlay Rigol DM3068 v3 — lecture en direct, vitesse +/-, graphique, CSV, alarme.

Fenêtre toujours au premier plan, gros chiffres rouges sur fond noir,
bannière OVERLOAD cyan (9.9E37), dans le style des incrustations des vidéos
de réparation.

v3 :
  - CORRECTION : le graphique ne se vidait plus toutes les 10 mesures
    (la resynchro périodique de :FUNCtion? effaçait la série à tort)
  - bouton ▶/⏸ start-stop d'acquisition (la série est conservée)
  - bouton 📄 CSV (en mode graphique) : exporte la série affichée
  - bouton 🔔 alarme : seuil en unité de base, bip + alerte au dépassement
  - bouton 📷 capture PNG de la fenêtre (nécessite Pillow, optionnel)
  - vitesse +/− : MAX → ÷4 → ÷10 → 1/5 s (cadence d'interrogation ;
    le plafond matériel est fixe : DCV ≈ 2,7 éch/s, CONT ≈ 28 éch/s,
    DIOD ≈ 25 éch/s, ACV/ACI ≈ 1 éch/s, FREQ/PER ≈ 1 éch/s — ce firmware
    refuse RATE/NPLC/ZERO/APER)
  - démarrage avec graphique ouvert : python dm3068_overlay.py --graph

Protocole : SCPI brut sur port TCP 5555 (aucune dépendance requise).
Fermeture : croix de la fenêtre ou touche Échap.
"""
import csv
import datetime
import queue
import socket
import sys
import threading
import time
import tkinter as tk
from collections import deque
from tkinter import simpledialog

try:
    import winsound
except ImportError:
    winsound = None
try:
    from PIL import ImageGrab
except ImportError:
    ImageGrab = None

IP = "192.168.2.218"
PORT = 5555
OVERLOAD = 9.9e37          # valeur renvoyée hors échelle
HISTORY = 600              # points gardés pour le graphique
CAPTURES_DIR = "captures"
LOGS_DIR = "logs"

# code renvoyé par :FUNCtion?  ->  (commande MEAS, unité de base, libellé)
FUNCS = {
    "DCV":   (":MEAS:VOLT:DC?",  "V",  "V continu"),
    "ACV":   (":MEAS:VOLT:AC?",  "V",  "V alternatif"),
    "DCI":   (":MEAS:CURR:DC?",  "A",  "A continu"),
    "ACI":   (":MEAS:CURR:AC?",  "A",  "A alternatif"),
    "2WR":   (":MEAS:RES?",      "Ω",  "Résistance"),
    "4WR":   (":MEAS:FRES?",     "Ω",  "Résistance 4 fils"),
    "CONT":  (":MEAS:CONT?",     "Ω",  "Continuité"),
    "FREQ":  (":MEAS:FREQ?",     "Hz", "Fréquence"),
    "PERI":  (":MEAS:PER?",      "s",  "Période"),
    "DIODE": (":MEAS:DIOD?",     "V",  "Test diode"),
    "CAP":   (":MEAS:CAP?",      "F",  "Capacité"),
}

# (extra sleep en s, libellé) — cadence d'interrogation au-dessus du plafond
SPEEDS = [(0.0, "MAX"), (1.0, "÷4"), (3.0, "÷10"), (10.0, "1/5s")]

BUTTONS = [
    ("V⎓", "DCV"), ("V~", "ACV"), ("A⎓", "DCI"), ("A~", "ACI"),
    ("Ω", "2WR"), ("Ω4", "4WR"), ("CONT", "CONT"), ("Hz", "FREQ"),
    ("s", "PERI"), ("DIOD", "DIODE"), ("F", "CAP"),
]


def fmt(val, func):
    """-> (chiffres, unité préfixée)  ex.: -1.839 mV ; 60.006 Hz."""
    if func == "DIODE":
        return f"{val:.3f}", "V"
    for p, sym in ((1e9, "G"), (1e6, "M"), (1e3, "k"),
                   (1.0, ""), (1e-3, "m"), (1e-6, "µ"), (1e-9, "n")):
        if abs(val) >= p:
            return f"{val / p:.3f}", f"{sym}{FUNCS[func][1]}"
    return f"{val:.3f}", FUNCS[func][1]


class Meter(threading.Thread):
    """Thread lecture : SCPI brut, cadence réglable, marche/arrêt."""

    def __init__(self, out_q):
        super().__init__(daemon=True)
        self.out_q = out_q
        self.stop_evt = threading.Event()
        self.desired_func = None
        self.speed_idx = 0
        self.running = True           # ▶/⏸ acquisition
        self.sock = None
        self.func = None

    def set_speed(self, idx):
        self.speed_idx = idx % len(SPEEDS)

    def _read_line(self):
        self.sock.settimeout(4)
        buf = b""
        while b"\n" not in buf:
            d = self.sock.recv(4096)
            if not d:
                raise ConnectionError("liaison fermée par l'instrument")
            buf += d
        return buf.decode(errors="replace").strip()

    def _ask(self, cmd):
        self.sock.sendall(cmd.encode() + b"\n")
        return self._read_line()

    def _connect(self):
        self.sock = socket.create_connection((IP, PORT), timeout=2)
        self.sock.settimeout(4)
        for _ in range(12):                       # vider la file d'erreurs
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
                    self.out_q.put(("status", f"● {IP}"))
                    self.out_q.put(("func", self.func))

                # clic bouton de fonction : basculer (MEAS bascule ET mesure ;
                # à l'arrêt on jette la lecture, la série reste gelée)
                if self.desired_func is not None:
                    self._ask(FUNCS[self.desired_func][0])
                    self.func = self.desired_func
                    self.desired_func = None
                    since_funcq = 0
                    self.out_q.put(("func", self.func))

                if not self.running:
                    if was_running:
                        self.out_q.put(("stopped",))
                        was_running = False
                    self.stop_evt.wait(0.3)
                    continue
                was_running = True

                # resynchro périodique avec le bouton physique du mètre
                if since_funcq >= 10:
                    self.func = self._ask(":FUNCtion?")
                    since_funcq = 0
                    self.out_q.put(("func", self.func))
                since_funcq += 1

                entry = FUNCS.get(self.func)
                if entry is None:
                    self.out_q.put(("reading", self.func, None, f"fct inconnue {self.func!r}"))
                    self.stop_evt.wait(1.0)
                    continue
                raw = self._ask(entry[0])
                try:
                    val = float(raw)
                except ValueError:
                    self.out_q.put(("reading", self.func, None, raw))
                    continue
                self.out_q.put(("reading", self.func, val, None, time.time()))

                extra = SPEEDS[self.speed_idx][0]
                if extra:
                    self.stop_evt.wait(extra)
            except Exception:
                self.out_q.put(("status", "● PERTE LIAISON"))
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
        self.title(f"DM3068 — {IP}")
        self.configure(bg="black")
        self.attributes("-topmost", True)
        self.bind("<Escape>", lambda e: self.destroy())

        self.series = deque(maxlen=HISTORY)     # (timestamp, valeur | None=OL)
        self.dirty = False
        self.func = None
        self.alarm_threshold = None
        self.last_beep = 0.0

        top = tk.Frame(self, bg="black")
        top.pack(fill="x", padx=10, pady=(6, 0))
        self.func_lbl = tk.Label(top, text="—", font=("Consolas", 13, "bold"),
                                 fg="#ffd400", bg="black")
        self.func_lbl.pack(side="left")
        self.stat_lbl = tk.Label(top, text="connexion…", font=("Consolas", 11),
                                 fg="#888888", bg="black")
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

        # --- boutons de fonction ---
        btns = tk.Frame(self, bg="black")
        btns.pack(fill="x", padx=8)
        for label, code in BUTTONS:
            b = tk.Button(btns, text=label, width=5, bd=0,
                          font=("Consolas", 11, "bold"),
                          fg="#dddddd", bg="#1a1a1a", activebackground="#333333",
                          activeforeground="#ffffff",
                          command=lambda c=code: self.switch_to(c))
            b.pack(side="left", padx=2)

        # --- boutons de contrôle ---
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

        self.graph_btn = ctl_btn(ctl, "📈", self.toggle_graph, fg="#a6e22e", width=4)
        self.graph_btn.pack(side="left", padx=2)

        self.csv_btn = ctl_btn(ctl, "📄 CSV", self.save_csv, fg="#a6e22e", width=7)
        self.csv_btn.pack(side="left", padx=2)
        self.csv_btn.config(state="disabled", disabledforeground="#555555")

        self.alarm_btn = ctl_btn(ctl, "🔔", self.set_alarm, fg="#ff8080", width=4)
        self.alarm_btn.pack(side="left", padx=2)

        if ImageGrab is not None:
            ctl_btn(ctl, "📷", self.snapshot, fg="#aaaaaa", width=4).pack(side="left", padx=2)

        self.q = queue.Queue()
        self.meter = Meter(self.q)
        self.meter.start()
        self.after(70, self.poll)
        self.after(140, self.redraw)

        if "--graph" in sys.argv:                # démarrer graphique ouvert
            self.toggle_graph()

    # ---------------- interactions ----------------

    def switch_to(self, code):
        self.meter.desired_func = code

    def bump_speed(self, direction):
        self.meter.set_speed(self.meter.speed_idx + direction)
        self.speed_lbl.config(text=f"⏱ {SPEEDS[self.meter.speed_idx][1]}")

    def toggle_run(self):
        self.meter.running = not self.meter.running
        self.run_btn.config(text="▶" if not self.meter.running else "⏸")

    def toggle_graph(self):
        self.graph_on = not self.graph_on
        if self.graph_on:
            self.canvas.pack(fill="x", padx=8, pady=(0, 8))
            self.csv_btn.config(state="normal")
        else:
            self.canvas.pack_forget()
            self.csv_btn.config(state="disabled")

    def set_alarm(self):
        if self.alarm_threshold is not None:      # désarmer
            self.alarm_threshold = None
            self.alarm_btn.config(text="🔔", fg="#ff8080")
            self.ol_lbl.config(text="")
            return
        unit = FUNCS[self.func][1] if self.func in FUNCS else "?"
        th = simpledialog.askfloat(
            "Alarme", f"Seuil d'alarme en {unit} (valeur absolue) :",
            parent=self, minvalue=0.0, initialvalue=1.0)
        if th is None:
            return
        self.alarm_threshold = th
        self.alarm_btn.config(text="🔔 ON", fg="#ffffff", bg="#881111")

    def save_csv(self):
        import os
        os.makedirs(LOGS_DIR, exist_ok=True)
        name = f"{LOGS_DIR}/dm3068_{datetime.datetime.now():%Y%m%d_%H%M%S}.csv"
        unit = FUNCS[self.func][1] if self.func in FUNCS else "?"
        with open(name, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["horodatage", "epoch_s", "fonction", f"valeur ({unit})"])
            for ts, v in self.series:
                stamp = datetime.datetime.fromtimestamp(ts).isoformat(timespec="milliseconds")
                w.writerow([stamp, f"{ts:.3f}", self.func or "?",
                            "OVERLOAD" if v is None else repr(v)])
        self.ol_lbl.config(text=f"📄 série écrite → {name}")
        self.after(3000, lambda: self.ol_lbl.config(text=""))

    def snapshot(self):
        import os
        os.makedirs(CAPTURES_DIR, exist_ok=True)
        x, y = self.winfo_rootx(), self.winfo_rooty()
        w, h = self.winfo_width(), self.winfo_height()
        img = ImageGrab.grab(bbox=(x, y, x + w, y + h))
        name = f"{CAPTURES_DIR}/snapshot_{datetime.datetime.now():%Y%m%d_%H%M%S}.png"
        img.save(name)
        self.ol_lbl.config(text=f"📷 capture → {name}")
        self.after(3000, lambda: self.ol_lbl.config(text=""))

    # ---------------- boucles UI ----------------

    def apply_func(self, code):
        """Met à jour le badge de fonction ; vide la série si CHANGEMENT réel."""
        changed = code != self.func
        self.func = code
        if code in FUNCS:
            self.func_lbl.config(text=f"{code}  ({FUNCS[code][2]})")
        else:
            self.func_lbl.config(text=str(code))
        if changed:
            self.series.clear()
            self.dirty = True

    def poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "status":
                    txt = msg[1]
                    self.stat_lbl.config(
                        text=txt,
                        fg="#33cc33" if "PERTE" not in txt else "#ff4040")
                elif kind == "func":
                    self.apply_func(msg[1])
                elif kind == "stopped":
                    self.ol_lbl.config(text="⏸ acquisition arrêtée", fg="#ffaa00")
                elif kind == "reading":
                    _, func, val, err, ts = msg
                    self.apply_func(func)
                    if err is not None:
                        self.val_lbl.config(text="!  ERR")
                        self.unit_lbl.config(text="")
                        self.ol_lbl.config(text=str(err), fg="#ff8080")
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
                    # alarme (hors surcharge)
                    if (self.alarm_threshold is not None and err is None
                            and val < OVERLOAD and abs(val) >= self.alarm_threshold):
                        self.ol_lbl.config(text=f"🔔 ALARME : seuil dépassé", fg="#ff4000")
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
        if not vals:
            self.stats_lbl.config(text=f"({hz})" if hz else "")
            return
        lo, hi = min(vals), max(vals)
        moy = sum(vals) / len(vals)
        lo_s = " ".join(fmt(lo, self.func)) if self.func in FUNCS else "?"
        moy_s = " ".join(fmt(moy, self.func)) if self.func in FUNCS else "?"
        hi_s = " ".join(fmt(hi, self.func)) if self.func in FUNCS else "?"
        self.stats_lbl.config(
            text=f"min {lo_s}   ·   moy {moy_s}   ·   max {hi_s}   ·   {hz}")

    def measured_hz(self):
        pts = [t for t, v in self.series if v is not None]
        if len(pts) >= 2:
            dt = pts[-1] - pts[0]
            if dt > 0:
                return f"{(len(pts) - 1) / dt:.1f} éch/s"
        return ""

    def draw(self):
        self.update_stats()
        c = self.canvas
        c.delete("all")
        w, h = int(c["width"]), int(c["height"])
        for frac in (0.25, 0.5, 0.75):                      # grille horizontale
            y = int(h * frac)
            c.create_line(0, y, w, y, fill="#222222")
        pts = list(self.series)
        vals = [v for _, v in pts if v is not None]
        hz = self.measured_hz()
        c.create_text(8, 8, anchor="nw", text=f"{self.func or '?'}   {hz}   [{len(pts)} éch]",
                      fill="#888888", font=("Consolas", 10))
        if len(vals) < 2:
            c.create_text(w // 2, h // 2, text="en attente de points…",
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

        # polyline en segments continus (les None = OVERLOAD coupent le trait)
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

        top_s = " ".join(fmt(hi, self.func)) if self.func in FUNCS else f"{hi:.3g}"
        bot_s = " ".join(fmt(lo, self.func)) if self.func in FUNCS else f"{lo:.3g}"
        c.create_text(w - 8, 8, anchor="ne", text=top_s,
                      fill="#666666", font=("Consolas", 10))
        c.create_text(w - 8, h - 8, anchor="se", text=bot_s,
                      fill="#666666", font=("Consolas", 10))


if __name__ == "__main__":
    Overlay().mainloop()
