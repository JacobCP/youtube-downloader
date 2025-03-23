import os
import subprocess
import tkinter as tk
from tkinter import filedialog, messagebox

from pytubefix import YouTube


def download_video():
    status_label.config(text="")  # Clear previous status
    url = url_entry.get()
    if not url.strip():
        messagebox.showerror("Error", "Please enter a YouTube URL")
        return
    try:
        # check if we can access the url
        curl_command = [
            "curl",
            "-s",  # silent mode
            "-k",  # allow insecure connections
            "-o", "/dev/null",  # redirect output to /dev/null
            "-w", "%{http_code}",  # write out the status code
            url,
        ]
        result = subprocess.run(curl_command, capture_output=True, text=True).stdout
        if not result.isnumeric():
            messagebox.showerror("Error", "Could not verify URL access")
            return
        if int(result) >= 300 and int(result) < 400:
            messagebox.showerror("Error", "It seems you don't have access to that URL")
            return

        yt = YouTube(url)

        media_type = download_type.get()
        if media_type == "audio":
            stream = yt.streams.filter(only_audio=True).first()
            default_file_name = stream.default_filename.replace(".mp4", ".mp3")
            filetypes = [("Audio files", "*.mp3")]
        elif media_type == "video":
            stream = yt.streams.get_highest_resolution()
            default_file_name = stream.default_filename
            filetypes = [("Video files", "*.mp4")]

        default_extension = os.path.splitext(default_file_name)[1]

        file_path = filedialog.asksaveasfilename(
            initialfile=default_file_name,
            defaultextension=default_extension,
            filetypes=filetypes,
        )

        if not file_path:
            messagebox.showerror("Error", "No file chosen")
            return

        # Show a message that download has started
        messagebox.showinfo("Download", "The download will start shortly")

        # Extract directory and filename separately from file_path
        directory, filename = os.path.split(file_path)

        stream.download(output_path=directory, filename=filename)

        status_label.config(text="Video downloaded successfully!")
        os.startfile(directory)
    except Exception as e:
        messagebox.showerror("Error", f"Failed to download video. Error: {e}")


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
audio_radio = tk.Radiobutton(
    frame, text="Audio (MP3)", variable=download_type, value="audio"
)

video_radio.grid(row=1, column=0, pady=5, sticky="w")
audio_radio.grid(row=1, column=1, pady=5, sticky="w")

download_button = tk.Button(frame, text="Download", command=download_video)
download_button.grid(row=2, column=0, columnspan=2, pady=5)

# Status label for displaying messages
status_label = tk.Label(frame, text="")
status_label.grid(row=3, column=0, columnspan=2, pady=5)

root.mainloop()
