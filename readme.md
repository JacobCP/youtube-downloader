Simple script providing a UI to download youtube videos, in audio or video format, for Windows.  
Uses pytubefix for the downloading and metadata, and tkinter for the UI.  
Lightweight - pytubefix is the only dependency, and it itself doesn't have any dependencies.  

Using pytubefix, which is a branch of pytube, because pytube has been broken for a while now, and pytubefix is the only working fork I've found.

Currently, sporadically throws an error that "login is needed" when trying to download audio, but if you continue to try, it usually works after a few tries.

Easy to package it as an .exe file using pyinstaller.  
`pyinstaller --onefile --noconsole ytdl.py`