#!/usr/bin/env python3
"""YouTube MP3 Converter: small local GUI, YouTube URL -> MP3, built on yt-dlp + ffmpeg.

Transient network problems are retried quietly in the background (exponential
backoff, resumes the partial download, waits out offline periods).
Permanent problems (private/removed video, bad URL, no disk space, ...) are
shown immediately in a dialog and are never retried.
"""
import queue
import random
import shutil
import socket
import sys
import subprocess
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import yt_dlp

MAX_FAILURES = 8        # consecutive transient failures with no progress
UNKNOWN_RETRIES = 2     # unrecognised errors get a couple of tries, then surface
BACKOFF_CAP = 60        # seconds


# --------------------------------------------------------------------------
# Error classification
# --------------------------------------------------------------------------
# (substring, friendly message). First match wins; checked before transient.
PERMANENT = [
    ("certificate verify failed", "SSL certificate check failed (this Python has no CA "
                                  "certificates, or a proxy is intercepting traffic). "
                                  "Run: pip install -U certifi"),
    ("private video", "This video is private."),
    ("video unavailable", "This video is unavailable."),
    ("video is unavailable", "This video is unavailable (removed, private, or blocked "
                             "for this network/region)."),
    ("this video is not available", "This video is not available."),
    ("has been removed", "This video has been removed."),
    ("account associated with this video has been terminated",
     "The uploader's account was terminated."),
    ("copyright", "This video is blocked for copyright reasons."),
    ("not available in your country", "This video is geo-blocked in your region."),
    ("blocked it in your country", "This video is geo-blocked in your region."),
    ("confirm your age", "This video is age-restricted and needs a signed-in account."),
    ("age-restricted", "This video is age-restricted and needs a signed-in account."),
    ("members-only", "This video is for channel members only."),
    ("join this channel", "This video is for channel members only."),
    ("not a bot", "YouTube is asking for a sign-in / bot check. Try again later "
                  "or update yt-dlp (pip install -U yt-dlp)."),
    ("is not a valid url", "That doesn't look like a valid URL."),
    ("unsupported url", "That URL isn't supported."),
    ("live event will begin", "This live event hasn't started yet."),
    ("premieres in", "This video hasn't premiered yet."),
    ("is live", "Live streams in progress can't be converted. Wait until it ends."),
    ("requested format is not available", "No audio format available for this video."),
    ("no video formats", "No audio format available for this video."),
    ("ffmpeg", "ffmpeg is missing or failed. Install it or reinstall imageio-ffmpeg."),
    ("ffprobe", "ffmpeg is missing or failed. Install it or reinstall imageio-ffmpeg."),
    ("no space left", "The disk is full."),
    ("permission denied", "Can't write to that folder (permission denied)."),
    ("read-only file system", "Can't write to that folder (read-only)."),
    ("http error 403", "YouTube refused the download (403). Updating yt-dlp usually "
                       "fixes this: pip install -U yt-dlp"),
    ("http error 404", "The video or stream could not be found (404)."),
]

TRANSIENT = [
    "timed out", "timeout", "connection reset", "connection aborted",
    "connection refused", "remote end closed", "incomplete read",
    "temporary failure in name resolution", "name or service not known",
    "nodename nor servname", "network is unreachable", "no route to host",
    "unable to download", "read error", "eof occurred", "ssl",
    "http error 5", "http error 429", "too many requests",
    "bad gateway", "service unavailable", "gateway", "transporterror",
    "urlopen error", "broken pipe", "getaddrinfo",
]


def clean(msg: str) -> str:
    msg = str(msg).strip()
    return msg[len("ERROR: "):] if msg.startswith("ERROR: ") else msg


def classify(exc: BaseException):
    """Return ('permanent', friendly) | ('transient', raw) | ('unknown', raw)."""
    if isinstance(exc, OSError) and not isinstance(exc, (socket.timeout, ConnectionError)):
        if exc.errno in (28, 13, 30):  # ENOSPC, EACCES, EROFS
            return "permanent", clean(exc.strerror or exc)
    text = clean(exc)
    low = text.lower()
    for needle, friendly in PERMANENT:
        if needle in low:
            return "permanent", friendly
    if isinstance(exc, (socket.timeout, ConnectionError, TimeoutError)):
        return "transient", text
    if any(n in low for n in TRANSIENT):
        return "transient", text
    return "unknown", text


def is_online(timeout=3) -> bool:
    try:
        socket.create_connection(("www.youtube.com", 443), timeout=timeout).close()
        return True
    except OSError:
        return False


def _use_certifi():
    """python.org builds on macOS ship without CA certs; point OpenSSL at certifi."""
    import os
    try:
        import certifi
        os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    except ImportError:
        pass


_use_certifi()


_ffmpeg_problem = ""


def find_ffmpeg():
    """Locate ffmpeg. Apps launched from Finder get a minimal PATH, so also look in
    the usual Homebrew locations and next to this script."""
    global _ffmpeg_problem
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    for cand in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
        if Path(cand).exists():
            return cand
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as e:
        _ffmpeg_problem = f"{type(e).__name__}: {e}"
    for cand in Path(__file__).resolve().parent.glob("venv/lib/python*/site-packages/imageio_ffmpeg/binaries/ffmpeg*"):
        if cand.is_file() and cand.suffix not in (".py", ".md", ".pyc"):
            return str(cand)
    return None


# --------------------------------------------------------------------------
# Download engine (GUI-independent; reports through emit(kind, **data))
# --------------------------------------------------------------------------
class _SilentLogger:
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


class Cancelled(Exception):
    pass


def run_job(url, outdir, bitrate, emit, cancel: threading.Event):
    """Download audio and convert to MP3. Emits: status, progress, log, done,
    error (permanent), cancelled."""
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        emit("error", message="ffmpeg not found. Install it (brew install ffmpeg) "
                              "or pip install imageio-ffmpeg."
                              + (f"\n\nDetails: {_ffmpeg_problem}" if _ffmpeg_problem else "")
                              + f"\nPython: {sys.executable}")
        return

    best = {"bytes": 0}

    def hook(d):
        if cancel.is_set():
            raise Cancelled()
        if d["status"] == "downloading":
            done = d.get("downloaded_bytes") or 0
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            best["bytes"] = max(best["bytes"], done)
            emit("progress", done=done, total=total, speed=d.get("speed"), eta=d.get("eta"))
        elif d["status"] == "finished":
            emit("status", text="Converting to MP3…")
            emit("progress", done=None, total=None)

    opts = {
        "format": "bestaudio/best",
        "outtmpl": str(Path(outdir) / "%(title).150B [%(id)s].%(ext)s"),
        "noplaylist": True,
        "continuedl": True,          # resume .part files after a dropped connection
        "retries": 3,                # fast in-process retries; ours handle the long haul
        "fragment_retries": 10,
        "extractor_retries": 3,
        "socket_timeout": 20,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "logger": _SilentLogger(),   # errors are surfaced by us, not printed to stderr
        "ffmpeg_location": ffmpeg,
        "progress_hooks": [hook],
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": str(bitrate),
        }],
    }

    failures = 0      # consecutive transient failures without progress
    unknowns = 0
    attempt = 0
    while True:
        attempt += 1
        before = best["bytes"]
        emit("status", text="Starting…" if attempt == 1 else f"Resuming (attempt {attempt})…")
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
            path = (info.get("requested_downloads") or [{}])[0].get("filepath")
            emit("done", path=path, title=info.get("title"))
            return
        except Cancelled:
            emit("cancelled")
            return
        except Exception as exc:  # yt_dlp.DownloadError and friends
            if cancel.is_set():
                emit("cancelled")
                return
            kind, text = classify(exc)

        if kind == "permanent":
            emit("error", message=text)
            return

        if kind == "unknown":
            unknowns += 1
            if unknowns > UNKNOWN_RETRIES:
                emit("error", message=text)
                return
        else:
            if best["bytes"] > before:
                failures = 0      # we made headway, so this isn't a stuck connection
            failures += 1
            if failures > MAX_FAILURES:
                emit("error", message=f"Network kept failing after {MAX_FAILURES} retries: {text}")
                return

        # Offline? Wait for the connection without burning retry budget.
        if not is_online():
            emit("log", level="warn", text="Network is down; waiting for it to come back…")
            while not is_online():
                if sleep_cancellable(5, cancel, lambda s: emit(
                        "status", text="Offline — waiting for connection…")):
                    emit("cancelled")
                    return
            failures = max(0, failures - 1)
            continue

        delay = min(BACKOFF_CAP, 2 ** min(max(failures, unknowns), 6)) + random.random()
        tries = f"{failures}/{MAX_FAILURES}" if kind == "transient" else f"{unknowns}/{UNKNOWN_RETRIES}"
        emit("log", level="warn",
             text=f"{'Network issue' if kind == 'transient' else 'Unexpected error'} "
                  f"({text[:120]}). Retrying in {delay:.0f}s [{tries}]")
        if sleep_cancellable(delay, cancel, lambda s: emit(
                "status", text=f"Network hiccup — retrying in {s:.0f}s "
                               f"({min(failures, MAX_FAILURES)}/{MAX_FAILURES})")):
            emit("cancelled")
            return


def sleep_cancellable(seconds, cancel, tick):
    """Sleep in short slices. Returns True if cancelled."""
    end = time.monotonic() + seconds
    while True:
        left = end - time.monotonic()
        if left <= 0:
            return False
        if cancel.is_set():
            return True
        tick(left)
        time.sleep(min(0.5, left))


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------
def fmt_bytes(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def fmt_eta(s):
    s = int(s)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {s:02d}s"


class App:
    def __init__(self, root):
        self.root = root
        root.title("YouTube MP3 Converter")
        root.minsize(560, 420)
        self.q = queue.Queue()
        self.cancel = threading.Event()
        self.worker = None
        self.last_path = None

        f = ttk.Frame(root, padding=14)
        f.pack(fill="both", expand=True)
        f.columnconfigure(1, weight=1)
        f.rowconfigure(5, weight=1)

        ttk.Label(f, text="Video URL").grid(row=0, column=0, sticky="w")
        self.url = ttk.Entry(f)
        self.url.grid(row=0, column=1, columnspan=2, sticky="ew", pady=3)
        self.url.focus()

        ttk.Label(f, text="Save to").grid(row=1, column=0, sticky="w")
        self.outdir = tk.StringVar(value=str(Path.home() / "Downloads"))
        ttk.Entry(f, textvariable=self.outdir).grid(row=1, column=1, sticky="ew", pady=3)
        ttk.Button(f, text="Browse…", command=self.browse).grid(row=1, column=2, padx=(6, 0))

        ttk.Label(f, text="Bitrate").grid(row=2, column=0, sticky="w")
        self.bitrate = ttk.Combobox(f, values=["128", "192", "256", "320"],
                                    state="readonly", width=6)
        self.bitrate.set("192")
        self.bitrate.grid(row=2, column=1, sticky="w", pady=3)

        bar = ttk.Frame(f)
        bar.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(8, 4))
        self.start_btn = ttk.Button(bar, text="Convert", command=self.start)
        self.start_btn.pack(side="left")
        self.cancel_btn = ttk.Button(bar, text="Cancel", command=self.cancel_job, state="disabled")
        self.cancel_btn.pack(side="left", padx=6)
        self.reveal_btn = ttk.Button(bar, text="Show in Finder", command=self.reveal, state="disabled")
        self.reveal_btn.pack(side="left")

        self.progress = ttk.Progressbar(f, mode="determinate", maximum=100)
        self.progress.grid(row=4, column=0, columnspan=3, sticky="ew")

        self.log = tk.Text(f, height=10, state="disabled", wrap="word",
                           font=("Menlo", 11), relief="flat", borderwidth=1)
        self.log.grid(row=5, column=0, columnspan=3, sticky="nsew", pady=(8, 4))
        self.log.tag_config("info", foreground="#444444")
        self.log.tag_config("warn", foreground="#b36b00")
        self.log.tag_config("error", foreground="#c0262d")
        self.log.tag_config("ok", foreground="#1b7f3b")

        self.status = tk.StringVar(value="Paste a YouTube URL and press Convert.")
        ttk.Label(f, textvariable=self.status).grid(row=6, column=0, columnspan=3, sticky="w")

        root.bind("<Return>", lambda e: self.start())
        self.root.after(100, self.pump)

    # -- actions ----------------------------------------------------------
    def browse(self):
        d = filedialog.askdirectory(initialdir=self.outdir.get())
        if d:
            self.outdir.set(d)

    def start(self):
        if self.worker and self.worker.is_alive():
            return
        url = self.url.get().strip()
        if not url:
            messagebox.showerror("Missing URL", "Paste a YouTube URL first.")
            return
        outdir = Path(self.outdir.get()).expanduser()
        try:
            outdir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            messagebox.showerror("Can't use that folder", str(e))
            return
        self.cancel.clear()
        self.last_path = None
        self.set_running(True)
        self.progress.configure(mode="determinate", value=0)
        self.write(f"Starting: {url}", "info")
        self.worker = threading.Thread(
            target=run_job,
            args=(url, outdir, self.bitrate.get(), lambda k, **d: self.q.put((k, d)), self.cancel),
            daemon=True)
        self.worker.start()

    def cancel_job(self):
        self.cancel.set()
        self.status.set("Cancelling…")

    def reveal(self):
        if self.last_path:
            subprocess.run(["open", "-R", self.last_path])

    # -- UI plumbing ------------------------------------------------------
    def set_running(self, running):
        self.start_btn.configure(state="disabled" if running else "normal")
        self.cancel_btn.configure(state="normal" if running else "disabled")
        self.reveal_btn.configure(state="disabled")

    def write(self, text, tag="info"):
        self.log.configure(state="normal")
        self.log.insert("end", time.strftime("%H:%M:%S ") + text + "\n", tag)
        self.log.see("end")
        self.log.configure(state="disabled")

    def pump(self):
        try:
            while True:
                kind, d = self.q.get_nowait()
                self.handle(kind, d)
        except queue.Empty:
            pass
        self.root.after(100, self.pump)

    def handle(self, kind, d):
        if kind == "status":
            self.status.set(d["text"])
        elif kind == "log":
            self.write(d["text"], d.get("level", "info"))
        elif kind == "progress":
            if d["done"] is None:
                self.progress.configure(mode="indeterminate")
                self.progress.start(12)
                return
            self.progress.stop()
            self.progress.configure(mode="determinate")
            if d["total"]:
                self.progress["value"] = 100 * d["done"] / d["total"]
                msg = f"Downloading {fmt_bytes(d['done'])} / {fmt_bytes(d['total'])}"
            else:
                msg = f"Downloading {fmt_bytes(d['done'])}"
            if d.get("speed"):
                msg += f"  ·  {fmt_bytes(d['speed'])}/s"
            if d.get("eta"):
                msg += f"  ·  {fmt_eta(d['eta'])} left"
            self.status.set(msg)
        elif kind == "done":
            self.finish()
            self.progress.stop()
            self.progress.configure(mode="determinate", value=100)
            self.last_path = d.get("path")
            self.status.set("Done.")
            self.write(f"Saved: {self.last_path}", "ok")
            self.reveal_btn.configure(state="normal" if self.last_path else "disabled")
        elif kind == "cancelled":
            self.finish()
            self.progress.stop()
            self.progress.configure(mode="determinate", value=0)
            self.status.set("Cancelled.")
            self.write("Cancelled.", "warn")
        elif kind == "error":
            self.finish()
            self.progress.stop()
            self.progress.configure(mode="determinate", value=0)
            self.status.set("Failed.")
            self.write(d["message"], "error")
            messagebox.showerror("Couldn't convert this video", d["message"])

    def finish(self):
        self.start_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")


def main():
    import platform
    print(f"[startup] arch={platform.machine()} python={sys.executable} ffmpeg={find_ffmpeg()} {_ffmpeg_problem}",
          file=sys.stderr, flush=True)
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
