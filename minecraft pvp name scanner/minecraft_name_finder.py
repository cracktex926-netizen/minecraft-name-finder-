#!/usr/bin/env python3
"""
Minecraft PvP Name Finder
Python 3.12+, Tkinter + SQLite + standard library only.

Run:
    python minecraft_name_finder.py
    python minecraft_name_finder.py --self-test

Notes:
- HTTP 200 means a profile exists (taken).
- HTTP 204 / 404 mean no profile was returned by this endpoint (candidate only,
  NOT a guarantee that the name is claimable).
- Transient failures and other HTTP statuses are not counted as checked.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import queue
import random
import re
import sqlite3
import string
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
from datetime import datetime, timezone
from typing import Optional


APP_TITLE = "NEXUS • Minecraft Name Finder"
DB_FILE = "minecraft_names.sqlite3"
API_URL = "https://api.mojang.com/users/profiles/minecraft/{}"
USER_AGENT = "NexusMinecraftNameFinder/1.0 (Python urllib)"
MAX_USERNAME_LENGTH = 16
MIN_USERNAME_LENGTH = 4

# A curated base list makes output feel like actual PvP handles rather than noise.
CORE_NAMES = [
    "Vex", "Noxy", "Vyrq", "Zyro", "Krypz", "Void", "Frost", "Drippy",
    "Qvack", "Rewz", "Vyn", "Zyph", "Rage", "Nexify", "Frosty", "Clutch",
    "Rush", "Stryfe", "Aero", "Nyx", "Kairo", "Zeno", "Riven", "Havoc",
    "Wraith", "Snipe", "Blink", "Flux", "Orbit", "Crave", "Sora", "Koda",
    "Vanta", "Jynx", "Haze", "Rogue", "Onyx", "Blitz", "Echo", "Keen",
    "Fable", "Drift", "Axiom", "Vexx", "Cynx", "Rift", "Frosty", "Misty",
    "Dusk", "Nova", "Ghost", "Venom", "Pixel", "Astra", "Quake", "Tempo",
]
PREFIXES = ["x", "q", "i", "Its", "Im", "Not", "Lil", "zy", "v", "o", "The"]
SUFFIXES = ["", "y", "z", "ify", "x", "qt", "s", "ツ"]
PVP_WORDS = [
    "Clutch", "Tryhard", "Combo", "Strafe", "Tap", "Rush", "Crit",
    "Flick", "WKey", "Snipe", "Duel", "Bridge", "Void", "Frost",
    "Drip", "Vibe", "Aim", "Wraith", "Haze", "Rage",
]


def valid_username(name: str) -> bool:
    return (
        MIN_USERNAME_LENGTH <= len(name) <= MAX_USERNAME_LENGTH
        and re.fullmatch(r"[A-Za-z0-9_]+", name) is not None
    )


class NameGenerator:
    """Produces plausible PvP-style names and avoids duplicates within a scan."""

    def __init__(self, mode: str = "random", seed: Optional[int] = None,
                 min_length: int = MIN_USERNAME_LENGTH,
                 max_length: int = MAX_USERNAME_LENGTH):
        self.mode = mode
        self.rng = random.Random(seed)
        self.min_length = min_length
        self.max_length = max_length
        self.seen: set[str] = set()
        self._sequential = self._build_sequential()
        self._seq_index = 0

    def _fits_length(self, name: str) -> bool:
        return self.min_length <= len(name) <= self.max_length

    def _build_sequential(self):
        names = []
        for core in CORE_NAMES:
            names.append(core)
            for prefix in PREFIXES:
                names.append(prefix + core)
            for suffix in SUFFIXES:
                if suffix:
                    names.append(core + suffix)
        for word in PVP_WORDS:
            names.append(word)
            for prefix in PREFIXES:
                names.append(prefix + word)
                for suffix in SUFFIXES:
                    names.append(prefix + word + suffix)
            for core in CORE_NAMES:
                names.extend((core + word, word + core))
        for core in CORE_NAMES:
            for prefix in PREFIXES:
                for suffix in SUFFIXES:
                    names.append(prefix + core + suffix)
            for other in CORE_NAMES:
                names.append(core + other)
                for suffix in SUFFIXES:
                    names.append(core + other + suffix)
        # Preserve order while removing case-insensitive duplicates.
        output, known = [], set()
        for name in names:
            key = name.lower()
            if valid_username(name) and self._fits_length(name) and key not in known:
                known.add(key)
                output.append(name)
        return output

    def _random_name(self) -> str:
        choices = [
            lambda: self.rng.choice(PREFIXES) + self.rng.choice(CORE_NAMES),
            lambda: self.rng.choice(CORE_NAMES) + self.rng.choice(SUFFIXES),
            lambda: self.rng.choice(PVP_WORDS),
            lambda: self.rng.choice(PREFIXES) + self.rng.choice(PVP_WORDS),
            lambda: self.rng.choice(CORE_NAMES) + self.rng.choice(CORE_NAMES),
            lambda: self.rng.choice(PREFIXES) + self.rng.choice(CORE_NAMES) + self.rng.choice(SUFFIXES),
            lambda: self.rng.choice(CORE_NAMES) + self.rng.choice(PVP_WORDS),
            lambda: self.rng.choice(PVP_WORDS) + self.rng.choice(CORE_NAMES),
        ]
        # Try a few meaningful constructions, then fall back to curated names.
        for _ in range(30):
            candidate = self.rng.choice(choices)()
            if valid_username(candidate) and self._fits_length(candidate):
                return candidate
        # A finite curated fallback pool avoids returning an out-of-range name.
        valid_pool = [n for n in self._sequential if self._fits_length(n)]
        if valid_pool:
            return self.rng.choice(valid_pool)
        return ""

    def generate(self, count: int):
        produced = 0
        attempts = 0
        max_attempts = max(1000, count * 60)
        while produced < count and attempts < max_attempts:
            attempts += 1
            if self.mode == "sequential":
                if self._seq_index >= len(self._sequential):
                    # Continue with a new shuffled pass, not character spam.
                    self.rng.shuffle(self._sequential)
                    self._seq_index = 0
                candidate = self._sequential[self._seq_index]
                self._seq_index += 1
            else:
                candidate = self._random_name()
            key = candidate.lower()
            if not candidate or key in self.seen or not valid_username(candidate) or not self._fits_length(candidate):
                continue
            self.seen.add(key)
            produced += 1
            yield candidate


class NameDatabase:
    def __init__(self, path: str = DB_FILE):
        self.path = path
        self._lock = threading.RLock()
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS usernames (
                    username TEXT PRIMARY KEY COLLATE NOCASE,
                    result TEXT NOT NULL,
                    checked_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS favorites (
                    username TEXT PRIMARY KEY COLLATE NOCASE,
                    added_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS notifications (
                    username TEXT PRIMARY KEY COLLATE NOCASE,
                    sent_at TEXT NOT NULL
                )
            """)

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=15)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    def was_checked(self, username: str) -> bool:
        with self._lock, self._connect() as conn:
            return conn.execute(
                "SELECT 1 FROM usernames WHERE username=? COLLATE NOCASE",
                (username,),
            ).fetchone() is not None

    def checked_set(self, usernames: list[str]) -> set[str]:
        """Fetch prior checks in batches, avoiding one SQLite connection per name."""
        found: set[str] = set()
        with self._lock, self._connect() as conn:
            for offset in range(0, len(usernames), 500):
                batch = usernames[offset:offset + 500]
                if not batch:
                    continue
                marks = ",".join("?" for _ in batch)
                rows = conn.execute(
                    f"SELECT username FROM usernames WHERE username COLLATE NOCASE IN ({marks})",
                    batch,
                ).fetchall()
                found.update(row[0].lower() for row in rows)
        return found

    def save_result(self, username: str, result: str):
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO usernames(username,result,checked_at) VALUES(?,?,?)",
                (username, result, datetime.now(timezone.utc).isoformat()),
            )

    def get_favorites(self):
        with self._lock, self._connect() as conn:
            return [r[0] for r in conn.execute(
                "SELECT username FROM favorites ORDER BY username COLLATE NOCASE"
            )]

    def toggle_favorite(self, username: str) -> bool:
        with self._lock, self._connect() as conn:
            found = conn.execute(
                "SELECT 1 FROM favorites WHERE username=? COLLATE NOCASE",
                (username,),
            ).fetchone()
            if found:
                conn.execute("DELETE FROM favorites WHERE username=? COLLATE NOCASE", (username,))
                return False
            conn.execute(
                "INSERT OR IGNORE INTO favorites(username,added_at) VALUES(?,?)",
                (username, datetime.now(timezone.utc).isoformat()),
            )
            return True

    def unnotified(self, names: list[str]) -> list[str]:
        with self._lock, self._connect() as conn:
            sent = {r[0].lower() for r in conn.execute("SELECT username FROM notifications")}
        return [n for n in names if n.lower() not in sent]

    def mark_notified(self, names: list[str]):
        with self._lock, self._connect() as conn:
            now = datetime.now(timezone.utc).isoformat()
            conn.executemany(
                "INSERT OR IGNORE INTO notifications(username,sent_at) VALUES(?,?)",
                [(n, now) for n in names],
            )


def _retry_after(headers) -> float:
    raw = headers.get("Retry-After") if headers else None
    try:
        return max(1.0, min(float(raw), 120.0))
    except (TypeError, ValueError):
        return 5.0


def query_mojang(username: str, stop_event: Optional[threading.Event] = None,
                 timeout: float = 6.0, retries: int = 2) -> tuple[str, str]:
    """
    Return (classification, detail).
    classification is taken, candidate, invalid, or error.
    Only 200/204/404 are conclusive endpoint responses.
    """
    if not valid_username(username):
        return "invalid", "Invalid Minecraft username format"

    url = API_URL.format(quote(username, safe=""))
    for attempt in range(retries + 1):
        if stop_event and stop_event.is_set():
            return "error", "Cancelled"
        request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        try:
            with urlopen(request, timeout=timeout) as response:
                status = response.status
                if status == 200:
                    return "taken", "Profile exists"
                if status in (204, 404):
                    return "candidate", "No profile returned; not guaranteed claimable"
                return "error", f"Unexpected HTTP {status}"
        except HTTPError as exc:
            if exc.code == 200:
                return "taken", "Profile exists"
            if exc.code in (204, 404):
                return "candidate", "No profile returned; not guaranteed claimable"
            if exc.code == 400:
                return "invalid", "HTTP 400: invalid request"
            if exc.code == 429:
                delay = _retry_after(exc.headers)
                if attempt >= retries:
                    return "error", f"Rate limited; retry after {delay:g}s"
            elif exc.code in (403, 500, 502, 503, 504):
                delay = min(2 ** attempt, 8)
                if attempt >= retries:
                    return "error", f"Temporary HTTP {exc.code}"
            else:
                return "error", f"HTTP {exc.code}"
        except (URLError, TimeoutError, OSError) as exc:
            if attempt >= retries:
                return "error", f"Network error: {exc}"
            delay = min(1.5 * (2 ** attempt), 8)
        if stop_event:
            if stop_event.wait(delay):
                return "error", "Cancelled"
        else:
            time.sleep(delay)
    return "error", "Request failed"


def send_discord_webhook(webhook_url: str, names: list[str], timeout: float = 8.0):
    """Send a batch and return (success, message). @everyone is explicitly allowed."""
    if not webhook_url.startswith(("https://discord.com/api/webhooks/",
                                   "https://discordapp.com/api/webhooks/")):
        return False, "Webhook URL does not look like a Discord webhook"
    chunks = [names[i:i + 40] for i in range(0, len(names), 40)]
    for chunk in chunks:
        content = "@everyone\n**Minecraft PvP Names Found!**\n" + "\n".join(
            f"- `{name}`" for name in chunk
        )
        payload = {
            "content": content[:1900],
            "allowed_mentions": {"parse": ["everyone"]},
        }
        request = Request(
            webhook_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
            method="POST",
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                if response.status not in (200, 204):
                    return False, f"Discord returned HTTP {response.status}"
        except HTTPError as exc:
            if exc.code == 429:
                try:
                    wait = float(exc.headers.get("Retry-After", "2"))
                except (TypeError, ValueError):
                    wait = 2
                time.sleep(min(max(wait, 1), 30))
                try:
                    with urlopen(request, timeout=timeout) as response:
                        if response.status not in (200, 204):
                            return False, f"Discord returned HTTP {response.status}"
                except Exception as retry_exc:
                    return False, f"Discord retry failed: {retry_exc}"
            else:
                return False, f"Discord HTTP {exc.code}"
        except Exception as exc:
            return False, f"Discord error: {exc}"
    return True, f"Sent {len(names)} candidate(s)"


class MinecraftNameFinder:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("1280x820")
        self.root.minsize(1050, 700)
        self.root.configure(bg="#0b0b12")

        self.db = NameDatabase()
        self.events: queue.Queue = queue.Queue()
        self.scan_id = 0
        self.scan_thread: Optional[threading.Thread] = None
        self.stop_event: Optional[threading.Event] = None
        self.running = False
        self.results: dict[str, str] = {}
        self.selected_mode = "random"
        self._build_style()
        self._build_ui()
        self._refresh_favorites()
        self._poll_events()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_style(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TProgressbar", troughcolor="#242333", background="#9b6bff",
                        bordercolor="#242333", lightcolor="#9b6bff", darkcolor="#9b6bff")
        style.configure("Treeview", background="#12121c", foreground="#e8e6f0",
                        fieldbackground="#12121c", rowheight=29, borderwidth=0,
                        font=("Segoe UI", 10))
        style.configure("Treeview.Heading", background="#1c1b2a", foreground="#bba3ff",
                        font=("Segoe UI Semibold", 9), relief="flat")
        style.map("Treeview", background=[("selected", "#44316b")],
                  foreground=[("selected", "#ffffff")])

    def _card(self, parent, title, value):
        frame = tk.Frame(parent, bg="#151420", highlightbackground="#282538",
                         highlightthickness=1, padx=16, pady=12)
        tk.Label(frame, text=title.upper(), bg="#151420", fg="#89869d",
                 font=("Segoe UI", 8, "bold")).pack(anchor="w")
        label = tk.Label(frame, text=value, bg="#151420", fg="#f3efff",
                         font=("Segoe UI Semibold", 22))
        label.pack(anchor="w", pady=(4, 0))
        return frame, label

    def _button(self, parent, text, command, primary=False):
        bg = "#8054df" if primary else "#242235"
        fg = "#ffffff" if primary else "#c9c4dc"
        return tk.Button(parent, text=text, command=command, bg=bg, fg=fg,
                         activebackground="#9b78ed", activeforeground="white",
                         relief="flat", bd=0, padx=13, pady=9,
                         font=("Segoe UI Semibold", 9), cursor="hand2",
                         disabledforeground="#686477")

    def _section(self, parent, title):
        tk.Label(parent, text=title.upper(), bg="#11111a", fg="#a99acb",
                 font=("Segoe UI", 8, "bold")).pack(anchor="w", pady=(12, 7))

    def _build_ui(self):
        top = tk.Frame(self.root, bg="#0b0b12", padx=18, pady=15)
        top.pack(fill="x")
        tk.Label(top, text="NEXUS", bg="#0b0b12", fg="#b18aff",
                 font=("Segoe UI Black", 20)).pack(side="left")
        tk.Label(top, text="MINECRAFT NAME FINDER  /  PVP EDITION",
                 bg="#0b0b12", fg="#77738a", font=("Segoe UI", 9, "bold")
                 ).pack(side="left", padx=14, pady=(6, 0))
        self.status_label = tk.Label(top, text="● IDLE", bg="#0b0b12", fg="#9d91b8",
                                     font=("Segoe UI", 9, "bold"))
        self.status_label.pack(side="right", pady=8)

        stats = tk.Frame(self.root, bg="#0b0b12", padx=18)
        stats.pack(fill="x", pady=(0, 13))
        self.stat_labels = {}
        for key, initial in [("Checked", "0"), ("Candidates", "0"),
                             ("Taken", "0"), ("Progress", "0%")]:
            frame, label = self._card(stats, key, initial)
            frame.pack(side="left", fill="x", expand=True, padx=(0, 10))
            self.stat_labels[key] = label

        body = tk.Frame(self.root, bg="#0b0b12", padx=18)
        body.pack(fill="both", expand=True)
        sidebar = tk.Frame(body, bg="#11111a", width=235, padx=14, pady=12)
        sidebar.pack(side="left", fill="y", padx=(0, 12))
        sidebar.pack_propagate(False)

        self._section(sidebar, "Scan settings")
        tk.Label(sidebar, text="Usernames per scan", bg="#11111a", fg="#aaa5bb",
                 font=("Segoe UI", 9)).pack(anchor="w")
        self.count_var = tk.StringVar(value="500")
        self.count_entry = tk.Entry(sidebar, textvariable=self.count_var, bg="#1b1a27",
                                    fg="#eeeaf8", insertbackground="white", relief="flat",
                                    font=("Segoe UI", 10))
        self.count_entry.pack(fill="x", pady=(5, 8), ipady=7)

        length_row = tk.Frame(sidebar, bg="#11111a")
        length_row.pack(fill="x", pady=(2, 9))
        tk.Label(length_row, text="Min. letters", bg="#11111a", fg="#aaa5bb",
                 font=("Segoe UI", 8)).grid(row=0, column=0, sticky="w")
        tk.Label(length_row, text="Max. letters", bg="#11111a", fg="#aaa5bb",
                 font=("Segoe UI", 8)).grid(row=0, column=1, sticky="w", padx=(8, 0))
        self.min_length_var = tk.StringVar(value="4")
        self.max_length_var = tk.StringVar(value="16")
        self.min_length_entry = tk.Entry(length_row, textvariable=self.min_length_var,
                                          bg="#1b1a27", fg="#eeeaf8",
                                          insertbackground="white", relief="flat",
                                          font=("Segoe UI", 10), width=8)
        self.max_length_entry = tk.Entry(length_row, textvariable=self.max_length_var,
                                          bg="#1b1a27", fg="#eeeaf8",
                                          insertbackground="white", relief="flat",
                                          font=("Segoe UI", 10), width=8)
        self.min_length_entry.grid(row=1, column=0, sticky="ew", pady=(4, 0), ipady=6)
        self.max_length_entry.grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=(4, 0), ipady=6)
        length_row.grid_columnconfigure(0, weight=1)
        length_row.grid_columnconfigure(1, weight=1)

        self.random_btn = self._button(sidebar, "⚡  Random Scan", lambda: self.start_scan("random"), True)
        self.random_btn.pack(fill="x", pady=3)
        self.sequential_btn = self._button(sidebar, "≡  Sequential Scan", lambda: self.start_scan("sequential"))
        self.sequential_btn.pack(fill="x", pady=3)
        self.stop_btn = self._button(sidebar, "■  Stop Scan", self.stop_scan)
        self.stop_btn.pack(fill="x", pady=3)
        self.stop_btn.configure(state="disabled")

        self._section(sidebar, "Discord webhook")
        self.webhook_var = tk.StringVar()
        self.webhook_entry = tk.Entry(sidebar, textvariable=self.webhook_var, show="•",
                                      bg="#1b1a27", fg="#eeeaf8", insertbackground="white",
                                      relief="flat", font=("Segoe UI", 8))
        self.webhook_entry.pack(fill="x", ipady=7)
        tk.Label(sidebar, text="Sends candidate batches with @everyone.",
                 bg="#11111a", fg="#706c80", wraplength=195,
                 font=("Segoe UI", 8)).pack(anchor="w", pady=5)

        self._section(sidebar, "Favorites")
        self.fav_list = tk.Listbox(sidebar, bg="#171622", fg="#d8d1eb",
                                   selectbackground="#44316b", relief="flat",
                                   highlightthickness=0, font=("Consolas", 9), height=9)
        self.fav_list.pack(fill="both", expand=True)
        self._button(sidebar, "Remove Favorite", self.remove_favorite).pack(fill="x", pady=(6, 2))

        center = tk.Frame(body, bg="#11111a", padx=13, pady=12)
        center.pack(side="left", fill="both", expand=True, padx=(0, 12))
        header = tk.Frame(center, bg="#11111a")
        header.pack(fill="x")
        tk.Label(header, text="Live scan results", bg="#11111a", fg="#f1edfa",
                 font=("Segoe UI Semibold", 13)).pack(side="left")
        self._button(header, "Copy Selected", self.copy_selected).pack(side="right", padx=(5, 0))
        self._button(header, "Copy All", self.copy_all).pack(side="right")
        self._button(header, "☆ Favorite", self.favorite_selected).pack(side="right", padx=(0, 5))

        columns = ("username", "status", "detail")
        self.tree = ttk.Treeview(center, columns=columns, show="headings", selectmode="extended")
        self.tree.heading("username", text="USERNAME")
        self.tree.heading("status", text="RESULT")
        self.tree.heading("detail", text="DETAIL")
        self.tree.column("username", width=145, anchor="w")
        self.tree.column("status", width=110, anchor="center")
        self.tree.column("detail", width=310, anchor="w")
        self.tree.pack(fill="both", expand=True, pady=(12, 0))
        self.tree.tag_configure("candidate", foreground="#83e6b0")
        self.tree.tag_configure("taken", foreground="#ee8498")
        self.tree.tag_configure("invalid", foreground="#e7bd72")

        right = tk.Frame(body, bg="#11111a", width=245, padx=13, pady=12)
        right.pack(side="right", fill="y")
        right.pack_propagate(False)
        tk.Label(right, text="Live log", bg="#11111a", fg="#f1edfa",
                 font=("Segoe UI Semibold", 13)).pack(anchor="w")
        self.log = tk.Text(right, bg="#0d0d15", fg="#a9a3ba", insertbackground="white",
                           relief="flat", wrap="word", font=("Consolas", 8),
                           state="disabled", padx=9, pady=9)
        self.log.pack(fill="both", expand=True, pady=(12, 0))

        bottom = tk.Frame(self.root, bg="#0b0b12", padx=18, pady=12)
        bottom.pack(fill="x")
        self.progress_text = tk.Label(bottom, text="Waiting for scan",
                                      bg="#0b0b12", fg="#8e899f", font=("Segoe UI", 9))
        self.progress_text.pack(anchor="w", pady=(0, 7))
        self.progress = ttk.Progressbar(bottom, maximum=100, mode="determinate")
        self.progress.pack(fill="x")

    def _log(self, message: str):
        self.log.configure(state="normal")
        self.log.insert("end", f"[{datetime.now().strftime('%H:%M:%S')}] {message}\n")
        self.log.see("end")
        # Bound the widget size on long sessions.
        lines = int(self.log.index("end-1c").split(".")[0])
        if lines > 400:
            self.log.delete("1.0", "80.0")
        self.log.configure(state="disabled")

    def _set_running_ui(self, running: bool):
        self.running = running
        self.random_btn.configure(state="disabled" if running else "normal")
        self.sequential_btn.configure(state="disabled" if running else "normal")
        self.stop_btn.configure(state="normal" if running else "disabled")
        self.count_entry.configure(state="disabled" if running else "normal")
        self.min_length_entry.configure(state="disabled" if running else "normal")
        self.max_length_entry.configure(state="disabled" if running else "normal")
        self.status_label.configure(text="● SCANNING" if running else "● IDLE",
                                    fg="#b18aff" if running else "#9d91b8")

    def start_scan(self, mode: str):
        if self.running:
            return
        try:
            count = int(self.count_var.get().strip())
            if not 1 <= count <= 10000:
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid scan size", "Enter a number from 1 to 10,000.")
            return

        try:
            min_length = int(self.min_length_var.get().strip())
            max_length = int(self.max_length_var.get().strip())
            if not (4 <= min_length <= 16 and 4 <= max_length <= 16 and min_length <= max_length):
                raise ValueError
        except ValueError:
            messagebox.showerror(
                "Invalid username length",
                "Minimum and maximum must be between 4 and 16, and minimum must not exceed maximum."
            )
            return

        self.scan_id += 1
        current_id = self.scan_id
        self.stop_event = threading.Event()
        self.selected_mode = mode
        self.results = {}
        self.progress.configure(value=0)
        self.progress_text.configure(text=f"Preparing {count:,} usernames…")
        for key in self.stat_labels:
            self.stat_labels[key].configure(text="0%" if key == "Progress" else "0")
        for item in self.tree.get_children():
            self.tree.delete(item)
        self._set_running_ui(True)
        self._log(f"Started {mode} scan • target {count:,}")
        self.scan_thread = threading.Thread(
            target=self._scan_worker,
            args=(current_id, mode, count, self.stop_event, self.webhook_var.get().strip(),
                  min_length, max_length),
            daemon=True,
        )
        self.scan_thread.start()

    def stop_scan(self):
        if not self.running:
            return
        if self.stop_event:
            self.stop_event.set()
        # Invalidate this run immediately. Its worker owns its executor and can
        # only publish events carrying the old ID, which the UI discards.
        self.scan_id += 1
        self._set_running_ui(False)
        self.progress_text.configure(text="Scan stopped • ready for a new scan")
        self._log("Stop requested • pending work cancelled; safe to start again")
        self.scan_thread = None
        self.stop_event = None

    def _scan_worker(self, scan_id: int, mode: str, count: int,
                     stop_event: threading.Event, webhook: str,
                     min_length: int, max_length: int):
        generator = NameGenerator(mode, min_length=min_length, max_length=max_length)
        generated = list(generator.generate(count * 4))
        if stop_event.is_set():
            self.events.put((scan_id, "finished", {"stopped": True, "total": 0}))
            return
        previously_checked = self.db.checked_set(generated)
        candidates = [name for name in generated if name.lower() not in previously_checked][:count]
        skipped = len(generated) - len(candidates)

        self.events.put((scan_id, "log", f"Generated {len(candidates):,}; skipped {skipped:,} previously checked"))
        if not candidates:
            self.events.put((scan_id, "finished", {"stopped": stop_event.is_set(), "total": 0}))
            return

        checked = taken = found = errors = 0
        pending_notifications = []
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix=f"scan-{scan_id}")
        future_map = {
            executor.submit(query_mojang, name, stop_event): name for name in candidates
        }
        try:
            for future in concurrent.futures.as_completed(future_map):
                if stop_event.is_set():
                    break
                name = future_map[future]
                try:
                    result, detail = future.result()
                except Exception as exc:
                    result, detail = "error", str(exc)
                if result == "error":
                    errors += 1
                    if errors == 1 or errors % 10 == 0:
                        self.events.put((scan_id, "log", f"Temporary request failures: {errors} (not counted)"))
                    continue
                if result == "invalid":
                    continue
                self.db.save_result(name, result)
                checked += 1
                if result == "taken":
                    taken += 1
                elif result == "candidate":
                    found += 1
                    pending_notifications.append(name)
                    self.events.put((scan_id, "candidate", (name, detail)))
                self.events.put((scan_id, "progress", {
                    "checked": checked, "taken": taken, "found": found,
                    "target": len(candidates), "name": name, "result": result
                }))
                if len(pending_notifications) >= 10 and webhook:
                    batch = self.db.unnotified(pending_notifications)
                    if batch:
                        ok, msg = send_discord_webhook(webhook, batch)
                        if ok:
                            self.db.mark_notified(batch)
                        self.events.put((scan_id, "log", f"Discord: {msg}"))
                    pending_notifications.clear()
        finally:
            for future in future_map:
                future.cancel()
            executor.shutdown(wait=False, cancel_futures=True)

        if pending_notifications and webhook and not stop_event.is_set():
            batch = self.db.unnotified(pending_notifications)
            if batch:
                ok, msg = send_discord_webhook(webhook, batch)
                if ok:
                    self.db.mark_notified(batch)
                self.events.put((scan_id, "log", f"Discord: {msg}"))
        self.events.put((scan_id, "finished", {
            "stopped": stop_event.is_set(), "total": checked, "taken": taken,
            "found": found, "errors": errors
        }))

    def _poll_events(self):
        try:
            while True:
                scan_id, kind, payload = self.events.get_nowait()
                if scan_id != self.scan_id:
                    continue
                if kind == "log":
                    self._log(payload)
                elif kind == "candidate":
                    name, detail = payload
                    self.results[name] = "candidate"
                    self.tree.insert("", "end", values=(name, "CANDIDATE", detail), tags=("candidate",))
                elif kind == "progress":
                    self.results[payload["name"]] = payload["result"]
                    self.stat_labels["Checked"].configure(text=str(payload["checked"]))
                    self.stat_labels["Candidates"].configure(text=str(payload["found"]))
                    self.stat_labels["Taken"].configure(text=str(payload["taken"]))
                    pct = min(100, payload["checked"] / max(1, payload["target"]) * 100)
                    self.stat_labels["Progress"].configure(text=f"{pct:.1f}%")
                    self.progress.configure(value=pct)
                    self.progress_text.configure(
                        text=f"Checked {payload['checked']:,} / {payload['target']:,} • latest: {payload['name']}"
                    )
                    if payload["result"] == "taken":
                        self.tree.insert("", "end", values=(payload["name"], "TAKEN", "Profile exists"), tags=("taken",))
                elif kind == "finished":
                    data = payload
                    self._set_running_ui(False)
                    self.progress_text.configure(
                        text=("Scan stopped" if data.get("stopped") else "Scan complete")
                        + f" • {data.get('total', 0):,} checked"
                    )
                    self._log(
                        f"{'Stopped' if data.get('stopped') else 'Finished'} • "
                        f"{data.get('total', 0)} checked, {data.get('found', 0)} candidates, "
                        f"{data.get('taken', 0)} taken, {data.get('errors', 0)} errors"
                    )
                    self._refresh_favorites()
        except queue.Empty:
            pass
        self.root.after(80, self._poll_events)

    def _selected_names(self):
        names = []
        for item in self.tree.selection():
            values = self.tree.item(item, "values")
            if values:
                names.append(values[0])
        return names

    def copy_selected(self):
        names = self._selected_names()
        if not names:
            self._log("Copy Selected: select one or more rows first")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(names))
        self._log(f"Copied {len(names)} selected username(s)")

    def copy_all(self):
        names = list(self.results.keys())
        if not names:
            names = [self.tree.item(i, "values")[0] for i in self.tree.get_children()]
        if not names:
            self._log("Copy All: no results yet")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(names))
        self._log(f"Copied {len(names)} candidate username(s)")

    def favorite_selected(self):
        names = self._selected_names()
        if not names:
            self._log("Favorite: select a username first")
            return
        for name in names:
            state = self.db.toggle_favorite(name)
            self._log(f"{'Added' if state else 'Removed'} favorite: {name}")
        self._refresh_favorites()

    def _refresh_favorites(self):
        self.fav_list.delete(0, "end")
        for name in self.db.get_favorites():
            self.fav_list.insert("end", name)

    def remove_favorite(self):
        selection = self.fav_list.curselection()
        if not selection:
            return
        name = self.fav_list.get(selection[0])
        self.db.toggle_favorite(name)
        self._refresh_favorites()
        self._log(f"Removed favorite: {name}")

    def _on_close(self):
        if self.stop_event:
            self.stop_event.set()
        self.scan_id += 1
        self.root.destroy()


# Lightweight automated checks for the critical non-GUI logic.
def run_self_tests():
    import tempfile
    import unittest
    from unittest.mock import patch

    class Response:
        def __init__(self, status):
            self.status = status
        def __enter__(self): return self
        def __exit__(self, *args): return False

    class Tests(unittest.TestCase):
        def test_generator_quality_and_lengths(self):
            names = list(NameGenerator("random", seed=10).generate(500))
            self.assertEqual(len(names), len({n.lower() for n in names}))
            self.assertTrue(all(valid_username(n) for n in names))
            self.assertTrue(any(any(c.isalpha() for c in n) for n in names))

        def test_sequential_generator(self):
            names = list(NameGenerator("sequential").generate(100))
            self.assertEqual(len(names), len({n.lower() for n in names}))
            self.assertTrue(all(valid_username(n) for n in names))

        def test_custom_length_range(self):
            for mode in ("random", "sequential"):
                names = list(NameGenerator(mode, seed=42, min_length=6, max_length=8).generate(80))
                self.assertTrue(all(6 <= len(n) <= 8 for n in names))
                self.assertEqual(len(names), len({n.lower() for n in names}))

        def test_database_duplicate_prevention_and_favorites(self):
            with tempfile.TemporaryDirectory() as tmp:
                db = NameDatabase(tmp + "/test.sqlite")
                self.assertFalse(db.was_checked("xVex"))
                db.save_result("xVex", "candidate")
                self.assertTrue(db.was_checked("XVEX"))
                self.assertTrue(db.toggle_favorite("xVex"))
                self.assertIn("xVex", db.get_favorites())
                self.assertFalse(db.toggle_favorite("XVEX"))

        def test_api_statuses(self):
            with patch("__main__.urlopen", return_value=Response(200)):
                self.assertEqual(query_mojang("xVex")[0], "taken")
            with patch("__main__.urlopen", return_value=Response(204)):
                self.assertEqual(query_mojang("xVex")[0], "candidate")
            with patch("__main__.urlopen", return_value=Response(404)):
                self.assertEqual(query_mojang("xVex")[0], "candidate")

        def test_api_error_not_available(self):
            with patch("__main__.urlopen", side_effect=HTTPError(
                "https://example.test", 500, "Server Error", {}, None
            )):
                result, _ = query_mojang("xVex", retries=0)
                self.assertEqual(result, "error")

        def test_invalid_name(self):
            self.assertEqual(query_mojang("abc")[0], "invalid")

    suite = unittest.defaultTestLoader.loadTestsFromTestCase(Tests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


def main():
    parser = argparse.ArgumentParser(description="Minecraft PvP Name Finder")
    parser.add_argument("--self-test", action="store_true", help="run automated logic tests")
    args = parser.parse_args()
    if args.self_test:
        raise SystemExit(run_self_tests())
    root = tk.Tk()
    MinecraftNameFinder(root)
    root.mainloop()


if __name__ == "__main__":
    main()
