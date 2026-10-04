# TTS Book Reader

A local text-to-speech book reader that plays txt/epub novels with adjustable voice and speed.

## One-Click Launch

Double-click `start.bat` to run the application.

## Requirements

All dependencies are listed in `requirements.txt`. Install with:
```
pip install -r requirements.txt
```

## Features

- **Format support**: TXT (UTF-8/GBK) and EPUB files
- **Fixed voice**: Select one voice and it stays consistent throughout playback
- **Segmented playback**: Text is automatically split at sentence boundaries (~200 chars per segment)
- **Bookmarks**: Manually mark segments; jump to any bookmark anytime
- **Auto-resume**: Automatically resumes from the last played position when you reopen the same file
- **In-memory playback**: Audio is never saved to disk — streamed directly from memory
- **Keyboard shortcuts**:
  - `Ctrl+O` — Open file
  - `Space` — Play / Pause / Resume
  - `→` / `←` — Next / Previous segment
  - `B` — Toggle bookmark at current segment

## Voice Selection

The app uses Microsoft Edge TTS (same engine as Edge browser). Voice options include:
- English (many accents): en-US, en-GB, en-AU, etc.
- Chinese: zh-CN (Xiaoxiao, Xiaoyi, Yunxi, etc.)
- 20+ other languages

Select your preferred voice from the dropdown. The choice is saved automatically.

## Data Storage

Bookmarks and settings are stored in `%USERPROFILE%\.tts_reader_config\`:
- `bookmarks.json` — your saved bookmarks
- `settings.json` — voice and speed preferences

## Architecture

- **TTS engine**: edge-tts (Microsoft Edge online TTS, free, no API key needed)
- **Audio playback**: sounddevice + soundfile (direct buffer playback, no temp files)
- **Epub parsing**: ebooklib + BeautifulSoup
- **GUI**: tkinter (built into Python, no extra dependencies)
