---
name: stream-clipper
description: Use when the user wants to clip a live stream or a long video into vertical shorts - "clip this stream", "watch this Twitch/YouTube/Kick live and clip the best moments", "make shorts from this VOD", "clip this video", or pastes a live stream link. Records the stream, watches live chat (Twitch) and audio for spikes, and auto-edits each highlight into a 1080x1920 short with word-by-word captions, optional face cam, and levelled sound.
---

# Stream Clipper

Turns live streams and long videos into ready-to-post vertical clips. Everything runs locally.

## First run (setup)

From this skill's folder (`~/.claude/skills/stream-clipper`):

- Mac/Linux: `bash setup.sh`
- Windows: `powershell -ExecutionPolicy Bypass -File setup.ps1`

Needs Python 3.10+ and ffmpeg. If ffmpeg is missing, install it (`brew install ffmpeg`, `winget install Gyan.FFmpeg`, or `sudo apt install ffmpeg`) and rerun setup. Setup is done when `.venv/` exists.

Below, `PY` means `.venv/bin/python` (Mac/Linux) or `.venv\Scripts\python` (Windows), run from this folder.

## Pick the mode

| User has | Run |
|---|---|
| A live stream link (Twitch, YouTube live, Kick) | `PY scripts/clipper.py --out <folder> live <url> [--minutes N]` |
| A finished video / VOD link or file | `PY scripts/clipper.py --out <folder> file <url-or-path> [--top 5]` |
| A video plus exact times | `PY scripts/clipper.py --out <folder> edit <path> --start 120 --end 155` |

Default output folder is `clips/` in the current directory; pass `--out` to put clips where the user wants them (e.g. their Desktop).

- **live**: records in 20s segments, reads Twitch chat anonymously (no login), and also listens for audio spikes. When chat jumps to 3x its normal rate for 3+ seconds, or the audio jumps ~9 dB, it clips 25s before to 10s after. Runs until the stream ends, `--minutes` is reached, or Ctrl+C. Run it in the background and report each `CLIP SAVED` line to the user.
- **file**: scans audio for the loudest spikes and clips the top N.
- **edit**: makes one vertical clip from a time range.

Every run appends to `<out>/clips.csv` (file, time range, trigger, peak chat rate).

## Tuning (`config.json`)

Edit `config.json` in this folder, or pass `--config other.json`.

- Too many clips: raise `chat_spike_multiplier` (e.g. 4) or `chat_min_msgs_per_sec`; raise `audio_jump_db`; set `audio_spikes: false` to use chat only.
- Too few clips: lower those values.
- Clip length: `pre_seconds` / `post_seconds`.
- Face cam on top: set `facecam_box` to `[x, y, width, height]` as fractions of the frame, e.g. bottom-left cam `[0, 0.65, 0.25, 0.35]`. To find it, grab a frame (`ffmpeg -ss 60 -i clip.mp4 -frames:v 1 frame.png`), look at it, and estimate the box. With no box the clip uses a blurred-background layout; `layout: "crop"` does a plain centre crop instead.
- Captions: `captions`, `caption_language` (default `en`; set the streamer's language, or `null` to auto-detect), `whisper_model` (`base` is faster, `small` is more accurate), `caption_words`, `caption_color`.
- Stop after N clips: `max_clips`.

## Rules

- Only clip streamers who allow clipping, and credit or tag them the way they ask. Remind the user of this the first time.
- Don't post clips for the user unless they ask.
