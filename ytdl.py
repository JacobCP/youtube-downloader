import os
import sys
import subprocess
import tkinter as tk
from tkinter import filedialog, messagebox
import glob

def get_yt_dlp_path():
    """Get the path to yt-dlp.exe, handling both development and PyInstaller bundle"""
    if getattr(sys, 'frozen', False):
        # Running as PyInstaller bundle
        base_path = sys._MEIPASS
    else:
        # Running as script
        base_path = os.path.dirname(__file__)
    
    return os.path.join(base_path, "yt-dlp.exe")

def download_video():
    status_label.config(text="")  # Clear previous status
    url = url_entry.get()
    
    if not url.strip():
        messagebox.showerror("Error", "Please enter a YouTube URL")
        return
    
    try:
        # Let user choose download directory
        download_dir = filedialog.askdirectory(title="Choose download folder")
        if not download_dir:
            messagebox.showerror("Error", "No folder chosen")
            return

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
            if media_type == "audio":
                actual_file_path = actual_file_path.replace(".mp4", ".mp3")
            subprocess.run(f'explorer /select,"{os.path.abspath(actual_file_path)}"', shell=True)
        else:
            # Fallback to opening the directory
            os.startfile(download_dir)
        
    except Exception as e:
        messagebox.showerror("Error", e)


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

download_button = tk.Button(frame, text="Download", command=download_video)
download_button.grid(row=2, column=0, columnspan=2, pady=5)

# Status label for displaying messages
status_label = tk.Label(frame, text="")
status_label.grid(row=3, column=0, columnspan=2, pady=5)

root.mainloop()
