Simple script providing a UI to download youtube videos, in audio or video format, for Windows.  
Uses pytubefix for the downloading and metadata, and tkinter for the UI.  
Lightweight - pytubefix is the only dependency, and it itself doesn't have any dependencies.  

Using pytubefix, which is a branch of pytube, because pytube has been broken for a while now, and pytubefix is the only working fork I've found.

It's a game of cat and mouse - since youtube is trying to block bots.
I'll try to update the script periodically to use the latest version - but no guarantees that it will continue to work.

In order to work, you'll need to bypass the .exe (see below) from any content filter.
But it's designed not to overide/bypass content filters, by first checking if the url returns a redirect http code using curl.
If it does, it will show an error message and not attempt to download.

Easy to package it as an .exe file using pyinstaller.  
`pyinstaller --onefile --noconsole ytdl.py`