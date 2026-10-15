Simple script providing a UI to download youtube videos, in audio or video format, for Windows.
Uses yt-dlp for downloading and tkinter for the UI.

Using yt-dlp, which is the most reliable and actively maintained YouTube downloader.
It handles all the complexity of YouTube's changing APIs.

It's a game of cat and mouse - since youtube is trying to block bots.
Releases are rebuilt automatically whenever the program changes or yt-dlp puts out
a new version, so the newest download on the releases page is always current.

In order to work, you'll need to bypass the .exe files from any content filter.

## Getting it

Download `ytdl.exe` from the [latest release](../../releases/latest). Everything is
bundled inside it - there is nothing to install.

The app does not update itself. When YouTube changes and downloads start failing,
come back to the releases page and download the newest one.

## Building it yourself

The bundled tools (yt-dlp, ffmpeg, ffprobe, deno) are **not** kept in this repo.
They are downloaded fresh at build time, so a clean clone always builds against
current versions. `versions.json` records what gets fetched.

    pip install pyinstaller
    .\build.ps1

That downloads the tools and produces `dist\ytdl.exe`.

## How releases happen

`.github/workflows/release.yml` publishes a new release in two cases:

- **A change is merged into `main`** that touches the program itself (`ytdl.py`,
  `build.ps1`, `requirements.txt` or `versions.json`). Readme and workflow edits
  don't trigger a release.
- **yt-dlp puts out a new version.** Once a day the workflow compares the yt-dlp
  version recorded in `versions.json` against yt-dlp's newest release, and builds
  if they differ.

Either way it builds a fresh `ytdl.exe` against the newest yt-dlp, publishes it,
and then records the version it shipped. On a day with no change it stops without
doing anything.

You can also run it by hand from the Actions tab. It defaults to a dry run, which
builds the exe and attaches it to the workflow run without publishing a release.

## When a content filter blocks it

If a download fails because something rejected a request, the app re-runs the
extraction with traffic logging and tells you the exact addresses that were
refused, along with their HTTP status. It also writes `ytdl-diagnostic.log` into
your chosen download folder, with cookies stripped out so it is safe to pass on.

This matters because a filter can allow the video itself while still refusing the
address yt-dlp needs to fetch the stream details - typically
`https://www.youtube.com/youtubei/v1/player`. The error on its own says only
"Unable to download API page", which is not enough to ask for the address to be
allowed.

## Features
- Simple GUI interface for entering YouTube URLs
- Download as video (MP4) or audio (MP3)
- File highlighting in Windows Explorer after download
