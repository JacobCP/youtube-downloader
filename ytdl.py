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
_LENGTH_RE = re.compile(r"^header: Content-Length: (\d+)", re.IGNORECASE)
_SERVER_RE = re.compile(r"^header: Server: (.*)$", re.IGNORECASE)

# How Google's own servers label their replies. An empty reply carrying one of
# these came from YouTube, not from a filter in between.
_GOOGLE_SERVER_RE = re.compile(
    r"^(esf|gws|gse|sffe|gvs|google|scaffolding|http server \(unknown\))", re.IGNORECASE)

# YouTube's API, which answers "what streams does this video have?". A filter that
# lets the video page through but not this leaves nothing to download.
PLAYER_API = "https://www.youtube.com/youtubei/v1/player"

# Headers worth keeping out of a log that gets handed to someone else.
_SECRET_HEADER_RE = re.compile(r"(?i)\\r\\n(cookie|authorization|x-goog-[a-z-]*auth[a-z-]*):[^\\]*")


def parse_traffic(text):
    """Turn yt-dlp --print-traffic output into a list of
    (method, url, status, content_length, server) - the last two are None if the
    reply didn't include them.

    The log is http.client's debug output: a 'send:' line holding the raw request,
    followed by a 'reply:' line holding the status, then 'header:' lines. CONNECT
    lines are the proxy handshake rather than real requests, so they get skipped."""
    requests, pending, last = [], None, None
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
            last = len(requests)
            requests.append((pending[0], pending[1], int(match.group(1)), None, None))
            pending = None
            continue

        if last is not None:
            method, address, status, length, server = requests[last]
            match = _LENGTH_RE.match(line)
            if match:
                requests[last] = (method, address, status, int(match.group(1)), server)
            match = _SERVER_RE.match(line)
            if match:
                requests[last] = (method, address, status, length, match.group(1).strip())

    return requests


def is_emptied(request):
    """A 'successful' reply from YouTube's API with nothing in it - what a filter
    sends when it quietly swallows a request instead of refusing it"""
    method, address, status, length, server = request
    return 200 <= status < 300 and length == 0 and "/youtubei/" in address


def from_google(request):
    """Did this reply come from Google's own servers, going by its label?"""
    return bool(request[4] and _GOOGLE_SERVER_RE.match(request[4]))


def diagnose(url, log_dir):
    """Re-run the extraction with traffic logging and report what got rejected.

    Returns (summary_text, log_path, blocked), where blocked lists each blocked
    address as a dict of address / purpose / outcome. --simulate means nothing is
    downloaded; we only need to get far enough to hit the request that failed."""
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
        return "Could not run the connection check: {}".format(error), None, []

    output = (result.stdout or "") + (result.stderr or "")
    requests = parse_traffic(output)
    # Only codes a filter uses count. Others (400, 404, 429, 5xx...) are YouTube's
    # own answers, so they aren't reported as blocked - they stay in the log.
    rejected = [r for r in requests if filter_certainty(r[2])]
    # An empty reply labelled as coming from Google is YouTube's own hiccup;
    # only one without that label can be the filter's doing
    emptied = [r for r in requests if is_emptied(r) and not from_google(r)]
    youtube_emptied = [r for r in requests if is_emptied(r) and from_google(r)]

    # Save the full log so it can be shown to whoever runs the filter.
    log_path = None
    try:
        log_path = os.path.join(log_dir, "ytdl-diagnostic.log")
        with open(log_path, "w", encoding="utf-8", errors="replace") as handle:
            handle.write("Diagnostic for: {}\n\n".format(url))
            handle.write("Requests made ({} total):\n".format(len(requests)))
            for request in requests:
                method, address, status, length, server = request
                note = "  (empty reply)" if is_emptied(request) else ""
                label = "  [server: {}]".format(server) if server else ""
                handle.write("  {}  {:6} {}{}{}\n".format(status, method, address, note, label))
            handle.write("\n--- full traffic log (cookies removed) ---\n")
            handle.write(_SECRET_HEADER_RE.sub(r"\\r\\n\1: [removed]", output))
    except Exception:
        log_path = None

    if not requests:
        return "The connection check produced no traffic to inspect.", log_path, []

    if youtube_emptied and not rejected and not emptied:
        return (
            "The connection check got an empty reply again, but it came from "
            "YouTube's own servers - so this is YouTube, not a filter."
        ), log_path, []

    if not rejected and not emptied:
        return (
            "The connection check completed {} request(s) and none were rejected, "
            "so the problem may be intermittent - try again.".format(len(requests))
        ), log_path, []

    blocked, seen = [], set()
    for request in [r for r in requests if r in rejected or r in emptied]:
        address = short_address(request[1])
        if address not in seen:
            seen.add(address)
            blocked.append(blocked_item(address, request))

    lines = ["Blocked addresses - these need to be allowed for downloading to work:"]
    for item in blocked:
        lines += ["", "  " + item["address"],
                  "  What it is: " + item["purpose"],
                  "  What happened: " + item["outcome"]]
    return "\n".join(lines), log_path, blocked


# What each YouTube address is for, in words someone can pass on to a filter
# provider. Checked in order; the first match wins.
_ADDRESS_PURPOSES = (
    ("/youtubei/v1/player", "YouTube's list of the picture and sound streams a video has. "
                            "Needed for every download."),
    ("/youtubei/", "YouTube's internal API, which the program uses to look up the video."),
    ("googlevideo.com", "Where the video and audio files themselves are downloaded from."),
    ("/s/player/", "YouTube's player code, which is needed to unlock the streams."),
    ("youtube.com/watch", "The video's own page."),
    ("ytimg.com", "Thumbnail pictures. Not needed for downloading."),
    ("accounts.google.com", "Google's sign-in service."),
)


def short_address(url):
    """The address without its query string - the part a filter allows or blocks.
    Stream addresses in particular carry hundreds of characters of parameters,
    and come from many numbered servers, all of which need allowing."""
    address = url.split("?", 1)[0]
    if ".googlevideo.com/" in address:
        return "https://*.googlevideo.com (every server under it)"
    return address


def describe_address(url):
    for fragment, purpose in _ADDRESS_PURPOSES:
        if fragment in url:
            return purpose
    return "An address the program needed while looking up the video."


EMPTIED_OUTCOME = ("It answered, but sent back nothing - and not from YouTube's own "
                   "servers, so possibly your filter.")


# How sure an error code is to have come from a filter rather than YouTube.
#   418 "I'm a teapot" - what NetFree-style filters send; YouTube never does
#   451 "unavailable for legal reasons" - a block page by definition
#   403 "forbidden" - filters send it, but so does YouTube (e.g. for an expired
#       or unauthorised stream link), so it only ever counts as "possibly"
DEFINITE_FILTER_CODES = (418, 451)
POSSIBLE_FILTER_CODES = (403,)


def filter_certainty(status):
    if status in DEFINITE_FILTER_CODES:
        return "definite"
    if status in POSSIBLE_FILTER_CODES:
        return "possible"
    return None


def describe_outcome(request):
    status = request[2]
    if is_emptied(request):
        return EMPTIED_OUTCOME
    if status == 418:
        return "It was refused (code 418 - a code filters use, never YouTube)."
    if status == 451:
        return "It was refused as blocked content (code 451)."
    if status == 403:
        return "It was refused (code 403). This may be your filter, or YouTube itself."
    return "It was refused (code {}).".format(status)


def blocked_item(address, request):
    return {"address": address, "purpose": describe_address(address),
            "outcome": describe_outcome(request),
            "certainty": "possible" if is_emptied(request) else filter_certainty(request[2])}


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


# yt-dlp's default way of asking YouTube for a video's streams is a single
# request as the "visionos" app (its other default, "web", now only gets streams
# yt-dlp can't save). When YouTube answers that one request with nothing, the
# download fails - so the retry asks as YouTube's TV apps instead, which also
# need no extra security token.
RETRY_PLAYER_CLIENTS = "tv,tv_downgraded,visionos"


def build_command(url, download_dir, media_type, quality, path_file, verbose=False,
                  player_clients=None):
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

    if player_clients:
        cmd.extend(["--extractor-args", "youtube:player_client=" + player_clients])

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
               "unable to download api page", "failed to resolve", "connection",
               "failed to parse json")
    return any(signal in text for signal in signals)


def player_api_emptied(output):
    """yt-dlp asked YouTube's API for the video's streams and got back an empty
    reply - not an error code, just nothing. Without that answer yt-dlp only has
    what's in the web page, which YouTube now limits to a streaming format
    yt-dlp can't save, so every quality fails with "Requested format is not
    available".

    This is usually YouTube itself: the same video can fail and then work a
    minute later, which an address-blocking filter would never do."""
    return "player API JSON" in output and "Failed to parse JSON" in output


# The download's own errors when fetching the video file failed - the step the
# connection check can't repeat, since it never downloads anything. e.g.
#   ERROR: unable to download video data: HTTP Error 418: I'm a teapot
#   WARNING: [download] Got error: HTTP Error 403: Forbidden. Retrying fragment 1
_HTTP_ERROR_RE = re.compile(r"HTTP Error (\d{3})")


def stream_block_status(output):
    """The most filter-like error code hit while downloading the video file
    itself, or None"""
    found = []
    for line in output.splitlines():
        lowered = line.lower()
        if "video data" in lowered or "fragment" in lowered or "[download]" in lowered:
            match = _HTTP_ERROR_RE.search(line)
            if match and filter_certainty(int(match.group(1))):
                found.append(int(match.group(1)))
    for status in DEFINITE_FILTER_CODES + POSSIBLE_FILTER_CODES:
        if status in found:
            return status
    return None


# Failures where the connection itself was cut rather than answered: a filter
# that resets or drops connections, or a broken internet connection - from here
# the two look the same.
_CUT_SIGNALS = (
    "connection reset", "connectionreseterror", "connection aborted",
    "connectionabortederror", "remote end closed", "remotedisconnected",
    "connection refused", "incompleteread", "ssl:", "sslerror", "certificate",
    "getaddrinfo failed", "failed to resolve", "name resolution",
    "nodename nor servname", "timed out", "winerror 10054", "winerror 10060",
)


def connection_cut(errors):
    text = "\n".join(errors).lower()
    return any(signal in text for signal in _CUT_SIGNALS)


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

GENERAL_ADVICE = (
    "Video download failed.\n\n"
    "Is the url correct?\n"
    "Is the video allowed by your filtering settings?\n\n"
    "Still not working?\nTry downloading the latest version from tinyurl.com/youtubekosherdl\n\n"
    "Still not working?\nlet whoever gave you this program know."
)


def filter_request(blocked, video_url):
    """A message, ready to send to a filter provider, asking for the blocked
    addresses to be allowed"""
    lines = [
        "Hello,",
        "",
        "I'm trying to download a YouTube video, but my filter is blocking some",
        "addresses that YouTube downloads need. Could you please allow these:",
        "",
    ]
    for item in blocked:
        lines += [item["address"], "  " + item["purpose"], "  " + item["outcome"], ""]
    lines += ["The video I was downloading: {}".format(video_url), "", "Thank you"]
    return "\n".join(lines)


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

            self._attempt(path_file)
            # YouTube sometimes answers the stream lookup with nothing. Retry
            # straight away, asking a different way, before giving up.
            if (self.process.returncode != 0 and not self.cancelled
                    and player_api_emptied("\n".join(self.log))):
                self.messages.put(("progress", {"kind": "retrying"}))
                self.log.append("--- YouTube sent an empty answer; trying again as its TV app "
                                "(player_client={}) ---".format(RETRY_PLAYER_CLIENTS))
                self._attempt(path_file, player_clients=RETRY_PLAYER_CLIENTS)

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
                    "secondary": summary, "blocked": [], "verdict": None, "url": self.url,
                }))

        finally:
            if path_file:
                try:
                    os.remove(path_file)
                except OSError:
                    pass  # nothing worth bothering the user about

    def _attempt(self, path_file, player_clients=None):
        """Run yt-dlp once, passing its progress on and keeping everything else
        it prints in self.log"""
        cmd = build_command(self.url, self.download_dir, self.media_type, self.quality,
                            path_file, verbose=self.full_errors, player_clients=player_clients)
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

    def _failure_report(self):
        """What to tell the user about a failed download: a plain summary, the
        details someone would need to work out why, and - when a filter is in
        the way - which addresses it blocked"""
        output = "\n".join(self.log)
        errors, warnings = explain_failure(output)
        summary = GENERAL_ADVICE

        # A rejected request means something in between us and YouTube said no.
        # Find out which address it was, so it can be reported or allowed.
        blocked, log_path = [], None
        if looks_blocked("\n".join(errors) or output) or player_api_emptied(output):
            self.messages.put(("progress", {"kind": "checking"}))
            diagnosis, log_path, blocked = diagnose(self.url, self.download_dir)
            summary += "\n\n" + diagnosis

        # What the download itself showed counts too, since the re-check is a
        # second attempt that may not hit the same wall
        addresses = [b["address"] for b in blocked]
        stream_status = stream_block_status(output)
        stream_address = short_address("https://x.googlevideo.com/videoplayback")
        if stream_status and stream_address not in addresses:
            blocked.append(blocked_item(stream_address,
                                        ("GET", stream_address, stream_status, None, None)))

        if any(item["certainty"] == "definite" for item in blocked):
            verdict = "blocked"
        elif blocked:
            verdict = "maybe_blocked"
        elif player_api_emptied(output):
            # Empty stream lookups, even after retrying another way, with no
            # sign of a filter - and a filter blocks an address every time,
            # while these come and go
            verdict = "youtube_glitch"
        elif connection_cut(errors):
            verdict = "connection_cut"
        else:
            verdict = None

        # Shown under the blocked-addresses panel, so it leaves out the list,
        # and the panel's headline already says the download failed
        secondary = GENERAL_ADVICE.split("\n\n", 1)[1]
        for extra in (["Full details saved to:\n" + log_path] if log_path else []) + \
                     (["What yt-dlp said:\n" + "\n".join(errors)] if errors else []):
            summary += "\n\n" + extra
            secondary += "\n\n" + extra

        return {"summary": summary, "secondary": secondary,
                "details": self._details(errors, warnings),
                "blocked": blocked, "verdict": verdict, "url": self.url}

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
    "warn_bg": "#fffbeb",
    "warn_border": "#f59e0b",
    "warn_text": "#92400e",
    "info_bg": "#eff6ff",
    "info_border": "#3b82f6",
    "info_text": "#1e3a8a",
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
        elif update["kind"] == "retrying":
            self.show_stage("YouTube didn't answer properly - trying another way...",
                            indeterminate=True)
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
            ErrorDialog(self.root, payload, self.show_full_errors.get())
        else:
            name = os.path.basename(payload) if payload else "your file"
            self.show_result("Done in {}  -  saved {}".format(elapsed, name))
            show_in_folder(payload, download.download_dir)

    def show_result(self, text):
        self.result_label.config(text=text)
        self.result_label.grid()


class ErrorDialog:
    """Says why a download failed, in a window whose text can be selected and
    copied - unlike a message box - so it can be sent to whoever can help.

    A filter blocking YouTube is the most common failure, and the one a person
    can actually get fixed, so it gets its own layout: what is going on in plain
    words, the addresses to ask for, and a ready-made request to send. How
    firmly it says so depends on how strong the evidence is (see PANELS)."""

    def __init__(self, parent, report, show_details):
        self.report = report
        self.family = family = pick_font_family(parent)
        verdict = report.get("verdict")
        asks_filter = verdict in ("blocked", "maybe_blocked")
        window = self.window = tk.Toplevel(parent, bg=COLORS["bg"])
        window.title(PANELS[verdict]["title"] if verdict else "Download failed")
        window.transient(parent)
        window.minsize(600, 0)

        frame = ttk.Frame(window, style="App.TFrame", padding=20)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        self.boxes = []
        row = 0

        if verdict:
            row = self.build_panel(frame, PANELS[verdict], report["blocked"])
            ttk.Label(frame, text="Other things to try", style="Subtitle.TLabel").grid(
                row=row, column=0, sticky="w", pady=(16, 4))
            row += 1
        else:
            ttk.Label(frame, text="Download failed", style="Title.TLabel",
                      foreground=COLORS["accent"]).grid(row=row, column=0, sticky="w", pady=(0, 10))
            row += 1

        summary_box = self.text_box(frame, report["secondary"] if verdict else report["summary"],
                                    (family, 9 if verdict else 10))
        summary_box.grid(row=row, column=0, sticky="we")
        row += 1

        if show_details:
            ttk.Label(frame, text="Details", style="Subtitle.TLabel").grid(
                row=row, column=0, sticky="w", pady=(14, 4))
            row += 1
            frame.rowconfigure(row, weight=1)
            holder = ttk.Frame(frame, style="App.TFrame")
            holder.grid(row=row, column=0, sticky="nsew")
            holder.columnconfigure(0, weight=1)
            holder.rowconfigure(0, weight=1)
            details_box = self.text_box(holder, report["details"], ("Consolas", 9),
                                        wrap="char", fit=False, height=14)
            details_box.grid(row=0, column=0, sticky="nsew")
            bar = ttk.Scrollbar(holder, orient="vertical", command=details_box.yview)
            bar.grid(row=0, column=1, sticky="ns")
            details_box.configure(yscrollcommand=bar.set)
            row += 1

        buttons = ttk.Frame(frame, style="App.TFrame")
        buttons.grid(row=row, column=0, sticky="we", pady=(16, 0))
        buttons.columnconfigure(0, weight=1)
        self.copied = ttk.Label(buttons, style="Subtitle.TLabel")
        self.copied.grid(row=0, column=0, sticky="w")
        if asks_filter:
            ttk.Button(buttons, text="Copy full report", style="Quiet.TButton",
                       command=self.copy_report).grid(row=0, column=1, padx=(0, 8))
            main = ttk.Button(buttons, text="Copy request for your filter provider",
                              style="Accent.TButton", command=self.copy_request)
            main.grid(row=0, column=2, padx=(0, 8))
            close = ttk.Button(buttons, text="Close", style="Quiet.TButton", command=window.destroy)
            close.grid(row=0, column=3, sticky="ns")
        elif verdict:
            main = ttk.Button(buttons, text="Copy full report", style="Accent.TButton",
                              command=self.copy_report)
            main.grid(row=0, column=1, padx=(0, 8))
            close = ttk.Button(buttons, text="Close", style="Quiet.TButton", command=window.destroy)
            close.grid(row=0, column=2, sticky="ns")
        else:
            ttk.Button(buttons, text="Copy everything", style="Quiet.TButton",
                       command=self.copy_report).grid(row=0, column=1, padx=(0, 8))
            main = close = ttk.Button(buttons, text="Close", style="Accent.TButton",
                                      command=window.destroy)
            close.grid(row=0, column=2)

        window.bind("<Escape>", lambda event: window.destroy())
        main.focus_set()

        # Size each text box to its wrapped content, now its width is known.
        # Measured in pixels, since the address box mixes font sizes; a Text
        # widget's height is in lines of its base font.
        from tkinter import font
        window.update_idletasks()
        for box in self.boxes:
            box.configure(height=200)
            window.update_idletasks()
            pixels = box.count("1.0", "end", "ypixels")
            pixels = pixels[0] if isinstance(pixels, tuple) else (pixels or 0)
            line = font.Font(font=box.cget("font")).metrics("linespace")
            box.configure(height=max(1, -(-pixels // line)))
        window.grab_set()

    def build_panel(self, frame, config, blocked):
        """The highlighted explanation at the top. Returns the next free grid row."""
        family, c = self.family, COLORS
        bg, border, fg = (c[config["tone"] + "_bg"], c[config["tone"] + "_border"],
                          c[config["tone"] + "_text"])
        panel = tk.Frame(frame, bg=bg, highlightthickness=1, highlightbackground=border)
        panel.grid(row=0, column=0, sticky="we")
        panel.columnconfigure(1, weight=1)
        tk.Frame(panel, bg=border, width=6).grid(row=0, column=0, rowspan=3, sticky="ns")

        tk.Label(panel, text=config["title"], bg=bg, fg=fg, font=(family, 16, "bold"),
                 anchor="w", justify="left").grid(row=0, column=1, sticky="we", padx=16, pady=(14, 6))
        tk.Label(panel, bg=bg, fg=c["text"], font=(family, 10), anchor="w", justify="left",
                 wraplength=540, text=config["text"]
                 ).grid(row=1, column=1, sticky="we", padx=16, pady=(0, 12 if blocked else 16))
        if not blocked:
            return 1

        # The addresses, in a box whose text can be selected and copied
        box = tk.Text(panel, font=(family, 10), wrap="word", relief="flat", width=60, height=4,
                      bg=c["card"], fg=c["text"], padx=14, pady=10, highlightthickness=1,
                      highlightbackground=border, highlightcolor=border,
                      selectbackground=c["accent_soft"], selectforeground=c["text"],
                      inactiveselectbackground=c["accent_soft"], cursor="xterm")
        box.tag_configure("address", font=("Consolas", 11, "bold"), spacing1=6)
        box.tag_configure("note", foreground=c["muted"], font=(family, 9), lmargin1=16, lmargin2=16)
        for index, item in enumerate(blocked):
            if index:
                box.insert("end", "\n")
            box.insert("end", item["address"] + "\n", "address")
            box.insert("end", item["purpose"] + "\n" + item["outcome"] + "\n", "note")
        box.delete("end-2c", "end-1c")  # no trailing blank line
        box.configure(state="disabled")
        box.grid(row=2, column=1, sticky="we", padx=16, pady=(0, 16))
        self.boxes.append(box)
        return 1

    def text_box(self, parent, text, font, wrap="word", fit=True, height=4):
        """Read-only, but selectable and copyable with Ctrl+C"""
        c = COLORS
        box = tk.Text(parent, font=font, height=height, width=60, wrap=wrap, relief="flat",
                      bg=c["card"], fg=c["text"], padx=12, pady=10,
                      highlightthickness=1, highlightbackground=c["border"],
                      highlightcolor=c["border"], selectbackground=c["accent_soft"],
                      selectforeground=c["text"], inactiveselectbackground=c["accent_soft"])
        box.insert("1.0", text)
        box.configure(state="disabled")
        if fit:
            self.boxes.append(box)
        return box

    def copy_text(self, text, note):
        self.window.clipboard_clear()
        self.window.clipboard_append(text)
        self.copied.config(text=note)

    def copy_request(self):
        self.copy_text(filter_request(self.report["blocked"], self.report["url"]),
                       "Copied - paste it into an email to your filter provider")

    def copy_report(self):
        self.copy_text("{}\n\n{}".format(self.report["summary"], self.report["details"]),
                       "Copied - paste it into an email or message")


# The top panel of the error window, by how sure we are a filter is the cause
PANELS = {
    "blocked": {
        "tone": "warn",
        "title": "Your filter is blocking this download",
        "text": (
            "The video is fine, and you didn't do anything wrong. Your internet "
            "filter is stopping this program from reaching parts of YouTube it "
            "needs, so nothing can be downloaded. Choosing a different quality "
            "won't help.\n\n"
            "To fix it, ask your filter provider to allow the addresses below. "
            "The red button copies a ready-made request you can paste into an "
            "email or their support form."),
    },
    "maybe_blocked": {
        "tone": "info",
        "title": "Your filter may be blocking this download",
        "text": (
            "Parts of YouTube refused this program. That is often a filter, but "
            "YouTube sometimes refuses on its own too, so first try again in a "
            "few minutes.\n\n"
            "If it keeps happening, ask your filter provider to allow the "
            "addresses below. The red button copies a ready-made request you can "
            "paste into an email or their support form."),
    },
    "youtube_glitch": {
        "tone": "info",
        "title": "YouTube didn't answer properly",
        "text": (
            "When the program asked YouTube about this video, YouTube sent back an "
            "empty answer - and again when it tried a second way. That is almost "
            "always on YouTube's side rather than your filter (a filter blocks "
            "the same way every time), and the video is probably fine.\n\n"
            "Wait a few minutes and try again. If it keeps happening, download "
            "the latest version of this program from tinyurl.com/youtubekosherdl - "
            "YouTube changes often, and newer versions keep up."),
    },
    "connection_cut": {
        "tone": "info",
        "title": "The connection to YouTube was cut",
        "text": (
            "Something stopped the connection part-way. Often that's a filter, "
            "but an unreliable internet connection looks the same from here.\n\n"
            "Check that your internet is working, then try again. If it keeps "
            "happening, it's probably your filter: send them the full report - "
            "the red button copies it."),
    },
}


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
