import os
import sys
import subprocess
import threading
import tkinter as tk
from tkinter import filedialog, messagebox
import traceback

def get_bundle_dir():
    """Get the directory holding the bundled executables (yt-dlp, ffmpeg, ffprobe),
    handling both development and PyInstaller bundle"""
    if getattr(sys, 'frozen', False):
        # Running as PyInstaller bundle
        return sys._MEIPASS
    else:
        # Running as script
        return os.path.dirname(__file__)

def get_yt_dlp_path():
    """Get the path to yt-dlp.exe"""
    return os.path.join(get_bundle_dir(), "yt-dlp.exe")

# Format selectors for each quality option.
# YouTube only serves a single pre-muxed (video+audio) file at 360p, so anything
# above that arrives as separate video and audio streams that ffmpeg has to merge.
# "hd" asks for that merge; "small" prefers the one pre-muxed file so there is
# nothing to merge and the download stays quick.
#   height<=?N   the "?" stops the filter from erroring on formats that don't
#                report a height
#   bv*+ba/b     last-resort catch-all. A bare "/b" is not enough: "b" only matches
#                pre-muxed formats, so a video offered exclusively above the height
#                limit and only as separate streams would fail outright with
#                "Requested format is not available"
QUALITY_FORMATS = {
    "hd": "bv*[height<=?1080]+ba/b[height<=?1080]/bv*+ba/b",
    "small": "b[height<=?360]/bv*[height<=?360]+ba/bv*+ba/b",
}

def build_command(url, output_template, media_type, quality_choice):
    """Build the yt-dlp command line for a download"""
    cmd = [
        get_yt_dlp_path(),
        "--no-check-certificate",
        # ffmpeg.exe sits next to yt-dlp.exe in both the script and the bundle;
        # point at it explicitly rather than relying on PATH
        "--ffmpeg-location", get_bundle_dir(),
        "-o", output_template,
        "--print", "after_move:filepath",  # This prints the actual file path to stdout
        url
    ]

    if media_type == "audio":
        cmd.extend([
            "--extract-audio",
            "--audio-format", "mp3",
            "--audio-quality", "0"  # best quality
        ])
    else:
        cmd.extend([
            "--format", QUALITY_FORMATS[quality_choice],
            # Within the height limit, prefer the highest resolution, then h264
            # in an mp4 container so the file plays on anything
            "--format-sort", "res,vcodec:h264,ext:mp4:m4a",
            # mkv is the fallback container: above 1080p YouTube only offers
            # VP9/AV1, which don't always fit in an mp4
            "--merge-output-format", "mp4/mkv"
        ])

    return cmd

def download_video():
    status_label.config(text="")  # Clear previous status
    url = url_entry.get()

    if not url.strip():
        messagebox.showerror("Error", "Please enter a YouTube URL")
        return

    # Let user choose download directory
    download_dir = filedialog.askdirectory(title="Choose download folder")
    if not download_dir:
        messagebox.showerror("Error", "No folder chosen")
        return

    download_button.config(state="disabled")
    status_label.config(text="Downloading... this can take a while for long videos.")

    # Run yt-dlp off the main thread so the window stays responsive. Merging
    # video and audio makes downloads long enough that a frozen window would
    # look like the program had crashed.
    worker = threading.Thread(
        target=run_download,
        args=(url, download_dir, download_type.get(), quality.get(), show_full_errors.get()),
        daemon=True
    )
    worker.start()

def run_download(url, download_dir, media_type, quality_choice, full_errors):
    """Background worker. Does not touch the GUI directly - results are handed
    back to the main thread with root.after()"""
    try:
        # Use yt-dlp template to automatically use YouTube title
        # %(title)s will be replaced with the actual video title
        # yt-dlp will automatically clean invalid characters
        output_template = os.path.join(download_dir, "%(title)s.%(ext)s")

        cmd = build_command(url, output_template, media_type, quality_choice)

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

            # If full errors checkbox is checked, append the full error details
            if full_errors:
                my_msg += f"Full error details:\n{error_msg}"

            raise Exception(my_msg)

        # Extract the actual file path from stdout
        # yt-dlp prints the filepath to stdout when using --print after_move:filepath
        actual_file_path = None
        if result.stdout:
            # Get the last non-empty line from stdout (the filepath)
            stdout_lines = [line.strip() for line in result.stdout.strip().split('\n') if line.strip()]
            if stdout_lines:
                actual_file_path = stdout_lines[-1]

        root.after(0, lambda: on_download_success(actual_file_path, download_dir, media_type))

    except Exception as error_message:
        # If full errors checkbox is checked, show the full traceback instead
        if full_errors:
            message = f"Full traceback:\n{traceback.format_exc()}"
        else:
            message = str(error_message)

        # Bind the message now: root.after runs the lambda after this block ends,
        # by which point the "except ... as" name no longer exists
        root.after(0, lambda m=message: on_download_error(m))

def on_download_success(actual_file_path, download_dir, media_type):
    status_label.config(text="Download completed successfully!")
    download_button.config(state="normal")

    # Highlight the downloaded file in Windows Explorer
    if actual_file_path and os.path.exists(actual_file_path):
        if media_type == "audio":
            actual_file_path = actual_file_path.replace(".mp4", ".mp3")
        subprocess.run(f'explorer /select,"{os.path.abspath(actual_file_path)}"', shell=True)
    else:
        # Fallback to opening the directory
        os.startfile(download_dir)

def on_download_error(message):
    status_label.config(text="")
    download_button.config(state="normal")
    messagebox.showerror("Error", message)

def update_quality_state():
    """The resolution choice only applies to video downloads"""
    state = "disabled" if download_type.get() == "audio" else "normal"
    for radio in quality_radios:
        radio.config(state=state)


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

video_radio = tk.Radiobutton(frame, text="Video", variable=download_type, value="video", command=update_quality_state)
audio_radio = tk.Radiobutton(frame, text="Audio", variable=download_type, value="audio", command=update_quality_state)

video_radio.grid(row=1, column=0, pady=5, sticky="w")
audio_radio.grid(row=1, column=1, pady=5, sticky="w")

# Radio button setup for selecting video resolution
quality = tk.StringVar(value="hd")  # default option is HD

hd_radio = tk.Radiobutton(frame, text="HD (up to 1080p)", variable=quality, value="hd")
small_radio = tk.Radiobutton(frame, text="Small file (360p)", variable=quality, value="small")

hd_radio.grid(row=2, column=0, pady=5, sticky="w")
small_radio.grid(row=2, column=1, pady=5, sticky="w")

quality_radios = (hd_radio, small_radio)
update_quality_state()

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
