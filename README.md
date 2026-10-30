# YouTube MP3 Converter

A small Mac app that converts a YouTube video to an MP3 file. It works on very long videos (7+ hours), retries network hiccups automatically in the background, and shows a clear message when something can't be fixed by retrying (private or removed video, bad URL, full disk, etc.).

Built with Python/Tkinter, [yt-dlp](https://github.com/yt-dlp/yt-dlp) and ffmpeg (bundled, no Homebrew needed).

## Install

**Requirements:** an Apple Silicon Mac (M1 or later) and [Python 3.11 from python.org](https://www.python.org/downloads/macos/) (it includes Tk, which the app needs).

```bash
git clone https://github.com/alanb43/youtube-mp3-converter.git
cd youtube-mp3-converter
python3 build_app.py
```

This installs **YouTube MP3 Converter** into `~/Applications`. Open it from Spotlight (⌘Space, type "YouTube MP3") or Finder.

### Run without installing

```bash
./run.sh
```

## Use

1. Paste a YouTube URL.
2. Choose where to save the file (default: Downloads) and a bitrate.
3. Click **Convert**.

**Which bitrate?** YouTube's audio is already compressed, so a higher bitrate mostly makes bigger files. Use **128 kbps** for speech (lectures, podcasts, audiobooks) and **192 kbps** for music. A 7-hour video is about 400 MB at 128 kbps.

## How errors are handled

- **Temporary network problems** (timeouts, dropped connections, HTTP 5xx/429, going offline): retried automatically with increasing delays, resuming the partial download. It only gives up after 8 failed attempts in a row with no progress. The status line shows what's happening, and there are no popups.
- **Permanent problems** (private/removed/geo-blocked video, age-restricted, live stream, invalid URL, disk full, ffmpeg missing): shown immediately in a dialog and never retried.

If YouTube changes something and downloads start failing with 403 errors, update yt-dlp by re-running `python3 build_app.py`.

Startup problems are logged to `~/Library/Logs/YouTubeMP3Converter.log`.

## Updating

Pull the latest code and re-run `python3 build_app.py`. It reinstalls the app with the newest yt-dlp.

## Legal

Downloading from YouTube may violate its Terms of Service, and copyright law varies by country. Only use this on content you own, that is licensed for it, or that is in the public domain. This project is provided as-is, with no affiliation to YouTube or Google.
