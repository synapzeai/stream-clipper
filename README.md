# Stream Clipper

**Clip live streams while they're still live.** Paste a stream link and it records the stream, reads the live chat, and the second chat goes crazy it cuts the clip. Each clip comes out as a vertical 1080x1920 short with word-by-word captions, optional face cam, and levelled sound.

Works as a **Claude Code skill** (just ask Claude "clip this stream: <link>") or as a plain command-line tool.

- Twitch: live chat spikes + audio spikes
- YouTube live, Kick and anything else yt-dlp can open: audio spikes
- Finished videos and VODs: finds the loudest moments and clips them
- Runs 100% on your machine. No API keys, no accounts.

> Only clip streamers who allow clipping, and credit them the way they ask.

---

## Install

You need **Python 3.10+** and **ffmpeg**.

### As a Claude Code skill (recommended)

```bash
git clone https://github.com/synapzeai/stream-clipper ~/.claude/skills/stream-clipper
cd ~/.claude/skills/stream-clipper
bash setup.sh            # Windows: powershell -ExecutionPolicy Bypass -File setup.ps1
```

Then open Claude Code anywhere and say:

> clip this stream https://www.twitch.tv/somechannel

Claude handles the rest.

### Install ffmpeg

- Mac: `brew install ffmpeg`
- Windows: `winget install Gyan.FFmpeg`
- Linux: `sudo apt install ffmpeg`

---

## Use it from the terminal

```bash
# watch a live stream and clip highlights (stops when the stream ends, or after 60 min here)
.venv/bin/python scripts/clipper.py live https://www.twitch.tv/somechannel --minutes 60

# clip the 5 biggest moments from a finished video or VOD
.venv/bin/python scripts/clipper.py file https://www.youtube.com/watch?v=... --top 5

# make one vertical clip from exact times
.venv/bin/python scripts/clipper.py edit myvideo.mp4 --start 120 --end 155
```

Windows: use `.venv\Scripts\python` instead of `.venv/bin/python`.

Clips land in `clips/` (change with `--out`), with a `clips.csv` log of every clip.

While it runs you'll see a status line:

```
[15:12:41] rec 00:07:39 | chat 14.2/s (normal 4.6) | clips 2 | queued 0
[15:12:44] HIGHLIGHT (chat) at 00:07:31, clipping 00:07:06-00:07:41
[15:13:05] CLIP SAVED -> clips/xqc_20261119-151305_chat.mp4
```

## How it works

1. `streamlink` pulls the live stream (skipping Twitch ad breaks, with `yt-dlp` as a fallback) and `ffmpeg` saves it in 20-second chunks.
2. For Twitch, it joins the chat anonymously (read-only, no login) and tracks messages per second against a rolling "normal" level. Hype words like CLIP, LMAO, NO WAY count extra.
3. When chat hits 3x normal for 3+ seconds (or the audio jumps), it marks a highlight and cuts 25s before to 10s after.
4. Each clip is reframed to 9:16, captioned with Whisper (word-by-word highlight), and loudness-levelled to -14 LUFS.
5. Old raw chunks are deleted as it goes so it doesn't fill your disk.

## Settings (`config.json`)

| Setting | Default | What it does |
|---|---|---|
| `pre_seconds` / `post_seconds` | 25 / 10 | clip length around the spike |
| `chat_spike_multiplier` | 3.0 | how far above normal chat must go |
| `chat_min_msgs_per_sec` | 1.5 | ignore spikes in tiny chats |
| `chat_sustain_seconds` | 3 | how long the spike must last |
| `keywords` / `keyword_weight` | clip, lmao, no way... / 1.5 | hype words count extra |
| `audio_spikes` / `audio_jump_db` | true / 9 | clip on sudden loud moments too |
| `cooldown_seconds` | 45 | minimum gap between clips |
| `facecam_box` | null | `[x, y, w, h]` fractions of the frame, e.g. `[0, 0.65, 0.25, 0.35]` puts that cam on top |
| `layout` | auto | `auto`, `facecam`, `blur` (full frame on blurred fill), `crop` (centre crop) |
| `captions` / `whisper_model` | true / small | `base` is faster, `small` is more accurate |
| `caption_language` | en | language spoken on stream (`es`, `fr`... or `null` to auto-detect) |
| `max_clips` | 0 | stop after N clips (0 = no limit) |
| `keep_raw_minutes` | 20 | how much raw recording to keep on disk |

Too many clips? Raise `chat_spike_multiplier`. Too few? Lower it.

## License

MIT. Built with Claude Code by [SynapzeAI](https://github.com/synapzeai).
