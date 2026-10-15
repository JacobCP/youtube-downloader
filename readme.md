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

## Working with content filters

This program is meant to work on computers with a content filter installed.
When it doesn't, that's a shortcoming of the program, to be fixed here - users
are never told to contact their filter company.

**Finding the streams another way.** yt-dlp normally finds a video's streams with
a single request to YouTube's player API (`https://www.youtube.com/youtubei/v1/player`).
Filters can answer that request with nothing, and the download then fails with
"Requested format is not available" at every quality. When the streams can't be
found, the app tries again, in turn:

1. reading the stream list straight from the video's web page, loaded as Safari
   (`player_client=web_safari;webpage_client=web_safari`) - no player API request
   at all, and Safari's HLS streams need no extra security token;
2. asking the player API as YouTube's TV app (`player_client=tv,tv_downgraded`).

**When nothing gets through,** the error window explains it in plain words and
its main button copies a report for whoever gave the user the program. How firmly
it blames the filter depends on the evidence:

- **"The filter on this computer blocked this download"** - code 418 (filters use
  it, YouTube never does) or code 451.
- **"Parts of YouTube refused this download"** - code 403, which filters send but so
  does YouTube, or an empty reply that didn't come from Google's servers.
- **"The program couldn't find this video's streams"** - every way of finding the
  streams came back empty, with no firm evidence of who emptied them.
- **"The connection to YouTube was cut"** - a reset, dropped or failed connection.

Other error codes (400, 404, 429, 5xx) are YouTube's own answers and are never
blamed on the filter. Blocks on the video servers (`*.googlevideo.com`) are read
from the download's own error.

To see exactly what was refused, the app re-runs the lookup with traffic logging
and writes `ytdl-diagnostic.log` into the download folder (cookies removed). It
lists every request with its status, marks empty replies, and records which
server sent each one.

## Features
- Simple GUI interface for entering YouTube URLs
- Download as video (MP4) or audio (MP3)
- Choice of video quality - Quick (360p), HD (1080p) or Best (up to 4K) - with
  what each one costs in download time shown next to it
- Live progress while downloading: percentage, size, speed, time left and time
  elapsed, plus a Cancel button that also tidies up the partial files
- File highlighting in Windows Explorer after download

## A note on quality
YouTube serves only one file with picture and sound already together, at 360p.
Anything sharper arrives as a separate video stream and audio stream that the
bundled ffmpeg joins afterwards. So higher quality costs twice: a much bigger
download (1080p is typically about 5x the size of 360p, 4K 20x or more), then a
merge step. That's why Quick exists, and why the app says how slow each choice is.

## File names
Windows doesn't allow `\ / : * ? " < > |` in file names, and YouTube titles are
full of them. The app swaps them for plain characters ("Part 1: Intro" is saved
as "Part 1 - Intro"). It used to be the case that titles with these characters -
or any characters outside the Windows console's code page, such as emoji - often
ended with the folder opening instead of the file being highlighted. The path of
the finished file is now read back as UTF-8, so that no longer happens.
