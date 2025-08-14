import os
import sys
import subprocess
import tkinter as tk
from tkinter import filedialog, messagebox

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
    filename = filename_entry.get()
    
    if not url.strip():
        messagebox.showerror("Error", "Please enter a YouTube URL")
        return
    
    if not filename.strip():
        messagebox.showerror("Error", "Please enter a filename")
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
        
        # Clean filename (remove invalid characters)
        clean_filename = filename.strip()
        invalid_chars = '<>:"/\\|?*'
        for char in invalid_chars:
            clean_filename = clean_filename.replace(char, '_')
        
        # Determine file extension and build full path
        if media_type == "audio":
            file_extension = ".mp3"
        else:
            file_extension = ".mp4"
        
        # Ensure filename doesn't already have the extension
        if not clean_filename.lower().endswith(file_extension.lower()):
            clean_filename += file_extension
        
        full_file_path = os.path.join(download_dir, clean_filename)
        
        # Build yt-dlp command with user-specified filename
        cmd = [
            get_yt_dlp_path(),
            "--no-check-certificate",
            "-o", full_file_path,
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

        status_label.config(text="Download completed successfully!")
        
        # Highlight the downloaded file in Windows Explorer
        if os.path.exists(full_file_path):
            subprocess.run(f'explorer /select,"{os.path.abspath(full_file_path)}"', shell=True)
        else:
            # Fallback to opening the directory if file doesn't exist
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

# Filename input
filename_label = tk.Label(frame, text="Filename:")
filename_label.grid(row=1, column=0, pady=5)

filename_entry = tk.Entry(frame, width=50)
filename_entry.grid(row=1, column=1, pady=5)

# Radio button setup for selecting download type
download_type = tk.StringVar(value="video")  # default option is video

video_radio = tk.Radiobutton(frame, text="Video", variable=download_type, value="video")
audio_radio = tk.Radiobutton(frame, text="Audio", variable=download_type, value="audio")

video_radio.grid(row=2, column=0, pady=5, sticky="w")
audio_radio.grid(row=2, column=1, pady=5, sticky="w")

download_button = tk.Button(frame, text="Download", command=download_video)
download_button.grid(row=3, column=0, columnspan=2, pady=5)

# Status label for displaying messages
status_label = tk.Label(frame, text="")
status_label.grid(row=4, column=0, columnspan=2, pady=5)

root.mainloop()
