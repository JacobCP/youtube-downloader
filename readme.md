Simple script providing a UI to download youtube videos for Windows.  
Uses pytube for the downloading and metadata, and tkinter for the UI.  
Lightweight - pytube is the only dependency, and it itself doesn't have any dependencies.  

Easy to package it as an .exe file using pyinstaller.  
`pyinstaller --onefile --noconsole ytdl.py`