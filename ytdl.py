import os
import re
import sys
import platform
import time
import queue
import tempfile
import threading
import subprocess
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import traceback

# Keeps yt-dlp from flashing a console window. Only exists on Windows.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def get_bundle_dir():
    """The folder holding the bundled tools (yt-dlp, ffmpeg, ffprobe, deno),
    handling both development and PyInstaller bundle"""
    if getattr(sys, 'frozen', False):
        # Running as PyInstaller bundle
        return sys._MEIPASS
    # Running as script
    return os.path.dirname(os.path.abspath(__file__))


def get_yt_dlp_path():
    """Get the path to yt-dlp.exe"""
    return os.path.join(get_bundle_dir(), "yt-dlp.exe")


# --- working out exactly which address a content filter rejected ---------------
#
# When a filter blocks us, yt-dlp says what it was doing ("Unable to download API
# page") but not which address it was talking to. These two helpers re-run the
# request with traffic logging turned on so we can name the exact URL, which is
# what you need in order to ask the filter's operators to allow it.

_SEND_RE = re.compile(r"^send: b['\"](.*)['\"]\s*$")
_REPLY_RE = re.compile(r"^reply: ['\"]HTTP/[\d.]+ (\d{3})")

# Headers worth keeping out of a log that gets handed to someone else.
_SECRET_HEADER_RE = re.compile(r"(?i)\\r\\n(cookie|authorization|x-goog-[a-z-]*auth[a-z-]*):[^\\]*")


def parse_traffic(text):
    """Turn yt-dlp --print-traffic output into a list of (method, url, status).

    The log is http.client's debug output: a 'send:' line holding the raw request,
    followed by a 'reply:' line holding the status. CONNECT lines are the proxy
    handshake rather than real requests, so they get skipped."""
    requests, pending = [], None
    for line in text.splitlines():
        line = line.strip()

        match = _SEND_RE.match(line)
        if match:
            parts = match.group(1).split("\\r\\n")
            bits = parts[0].split(" ")
            if len(bits) < 2 or bits[0] == "CONNECT":
                pending = None
                continue
            host = ""
            for header in parts[1:]:
                if header.lower().startswith("host:"):
                    host = header.split(":", 1)[1].strip()
                    break
            pending = (bits[0], "https://{}{}".format(host, bits[1]) if host else bits[1])
            continue

        match = _REPLY_RE.match(line)
        if match and pending:
            requests.append((pending[0], pending[1], int(match.group(1))))
            pending = None

    return requests


def diagnose(url, log_dir):
    """Re-run the extraction with traffic logging and report what got rejected.

    Returns (summary_text, log_path). --simulate means nothing is downloaded; we
    only need to get far enough to hit the request that failed."""
    cmd = [
        get_yt_dlp_path(),
        "--no-check-certificate",
        "--simulate",
        "--print-traffic",
        "--socket-timeout", "20",
        url,
    ]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120,
            creationflags=NO_WINDOW,
        )
    except Exception as error:
        return "Could not run the connection check: {}".format(error), None

    output = (result.stdout or "") + (result.stderr or "")
    requests = parse_traffic(output)
    rejected = [r for r in requests if r[2] >= 400]

    # Save the full log so it can be shown to whoever runs the filter.
    log_path = None
    try:
        log_path = os.path.join(log_dir, "ytdl-diagnostic.log")
        with open(log_path, "w", encoding="utf-8", errors="replace") as handle:
            handle.write("Diagnostic for: {}\n\n".format(url))
            handle.write("Requests made ({} total):\n".format(len(requests)))
            for method, address, status in requests:
                handle.write("  {}  {:6} {}\n".format(status, method, address))
            handle.write("\n--- full traffic log (cookies removed) ---\n")
            handle.write(_SECRET_HEADER_RE.sub(r"\\r\\n\1: [removed]", output))
    except Exception:
        log_path = None

    if not requests:
        return "The connection check produced no traffic to inspect.", log_path

    if not rejected:
        return (
            "The connection check completed {} request(s) and none were rejected, "
            "so the problem may be intermittent - try again.".format(len(requests))
        ), log_path

    lines = ["These addresses were rejected:", ""]
    for method, address, status in rejected:
        lines.append("  HTTP {}  {} {}".format(status, method, address))
    lines.append("")
    lines.append("The video itself may well be permitted - it is these addresses")
    lines.append("that need to be allowed for downloading to work.")
    return "\n".join(lines), log_path


# --- what to download ---------------------------------------------------------
#
# YouTube serves exactly one file with video and sound already together, and it
# is 360p. Everything sharper arrives as a separate video stream and audio stream
# that ffmpeg has to join afterwards. So "better" costs twice over: a much bigger
# download, then a merge step. Each choice says what it costs, so people can pick.
#
#   height<=?N   the "?" stops the filter erroring on formats with no height
#   bv*+ba/b     last-resort catch-all. A bare "/b" is not enough: "b" only matches
#                the pre-joined file, so a video offered only above the limit and
#                only as separate streams would fail with "Requested format is
#                not available"
QUALITIES = {
    "quick": {
        "label": "Quick",
        "detail": "360p",
        "speed": "Fastest",
        "hint": "Smallest file and quickest download. Fine on a phone or in a small window.",
        "format": "b[height<=?360]/bv*[height<=?360]+ba/bv*+ba/b",
    },
    "hd": {
        "label": "HD",
        "detail": "1080p",
        "speed": "Slower",
        "hint": "Sharp on a laptop or TV. Typically about 5x the size of Quick, "
                "so expect it to take around 5x as long.",
        "format": "bv*[height<=?1080]+ba/b[height<=?1080]/bv*+ba/b",
    },
    "best": {
        "label": "Best",
        "detail": "up to 4K",
        "speed": "Slowest",
        "hint": "The highest quality the video has. 4K can be 20x the size of Quick "
                "or more - a long video can take a long time. May be saved as .mkv.",
        "format": "bv*+ba/b",
    },
}
QUALITY_ORDER = ("quick", "hd", "best")
DEFAULT_QUALITY = "hd"

# Highest resolution first, then h264 in an mp4 wherever YouTube offers one, so
# the file plays on anything. Above 1080p YouTube only has VP9/AV1, which do not
# always fit an mp4 - hence mkv as the fallback container.
FORMAT_SORT = "res,fps,vcodec:h264,ext:mp4:m4a"


# --- file names Windows can live with ----------------------------------------
#
# Windows forbids \ / : * ? " < > | in file names, and YouTube titles are full of
# them. Left alone, yt-dlp swaps each one for a "full-width" look-alike (：＂？｜),
# and those were what broke highlighting the finished file: yt-dlp reports paths
# through the console's code page, which has no full-width characters, so they
# were silently dropped and the reported path did not exist. Replacing them with
# plain characters up front gives tidier names, and the path is now read back as
# UTF-8 as well (see build_command), which also covers emoji, Hebrew, and so on.
# Applied in order, to the title only, before the file name is worked out.
TITLE_REPLACEMENTS = (
    (r"(?<=\d):(?=\d)", "."),          # 10:30 -> 10.30
    (r"\s*[:|/\\]+\s*", " - "),        # "Part 1: Intro" -> "Part 1 - Intro"
    (r'"', "'"),
    (r"[<>*?]", ""),
    (r"^[\s.-]+|[\s.-]+$", ""),        # no leading/trailing junk (Windows hates trailing dots)
)

MEDIA_EXTENSIONS = (".mp4", ".mkv", ".webm", ".mp3", ".m4a")


# --- progress reporting ------------------------------------------------------
#
# yt-dlp is told to print one machine-readable line per progress update, which
# the worker thread turns into numbers for the progress bar. "NA" means yt-dlp
# does not know that value (yet).
PROGRESS_TAG = "[ytdl-progress]"
POST_TAG = "[ytdl-post]"
PROGRESS_TEMPLATE = (
    "download:" + PROGRESS_TAG +
    " %(progress.downloaded_bytes)s %(progress.total_bytes)s"
    " %(progress.total_bytes_estimate)s %(progress.speed)s %(progress.eta)s"
    " %(info.vcodec)s %(info.acodec)s"
)
POST_TEMPLATE = "postprocess:" + POST_TAG + " %(progress.status)s %(progress.postprocessor)s"

POSTPROCESSOR_STAGES = {
    "Merger": "Joining video and sound...",
    "ExtractAudio": "Converting to MP3...",
}


def build_command(url, download_dir, media_type, quality, path_file, verbose=False):
    """Build the yt-dlp command line for a download"""
    cmd = [
        get_yt_dlp_path(),
        "--no-check-certificate",
        # ffmpeg.exe sits next to yt-dlp.exe in both the script and the bundle.
        # Merging makes it essential for video, not just for mp3, so point at it
        # explicitly rather than relying on PATH.
        "--ffmpeg-location", get_bundle_dir(),
        "-o", os.path.join(download_dir, "%(title)s.%(ext)s"),
        # Report the finished file's path into path_file rather than stdout.
        # yt-dlp writes stdout in the console code page with errors="ignore",
        # which silently deletes any character that code page lacks, so the path
        # we got back did not exist. --print-to-file always writes UTF-8.
        # "after_move" is after post-processing, so this is the final file.
        "--print-to-file", "after_move:filepath", path_file,
        "--newline",
        "--progress-delta", "0.5",
        "--progress-template", PROGRESS_TEMPLATE,
        "--progress-template", POST_TEMPLATE,
    ]

    for pattern, replacement in TITLE_REPLACEMENTS:
        cmd.extend(["--replace-in-metadata", "title", pattern, replacement])

    if media_type == "audio":
        cmd.extend([
            "--extract-audio",
            "--audio-format", "mp3",
            "--audio-quality", "0",  # best quality
        ])
    else:
        cmd.extend([
            "--format", QUALITIES[quality]["format"],
            "--format-sort", FORMAT_SORT,
            "--merge-output-format", "mp4/mkv",
        ])

    if verbose:
        # Versions, the exact options in effect and each step yt-dlp takes - the
        # detail needed to work out why a download failed
        cmd.append("--verbose")

    # "--" so a pasted link can never be mistaken for an option
    cmd.extend(["--", url])
    return cmd


def _number(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def parse_progress(line):
    """Turn one of our progress lines into a dict, or None if it isn't one"""
    if line.startswith(PROGRESS_TAG):
        parts = line[len(PROGRESS_TAG):].split()
        if len(parts) < 7:
            return None
        done, total, estimate, speed, eta = (_number(p) for p in parts[:5])
        vcodec, acodec = parts[5], parts[6]
        if vcodec == "none":
            stream = "sound"
        elif acodec == "none":
            stream = "video"
        else:
            stream = "file"
        return {"kind": "download", "done": done, "total": total or estimate,
                "speed": speed, "eta": eta, "stream": stream}

    if line.startswith(POST_TAG):
        parts = line[len(POST_TAG):].split()
        if len(parts) < 2:
            return None
        return {"kind": "post", "status": parts[0], "name": parts[1]}

    return None


def format_size(num_bytes):
    if num_bytes is None:
        return "?"
    for unit in ("bytes", "KB", "MB", "GB"):
        if num_bytes < 1024 or unit == "GB":
            return "{:.0f} {}".format(num_bytes, unit) if unit == "bytes" else "{:.1f} {}".format(num_bytes, unit)
        num_bytes /= 1024.0


def format_duration(seconds):
    seconds = int(max(0, seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return "{}:{:02d}:{:02d}".format(hours, minutes, secs)
    return "{}:{:02d}".format(minutes, secs)


# yt-dlp ends most errors with a request to file a bug report, which is noise here
_REPORT_RE = re.compile(r";?\s*please report this issue on .*$", re.IGNORECASE)


def explain_failure(output):
    """Pull the lines that say what went wrong out of yt-dlp's output"""
    errors, warnings = [], []
    for line in output.splitlines():
        if line.startswith("ERROR:"):
            errors.append(_REPORT_RE.sub("", line[len("ERROR:"):]).strip())
        elif line.startswith("WARNING:"):
            warnings.append(_REPORT_RE.sub("", line[len("WARNING:"):]).strip())
    return errors, warnings


def looks_blocked(error_text):
    """Does this failure look like something refused a request, rather than a bad URL?"""
    text = (error_text or "").lower()
    signals = ("http error 4", "http error 5", "blocked", "forbidden",
               "unable to download api page", "failed to resolve", "connection")
    return any(signal in text for signal in signals)


def find_downloaded_file(reported_path, download_dir, started_at):
    """The finished file's path. Uses what yt-dlp reported, and failing that the
    newest media file that appeared in the folder during this download."""
    if reported_path and os.path.exists(reported_path):
        return reported_path

    newest, newest_time = None, started_at - 2
    try:
        for name in os.listdir(download_dir):
            if not name.lower().endswith(MEDIA_EXTENSIONS):
                continue
            path = os.path.join(download_dir, name)
            modified = os.path.getmtime(path)
            if modified >= newest_time:
                newest, newest_time = path, modified
    except OSError:
        return None
    return newest


# Leftovers of an unfinished download: partial files, yt-dlp's resume notes, a
# half-finished merge ("Title.temp.mp4"), and separate video/audio streams
# ("Title.f137.mp4") that never got joined.
_LEFTOVER_RE = re.compile(r"(\.part|\.ytdl|\.temp\.\w+|\.f\d+\.\w+)$", re.IGNORECASE)


def remove_leftovers(download_dir, started_at):
    """Delete partial files a cancelled download left in the folder. Only files
    that look like yt-dlp's own leftovers and changed during this download."""
    try:
        names = os.listdir(download_dir)
    except OSError:
        return
    for name in names:
        path = os.path.join(download_dir, name)
        try:
            if _LEFTOVER_RE.search(name) and os.path.getmtime(path) >= started_at - 2:
                os.remove(path)
        except OSError:
            pass


def kill_process_tree(process):
    """Stop yt-dlp and anything it started. yt-dlp.exe is itself a bundled exe
    that runs a child copy of itself (and ffmpeg), so killing only the process we
    started would leave the real work running."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)],
                       capture_output=True, creationflags=NO_WINDOW)
    else:
        process.kill()


# --- the download, on a background thread ------------------------------------

class Download:
    """Runs one yt-dlp download off the main thread. Never touches tkinter: it
    puts messages on a queue that the window reads (see App.poll)."""

    def __init__(self, url, download_dir, media_type, quality, full_errors):
        self.url = url
        self.download_dir = download_dir
        self.media_type = media_type
        self.quality = quality
        self.full_errors = full_errors
        self.messages = queue.Queue()
        self.process = None
        self.cancelled = False
        self.started_at = time.time()
        self.log = []  # everything yt-dlp printed, apart from progress lines

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def cancel(self):
        self.cancelled = True
        if self.process and self.process.poll() is None:
            try:
                kill_process_tree(self.process)
            except Exception:
                pass

    def _run(self):
        path_file = None
        try:
            handle, path_file = tempfile.mkstemp(prefix="ytdl-path-", suffix=".txt")
            os.close(handle)

            cmd = build_command(self.url, self.download_dir, self.media_type,
                                self.quality, path_file, verbose=self.full_errors)
            self.process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, text=True, errors="replace",
                creationflags=NO_WINDOW,
            )

            for line in self.process.stdout:
                line = line.rstrip()
                update = parse_progress(line)
                if update:
                    self.messages.put(("progress", update))
                elif line:
                    self.log.append(line)
            self.process.wait()

            if self.cancelled:
                remove_leftovers(self.download_dir, self.started_at)
                self.messages.put(("cancelled", None))
                return

            if self.process.returncode != 0:
                self.messages.put(("error", self._failure_report()))
                return

            reported = None
            try:
                with open(path_file, encoding="utf-8") as report:
                    lines = [entry.strip() for entry in report if entry.strip()]
                reported = lines[-1] if lines else None
            except OSError:
                pass

            found = find_downloaded_file(reported, self.download_dir, self.started_at)
            self.messages.put(("done", found))

        except Exception as error:
            if self.cancelled:
                self.messages.put(("cancelled", None))
            else:
                summary = "Something went wrong in the program itself:\n{}".format(error)
                self.messages.put(("error", {
                    "summary": summary,
                    "details": self._details([], [], crash=traceback.format_exc()),
                }))

        finally:
            if path_file:
                try:
                    os.remove(path_file)
                except OSError:
                    pass  # nothing worth bothering the user about

    def _failure_report(self):
        """What to tell the user about a failed download: a plain summary, and
        the details someone would need to work out why"""
        output = "\n".join(self.log)
        errors, warnings = explain_failure(output)
        summary = (
            "Video download failed.\n\n"
            "Is the url correct?\n"
            "Is the video allowed by your filtering settings?\n\n"
            "Still not working?\nTry downloading the latest version from tinyurl.com/youtubekosherdl\n\n"
            "Still not working?\nlet whoever gave you this program know."
        )
        if errors:
            summary += "\n\nWhat yt-dlp said:\n" + "\n".join(errors)

        # A rejected request means something in between us and YouTube said no.
        # Find out which address it was, so it can be reported or allowed.
        if looks_blocked("\n".join(errors) or output):
            self.messages.put(("progress", {"kind": "checking"}))
            diagnosis, log_path = diagnose(self.url, self.download_dir)
            summary += "\n\n" + diagnosis
            if log_path:
                summary += "\n\nFull details saved to:\n{}".format(log_path)

        return {"summary": summary, "details": self._details(errors, warnings)}

    def _details(self, errors, warnings, crash=None):
        """A plain-text report that can be copied and sent to someone"""
        quality = "audio (MP3)" if self.media_type == "audio" else "video, {} ({})".format(
            QUALITIES[self.quality]["label"], QUALITIES[self.quality]["detail"])
        lines = ["What went wrong:"]
        if errors:
            lines += ["  " + error for error in errors]
        elif crash:
            lines += ["  " + crash.strip().splitlines()[-1]]
        else:
            exit_code = self.process.returncode if self.process else "?"
            lines += ["  yt-dlp stopped with exit code {} without saying why".format(exit_code)]
        if warnings:
            lines += ["", "Warnings before that:"] + ["  " + warning for warning in warnings]

        lines += [
            "",
            "Link:       {}".format(self.url),
            "Saving to:  {}".format(self.download_dir),
            "Download:   {}".format(quality),
            "Computer:   {}".format(platform.platform()),
            "Program:    {}".format("ytdl.exe" if getattr(sys, "frozen", False) else "ytdl.py"),
        ]
        if not self.full_errors:
            lines += ["", "(Tick \"Show full error details\" and try again for yt-dlp's full log.)"]
        if self.log:
            lines += ["", "--- everything yt-dlp printed ---"] + self.log
        if crash:
            lines += ["", "--- program error ---", crash.rstrip()]
        return "\n".join(lines)


# --- the window ---------------------------------------------------------------

COLORS = {
    "bg": "#f3f4f6",
    "card": "#ffffff",
    "text": "#111827",
    "muted": "#6b7280",
    "border": "#e5e7eb",
    "field": "#f9fafb",
    "accent": "#dc2626",
    "accent_hover": "#b91c1c",
    "accent_pressed": "#991b1b",
    "accent_soft": "#fee2e2",
    "disabled": "#d1d5db",
    "fast": "#15803d",
    "slower": "#b45309",
    "slowest": "#b91c1c",
}
SPEED_COLORS = {"Fastest": COLORS["fast"], "Slower": COLORS["slower"], "Slowest": COLORS["slowest"]}


def pick_font_family(root):
    from tkinter import font
    available = set(font.families(root))
    for family in ("Segoe UI Variable Text", "Segoe UI", "Helvetica Neue", "Inter", "DejaVu Sans"):
        if family in available:
            return family
    return "TkDefaultFont"


def apply_style(root):
    """A flat, modern look on top of ttk's 'clam' theme, which (unlike the
    native Windows theme) lets every colour be set."""
    family = pick_font_family(root)
    c = COLORS
    style = ttk.Style(root)
    style.theme_use("clam")
    root.configure(bg=c["bg"])

    style.configure(".", font=(family, 10), background=c["card"], foreground=c["text"],
                    bordercolor=c["border"], focuscolor=c["accent"])
    style.configure("App.TFrame", background=c["bg"])
    style.configure("Card.TFrame", background=c["card"], relief="solid", borderwidth=1)
    style.configure("TLabel", background=c["card"])
    style.configure("Title.TLabel", background=c["bg"], font=(family, 18, "bold"))
    style.configure("Subtitle.TLabel", background=c["bg"], foreground=c["muted"])
    style.configure("Section.TLabel", font=(family, 10, "bold"))
    style.configure("Hint.TLabel", foreground=c["muted"], font=(family, 9))
    style.configure("Status.TLabel", font=(family, 10, "bold"))
    style.configure("Speed.TLabel", font=(family, 9, "bold"))

    style.configure("TEntry", padding=(10, 8), fieldbackground=c["field"],
                    bordercolor=c["border"], lightcolor=c["border"], darkcolor=c["border"])
    style.map("TEntry", bordercolor=[("focus", c["accent"])], lightcolor=[("focus", c["accent"])])

    # Segmented buttons: radio buttons drawn without the dot
    style.layout("Segment.TRadiobutton", [
        ("Radiobutton.padding", {"sticky": "nswe", "children": [
            ("Radiobutton.label", {"sticky": "nswe"})]})])
    style.configure("Segment.TRadiobutton", padding=(14, 8), anchor="center",
                    background=c["field"], foreground=c["text"], font=(family, 10))
    style.map("Segment.TRadiobutton",
              background=[("disabled", c["field"]), ("selected", c["accent_soft"]), ("active", c["border"])],
              foreground=[("disabled", c["disabled"]), ("selected", c["accent_pressed"])],
              font=[("selected", (family, 10, "bold"))])

    style.configure("Accent.TButton", padding=(16, 10), font=(family, 11, "bold"),
                    background=c["accent"], foreground="#ffffff", borderwidth=0,
                    bordercolor=c["accent"], lightcolor=c["accent"], darkcolor=c["accent"])
    style.map("Accent.TButton",
              background=[("disabled", c["disabled"]), ("pressed", c["accent_pressed"]), ("active", c["accent_hover"])],
              lightcolor=[("disabled", c["disabled"]), ("pressed", c["accent_pressed"]), ("active", c["accent_hover"])],
              darkcolor=[("disabled", c["disabled"]), ("pressed", c["accent_pressed"]), ("active", c["accent_hover"])],
              bordercolor=[("disabled", c["disabled"])])

    style.configure("Quiet.TButton", padding=(10, 6), background=c["field"],
                    bordercolor=c["border"], lightcolor=c["field"], darkcolor=c["field"])
    style.map("Quiet.TButton", background=[("active", c["border"])])

    style.configure("TCheckbutton", background=c["bg"], foreground=c["muted"], font=(family, 9))
    style.map("TCheckbutton", background=[("active", c["bg"])])

    style.configure("Accent.Horizontal.TProgressbar", thickness=8, background=c["accent"],
                    troughcolor=c["border"], bordercolor=c["border"],
                    lightcolor=c["accent"], darkcolor=c["accent"])


class App:
    def __init__(self, root):
        self.root = root
        self.download = None
        self.last_dir = None
        self.stage = ""
        self.detail = ""

        root.title("YouTube Downloader")
        root.minsize(520, 0)
        apply_style(root)

        self.url = tk.StringVar()
        self.media_type = tk.StringVar(value="video")
        self.quality = tk.StringVar(value=DEFAULT_QUALITY)
        self.show_full_errors = tk.BooleanVar(value=False)

        outer = ttk.Frame(root, style="App.TFrame", padding=24)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1, minsize=500)

        ttk.Label(outer, text="YouTube Downloader", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(outer, text="Paste a link, choose what you want, and pick where to save it.",
                  style="Subtitle.TLabel").grid(row=1, column=0, sticky="w", pady=(2, 16))

        card = ttk.Frame(outer, style="Card.TFrame", padding=20)
        card.grid(row=2, column=0, sticky="nsew")
        card.columnconfigure(0, weight=1)

        # Link
        ttk.Label(card, text="Video link", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        link_row = ttk.Frame(card)
        link_row.grid(row=1, column=0, sticky="we", pady=(6, 18))
        link_row.columnconfigure(0, weight=1)
        self.url_entry = ttk.Entry(link_row, textvariable=self.url)
        self.url_entry.grid(row=0, column=0, sticky="we")
        ttk.Button(link_row, text="Paste", style="Quiet.TButton",
                   command=self.paste).grid(row=0, column=1, padx=(8, 0), sticky="ns")

        # Video or audio
        ttk.Label(card, text="Save as", style="Section.TLabel").grid(row=2, column=0, sticky="w")
        type_row = ttk.Frame(card)
        type_row.grid(row=3, column=0, sticky="we", pady=(6, 18))
        for column, (value, text) in enumerate((("video", "Video  (MP4)"), ("audio", "Audio only  (MP3)"))):
            type_row.columnconfigure(column, weight=1, uniform="type")
            ttk.Radiobutton(type_row, text=text, value=value, variable=self.media_type,
                            style="Segment.TRadiobutton", command=self.update_quality_hint
                            ).grid(row=0, column=column, sticky="we", padx=(0 if column == 0 else 4, 0))

        # Quality, with what each choice costs in time
        ttk.Label(card, text="Video quality", style="Section.TLabel").grid(row=4, column=0, sticky="w")
        quality_row = ttk.Frame(card)
        quality_row.grid(row=5, column=0, sticky="we", pady=(6, 8))
        self.quality_buttons = []
        for column, key in enumerate(QUALITY_ORDER):
            info = QUALITIES[key]
            quality_row.columnconfigure(column, weight=1, uniform="quality")
            button = ttk.Radiobutton(quality_row, text="{}  ·  {}".format(info["label"], info["detail"]),
                                     value=key, variable=self.quality, style="Segment.TRadiobutton",
                                     command=self.update_quality_hint)
            button.grid(row=0, column=column, sticky="we", padx=(0 if column == 0 else 4, 0))
            self.quality_buttons.append(button)

        hint_row = ttk.Frame(card)
        hint_row.grid(row=6, column=0, sticky="we", pady=(0, 20))
        hint_row.columnconfigure(1, weight=1)
        self.speed_label = ttk.Label(hint_row, style="Speed.TLabel")
        self.speed_label.grid(row=0, column=0, sticky="nw", padx=(0, 8))
        self.hint_label = ttk.Label(hint_row, style="Hint.TLabel", wraplength=380, justify="left")
        self.hint_label.grid(row=0, column=1, sticky="we")

        self.download_button = ttk.Button(card, text="Download", style="Accent.TButton",
                                          command=self.start_download)
        self.download_button.grid(row=7, column=0, sticky="we")

        # Progress, shown while a download runs
        self.progress_frame = ttk.Frame(card)
        self.progress_frame.grid(row=8, column=0, sticky="we", pady=(18, 0))
        self.progress_frame.columnconfigure(0, weight=1)
        self.status_label = ttk.Label(self.progress_frame, style="Status.TLabel")
        self.status_label.grid(row=0, column=0, sticky="w")
        self.cancel_button = ttk.Button(self.progress_frame, text="Cancel", style="Quiet.TButton",
                                        command=self.cancel_download)
        self.cancel_button.grid(row=0, column=1, sticky="e")
        self.progress_bar = ttk.Progressbar(self.progress_frame, style="Accent.Horizontal.TProgressbar",
                                            maximum=100)
        self.progress_bar.grid(row=1, column=0, columnspan=2, sticky="we", pady=(8, 6))
        self.detail_label = ttk.Label(self.progress_frame, style="Hint.TLabel")
        self.detail_label.grid(row=2, column=0, columnspan=2, sticky="w")
        self.progress_frame.grid_remove()

        # A result line that stays after the progress area goes away
        self.result_label = ttk.Label(card, style="Hint.TLabel")
        self.result_label.grid(row=9, column=0, sticky="w", pady=(12, 0))
        self.result_label.grid_remove()

        ttk.Checkbutton(outer, text="Show full error details", variable=self.show_full_errors
                        ).grid(row=3, column=0, sticky="w", pady=(12, 0))

        root.bind("<Return>", lambda event: self.start_download())
        self.update_quality_hint()
        self.url_entry.focus_set()

    # --- controls ---

    def paste(self):
        try:
            text = self.root.clipboard_get().strip()
        except tk.TclError:
            return
        self.url.set(text)
        self.url_entry.icursor("end")

    def update_quality_hint(self):
        if self.media_type.get() == "audio":
            for button in self.quality_buttons:
                button.state(["disabled"])
            self.speed_label.config(text="")
            self.hint_label.config(text="Audio is always saved as the best-quality MP3.")
            return

        for button in self.quality_buttons:
            button.state(["!disabled"])
        info = QUALITIES[self.quality.get()]
        self.speed_label.config(text=info["speed"], foreground=SPEED_COLORS[info["speed"]])
        self.hint_label.config(text=info["hint"])

    def set_busy(self, busy):
        state = ["disabled"] if busy else ["!disabled"]
        self.download_button.state(state)
        self.url_entry.state(state)
        if busy:
            self.result_label.grid_remove()
            self.progress_frame.grid()
            self.cancel_button.state(["!disabled"])
        else:
            self.progress_bar.stop()
            self.progress_frame.grid_remove()

    # --- download lifecycle ---

    def start_download(self):
        if self.download:
            return
        url = self.url.get().strip()
        if not url:
            messagebox.showerror("Error", "Please enter a YouTube URL")
            return

        download_dir = filedialog.askdirectory(title="Choose download folder",
                                               initialdir=self.last_dir or None)
        if not download_dir:
            messagebox.showerror("Error", "No folder chosen")
            return
        self.last_dir = download_dir

        self.download = Download(url, download_dir, self.media_type.get(),
                                 self.quality.get(), self.show_full_errors.get())
        self.set_busy(True)
        self.show_stage("Starting...", indeterminate=True)
        self.detail = "Looking up the video"
        self.detail_label.config(text=self.detail)
        self.download.start()
        self.poll()

    def cancel_download(self):
        if self.download:
            self.cancel_button.state(["disabled"])
            self.show_stage("Cancelling...", indeterminate=True)
            self.download.cancel()

    def show_stage(self, text, indeterminate=False):
        self.stage = text
        self.status_label.config(text=text)
        if indeterminate:
            if str(self.progress_bar.cget("mode")) != "indeterminate":
                self.progress_bar.config(mode="indeterminate", value=0)
                self.progress_bar.start(12)
        elif str(self.progress_bar.cget("mode")) != "determinate":
            self.progress_bar.stop()
            self.progress_bar.config(mode="determinate")

    def poll(self):
        """Read what the worker has reported, then check again shortly"""
        download = self.download
        if not download:
            return
        elapsed = format_duration(time.time() - download.started_at)

        latest = None
        while True:
            try:
                kind, payload = download.messages.get_nowait()
            except queue.Empty:
                break
            if kind == "progress":
                latest = payload
            else:
                self.finish(kind, payload, elapsed)
                return

        if latest and not download.cancelled:
            self.show_progress(latest, download.media_type)
        bits = [self.detail] if self.detail else []
        self.detail_label.config(text="  ·  ".join(bits + ["{} elapsed".format(elapsed)]))
        self.root.after(150, self.poll)

    def show_progress(self, update, media_type):
        self.detail = ""
        if update["kind"] == "checking":
            self.show_stage("Checking which addresses are blocked...", indeterminate=True)
        elif update["kind"] == "post":
            stage = POSTPROCESSOR_STAGES.get(update["name"])
            if stage and update["status"] != "finished":
                self.show_stage(stage, indeterminate=True)
            elif stage or self.stage.startswith("Downloading"):
                self.show_stage("Finishing up...", indeterminate=True)
        else:
            what = "audio" if media_type == "audio" else update["stream"]
            done, total = update["done"], update["total"]
            percent = (done / total * 100) if done is not None and total else None
            self.show_stage("Downloading {}{}".format(
                what, "  {:.0f}%".format(percent) if percent is not None else "..."),
                indeterminate=percent is None)
            if percent is not None:
                self.progress_bar.config(value=min(percent, 100))
            bits = []
            if done is not None:
                bits.append("{} of {}".format(format_size(done), format_size(total)) if total else format_size(done))
            if update["speed"]:
                bits.append("{}/s".format(format_size(update["speed"])))
            if update["eta"] is not None:
                bits.append("about {} left".format(format_duration(update["eta"])))
            self.detail = "  ·  ".join(bits)

    def finish(self, kind, payload, elapsed):
        download, self.download = self.download, None
        self.set_busy(False)

        if kind == "cancelled":
            self.show_result("Download cancelled.")
        elif kind == "error":
            ErrorDialog(self.root, payload["summary"], payload["details"], self.show_full_errors.get())
        else:
            name = os.path.basename(payload) if payload else "your file"
            self.show_result("Done in {}  -  saved {}".format(elapsed, name))
            show_in_folder(payload, download.download_dir)

    def show_result(self, text):
        self.result_label.config(text=text)
        self.result_label.grid()


class ErrorDialog:
    """Says why a download failed, in a window whose text can be selected and
    copied - unlike a message box - so it can be sent to whoever can help."""

    def __init__(self, parent, summary, details, show_details):
        self.summary, self.details = summary, details
        family = pick_font_family(parent)
        window = self.window = tk.Toplevel(parent, bg=COLORS["bg"])
        window.title("Download failed")
        window.transient(parent)
        window.minsize(560, 0)

        frame = ttk.Frame(window, style="App.TFrame", padding=20)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)

        ttk.Label(frame, text="Download failed", style="Title.TLabel",
                  foreground=COLORS["accent"]).grid(row=0, column=0, sticky="w", pady=(0, 10))

        summary_box = self.text_box(frame, summary, (family, 10), height=summary.count("\n") + 2)
        summary_box.grid(row=1, column=0, sticky="we")

        row = 2
        if show_details:
            frame.rowconfigure(3, weight=1)
            ttk.Label(frame, text="Details", style="Subtitle.TLabel").grid(
                row=2, column=0, sticky="w", pady=(14, 4))
            holder = ttk.Frame(frame, style="App.TFrame")
            holder.grid(row=3, column=0, sticky="nsew")
            holder.columnconfigure(0, weight=1)
            holder.rowconfigure(0, weight=1)
            details_box = self.text_box(holder, details, ("Consolas", 9), height=16, wrap="char")
            details_box.grid(row=0, column=0, sticky="nsew")
            bar = ttk.Scrollbar(holder, orient="vertical", command=details_box.yview)
            bar.grid(row=0, column=1, sticky="ns")
            details_box.configure(yscrollcommand=bar.set)
            row = 4

        buttons = ttk.Frame(frame, style="App.TFrame")
        buttons.grid(row=row, column=0, sticky="we", pady=(14, 0))
        buttons.columnconfigure(0, weight=1)
        self.copied = ttk.Label(buttons, style="Subtitle.TLabel")
        self.copied.grid(row=0, column=0, sticky="w")
        ttk.Button(buttons, text="Copy everything", style="Quiet.TButton",
                   command=self.copy).grid(row=0, column=1, padx=(0, 8))
        close = ttk.Button(buttons, text="Close", style="Accent.TButton", command=window.destroy)
        close.grid(row=0, column=2)

        window.bind("<Escape>", lambda event: window.destroy())
        close.focus_set()
        window.grab_set()

    @staticmethod
    def text_box(parent, text, font, height, wrap="word"):
        """Read-only, but selectable and copyable with Ctrl+C"""
        box = tk.Text(parent, font=font, height=height, wrap=wrap, relief="flat",
                      bg=COLORS["card"], fg=COLORS["text"], padx=12, pady=10,
                      highlightthickness=1, highlightbackground=COLORS["border"],
                      highlightcolor=COLORS["border"], selectbackground=COLORS["accent_soft"],
                      selectforeground=COLORS["text"], inactiveselectbackground=COLORS["accent_soft"])
        box.insert("1.0", text)
        box.configure(state="disabled")
        return box

    def copy(self):
        self.window.clipboard_clear()
        self.window.clipboard_append("{}\n\n{}".format(self.summary, self.details))
        self.copied.config(text="Copied - paste it into an email or message")


def show_in_folder(path, folder):
    """Highlight the downloaded file in Windows Explorer. The command line goes
    straight to Windows, not through cmd.exe, which would expand % and & inside
    the file name - YouTube titles contain both."""
    if os.name != "nt":
        return
    if path and os.path.exists(path):
        subprocess.Popen('explorer /select,"{}"'.format(os.path.abspath(path)))
    else:
        os.startfile(folder)


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
