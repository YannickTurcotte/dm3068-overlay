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
  - scrolling graph that fills the window (maximize it!), real time axis,
    ~50 000-point history with spike-preserving min/max decimation,
    auto-scaled, min/avg/max stats, measured rate
  - speed − / +: MAX → ÷4 → ÷10 → 1/5 s (polling cadence; the hardware
    ceiling is fixed per function: DCV ≈ 2.7 sps, CONT ≈ 28 sps, DIOD ≈ 25
    sps, ACV/ACI ≈ 1 sps, FREQ/PER ≈ 1 sps — this firmware rejects
    RATE/NPLC/ZERO/APER)
  - ▶/■ CSV live logging: rows written in real time to logs/*.csv,
    "● REC n" counter in the stats line while recording
  - 🔔 threshold alarm with beep, 📷 window snapshot (Pillow)
  - 📂 opens the app folder, ⚙ settings (Segoe MDL2 Assets icons on Windows)
  - 🌐 optional built-in web display (⚙ toggle or --web): transparent page
    for OBS Browser Sources (http://localhost:8123) and phones on the LAN
    (http://<pc-ip>:8123 — add ?bg=black for a dark background)
  - start with graph open: python dm3068_overlay.py --graph

Protocol: raw SCPI over TCP port 5555 (no dependencies required).
Close: window X or Escape.
"""
import base64
import csv
import datetime
import io
import json
import math
import os
import queue
import socket
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import wave as wavemod
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
HISTORY = 50000            # points kept for the graph (~5 h at DCV 2.7 sps,
                           # ~30 min at CONT 28 sps); drawing is decimated
CAPTURES_DIR = "captures"
LOGS_DIR = "logs"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "dm3068_overlay.json")
WEB_PORT = 8123               # built-in web display (OBS Browser Source)
GEAR_GLYPH = chr(0xE713)      # Segoe MDL2 Assets "Setting" (real gear)
FOLDER_GLYPH = chr(0xE8B7)    # Segoe MDL2 Assets "Folder"

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
        "web_on": "🌐 web display ON",
        "web_off": "🌐 web display OFF",
        "web_busy": "⚠ web port 8123 busy — display off",
        "unknown": "unknown func {f}",
        "sps": "sps", "pts": "pts",
        "avg": "avg",
        "clear": "Clear",
        "alarm_title": "Alarm",
        "alarm_prompt": "Alarm threshold in {unit} (absolute value):",
        "set_title": "Settings", "set_ip": "IP address:",
        "set_lang": "Language:", "set_touch": "Touch mode (big buttons):",
        "set_web": "Web display (OBS / browser):",
        "on": "ON", "off": "OFF", "ok": "OK", "cancel": "Cancel",
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
        "web_on": "🌐 web ACTIVÉ",
        "web_off": "🌐 affichage web DÉSACTIVÉ",
        "web_busy": "⚠ port web 8123 déjà occupé — affichage désactivé",
        "unknown": "fct inconnue {f}",
        "sps": "éch/s", "pts": "éch",
        "avg": "moy",
        "clear": "Vider",
        "alarm_title": "Alarme",
        "alarm_prompt": "Seuil d'alarme en {unit} (valeur absolue) :",
        "set_title": "Configuration", "set_ip": "Adresse IP :",
        "set_lang": "Langue :", "set_touch": "Mode tactile (gros boutons) :",
        "set_web": "Affichage web (OBS / navigateur) :",
        "on": "ACTIVÉ", "off": "ÉTEINT", "ok": "OK", "cancel": "Annuler",
        "bad_ip": "Adresse IP invalide.",
        "waiting": "en attente de points…",
    },
}
LANG_CHOICES = [("English", "en"), ("Français", "fr")]


def load_config():
    cfg = {"ip": DEFAULT_IP, "lang": "en", "touch": False, "web": False}
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update({k: v for k, v in json.load(f).items()
                        if k in ("ip", "lang", "touch", "web")})
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


def fmt_delta(sec):
    """'45s', '4m30s', '1h05m' — for the graph time axis."""
    sec = int(sec)
    if sec >= 3600:
        return f"{sec // 3600}h{sec % 3600 // 60:02d}m"
    if sec >= 60:
        return f"{sec // 60}m{sec % 60:02d}s"
    return f"{sec}s"


def _silent_wav_b64():
    """2 s of 8-bit 8 kHz mono WAV with a microscopic sine (inaudible but a
    *real* audio track — iOS only postpones screen lock while media with an
    audio track actually plays; a muted video alone is ignored)."""
    buf = io.BytesIO()
    w = wavemod.Wave_write(buf)
    w.setnchannels(1)
    w.setsampwidth(1)
    w.setframerate(8000)
    frames = bytearray()
    for i in range(16000):
        frames.append(128 + int(2 * math.sin(i * 0.15)))
    w.writeframes(bytes(frames))
    w.close()
    return base64.b64encode(buf.getvalue()).decode()


WAV_B64 = _silent_wav_b64()


class WebServer:
    """Tiny stdlib HTTP server exposing the live reading to OBS Browser
    Sources and phones:  GET /     -> HTML page (transparent background by
                                     default, ?bg=black for dark),
                         GET /api  -> JSON snapshot, polled by the page."""

    def __init__(self, app, port=WEB_PORT):
        self.app = app
        self.port = port
        self.httpd = None

    def start(self):
        try:
            self.httpd = _HTTPD(("0.0.0.0", self.port), _make_handler(self.app))
        except OSError:
            self.app.q.put((self.app.gen, "webfail"))
            return False
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return True

    def stop(self):
        if self.httpd is None:
            return
        httpd, self.httpd = self.httpd, None
        def close():
            httpd.shutdown()
            httpd.server_close()
        threading.Thread(target=close, daemon=True).start()


class _HTTPD(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                if self.path.startswith("/api"):
                    body = json.dumps(app.web_snapshot()).encode()
                    ctype = "application/json"
                else:
                    body = BUILD_HTML.replace("__WAVB64__", WAV_B64).encode()
                    ctype = "text/html; charset=utf-8"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args):
            pass
    return Handler


# Web page themes: "transparent" is the zero-config OBS default (hidden
# theme button so nothing shows in the stream); "dark" and "light" are the
# readable ones for phones. ?bg=black|light|transparent forces a theme and
# remembers it; the ◐ button cycles otherwise (localStorage per device).
BUILD_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  html,body{margin:0;height:100%;}
  body{display:flex;flex-direction:column;justify-content:center;align-items:center;
       font-family:Consolas,monospace;overflow:hidden;
       background:var(--bg);color:var(--digits);
       -webkit-user-select:none;user-select:none;}
  #f{color:var(--func);font-size:4vmin;font-weight:bold;}
  #row{display:flex;align-items:baseline;}
  #v{font-size:20vmin;font-weight:bold;line-height:1;}
  #u{font-size:8vmin;font-weight:bold;margin-left:1vmin;}
  #ol{color:var(--ol);font-size:7vmin;font-weight:bold;min-height:8vmin;}
  #s{color:var(--stats);font-size:3vmin;}
  #ws{color:var(--stats);font-size:2.6vmin;min-height:3.2vmin;margin-top:1vmin;}
  #ws.on{color:#7cfc00;}
  #ws.err{color:#ff8080;}
  body.theme-dark{--bg:#000;--digits:#ff1a1a;--func:#ffd400;
                  --ol:#00ffff;--stats:#9aa0a6;}
  body.theme-light{--bg:#fff;--digits:#d40000;--func:#b8860b;
                   --ol:#0088aa;--stats:#444;}
  body.theme-transparent{--bg:transparent;--digits:#ff1a1a;--func:#ffd400;
                         --ol:#00ffff;--stats:#c8ccd0;}
  body.theme-transparent #v,body.theme-transparent #u,body.theme-transparent #ol,
  body.theme-transparent #f,body.theme-transparent #s{
       text-shadow:0 0 4px rgba(0,0,0,.55);}
  #ui{position:fixed;top:2vmin;right:2vmin;display:flex;gap:1vmin;
      transition:opacity .3s;}
  #ui button{font-size:5vmin;line-height:1;border:none;border-radius:1.5vmin;
       padding:1vmin 2vmin;background:rgba(0,0,0,.2);color:#333;cursor:pointer;}
  body.theme-dark #ui button{background:rgba(128,128,128,.35);color:#fff;}
  body.theme-transparent #ui{opacity:0;pointer-events:none;}
  body.theme-transparent.btn-reveal #ui{opacity:.65;pointer-events:auto;}
</style></head><body class="theme-dark">
<div id="f">—</div>
<div id="row"><span id="v">- - - -</span><span id="u"></span></div>
<div id="ol"></div>
<div id="s"></div>
<div id="ws"></div>
<audio id="wav" loop src="data:audio/wav;base64,__WAVB64__"></audio>
<div id="ui">
<button id="wake" title="keep this screen awake">☀</button>
<button id="btn" title="background: dark / light / transparent">&#9680;</button>
</div>
<script>
const ORDER = ["dark", "light", "transparent"];
function apply(name){
  document.body.className = "theme-" + name;
  try{ localStorage.setItem("dm3068theme", name); }catch(e){}
}
(function(){
  const m = location.search.match(/[?&]bg=(black|light|transparent)/);
  let t = m ? {black:"dark", light:"light", transparent:"transparent"}[m[1]] : null;
  if(!t){ try{ t = localStorage.getItem("dm3068theme"); }catch(e){} }
  document.body.className = "theme-" + (t || "transparent");
  document.getElementById("btn").addEventListener("click", function(){
    const cur = document.body.className.replace("theme-","");
    apply(ORDER[(ORDER.indexOf(cur) + 1) % ORDER.length]);
  });
  // keep-awake: Wake Lock API when the context allows it (HTTPS), plus a
  // NoSleep-style 2px muted video fed by a canvas stream and a silent
  // WebAudio tone — playing media is what actually keeps an iPhone screen
  // lit over plain HTTP. Needs one tap (autoplay rules): the ☀ button.
  // Every outcome is reported in the #ws status line so failures are never
  // silent.
  let wakeLock = null, vid = null, audioCtx = null, awake = false;
  const wakeBtn = document.getElementById("wake");
  const ws = document.getElementById("ws");
  function wakeMsg(text, cls){
    ws.textContent = text;
    ws.className = cls || "";
  }
  function setAwake(on){
    awake = on;
    wakeBtn.textContent = on ? "\u2600 ON" : "\u2600";
  }
  async function toggleWake(){
    if(awake){
      try{ if(wakeLock){ wakeLock.release(); wakeLock = null; } }catch(e){}
      if(vid){ vid.pause(); }
      try{ document.getElementById("wav").pause(); }catch(e){}
      try{ if(audioCtx){ audioCtx.suspend(); } }catch(e){}
      setAwake(false);
      wakeMsg("screen awake — off");
      return;
    }
    const parts = [];
    try{
      if("wakeLock" in navigator){
        wakeLock = await navigator.wakeLock.request("screen");
        parts.push("wakeLock");
      }
    }catch(e){}
    try{
      if(!vid){
        const cv = document.createElement("canvas");
        cv.width = cv.height = 2;
        vid = document.createElement("video");
        vid.muted = true; vid.loop = true;
        vid.setAttribute("playsinline", "");
        vid.style.cssText = "position:fixed;left:0;bottom:0;width:2px;height:2px;opacity:.01;";
        document.body.appendChild(vid);
        vid.srcObject = cv.captureStream(10);
      }
      await vid.play();
      parts.push("video");
    }catch(e){
      parts.push("videoERR:" + (e && e.name ? e.name : "?"));
    }
    try{
      const w = document.getElementById("wav");   // real (silent) audio track
      w.volume = 0.02;
      await w.play();
      parts.push("wav");
    }catch(e){
      parts.push("wavERR:" + (e && e.name ? e.name : "?"));
    }
    try{
      if(!audioCtx){
        audioCtx = new (window.AudioContext || window.webkitAudioContext)();
        const o = audioCtx.createOscillator();
        const g = audioCtx.createGain();
        g.gain.value = 0.001;                      // inaudible, not "silence"
        o.connect(g); g.connect(audioCtx.destination);
        o.start();
      } else if(audioCtx.state === "suspended"){
        await audioCtx.resume();
      }
      parts.push("audio");
    }catch(e){
      parts.push("audioERR:" + (e && e.name ? e.name : "?"));
    }
    if(parts.length > 0 && parts.some(function(p){ return p.indexOf("ERR") === -1; })){
      setAwake(true);
      wakeMsg("\u2600 screen awake — ON (" + parts.join(", ") + ")", "on");
    } else {
      setAwake(false);
      wakeBtn.textContent = "\u2600 \u2717";
      wakeMsg("\u2600 not available on this browser: " + parts.join(", "), "err");
    }
  }
  wakeBtn.addEventListener("click", toggleWake);
  document.addEventListener("visibilitychange", async function(){
    if(document.visibilityState !== "visible" || !awake) return;
    if(vid){ try{ await vid.play(); }catch(e){} }
    try{ const w = document.getElementById("wav"); if(w){ await w.play(); } }
    catch(e){}
    try{ if(audioCtx && audioCtx.state === "suspended"){ await audioCtx.resume(); } }
    catch(e){}
    if("wakeLock" in navigator && !wakeLock){
      try{ wakeLock = await navigator.wakeLock.request("screen"); }catch(e){}
    }
  });
  // In the transparent (OBS) theme the buttons are hidden: tapping anywhere
  // reveals them for 5 s so a phone user can still switch themes. OBS never
  // taps, so the stream stays clean.
  document.body.addEventListener("click", function(ev){
    if(ev.target.closest && ev.target.closest("#ui")) return;
    if(document.body.className.indexOf("theme-transparent") === -1) return;
    document.body.classList.add("btn-reveal");
    clearTimeout(window.__btnTimer);
    window.__btnTimer = setTimeout(function(){
      document.body.classList.remove("btn-reveal");
    }, 5000);
  });
})();
async function tick(){
  try{
    const d = await (await fetch("/api")).json();
    document.getElementById("f").textContent = d.func;
    document.getElementById("v").textContent = d.value;
    document.getElementById("u").textContent = d.unit;
    document.getElementById("ol").textContent = d.overload ? "OVERLOAD" : "";
    document.getElementById("s").textContent = d.stats;
  }catch(e){}
}
setInterval(tick, 250); tick();
</script></body></html>
"""


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
        self.touch = bool(cfg.get("touch", False))
        self.want_web = bool(cfg.get("web", False)) or "--web" in sys.argv

        # snapshot shared with the web display
        self.cur_digits = "- - - -"
        self.cur_unit = ""
        self.cur_overload = False
        self.web_on = False
        self.webserver = None

        self.title(f"DM3068 — {self.ip}")
        self.configure(bg="black")
        self.attributes("-topmost", True)
        self.protocol("WM_DELETE_WINDOW", self._quit)
        self.bind("<Escape>", lambda e: self._quit())

        # Segoe MDL2 Assets ships with Windows 10/11: real gear & folder
        # glyphs (emoji ⚙ renders as a "flower" on some systems)
        try:
            self.icon_font = (("Segoe MDL2 Assets", 12)
                              if "Segoe MDL2 Assets" in tkfont.families()
                              else None)
        except tk.TclError:
            self.icon_font = None
        gear = GEAR_GLYPH if self.icon_font else "⚙"
        folder = FOLDER_GLYPH if self.icon_font else "📁"

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
        self.canvas.bind("<Configure>",
                         lambda e: setattr(self, "dirty", True))
        self.graph_on = False

        # widgets that grow in touch mode: (widget, font OFF, font ON, padx)
        self.touch_btns = []

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
            self.touch_btns.append((b, ("Consolas", 11, "bold"),
                                    ("Consolas", 13, "bold"), 2))

        # --- control buttons ---
        def ctl_btn(parent, text, cb, fg="#dddddd", width=3, font=None):
            return tk.Button(parent, text=text, width=width, bd=0,
                             font=font or ("Consolas", 11, "bold"),
                             fg=fg, bg="#1a1a1a", activebackground="#333333",
                             activeforeground="#ffffff", command=cb)

        ctl = tk.Frame(self, bg="black")
        ctl.pack(fill="x", padx=8, pady=(6, 8))

        def reg(w, f_off, f_on, padx=(2, 2)):
            self.touch_btns.append((w, f_off, f_on, padx))

        self.speed_lbl = tk.Label(ctl, text="⏱ MAX", font=("Consolas", 11, "bold"),
                                  fg="#66d9ef", bg="black", width=9, anchor="w")
        self.speed_lbl.pack(side="left", padx=(2, 4))
        reg(self.speed_lbl, ("Consolas", 11, "bold"), ("Consolas", 13, "bold"), (2, 4))

        b = ctl_btn(ctl, "−", lambda: self.bump_speed(-1), fg="#66d9ef")
        b.pack(side="left", padx=2)
        reg(b, ("Consolas", 11, "bold"), ("Consolas", 13, "bold"))
        b = ctl_btn(ctl, "+", lambda: self.bump_speed(+1), fg="#66d9ef")
        b.pack(side="left", padx=2)
        reg(b, ("Consolas", 11, "bold"), ("Consolas", 13, "bold"))

        self.run_btn = ctl_btn(ctl, "⏸", self.toggle_run, fg="#ffaa00", width=4)
        self.run_btn.pack(side="left", padx=(8, 2))
        reg(self.run_btn, ("Consolas", 11, "bold"), ("Consolas", 13, "bold"), (8, 2))
        self.clear_btn = ctl_btn(ctl, self.T["clear"], self.clear_series,
                                 fg="#dddddd", width=7)
        self.clear_btn.pack(side="left", padx=2)
        reg(self.clear_btn, ("Consolas", 11, "bold"), ("Consolas", 13, "bold"))
        self.graph_btn = ctl_btn(ctl, "📈", self.toggle_graph, fg="#a6e22e", width=4)
        self.graph_btn.pack(side="left", padx=2)
        reg(self.graph_btn, ("Consolas", 11, "bold"), ("Consolas", 13, "bold"))

        self.csv_btn = ctl_btn(ctl, "▶ CSV", self.toggle_log, fg="#a6e22e", width=8)
        self.csv_btn.pack(side="left", padx=2)
        reg(self.csv_btn, ("Consolas", 11, "bold"), ("Consolas", 13, "bold"))

        self.alarm_btn = ctl_btn(ctl, "🔔", self.set_alarm, fg="#ff8080", width=4)
        self.alarm_btn.pack(side="left", padx=2)
        reg(self.alarm_btn, ("Consolas", 11, "bold"), ("Consolas", 13, "bold"))

        icon_off = self.icon_font or ("Consolas", 11, "bold")
        icon_on = ("Segoe MDL2 Assets", 15) if self.icon_font else \
            ("Consolas", 13, "bold")
        if ImageGrab is not None:
            b = ctl_btn(ctl, "📷", self.snapshot, fg="#aaaaaa", width=4)
            b.pack(side="left", padx=2)
            reg(b, icon_off, icon_on)
        b = ctl_btn(ctl, folder, self.open_folder, fg="#e8c86e", width=4,
                    font=self.icon_font)
        b.pack(side="left", padx=2)
        reg(b, icon_off, icon_on)
        b = ctl_btn(ctl, gear, self.open_settings, fg="#bbbbbb", width=4,
                    font=self.icon_font)
        b.pack(side="left", padx=2)
        reg(b, icon_off, icon_on)

        self.q = queue.Queue()
        self.meter = Meter(self.q, self.ip, self.gen)
        self.meter.start()
        self.after(70, self.poll)
        self.after(140, self.redraw)
        self.apply_touch(self.touch)
        if self.want_web:
            self._web_start()

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

        tk.Label(dlg, text=self.T["set_touch"], font=("Consolas", 11, "bold"),
                 fg="#dddddd", bg="black").grid(row=4, column=0, sticky="w",
                                                padx=10, pady=(4, 4))
        touch_chosen = {"on": self.touch}

        def ttoggle():
            touch_chosen["on"] = not touch_chosen["on"]
            tbtn.config(bg="#1f7a1f" if touch_chosen["on"] else "#1a1a1a",
                        text=self.T["on"] if touch_chosen["on"] else self.T["off"])

        tbtn = tk.Button(dlg, text=self.T["on"] if self.touch else self.T["off"],
                         width=8, bd=0, font=("Consolas", 11, "bold"),
                         fg="#ffffff", bg="#1f7a1f" if self.touch else "#1a1a1a",
                         activebackground="#333333", activeforeground="#ffffff",
                         command=ttoggle)
        tbtn.grid(row=5, column=0, sticky="w", padx=10, pady=(0, 12))

        tk.Label(dlg, text=self.T["set_web"], font=("Consolas", 11, "bold"),
                 fg="#dddddd", bg="black").grid(row=6, column=0, sticky="w",
                                                padx=10, pady=(4, 4))
        web_chosen = {"on": self.web_on}

        def wtoggle():
            web_chosen["on"] = not web_chosen["on"]
            wbtn.config(bg="#1f7a1f" if web_chosen["on"] else "#1a1a1a",
                        text=self.T["on"] if web_chosen["on"] else self.T["off"])

        wbtn = tk.Button(dlg, text=self.T["on"] if self.web_on else self.T["off"],
                         width=8, bd=0, font=("Consolas", 11, "bold"),
                         fg="#ffffff", bg="#1f7a1f" if self.web_on else "#1a1a1a",
                         activebackground="#333333", activeforeground="#ffffff",
                         command=wtoggle)
        wbtn.grid(row=7, column=0, sticky="w", padx=10, pady=(0, 12))

        def on_ok():
            ip = ip_var.get().strip()
            try:
                socket.inet_aton(ip)
            except OSError:
                messagebox.showwarning(self.T["set_title"], self.T["bad_ip"],
                                       parent=dlg)
                return
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump({"ip": ip, "lang": chosen["lang"],
                           "touch": touch_chosen["on"],
                           "web": web_chosen["on"]}, f)
            dlg.destroy()
            self.apply_settings(ip, chosen["lang"], touch_chosen["on"],
                                web_chosen["on"])

        row = tk.Frame(dlg, bg="black")
        row.grid(row=8, column=0, sticky="e", padx=10, pady=(0, 12))
        tk.Button(row, text=self.T["cancel"], width=8, bd=0,
                  font=("Consolas", 11, "bold"), fg="#dddddd", bg="#1a1a1a",
                  activebackground="#333333",
                  command=dlg.destroy).pack(side="left", padx=4)
        tk.Button(row, text=self.T["ok"], width=8, bd=0,
                  font=("Consolas", 11, "bold"), fg="#a6e22e", bg="#1a1a1a",
                  activebackground="#333333", command=on_ok).pack(side="left")

    def apply_settings(self, ip, lang, touch, web):
        lang_changed = lang != self.lang
        ip_changed = ip != self.ip
        touch_changed = bool(touch) != self.touch
        web_changed = bool(web) != self.web_on

        if lang_changed:
            self.lang = lang
            self.T = T[lang]
            self.clear_btn.config(text=self.T["clear"])
            self.apply_func(self.func)           # refresh the badge wording

        if touch_changed:
            self.apply_touch(touch)

        if web_changed:
            if web:
                self._web_start()
            else:
                self._web_stop()

        if ip_changed:
            self.ip = ip
            self.gen += 1
            self._log_stop()                     # never mix meters in one file
            self.meter.shutdown()                # kill the old thread promptly
            self.series.clear()
            self.dirty = True
            self.func = None
            self.func_lbl.config(text="—")
            self.title(f"DM3068 — {self.ip}" +
                       (f" · 🌐 :{WEB_PORT}" if self.web_on else ""))
            self.stat_lbl.config(text=self.T["connecting"], fg="#888888")
            self.ol_lbl.config(text="")
            self.meter = Meter(self.q, self.ip, self.gen)
            self.meter.start()

    # ---------------- interactions ----------------

    def switch_to(self, code):
        self.meter.desired_func = code

    def apply_touch(self, on):
        """Big-button mode: taller targets (≈44 px) + larger fonts."""
        self.touch = bool(on)
        for w, f_off, f_on, padx in self.touch_btns:
            w.config(font=f_on if self.touch else f_off)
            w.pack_forget()
            w.pack(side="left", padx=padx, ipady=10 if self.touch else 0)

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
            self.canvas.pack(fill="both", expand=True, padx=8, pady=(0, 8))
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

    def open_folder(self):
        try:
            os.startfile(BASE_DIR)               # Windows
        except AttributeError:
            import subprocess
            subprocess.Popen(["xdg-open", BASE_DIR])

    # ---------------- web display ----------------

    def web_snapshot(self):
        if self.func in FUNCS:
            fname = f"{self.func} — {self.T['funcs'][self.func]}"
        else:
            fname = str(self.func) if self.func else "—"
        return {
            "func": fname,
            "value": self.cur_digits,
            "unit": self.cur_unit,
            "overload": self.cur_overload,
            "stats": self.stats_text(),
        }

    def _web_start(self):
        if self.webserver is None:
            self.webserver = WebServer(self)
            if not self.webserver.start():
                self.webserver = None
                return                   # "webfail" arrives via the queue
        self.web_on = True
        self._refresh_title()
        self.ol_lbl.config(text=self.T["web_on"], fg="#a6e22e")
        self.after(6000, lambda: self.ol_lbl.config(text=""))

    def _web_stop(self):
        if self.webserver is not None:
            self.webserver.stop()
            self.webserver = None
        self.web_on = False
        self._refresh_title()
        self.ol_lbl.config(text=self.T["web_off"], fg="#9aa0a6")
        self.after(4000, lambda: self.ol_lbl.config(text=""))

    def _refresh_title(self):
        t = f"DM3068 — {self.ip}"
        if self.web_on:
            t += f" · 🌐 :{WEB_PORT}"
        self.title(t)

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
                elif kind == "webfail":
                    self.webserver = None
                    self.web_on = False
                    self._refresh_title()
                    self.ol_lbl.config(text=self.T["web_busy"], fg="#ff8080")
                elif kind == "reading":
                    _, _, func, val, err, ts = msg
                    self.apply_func(func)
                    if err is not None:
                        self.cur_digits, self.cur_unit = "!  ERR", ""
                        self.cur_overload = False
                        self.val_lbl.config(text="!  ERR")
                        self.unit_lbl.config(text="")
                        self.ol_lbl.config(
                            text=self.T["unknown"].format(f=func)
                            if err == "unknown" else str(err), fg="#ff8080")
                    elif val >= OVERLOAD:
                        self.cur_digits, self.cur_unit = "- - - -", ""
                        self.cur_overload = True
                        self.val_lbl.config(text="- - - -")
                        self.unit_lbl.config(text="")
                        self.ol_lbl.config(text="OVERLOAD", fg="#00ffff")
                        self.series.append((ts, None))
                        self.dirty = True
                    else:
                        digits, unit = fmt(val, func)
                        self.cur_digits, self.cur_unit = digits, unit
                        self.cur_overload = False
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

    def stats_text(self):
        vals = [v for _, v in self.series if v is not None]
        hz = self.measured_hz()
        rec = f"   ·   ● REC {self.log_count}" if self.log_f is not None else ""
        if not vals:
            return (f"({hz})" if hz else "") + rec
        lo, hi = min(vals), max(vals)
        avg = sum(vals) / len(vals)
        f3 = (lambda v: " ".join(fmt(v, self.func))
              if self.func in FUNCS else "?")
        return (f"min {f3(lo)}   ·   {self.T['avg']} {f3(avg)}   ·   "
                f"max {f3(hi)}   ·   {hz}{rec}")

    def update_stats(self):
        self.stats_lbl.config(text=self.stats_text())

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
        w = c.winfo_width()
        h = c.winfo_height()
        if w < 60 or h < 60:
            return
        for frac in (0.25, 0.5, 0.75):                      # horizontal grid
            y = int(18 + frac * (h - 34))
            c.create_line(0, y, w, y, fill="#222222")
        pts = list(self.series)
        finite = sum(1 for _, v in pts if v is not None)
        hz = self.measured_hz()
        span = (pts[-1][0] - pts[0][0]) if len(pts) >= 2 else 0.0
        span_txt = f" · {fmt_delta(span)}" if span > 0 else ""
        c.create_text(8, 8, anchor="nw",
                      text=f"{self.func or '?'}   {hz}   [{finite} {self.T['pts']}"
                           f"{span_txt}]",
                      fill="#888888", font=("Consolas", 10))
        if finite < 2:
            c.create_text(w // 2, h // 2, text=self.T["waiting"],
                          fill="#555555", font=("Consolas", 12))
            return
        if span <= 0:
            span = 1e-9
        t0, t_end = pts[0][0], pts[-1][0]

        # min/max buckets per pixel column: hours of data still show a 1-sample
        # glitch, and OVERLOAD rows break the trace
        buckets = {}
        lo = hi = None
        for t, v in pts:
            x = int((t - t0) / span * (w - 1))
            b = buckets.setdefault(x, [None, None, False])
            if v is None:
                b[2] = True
                continue
            if lo is None:
                lo = hi = v
            else:
                if v < lo:
                    lo = v
                if v > hi:
                    hi = v
            b[0] = v if b[0] is None else min(b[0], v)
            b[1] = v if b[1] is None else max(b[1], v)
        if lo is None:
            return
        if hi - lo < 1e-30:
            lo, hi = lo - 1, hi + 1
        pad = (hi - lo) * 0.1
        lo, hi = lo - pad, hi + pad

        def yv(v):
            return h - 14 - (v - lo) / (hi - lo) * (h - 34)

        f3 = (lambda v: " ".join(fmt(v, self.func))
              if self.func in FUNCS else f"{v:.3g}")

        # vertical time gridlines + relative labels ("-30s", "-1m00s", ...)
        step = None
        for s in (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600):
            if span / s <= 8:
                step = s
                break
        if step:
            tt = math.ceil(t0 / step) * step
            while tt <= t_end:
                x = (tt - t0) / span * (w - 1)
                c.create_line(x, 18, x, h - 14, fill="#1a1a1a")
                if x < w - 120:                  # keep the corner V label clear
                    c.create_text(x + 4, h - 4, anchor="sw", fill="#555555",
                                  font=("Consolas", 9),
                                  text="-" + fmt_delta(t_end - tt))
                tt += step

        # trace (continuous segments only)
        seg = []
        for x in sorted(buckets):
            bmin, bmax, has_ol = buckets[x]
            if has_ol or bmin is None:
                if len(seg) >= 2:
                    c.create_line(seg, fill="#ff1a1a", width=1)
                seg = []
                continue
            seg.extend((x, yv(bmin), x, yv(bmax)))
        if len(seg) >= 2:
            c.create_line(seg, fill="#ff1a1a", width=1)

        # voltage labels on the horizontal gridlines + extremes
        for frac in (0.25, 0.5, 0.75):
            y = int(18 + frac * (h - 34))
            gv = hi - frac * (hi - lo)
            c.create_text(w - 6, y - 2, anchor="ne", text=f3(gv),
                          fill="#666666", font=("Consolas", 9))
        c.create_text(w - 8, 8, anchor="ne", text=f3(hi),
                      fill="#666666", font=("Consolas", 10))
        c.create_text(w - 8, h - 8, anchor="se", text=f3(lo),
                      fill="#666666", font=("Consolas", 10))


if __name__ == "__main__":
    Overlay().mainloop()
