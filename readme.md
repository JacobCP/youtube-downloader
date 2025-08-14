Simple script providing a UI to download youtube videos, in audio or video format, for Windows.  
Uses precompiled yt-dlp.exe for downloading and tkinter for the UI.  
Lightweight - no external Python dependencies required, just the included yt-dlp.exe.  

Using yt-dlp.exe, which is the most reliable and actively maintained YouTube downloader.
The precompiled executable handles all the complexity of YouTube's changing APIs.

It's a game of cat and mouse - since youtube is trying to block bots.
To stay current, simply replace the yt-dlp.exe file with the latest version from the official releases.

In order to work, you'll need to bypass the .exe files from any content filter.
But it's designed not to override/bypass content filters, by first checking if the url returns a redirect http code using curl.
If it does, it will show an error message and not attempt to download.

Easy to package it as an .exe file using pyinstaller.  
`pyinstaller --onefile --noconsole --add-data "yt-dlp.exe;." ytdl.py`

## Features
- Simple GUI interface for entering YouTube URLs
- User-specified filenames
- Download as video (MP4) or audio (MP3)
- File highlighting in Windows Explorer after download
- Uses --no-check-certificate for better compatibility