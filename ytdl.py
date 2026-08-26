import os
import re
import ssl
import sys
import subprocess
import urllib.error
import urllib.request
import tkinter as tk
from tkinter import filedialog, messagebox
import traceback

def get_yt_dlp_path():
    """Get the path to yt-dlp.exe, handling both development and PyInstaller bundle"""
    if getattr(sys, 'frozen', False):
        # Running as PyInstaller bundle
        base_path = sys._MEIPASS
    else:
        # Running as script
        base_path = os.path.dirname(__file__)

    return os.path.join(base_path, "yt-dlp.exe")


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
            creationflags=subprocess.CREATE_NO_WINDOW,
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


# --- checking with the content filter before doing anything --------------------
#
# The rule this program follows: if the filter blocks the video itself, we stop.
# We only work around a block that is on YouTube's stream-details API rather than
# on the video, and only after confirming the video's own page is permitted.

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# Text a filter puts in its refusal page. Checked case-insensitively.
BLOCK_SIGNATURES = ("netfree", "blocked by", "content filter", "access denied",
                    "this site is blocked", "not permitted")


def check_url_allowed(url):
    """Fetch the video's own page and see whether the filter permits it.

    Returns (allowed, detail). allowed is None when we could not tell - in that
    case we do NOT work around anything, because we cannot show the video is
    permitted. Certificate checking is off because a filter that inspects traffic
    presents its own certificate; this matches the rest of the program."""
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE

    request = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
    try:
        with urllib.request.urlopen(request, timeout=25, context=context) as response:
            status = response.status
            body = response.read(80000).decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        body = ""
        try:
            body = error.read(8000).decode("utf-8", "replace")
        except Exception:
            pass
        named = next((s for s in BLOCK_SIGNATURES if s in body.lower()), None)
        return False, "the page was refused with HTTP {}{}".format(
            error.code, " ({})".format(named) if named else "")
    except Exception as error:
        return None, "could not reach the page: {}".format(error)

    if status >= 400:
        return False, "the page was refused with HTTP {}".format(status)

    lowered = body.lower()
    named = next((s for s in BLOCK_SIGNATURES if s in lowered), None)
    # A real YouTube watch page always carries the player bootstrap. A filter's
    # refusal page will not, which is what stops a passing mention of a signature
    # word in a video title from being mistaken for a block.
    looks_like_youtube = "ytinitialdata" in lowered or "ytinitialplayerresponse" in lowered
    if named and not looks_like_youtube:
        return False, "the filter returned its own page ({})".format(named)

    if not looks_like_youtube:
        return None, "the page came back but did not look like a YouTube page"

    return True, "the video's own page is permitted"


def download_video():
    status_label.config(text="")  # Clear previous status
    url = url_entry.get()

    if not url.strip():
        messagebox.showerror("Error", "Please enter a YouTube URL")
        return

    download_dir = None
    try:
        # Let user choose download directory
        download_dir = filedialog.askdirectory(title="Choose download folder")
        if not download_dir:
            messagebox.showerror("Error", "No folder chosen")
            return

        # Before anything else: does the filter allow this video at all?
        status_label.config(text="Checking the video is permitted...")
        root.update_idletasks()
        allowed, detail = check_url_allowed(url)
        status_label.config(text="")

        if allowed is False:
            raise Exception(
                "Not downloading.\n\n"
                "Your content filter is blocking this video's own page - "
                "{}.\n\n"
                "This program does not work around a filter's decision about a "
                "video. If you think it should be allowed, that is a question "
                "for whoever runs the filter.".format(detail))

        # Show a message that download has started
        messagebox.showinfo("Download", "The download will start shortly")

        media_type = download_type.get()

        # Use yt-dlp template to automatically use YouTube title
        # %(title)s will be replaced with the actual video title
        # yt-dlp will automatically clean invalid characters
        output_template = os.path.join(download_dir, "%(title)s.%(ext)s")

        # Build yt-dlp command with automatic title
        cmd = [
            get_yt_dlp_path(),
            "--no-check-certificate",
            "-o", output_template,
            "--print", "after_move:filepath",  # This prints the actual file path to stdout
        ]

        # The video's page is permitted (checked above), so take the stream details
        # from that page instead of from YouTube's separate API address, which some
        # filters refuse. Restricting to the "web" client is what does this: it is
        # the only client that reads its player response out of the page yt-dlp has
        # already downloaded, so no request to /youtubei/v1/player is made at all.
        if allowed is True:
            cmd.extend(["--extractor-args", "youtube:player_client=web"])

        # YouTube often only puts the stream details in the page for a signed-in
        # session, so borrow the browser's. Firefox is the default because Chrome
        # and Edge encrypt their cookie store on Windows in a way yt-dlp cannot
        # always read (yt-dlp issue 10927).
        browser = cookie_browser.get().strip()
        if browser:
            cmd.extend(["--cookies-from-browser", browser])

        cmd.append(url)

        if media_type == "audio":
            cmd.extend([
                "--extract-audio",
                "--audio-format", "mp3",
                "--audio-quality", "0"  # best quality
            ])
        else:
            cmd.extend([
                "--format", "best[height<=1080]/best"  # best quality available
            ])

        # Run yt-dlp
        result = subprocess.run(cmd, capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW)

        if result.returncode != 0:
            error_msg = result.stderr if result.stderr else "Unknown error occurred"
            my_msg = (
                "Video download failed.\n\n"
                "Is the url correct?\n"
                "Is the video allowed by your filtering settings?\n\n"
                "Still not working?\nTry downloading the latest version from tinyurl.com/youtubekosherdl\n\n"
                "Still not working?\nlet whoever gave you this program know.\n\n"
            )

            # A rejected request means something in between us and YouTube said no.
            # Find out which address it was, so it can be reported or allowed.
            if looks_blocked(error_msg):
                status_label.config(text="Checking which addresses are blocked...")
                root.update_idletasks()
                summary, log_path = diagnose(url, download_dir)
                status_label.config(text="")
                my_msg += summary + "\n\n"
                if log_path:
                    my_msg += "Full details saved to:\n{}\n\n".format(log_path)

            # If full errors checkbox is checked, append the full error details
            if show_full_errors.get():
                my_msg += "Full error details:\n{}".format(error_msg)

            raise Exception(my_msg)

        # Extract the actual file path from stdout
        # yt-dlp prints the filepath to stdout when using --print after_move:filepath
        actual_file_path = None
        if result.stdout:
            # Get the last non-empty line from stdout (the filepath)
            stdout_lines = [line.strip() for line in result.stdout.strip().split('\n') if line.strip()]
            if stdout_lines:
                actual_file_path = stdout_lines[-1]

        status_label.config(text="Download completed successfully!")

        # Highlight the downloaded file in Windows Explorer
        if actual_file_path and os.path.exists(actual_file_path):
            subprocess.run(f'explorer /select,"{os.path.abspath(actual_file_path)}"', shell=True)
        else:
            # Fallback to opening the directory
            os.startfile(download_dir)

    except Exception as error_message:
        status_label.config(text="")
        # If full errors checkbox is checked, append the full traceback
        if show_full_errors.get():
            error_message = f"Full traceback:\n{traceback.format_exc()}"

        messagebox.showerror("Error", error_message)


def looks_blocked(error_text):
    """Does this failure look like something refused a request, rather than a bad URL?"""
    text = (error_text or "").lower()
    signals = ("http error 4", "http error 5", "blocked", "forbidden",
               "unable to download api page", "failed to resolve", "connection")
    return any(signal in text for signal in signals)


def main():
    global root, url_entry, download_type, show_full_errors, status_label, cookie_browser

    # Set up the GUI
    root = tk.Tk()
    root.title("YouTube Video Downloader")

    frame = tk.Frame(root)
    frame.pack(padx=10, pady=10)

    url_label = tk.Label(frame, text="YouTube URL:")
    url_label.grid(row=0, column=0, pady=5)

    url_entry = tk.Entry(frame, width=50)
    url_entry.grid(row=0, column=1, pady=5)

    # Radio button setup for selecting download type
    download_type = tk.StringVar(value="video")  # default option is video

    video_radio = tk.Radiobutton(frame, text="Video", variable=download_type, value="video")
    audio_radio = tk.Radiobutton(frame, text="Audio", variable=download_type, value="audio")

    video_radio.grid(row=1, column=0, pady=5, sticky="w")
    audio_radio.grid(row=1, column=1, pady=5, sticky="w")

    # Which browser's signed-in session to borrow, if any. YouTube frequently
    # withholds stream details from a signed-out request. Blank means don't.
    cookie_browser = tk.StringVar(value="")
    cookie_label = tk.Label(frame, text="Use sign-in from:", font=("Arial", 8))
    cookie_label.grid(row=2, column=0, pady=2, sticky="w")
    cookie_menu = tk.OptionMenu(frame, cookie_browser, "", "firefox", "chrome", "edge", "brave", "opera", "vivaldi")
    cookie_menu.config(font=("Arial", 8))
    cookie_menu.grid(row=2, column=1, pady=2, sticky="w")

    # Checkbox for showing full error details
    show_full_errors = tk.BooleanVar(value=False)  # default is unchecked
    error_checkbox = tk.Checkbutton(frame, text="output full errors", variable=show_full_errors, font=("Arial", 8))
    error_checkbox.grid(row=3, column=0, columnspan=2, pady=2, sticky="w")

    download_button = tk.Button(frame, text="Download", command=download_video)
    download_button.grid(row=4, column=0, columnspan=2, pady=5)

    # Status label for displaying messages
    status_label = tk.Label(frame, text="")
    status_label.grid(row=5, column=0, columnspan=2, pady=5)

    root.mainloop()


if __name__ == "__main__":
    main()
