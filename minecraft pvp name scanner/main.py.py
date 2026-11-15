#!/usr/bin/env python3
# Minecraft PvP Name Scanner
# Python 3.10+ | Standard library only (no pip installs required)
#
# IMPORTANT:
# Mojang's public profile lookup can tell whether a name currently resolves to
# a profile. A 204 response means "no profile found", NOT a guarantee that the
# name is claimable. Name restrictions, cooldowns, reservations, or other
# Minecraft services rules may still prevent claiming it.
#
# The Discord webhook is entered at runtime and is never saved to disk.

import json
import queue
import random
import re
import sqlite3
import string
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk


APP_TITLE = "PvP Name Scanner"
DB_PATH = Path(__file__).with_name("pvp_name_scanner_history.sqlite3")
API_URL = "https://api.mojang.com/users/profiles/minecraft/{}"
USER_AGENT = "PvPNameScanner/1.0 (personal-use)"
MAX_NAME_LENGTH = 16
MIN_NAME_LENGTH = 3

# A small, curated vocabulary creates readable combinations rather than
# completely random character strings.
FOUR_LETTER_NAMES = [
    "void", "vexx", "zyro", "kryp", "ryze", "nexx", "vayn", "zenn",
    "aero", "onyx", "rift", "grim", "flux", "clut", "flic", "tapy",
    "peak", "snipe", "rune", "vexy", "blit", "drip", "glow", "haze",
    "faze", "cold", "dark", "rage", "swep", "wkey", "cryo", "fury",
]
PVP_ROOTS = [
    "clutch", "strafe", "combo", "sweat", "sweaty", "aim", "aimbot",
    "block", "hit", "reach", "click", "cps", "duel", "pot", "pearl",
    "crits", "tap", "wtap", "stun", "combo", "flick", "track", "burst",
    "ace", "snip", "peak", "drip", "vex", "void", "ryze", "kryp",
]
ROOTS = [
    "void", "vex", "nova", "echo", "frost", "venom", "shade", "ghost",
    "drift", "blaze", "storm", "night", "dusk", "raven", "wraith", "flux",
    "onyx", "cryo", "slate", "zero", "swift", "clutch", "flick", "trace",
    "pixel", "aero", "orbit", "lunar", "solar", "reign", "prime", "zen",
    "grim", "hollow", "silent", "rapid", "toxic", "savage", "myth", "rift",
    "viper", "ace", "rune", "kairo", "zyro", "nex", "ryze", "kryp",
    "blitz", "tempo", "glide", "strafe", "combo", "tap", "snipe", "peak",
    "chill", "cloud", "ember", "flare", "fable", "faint", "fury", "frosty",
    "myst", "vanta", "aether", "cipher", "monk", "rival", "crisp", "clean",
] + PVP_ROOTS
PREFIXES = [
    "x", "i", "v", "q", "z", "its", "im", "not", "real", "just",
    "hey", "og", "the", "yo", "my",
]
SUFFIXES = [
    "y", "z", "q", "xo", "ify", "ifyy", "qt", "v", "i", "ii", "on",
    "er", "ed", "fn", "gg", "fps", "lol",
]


def valid_name(name: str) -> bool:
    return (
        MIN_NAME_LENGTH <= len(name) <= MAX_NAME_LENGTH
        and re.fullmatch(r"[A-Za-z0-9_]+", name) is not None
    )


def generate_names(amount: int, excluded: set[str], stop_event: threading.Event,
                   min_len: int = 4, max_len: int = 5):
    """Generate unique Minecraft-compatible names in the selected length range."""
    roots = list(dict.fromkeys(
        [w.lower() for w in FOUR_LETTER_NAMES + PVP_ROOTS + ROOTS
         if min_len <= len(w) <= max_len and w.isalpha()]
    ))
    alphabet = string.ascii_lowercase
    found = set()
    # First use curated names, then generate additional short pronounceable
    # combinations. The 4–5 letter search space is large enough for 50k uniques.
    random.shuffle(roots)
    for name in roots:
        if stop_event.is_set() or len(found) >= amount:
            return
        if name not in excluded and name not in found:
            found.add(name)
            yield name

    syllables = ["ka", "zy", "ry", "vx", "neo", "kry", "shi", "vex",
                 "no", "ra", "xi", "clu", "dra", "zen", "tox", "aki",
                 "tri", "exo", "qua", "oni", "rei", "lyn", "iko", "uru"]
    attempts = 0
    max_attempts = amount * 80 + 10000
    while len(found) < amount and not stop_event.is_set() and attempts < max_attempts:
        attempts += 1
        length = random.randint(min_len, max_len)
        if random.random() < 0.72:
            name = (random.choice(syllables) + random.choice(syllables)).lower()
            name = name[:length]
        else:
            name = "".join(random.choice(alphabet) for _ in range(length))
        if min_len <= len(name) <= max_len and name not in excluded and name not in found:
            found.add(name)
            yield name


class History:
    def __init__(self, path: Path):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        with self.lock:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS checked_names (
                    name TEXT PRIMARY KEY COLLATE NOCASE,
                    result TEXT NOT NULL,
                    checked_at TEXT NOT NULL
                )
            """)
            self.conn.commit()

    def known_names(self) -> set[str]:
        with self.lock:
            rows = self.conn.execute("SELECT name FROM checked_names").fetchall()
        return {r[0].lower() for r in rows}

    def add(self, name: str, result: str):
        with self.lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO checked_names(name, result, checked_at) VALUES (?, ?, ?)",
                (name.lower(), result, datetime.now().isoformat(timespec="seconds")),
            )
            self.conn.commit()

    def close(self):
        with self.lock:
            self.conn.close()


class MojangChecker:
    def __init__(self, stop_event: threading.Event, log_callback):
        self.stop_event = stop_event
        self.log = log_callback
        self.pause_lock = threading.Lock()
        self.pause_until = 0.0

    def _wait_for_global_pause(self):
        while not self.stop_event.is_set():
            with self.pause_lock:
                remaining = self.pause_until - time.monotonic()
            if remaining <= 0:
                return
            self.stop_event.wait(min(remaining, 0.5))

    def _extend_pause(self, seconds: float):
        with self.pause_lock:
            self.pause_until = max(self.pause_until, time.monotonic() + seconds)

    @staticmethod
    def _retry_after(headers, fallback: float) -> float:
        value = headers.get("Retry-After")
        try:
            return max(1.0, min(float(value), 300.0)) if value else fallback
        except (TypeError, ValueError):
            return fallback

    def check(self, name: str):
        """Return 'taken', 'not_found', or 'error'. Errors are never free."""
        backoff = 2.0
        for attempt in range(6):
            if self.stop_event.is_set():
                return "stopped"
            self._wait_for_global_pause()
            if self.stop_event.is_set():
                return "stopped"

            req = urllib.request.Request(
                API_URL.format(name),
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                method="GET",
            )
            try:
                with urllib.request.urlopen(req, timeout=12) as response:
                    status = response.status
                    if status == 200:
                        return "taken"
                    if status == 204:
                        return "not_found"
                    # A successful response with an unexpected status is not
                    # interpreted as availability.
                    return "error"

            except urllib.error.HTTPError as exc:
                if exc.code == 204:
                    return "not_found"
                if exc.code == 404:
                    # Endpoint behavior can vary; do not assume a 404 is free.
                    return "error"
                if exc.code == 429:
                    wait = self._retry_after(exc.headers, backoff)
                    self._extend_pause(wait)
                    self.log(f"Mojang-Limit (429): warte {wait:.0f} Sekunden …")
                elif 500 <= exc.code <= 599:
                    self.log(f"Mojang-Serverfehler ({exc.code}), neuer Versuch …")
                else:
                    self.log(f"API-Fehler {exc.code} bei {name}; Name wird nicht als frei gewertet.")
                    return "error"

            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                self.log(f"Verbindungsfehler bei {name}: {exc}; neuer Versuch …")

            if attempt < 5:
                self.stop_event.wait(backoff)
                backoff = min(backoff * 2, 30.0)

        return "error"


class DiscordWebhook:
    def __init__(self, url: str, log_callback):
        self.url = url.strip()
        self.log = log_callback
        self.lock = threading.Lock()
        self.pending = []

    def add(self, name: str):
        with self.lock:
            self.pending.append(name)
            if len(self.pending) >= 5:
                self._send_batch_locked()

    def flush(self):
        with self.lock:
            if self.pending:
                self._send_batch_locked()

    def _send_batch_locked(self):
        if not self.url:
            self.pending.clear()
            return

        batch = self.pending[:]
        self.pending.clear()
        content = "**Minecraft Name Finder – no profile found by API:**\n" + "\n".join(f"`{n}`" for n in batch)
        payload = json.dumps({"content": content}).encode("utf-8")

        for attempt in range(5):
            req = urllib.request.Request(
                self.url,
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": USER_AGENT,
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=15) as response:
                    if 200 <= response.status < 300:
                        self.log(f"Discord: {len(batch)} Namen gesendet.")
                        return
                    self.log(f"Discord-Webhook: unerwartete Antwort {response.status}.")
                    break
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    try:
                        wait = min(float(exc.headers.get("Retry-After", "2")), 60)
                    except ValueError:
                        wait = 2
                    self.log(f"Discord-Rate-Limit: warte {wait:.0f} Sekunden …")
                    time.sleep(max(1, wait))
                    continue
                if exc.code in (401, 403, 404):
                    self.log(
                        f"Discord-Webhook-Fehler {exc.code}: "
                        "Webhook ungültig, gelöscht oder ohne Berechtigung."
                    )
                else:
                    self.log(f"Discord-Webhook-HTTP-Fehler {exc.code}.")
                break
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                self.log(f"Discord-Verbindungsfehler: {exc}")
                time.sleep(2 ** attempt)
        else:
            self.log("Discord: Senden nach mehreren Versuchen fehlgeschlagen.")

        # Do not silently lose the names: restore the failed batch for a later
        # flush attempt during shutdown.
        self.pending = batch + self.pending


class ScannerApp:
    BG = "#080a0d"
    SIDEBAR = "#151d30"
    PANEL = "#111318"
    PANEL2 = "#191b23"
    BORDER = "#252632"
    TEXT = "#f1f0f7"
    MUTED = "#9293a3"
    GREEN = "#9a55ff"
    CYAN = "#54b7ff"
    RED = "#ff6685"

    def __init__(self, root):
        self.root = root
        self.root.title("Minecraft Name Finder")
        self.root.geometry("1180x760")
        self.root.minsize(980, 660)
        self.root.configure(bg=self.BG)

        self.history = History(DB_PATH)
        self.stop_event = threading.Event()
        self.ui_queue = queue.Queue()
        self.scan_thread = None
        self.results = []
        self.checked = 0
        self.errors = 0
        self.total = 0
        self.webhook = None
        self.running = False
        self.known_count = 0
        self.audio_var = tk.BooleanVar(value=False)
        self.min_len_var = tk.StringVar(value="4")
        self.max_len_var = tk.StringVar(value="5")
        self.page = "Dashboard"

        self._build()
        self.root.after(100, self._process_queue)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _label(self, parent, text, size=10, color=None, bold=False, bg=None):
        return tk.Label(
            parent, text=text, bg=bg or self.PANEL, fg=color or self.TEXT,
            font=("Segoe UI", size, "bold" if bold else "normal"),
            anchor="w"
        )

    def _button(self, parent, text, command, primary=False, width=None):
        return tk.Button(
            parent, text=text, command=command,
            bg=self.GREEN if primary else self.PANEL2,
            fg="#ffffff" if primary else self.TEXT,
            activebackground="#00c98d" if primary else "#202a34",
            activeforeground="#ffffff" if primary else self.TEXT,
            relief="flat", bd=0, padx=14, pady=8, cursor="hand2",
            font=("Segoe UI", 9, "bold"), width=width
        )

    def _panel(self, parent, padx=14, pady=12):
        return tk.Frame(parent, bg=self.PANEL, highlightthickness=1,
                        highlightbackground=self.BORDER, padx=padx, pady=pady)

    def _build(self):
        # Sidebar, inspired by the supplied Discord scanner reference.
        sidebar = tk.Frame(self.root, bg=self.SIDEBAR, width=178)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        brand = tk.Frame(sidebar, bg=self.SIDEBAR)
        brand.pack(fill="x", padx=14, pady=(18, 30))
        self._label(brand, "●  MINECRAFT", 12, "#ffffff", True, self.SIDEBAR).pack(anchor="w")
        self._label(brand, "NAME FINDER", 7, self.GREEN, True, self.SIDEBAR).pack(anchor="w", padx=(22, 0))
        self._label(sidebar, "NAVIGATION", 8, "#73758a", True, self.SIDEBAR).pack(anchor="w", padx=15, pady=(0, 8))
        self._nav_button(sidebar, "Scanner", "▦")
        self._nav_button(sidebar, "Favorites", "☆")
        self._label(sidebar, "MOJANG API", 8, "#73758a", True, self.SIDEBAR).pack(anchor="w", padx=15, pady=(22, 8))
        self._label(sidebar, "●  LOCAL HISTORY\n   Duplicate protection", 8, "#a6a8b8", False, self.SIDEBAR).pack(anchor="w", padx=14)
        self._label(sidebar, "PYTHON NAME FINDER", 8, "#a6a8b8", True, self.SIDEBAR).pack(side="bottom", anchor="w", padx=14, pady=16)

        main = tk.Frame(self.root, bg=self.BG)
        main.pack(side="left", fill="both", expand=True)
        topbar = tk.Frame(main, bg="#171f31", height=46)
        topbar.pack(fill="x")
        topbar.pack_propagate(False)
        self._label(topbar, "Minecraft Name Finder", 10, "#d8d8e3", True, "#171f31").pack(side="left", padx=18, pady=12)
        self._label(topbar, "Mojang API  •  Ready", 8, "#b8a1ff", True, "#171f31").pack(side="right", padx=18)

        content = tk.Frame(main, bg=self.BG)
        content.pack(fill="both", expand=True, padx=20, pady=17)
        heading = tk.Frame(content, bg=self.BG)
        heading.pack(fill="x", pady=(0, 14))
        self._label(heading, "Username Scanner", 22, self.TEXT, True, self.BG).pack(side="left")
        self._label(heading, "Find Minecraft names and check profile status.", 9, self.MUTED, False, self.BG).pack(side="left", padx=13, pady=(9, 0))

        controls = self._panel(content, 13, 11)
        controls.pack(fill="x", pady=(0, 13))
        self._label(controls, "NAME LENGTH", 8, self.MUTED, True).grid(row=0, column=0, sticky="w", padx=(0, 6))
        self._label(controls, "FROM", 8, self.MUTED, True).grid(row=0, column=1, sticky="w", padx=5)
        self._label(controls, "TO", 8, self.MUTED, True).grid(row=0, column=2, sticky="w", padx=5)
        self._label(controls, "NAMES PER SCAN", 8, self.MUTED, True).grid(row=0, column=3, sticky="w", padx=5)
        self._label(controls, "WORKERS", 8, self.MUTED, True).grid(row=0, column=4, sticky="w", padx=5)
        self._label(controls, "DISCORD WEBHOOK (OPTIONAL)", 8, self.MUTED, True).grid(row=0, column=5, sticky="w", padx=5)

        lengths = [str(n) for n in range(4, 17)]
        self.min_combo = ttk.Combobox(controls, textvariable=self.min_len_var, values=lengths, width=4, state="readonly")
        self.min_combo.grid(row=1, column=1, sticky="ew", padx=5, pady=(5, 0), ipady=4)
        self.max_combo = ttk.Combobox(controls, textvariable=self.max_len_var, values=lengths, width=4, state="readonly")
        self.max_combo.grid(row=1, column=2, sticky="ew", padx=5, pady=(5, 0), ipady=4)
        self._label(controls, "Characters", 8, self.MUTED, False).grid(row=1, column=0, sticky="w", pady=(5, 0))
        self.amount_var = tk.StringVar(value="10000")
        amount = tk.Entry(controls, textvariable=self.amount_var, bg="#090a10", fg=self.TEXT, insertbackground=self.TEXT, relief="flat", width=9)
        amount.grid(row=1, column=3, sticky="ew", padx=5, pady=(5, 0), ipady=7)
        self.workers_var = tk.StringVar(value="4")
        workers = tk.Entry(controls, textvariable=self.workers_var, bg="#090a10", fg=self.TEXT, insertbackground=self.TEXT, relief="flat", width=5)
        workers.grid(row=1, column=4, sticky="ew", padx=5, pady=(5, 0), ipady=7)
        self.webhook_entry = tk.Entry(controls, bg="#090a10", fg=self.TEXT, insertbackground=self.TEXT, relief="flat", show="•")
        self.webhook_entry.grid(row=1, column=5, sticky="ew", padx=5, pady=(5, 0), ipady=7)
        controls.grid_columnconfigure(5, weight=1)
        self.start_btn = self._button(controls, "▶  START SCAN", self.start_scan, True)
        self.start_btn.grid(row=1, column=6, padx=(10, 4), pady=(5, 0))
        self.stop_btn = self._button(controls, "■  STOP", self.stop_scan)
        self.stop_btn.grid(row=1, column=7, padx=(4, 0), pady=(5, 0))
        self.stop_btn.configure(state="disabled")

        work = tk.Frame(content, bg=self.BG)
        work.pack(fill="both", expand=True)
        center = self._panel(work, 14, 12)
        center.pack(side="left", fill="both", expand=True, padx=(0, 12))
        head = tk.Frame(center, bg=self.PANEL)
        head.pack(fill="x", pady=(0, 10))
        self._label(head, "◉  Live Scan Results", 11, self.TEXT, True).pack(side="left")
        self._button(head, "Clear", self.clear_results).pack(side="right")
        self.result_box = tk.Listbox(center, bg="#0a0b10", fg="#c7a7ff", selectbackground="#51308b", selectforeground="#ffffff", relief="flat", bd=0, font=("Consolas", 11), activestyle="none")
        self.result_box.pack(fill="both", expand=True)
        foot = tk.Frame(center, bg=self.PANEL)
        foot.pack(fill="x", pady=(10, 0))
        self.progress_label = self._label(foot, "Scanner is idle. Choose a name length and start.", 8, self.MUTED, False, self.PANEL)
        self.progress_label.pack(anchor="w")
        self.progress = ttk.Progressbar(foot, maximum=100, mode="determinate")
        self.progress.pack(fill="x", pady=(7, 8))
        self._button(foot, "Copy results", self.copy_results).pack(side="left")
        self._button(foot, "Save .txt / .csv", self.save_results).pack(side="left", padx=7)

        right = self._panel(work, 11, 12)
        right.pack(side="right", fill="y")
        right.configure(width=245)
        right.pack_propagate(False)
        log_head = tk.Frame(right, bg=self.PANEL)
        log_head.pack(fill="x")
        self._label(log_head, "◉  Live Log", 10, self.TEXT, True).pack(side="left")
        self.log_count = self._label(log_head, "0 events", 7, self.MUTED, False)
        self.log_count.pack(side="right")
        tk.Frame(right, bg=self.BORDER, height=1).pack(fill="x", pady=9)
        self.log_box = tk.Text(right, height=20, bg="#0a0b10", fg="#b9b6c8", relief="flat", font=("Consolas", 8), state="disabled", wrap="word")
        self.log_box.pack(fill="both", expand=True)
        self.log_events = 0

        stats = tk.Frame(content, bg=self.BG)
        stats.pack(fill="x", pady=(13, 0))
        self.scanned_card = self._stat_card(stats, "CHECKED", "0", self.CYAN)
        self.scanned_card.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.found_card = self._stat_card(stats, "NO PROFILE FOUND", "0", self.GREEN)
        self.found_card.pack(side="left", fill="x", expand=True, padx=4)
        # Alias used by the scan-start, progress, and clear handlers.
        self.available_count = self.found_card.value_label
        self.taken_card = self._stat_card(stats, "TAKEN", "0", self.RED)
        self.taken_card.pack(side="left", fill="x", expand=True, padx=4)
        self.error_card = self._stat_card(stats, "PROGRESS / ERRORS", "0% / 0", "#f2c94c")
        self.error_card.pack(side="left", fill="x", expand=True, padx=(8, 0))

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TCombobox", fieldbackground="#090a10", background="#222335", foreground=self.TEXT, arrowcolor=self.GREEN, bordercolor=self.BORDER)
        style.configure("Horizontal.TProgressbar", troughcolor="#242431", background=self.GREEN, bordercolor="#242431")

    def _nav_button(self, parent, name, symbol):
        btn = tk.Button(
            parent, text=f"  {symbol}   {name}", anchor="w",
            command=lambda: self._show_page(name),
            bg="#30204d" if name == "Scanner" else self.SIDEBAR,
            fg=self.GREEN if name == "Scanner" else "#a0a2b2",
            activebackground="#39245d", activeforeground=self.GREEN,
            relief="flat", bd=0, padx=8, pady=8,
            font=("Segoe UI", 9, "bold" if name == "Dashboard" else "normal"),
            cursor="hand2"
        )
        btn.pack(fill="x", padx=7, pady=2)

    def _show_page(self, name):
        self.log(f"{name} selected. Scanner controls remain available on this screen.")

    def _stat_card(self, parent, title, value, accent):
        card = self._panel(parent, 12, 10)
        self._label(card, title, 8, self.MUTED, True).pack(anchor="w")
        val = self._label(card, value, 19, self.TEXT, True)
        val.pack(anchor="w", pady=(4, 0))
        card.value_label = val
        card.accent = accent
        return card

    def log(self, message):
        self.ui_queue.put(("log", message))

    def start_scan(self):
        if self.running:
            return
        try:
            amount = int(self.amount_var.get())
            workers = int(self.workers_var.get())
            if not 1 <= amount <= 10000:
                raise ValueError("Names per scan must be between 1 and 10,000.")
            if not 1 <= workers <= 8:
                raise ValueError("Workers must be between 1 and 8. 4 is recommended to reduce API rate limits.")
        except ValueError as exc:
            messagebox.showerror("Check settings", str(exc))
            return

        webhook_url = self.webhook_entry.get().strip()
        if webhook_url and not re.fullmatch(r"https://discord(?:app)?\.com/api/webhooks/\d+/[\w.-]+", webhook_url):
            messagebox.showerror("Invalid webhook", "Enter a valid Discord webhook URL.")
            return

        min_len = int(self.min_len_var.get())
        max_len = int(self.max_len_var.get())
        if min_len > max_len:
            messagebox.showerror("Check name length", "The 'from' length cannot be greater than the 'to' length.")
            return
        self.stop_event.clear()
        self.results.clear()
        self.checked = self.errors = 0
        self.total = amount
        self.result_box.delete(0, tk.END)
        self.progress.configure(value=0)
        self.progress_label.configure(text=f"Scanning Minecraft names with {min_len}–{max_len} characters …")
        self.available_count.configure(text="0")
        self.found_card.value_label.configure(text="0")
        self.scanned_card.value_label.configure(text="0")
        self.error_card.value_label.configure(text="0")
        self.webhook = DiscordWebhook(webhook_url, self.log)
        self.running = True
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.log(f"Scan started: {amount:,} names, length {min_len}–{max_len}, {workers} workers. Previous names are skipped.")
        self.log("Discord webhook enabled: available-name notifications will be sent." if webhook_url
                 else "Discord webhook not set: results will only appear in the app.")
        self.scan_thread = threading.Thread(
            target=self._run_scan, args=(amount, workers, min_len, max_len), daemon=True
        )
        self.scan_thread.start()

    def _run_scan(self, amount, workers, min_len, max_len):
        try:
            known = self.history.known_names()
            self.ui_queue.put(("known", len(known)))
            candidates = list(generate_names(amount, known, self.stop_event, min_len, max_len))
            self.ui_queue.put(("total", len(candidates)))
            checker = MojangChecker(self.stop_event, self.log)

            def task(name):
                return name, checker.check(name)

            # Keep only a small number of requests queued at once. This reduces
            # memory use and makes stopping responsive even for 50k scans.
            batch_size = max(workers * 4, 4)
            for offset in range(0, len(candidates), batch_size):
                if self.stop_event.is_set():
                    break
                batch = candidates[offset:offset + batch_size]
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    future_to_name = {pool.submit(task, name): name for name in batch}
                    for future in as_completed(future_to_name):
                        if self.stop_event.is_set():
                            for pending in future_to_name:
                                pending.cancel()
                            break
                        name = future_to_name[future]
                        try:
                            _, result = future.result()
                        except Exception as exc:
                            self.history.add(name, "error")
                            self.log(f"Internal check error for {name}: {exc}")
                            self.ui_queue.put(("progress", name, "error"))
                            continue

                        # Remember every completed check, including API errors,
                        # so the next scan always moves on to fresh candidates.
                        # An error is never reported as an available name.
                        if result in ("taken", "not_found", "error"):
                            self.history.add(name, result)
                        if result == "not_found":
                            self.webhook.add(name)
                        if result in ("taken", "not_found", "error"):
                            self.ui_queue.put(("progress", name, result))
                if self.stop_event.is_set():
                    break

            if self.webhook:
                self.webhook.flush()
            self.ui_queue.put(("finished", self.stop_event.is_set()))
        except Exception as exc:
            self.log(f"Scan stopped: {exc}")
            if self.webhook:
                self.webhook.flush()
            self.ui_queue.put(("finished", True))

    def stop_scan(self):
        if self.running:
            self.stop_event.set()
            self.log("Stop requested. Waiting for active requests to finish …")
            self.stop_btn.configure(state="disabled")

    def _process_queue(self):
        try:
            while True:
                item = self.ui_queue.get_nowait()
                kind = item[0]
                if kind == "log":
                    self.log_box.configure(state="normal")
                    self.log_box.insert(tk.END, item[1] + "\n")
                    self.log_events += 1
                    self.log_count.configure(text=f"{self.log_events} events")
                    self.log_box.see(tk.END)
                    self.log_box.configure(state="disabled")
                elif kind == "known":
                    self.known_count = item[1]
                elif kind == "total":
                    self.total = item[1]
                elif kind == "progress":
                    _, name, result = item
                    self.checked += 1
                    if result == "not_found" and name:
                        self.results.append(name)
                        self.result_box.insert(tk.END, name)
                    elif result == "taken":
                        self.taken_card.value_label.configure(text=str(int(self.taken_card.value_label.cget("text")) + 1))
                    elif result == "error":
                        self.errors += 1
                    pct = self.checked / self.total * 100 if self.total else 0
                    self.progress.configure(value=pct)
                    self.progress_label.configure(
                        text=f"Live scan · {self.checked:,} / {self.total:,} checked · {pct:.1f}%"
                    )
                    self.found_card.value_label.configure(text=str(len(self.results)))
                    self.scanned_card.value_label.configure(text=f"{self.checked:,}")
                    pct_text = f"{pct:.0f}% / {self.errors}"
                    self.error_card.value_label.configure(text=pct_text)
                    self.available_count.configure(text=str(len(self.results)))
                elif kind == "finished":
                    self.running = False
                    self.start_btn.configure(state="normal")
                    self.stop_btn.configure(state="disabled")
                    self.log("Scan stopped." if item[1] else "Scan completed.")
        except queue.Empty:
            pass
        self.root.after(100, self._process_queue)

    def clear_results(self):
        if self.running:
            return
        self.results.clear()
        self.result_box.delete(0, tk.END)
        self.available_count.configure(text="0")
        self.found_card.value_label.configure(text="0")
        self.log("Visible result list cleared. Local duplicate history remains intact.")

    def copy_results(self):
        if not self.results:
            messagebox.showinfo("No results", "There are no results to copy yet.")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(self.results))
        self.log(f"Copied {len(self.results)} names to clipboard.")

    def save_results(self):
        if not self.results:
            messagebox.showinfo("No results", "There are no results to save yet.")
            return
        path = filedialog.asksaveasfilename(
            title="Save results", defaultextension=".txt",
            filetypes=[("Text file", "*.txt"), ("CSV file", "*.csv")]
        )
        if not path:
            return
        try:
            if path.lower().endswith(".csv"):
                content = "name,note\n" + "\n".join(
                    f"{n},no profile found - claimability not guaranteed" for n in self.results
                )
            else:
                content = (
                    f"Minecraft names ({self.min_len_var.get()}–{self.max_len_var.get()} characters)\n"
                    "No profile found by API; claimability is not guaranteed.\n\n"
                    + "\n".join(self.results)
                )
            Path(path).write_text(content, encoding="utf-8")
            self.log(f"Saved results to {path}")
        except OSError as exc:
            messagebox.showerror("Save failed", str(exc))

    def _on_close(self):
        if self.running:
            if not messagebox.askyesno("Exit", "Stop the scan and close the app?"):
                return
            self.stop_event.set()
        try:
            self.history.close()
        except Exception:
            pass
        self.root.destroy()

def main():
    root = tk.Tk()
    ScannerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
