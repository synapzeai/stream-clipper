#!/usr/bin/env python3
"""Stream Clipper: records a live stream, watches chat (Twitch) and audio for spikes,
and turns every highlight into a captioned vertical short.

  python scripts/clipper.py live <stream url> [--minutes 60]
  python scripts/clipper.py file <video file or VOD url> [--top 5]
  python scripts/clipper.py edit <video file> --start 120 --end 155

Only clip streamers who allow it, and credit them the way they ask.
"""
import argparse, csv, json, os, queue, re, shutil, signal, socket, statistics, subprocess, sys, tempfile, threading, time
from collections import deque
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DEFAULTS = {
    "pre_seconds": 25, "post_seconds": 10, "segment_seconds": 20, "cooldown_seconds": 45,
    "chat_window_seconds": 5, "chat_spike_multiplier": 3.0, "chat_min_msgs_per_sec": 1.5, "chat_sustain_seconds": 3,
    "keywords": ["clip", "lmao", "lmfao", "no way", "omg", "wtf", "w", "insane", "kekw", "lul", "pog", "poggers", "holy"],
    "keyword_weight": 1.5,
    "audio_spikes": True, "audio_jump_db": 9.0, "audio_floor_db": -30.0,
    "facecam_box": None, "layout": "auto",
    "captions": True, "whisper_model": "small", "caption_language": "en", "caption_words": 3, "caption_color": "#FFD23F",
    "loudness_lufs": -14, "keep_raw_minutes": 20, "out_dir": "clips", "max_clips": 0,
}

def log(*a):
    print(time.strftime("[%H:%M:%S]"), *a, flush=True)

def load_config(path):
    cfg = dict(DEFAULTS)
    p = Path(path) if path else ROOT / "config.json"
    if p.exists():
        cfg.update(json.loads(p.read_text()))
    return cfg

def need(tool):
    if not shutil.which(tool):
        sys.exit(f"Missing '{tool}'. Install it first (see README).")

def streamlink_cmd():
    exe = shutil.which("streamlink") or (str(Path(sys.executable).parent / "streamlink") if (Path(sys.executable).parent / "streamlink").exists() else None)
    return [exe] if exe else None

def ytdlp_cmd():
    exe = shutil.which("yt-dlp")
    return [exe] if exe else [sys.executable, "-m", "yt_dlp"]

def run(cmd, **kw):
    return subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **kw)

def probe(path):
    out = run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)]).stdout
    d = json.loads(out)
    v = next((s for s in d["streams"] if s["codec_type"] == "video"), None)
    a = next((s for s in d["streams"] if s["codec_type"] == "audio"), None)
    return {"w": int(v["width"]) if v else 0, "h": int(v["height"]) if v else 0, "audio": a is not None,
            "duration": float(d["format"].get("duration", 0) or 0)}

# --------------------------------------------------------------------------- audio loudness

def loudness_series(path, start_offset=0.0, step=0.5):
    """Return [(time, dB)] every `step` seconds for a media file."""
    import numpy as np
    sr = 8000
    p = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"],
                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    x = np.frombuffer(p.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    n = int(sr * step)
    out = []
    for i in range(0, len(x) - n + 1, n):
        rms = float(np.sqrt(np.mean(x[i:i + n] ** 2)) + 1e-9)
        out.append((start_offset + i / sr, 20 * np.log10(rms)))
    return out

class AudioSpikes:
    def __init__(self, cfg):
        self.cfg, self.series = cfg, deque(maxlen=int(240 / 0.5))

    def feed(self, pts):
        """Feed [(t, dB)]; return list of spike start times."""
        hits = []
        for t, db in pts:
            self.series.append((t, db))
            if len(self.series) < 120:  # need 60s of history
                continue
            vals = [d for _, d in self.series]
            base = statistics.median(vals[:-4])
            win = sum(vals[-4:]) / 4  # last 2 seconds
            if win > self.cfg["audio_floor_db"] and win - base >= self.cfg["audio_jump_db"]:
                hits.append(t - 2.0)
        return hits

# --------------------------------------------------------------------------- twitch chat

class TwitchChat(threading.Thread):
    """Anonymous read-only Twitch IRC (no login). Stores (wallclock, user, text)."""
    def __init__(self, channel):
        super().__init__(daemon=True)
        self.channel = channel.lower()
        self.msgs = deque(maxlen=50000)
        self.ok = False
        self.stop = threading.Event()
        self.lock = threading.Lock()

    def recent(self, seconds, now):
        """Thread-safe snapshot of messages from the last `seconds`."""
        with self.lock:
            out = []
            for m in reversed(self.msgs):
                if m[0] < now - seconds:
                    break
                out.append(m)
        return out

    def run(self):
        while not self.stop.is_set():
            try:
                s = socket.create_connection(("irc.chat.twitch.tv", 6667), timeout=30)
                s.sendall(f"PASS SCHMOOPIIE\r\nNICK justinfan{int(time.time()) % 90000 + 10000}\r\nJOIN #{self.channel}\r\n".encode())
                buf = ""
                while not self.stop.is_set():
                    data = s.recv(8192).decode("utf-8", "ignore")
                    if not data:
                        break
                    buf += data
                    *lines, buf = buf.split("\r\n")
                    for line in lines:
                        if line.startswith("PING"):
                            s.sendall(line.replace("PING", "PONG", 1).encode() + b"\r\n")
                        elif " JOIN #" in line:
                            self.ok = True
                        m = re.match(r":(\w+)!\S+ PRIVMSG #\w+ :(.*)", line)
                        if m:
                            with self.lock:
                                self.msgs.append((time.time(), m.group(1), m.group(2)))
            except OSError:
                time.sleep(3)

class ChatSpikes:
    def __init__(self, cfg):
        self.cfg = cfg
        self.kw = [k.lower() for k in cfg["keywords"]]
        self.history = deque(maxlen=400)  # per-second weighted rates
        self.above = 0
        self.rate = 0.0
        self.base = 0.0

    def weight(self, text):
        t = " " + re.sub(r"[^a-z0-9 ]", " ", text.lower()) + " "
        return self.cfg["keyword_weight"] if any(f" {k} " in t for k in self.kw) else 1.0

    def tick(self, chat, now):
        """Called once a second. Returns spike wallclock time or None."""
        w = self.cfg["chat_window_seconds"]
        recent = chat.recent(w, now)
        self.rate = sum(self.weight(m[2]) for m in recent) / w
        hist = list(self.history)
        self.history.append(self.rate)
        if len(hist) < 60:  # 60s warm-up so 'normal' is real
            return None
        self.base = statistics.median(hist[-180:])
        hot = self.rate >= max(self.base * self.cfg["chat_spike_multiplier"], self.cfg["chat_min_msgs_per_sec"])
        self.above = self.above + 1 if hot else 0
        if self.above == self.cfg["chat_sustain_seconds"]:
            return now - self.cfg["chat_sustain_seconds"] - w / 2
        return None

# --------------------------------------------------------------------------- captions

def find_font():
    for f in ["/System/Library/Fonts/Supplemental/Arial Black.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
              "/Library/Fonts/Arial Bold.ttf", "C:/Windows/Fonts/ariblk.ttf", "C:/Windows/Fonts/arialbd.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"]:
        if os.path.exists(f):
            return f
    return None

_whisper = {}
def transcribe(path, model_name, language="en"):
    from faster_whisper import WhisperModel
    if model_name not in _whisper:
        log(f"loading Whisper '{model_name}' (first run downloads it)...")
        _whisper[model_name] = WhisperModel(model_name, device="cpu", compute_type="int8")
    segs, _ = _whisper[model_name].transcribe(str(path), word_timestamps=True, vad_filter=True, language=language or None)
    words = []
    for s in segs:
        for w in s.words or []:
            if w.word.strip():
                words.append((w.start, w.end, w.word.strip()))
    return words

def caption_frames(words, duration, folder, cfg, fps=15, width=1080):
    """Render a transparent PNG sequence (word-by-word highlight). Returns True if any captions."""
    from PIL import Image, ImageDraw, ImageFont
    fp = find_font()
    size = 74
    font = ImageFont.truetype(fp, size) if fp else ImageFont.load_default(size)
    chunks, cur = [], []
    for i, w in enumerate(words):
        cur.append(w)
        nxt = words[i + 1] if i + 1 < len(words) else None
        if len(cur) == cfg["caption_words"] or re.search(r"[.!?,]$", w[2]) or (nxt and nxt[0] - w[1] > 0.6) or not nxt:
            chunks.append(cur); cur = []
    blank = Image.new("RGBA", (width, 260), (0, 0, 0, 0))
    cache = {}

    def render(ci, wi):
        if (ci, wi) in cache:
            return cache[(ci, wi)]
        toks = [re.sub(r"[^\w'%$#@!?.,-]", "", w[2]).upper() for w in chunks[ci]]
        im = Image.new("RGBA", (width, 260), (0, 0, 0, 0))
        d = ImageDraw.Draw(im, "RGBA")
        f = font
        space = size * 0.3
        widths = [d.textlength(t, font=f) for t in toks]
        total = sum(widths) + space * (len(toks) - 1)
        while total > width - 80 and f.size > 40:
            f = ImageFont.truetype(fp, f.size - 4) if fp else ImageFont.load_default(f.size - 4)
            widths = [d.textlength(t, font=f) for t in toks]
            total = sum(widths) + space * (len(toks) - 1)
        x = (width - total) / 2
        for k, t in enumerate(toks):
            col = cfg["caption_color"] if k == wi else "#FFFFFF"
            d.text((x, 130), t, font=f, fill=col, anchor="lm", stroke_width=9, stroke_fill="#000000")
            x += widths[k] + space
        cache[(ci, wi)] = im
        return im

    n = int(duration * fps) + 1
    any_cap = False
    for i in range(n):
        t = i / fps
        im = blank
        for ci, ch in enumerate(chunks):
            end = chunks[ci + 1][0][0] if ci + 1 < len(chunks) else ch[-1][1] + 0.5
            end = min(end, ch[-1][1] + 0.8)
            if ch[0][0] <= t < end:
                wi = max([k for k, w in enumerate(ch) if w[0] <= t] or [0])
                im = render(ci, wi); any_cap = True
                break
        im.save(folder / f"c{i:05d}.png")
    return any_cap

# --------------------------------------------------------------------------- vertical edit

def make_vertical(src, dst, cfg, title=None):
    """src: raw landscape clip -> dst: 1080x1920 short with captions + levelled audio."""
    info = probe(src)
    W, H = info["w"], info["h"]
    box = cfg.get("facecam_box")
    layout = cfg["layout"]
    if layout == "auto":
        layout = "facecam" if box else "blur"
    if layout == "facecam" and box:
        x, y, w, h = [float(v) for v in box]
        cam = f"crop={int(W*w)}:{int(H*h)}:{int(W*x)}:{int(H*y)},scale=1080:640:force_original_aspect_ratio=increase,crop=1080:640"
        gw = min(W, int(H * 1080 / 1280)) // 2 * 2
        vf = (f"[0:v]split[a][b];[a]{cam}[top];[b]crop={gw}:{H}:(iw-{gw})/2:0,scale=1080:1280[bot];"
              f"[top][bot]vstack[base]")
        cap_y = 640 - 130
    elif layout == "crop":
        gw = min(W, int(H * 9 / 16)) // 2 * 2
        vf = f"[0:v]crop={gw}:{H}:(iw-{gw})/2:0,scale=1080:1920[base]"
        cap_y = 1250
    else:  # blur: full frame centred over a blurred fill
        vf = ("[0:v]split[a][b];[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,gblur=sigma=28,eq=brightness=-0.12[bg];"
              "[b]scale=1080:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2[base]")
        cap_y = 960 + int(1080 * H / W / 2) + 40
    tmp = Path(tempfile.mkdtemp(prefix="clipcap_"))
    inputs = ["-i", str(src)]
    chain = vf
    out_v = "[base]"
    try:
        if cfg["captions"] and info["audio"]:
            words = transcribe(src, cfg["whisper_model"], cfg.get("caption_language", "en"))
            if words and caption_frames(words, info["duration"], tmp, cfg):
                inputs += ["-framerate", "15", "-i", str(tmp / "c%05d.png")]
                chain += f";[1:v]format=rgba[cap];[base][cap]overlay=0:{cap_y}:eof_action=pass[v]"
                out_v = "[v]"
        af = ["-af", f"loudnorm=I={cfg['loudness_lufs']}:TP=-1.5:LRA=11"] if info["audio"] else []
        cmd = ["ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex", chain, "-map", out_v]
        if info["audio"]:
            cmd += ["-map", "0:a:0", *af, "-c:a", "aac", "-b:a", "160k", "-ar", "48000"]
        cmd += ["-r", "30", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                "-movflags", "+faststart", "-shortest", str(dst)]
        run(cmd)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return dst

# --------------------------------------------------------------------------- live mode

class Recorder:
    def __init__(self, url, rec_dir, seg):
        self.url, self.dir, self.seg = url, rec_dir, seg
        self.t0 = None
        self.list = rec_dir / "segments.csv"
        self.anchors = []  # (wallclock when segment appeared, segment end in stream time)
        self.nseg = 0

    def start(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        sl = streamlink_cmd()
        # streamlink skips Twitch ad breaks; yt-dlp is the fallback
        src = ([*sl, "--stdout", "--loglevel", "error", self.url, "best"] if sl else
               [*ytdlp_cmd(), "-q", "--no-warnings", "--no-part", "-f", "best", "-o", "-", self.url])
        self.y = subprocess.Popen(src, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.f = subprocess.Popen(["ffmpeg", "-v", "error", "-i", "pipe:0", "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
                                   "-f", "segment", "-segment_time", str(self.seg), "-segment_format", "mpegts",
                                   "-segment_list", str(self.list), "-segment_list_type", "csv", "-reset_timestamps", "1",
                                   str(self.dir / "seg_%05d.ts")], stdin=self.y.stdout, stderr=subprocess.DEVNULL)
        self.y.stdout.close()

    def poll_t0(self):
        if self.t0 is None and (self.dir / "seg_00000.ts").exists() and (self.dir / "seg_00000.ts").stat().st_size > 0:
            self.t0 = time.time() - 1.0
        return self.t0

    def segments(self):
        if not self.list.exists():
            return []
        rows = []
        for r in csv.reader(self.list.read_text().splitlines()):
            if len(r) >= 3:
                rows.append((self.dir / r[0], float(r[1]), float(r[2])))
        return rows

    def update_anchors(self):
        segs = self.segments()
        if len(segs) > self.nseg:
            self.nseg = len(segs)
            self.anchors.append((time.time(), segs[-1][2]))

    def wall_to_pos(self, wall):
        """Map a wallclock time to stream position. Re-anchors on every segment so skipped ads don't cause drift."""
        if not self.anchors:
            return wall - self.t0
        best = min(self.anchors, key=lambda a: abs(a[0] - wall))
        return best[1] + (wall - best[0])

    def covered_until(self):
        s = self.segments()
        return s[-1][2] if s else 0.0

    def alive(self):
        return self.f.poll() is None

    def stop(self):
        for p in (self.y, self.f):
            if p.poll() is None:
                p.send_signal(signal.SIGINT)
        for p in (self.y, self.f):
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()

def cut_from_segments(segs, a, b, dst):
    use = [s for s in segs if s[2] > a and s[1] < b]
    if not use:
        return False
    lst = dst.with_suffix(".txt")
    lst.write_text("".join(f"file '{p.resolve()}'\n" for p, _, _ in use))
    off = max(0.0, a - use[0][1])
    run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-ss", f"{off:.2f}", "-t", f"{b - a:.2f}",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-c:a", "aac", "-b:a", "192k", str(dst)])
    lst.unlink(missing_ok=True)
    return True

def clip_name(url):
    m = re.search(r"(?:twitch\.tv|kick\.com|youtube\.com/@?)/?([\w-]+)", url)
    return (m.group(1) if m else "stream")[:30]

def write_log(out, row):
    f = out / "clips.csv"
    new = not f.exists()
    with f.open("a", newline="") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["file", "made_at", "source", "start_sec", "end_sec", "trigger", "peak"])
        w.writerow(row)

def live(url, cfg, minutes):
    need("ffmpeg")
    out = Path(cfg["out_dir"]).resolve(); out.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="clipper_rec_"))
    rec = Recorder(url, work, cfg["segment_seconds"])
    chan = re.search(r"twitch\.tv/([\w]+)", url)
    chat = TwitchChat(chan.group(1)) if chan else None
    chat_det = ChatSpikes(cfg) if chat else None
    audio_det = AudioSpikes(cfg) if cfg["audio_spikes"] else None
    if chat: chat.start()
    rec.start()
    log(f"recording {url}")
    log("chat: Twitch IRC (anonymous)" if chat else "chat: not available for this site, using audio spikes")
    pending, made, last_hit, seen_segs = [], 0, -1e9, 0
    jobs = queue.Queue()
    stop_at = time.time() + minutes * 60 if minutes else None
    name = clip_name(url)

    def worker():
        nonlocal made
        while True:
            job = jobs.get()
            if job is None:
                return
            a, b, why, peak = job
            stamp = time.strftime("%Y%m%d-%H%M%S")
            raw = work / f"raw_{stamp}.mp4"
            dst = out / f"{name}_{stamp}_{why}.mp4"
            try:
                if cut_from_segments(rec.segments(), a, b, raw):
                    make_vertical(raw, dst, cfg)
                    made += 1
                    write_log(out, [dst.name, stamp, url, f"{a:.1f}", f"{b:.1f}", why, f"{peak:.1f}"])
                    log(f"CLIP SAVED -> {dst}")
            except subprocess.CalledProcessError as e:
                log("clip failed:", (e.stderr or "")[-400:])
            finally:
                raw.unlink(missing_ok=True)

    wt = threading.Thread(target=worker, daemon=True); wt.start()
    last_status = 0
    try:
        while True:
            time.sleep(1)
            now = time.time()
            t0 = rec.poll_t0()
            if t0 is None:
                if not rec.alive():
                    sys.exit("Could not open the stream. Is it live? Check the URL.")
                continue
            rec.update_anchors()
            pos = rec.wall_to_pos(now)
            hits = []
            if chat_det:
                w = chat_det.tick(chat, now)
                if w: hits.append((rec.wall_to_pos(w), "chat", chat_det.rate))
            segs = rec.segments()
            if audio_det and len(segs) > seen_segs:
                for p, s, e in segs[seen_segs:]:
                    for t in audio_det.feed(loudness_series(p, s)):
                        hits.append((t, "audio", 0.0))
                seen_segs = len(segs)
            for t, why, peak in hits:
                if t - last_hit >= cfg["cooldown_seconds"]:
                    last_hit = t
                    a, b = max(0.0, t - cfg["pre_seconds"]), t + cfg["post_seconds"]
                    pending.append((a, b, why, peak))
                    log(f"HIGHLIGHT ({why}) at {fmt(t)}, clipping {fmt(a)}-{fmt(b)}")
            cov = rec.covered_until()
            for job in [j for j in pending if j[1] <= cov]:
                pending.remove(job); jobs.put(job)
            if now - last_status >= 5:
                last_status = now
                c = f" | chat {chat_det.rate:.1f}/s (normal {chat_det.base:.1f})" if chat_det else ""
                log(f"rec {fmt(pos)}{c} | clips {made} | queued {len(pending) + jobs.qsize()}")
            keep = cfg["keep_raw_minutes"] * 60
            for p, s, e in segs[:-2]:
                if e < pos - keep and p.exists() and not any(j[0] <= e for j in pending):
                    p.unlink()
            if cfg["max_clips"] and made >= cfg["max_clips"]:
                log("max_clips reached"); break
            if stop_at and now >= stop_at:
                log("time limit reached"); break
            if not rec.alive():
                log("stream ended"); break
    except KeyboardInterrupt:
        log("stopping...")
    finally:
        rec.stop()
    cov = rec.covered_until()
    for a, b, why, peak in pending:
        if a < cov:
            jobs.put((a, min(b, cov), why, peak))
    jobs.put(None); wt.join()
    if chat: chat.stop.set()
    shutil.rmtree(work, ignore_errors=True)
    log(f"done: {made} clip(s) in {out}")

def fmt(s):
    s = int(max(0, s)); return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"

# --------------------------------------------------------------------------- file mode

def file_mode(src, cfg, top):
    need("ffmpeg")
    out = Path(cfg["out_dir"]).resolve(); out.mkdir(parents=True, exist_ok=True)
    tmpdir = None
    if re.match(r"https?://", src):
        tmpdir = Path(tempfile.mkdtemp(prefix="clipper_dl_"))
        log("downloading video...")
        run([*ytdlp_cmd(), "-q", "--no-warnings", "-f", "bv*[height<=1080]+ba/b", "--merge-output-format", "mp4",
             "-o", str(tmpdir / "src.%(ext)s"), src])
        src = str(next(tmpdir.glob("src.*")))
    log("scanning audio for the biggest moments...")
    import numpy as np
    pts = loudness_series(src)
    if len(pts) < 20:
        sys.exit("Video is too short to scan.")
    t_arr = np.array([p[0] for p in pts]); db = np.array([p[1] for p in pts])
    short = np.convolve(db, np.ones(4) / 4, mode="same")              # 2s loudness
    half = 60                                                         # +-30s context
    base = np.array([np.median(db[max(0, i - half):i + half]) for i in range(len(db))])
    score = np.where(short > cfg["audio_floor_db"], short - base, -99)
    score[:6] = -99; score[-6:] = -99  # ignore file edges
    picked = []
    for i in np.argsort(-score):
        if score[i] < 2.0:  # nothing stands out from its surroundings
            break
        t = float(t_arr[i]) - 1.0
        if all(abs(t - p) >= cfg["cooldown_seconds"] for p in picked):
            picked.append(t)
            log(f"moment at {fmt(t)} (+{score[i]:.1f} dB above its surroundings)")
        if len(picked) >= top:
            break
    if not picked:
        log("no moment stood out; the audio is very flat")
    dur = probe(src)["duration"]
    name = Path(src).stem[:30]
    for t in sorted(picked):
        span = cfg["pre_seconds"] + cfg["post_seconds"]
        a = min(max(0.0, t - cfg["pre_seconds"]), max(0.0, dur - span))  # slide, don't shorten
        b = min(dur, a + span)
        log(f"highlight at {fmt(t)}, clipping {fmt(a)}-{fmt(b)}")
        dst = out / f"{name}_{int(a)}s.mp4"
        edit(src, a, b, dst, cfg)
        write_log(out, [dst.name, time.strftime("%Y%m%d-%H%M%S"), src, f"{a:.1f}", f"{b:.1f}", "audio", ""])
    if tmpdir:
        shutil.rmtree(tmpdir, ignore_errors=True)
    log(f"done: {len(picked)} clip(s) in {out}")

def edit(src, a, b, dst, cfg):
    raw = Path(tempfile.mkdtemp(prefix="clipper_cut_")) / "raw.mp4"
    run(["ffmpeg", "-v", "error", "-y", "-ss", f"{a:.2f}", "-i", str(src), "-t", f"{b - a:.2f}",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-c:a", "aac", "-b:a", "192k", str(raw)])
    make_vertical(raw, Path(dst), cfg)
    shutil.rmtree(raw.parent, ignore_errors=True)
    log(f"CLIP SAVED -> {dst}")

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="path to config.json (default: next to this repo)")
    ap.add_argument("--out", help="output folder for clips")
    sub = ap.add_subparsers(dest="cmd", required=True)
    l = sub.add_parser("live"); l.add_argument("url"); l.add_argument("--minutes", type=float, default=0)
    f = sub.add_parser("file"); f.add_argument("src"); f.add_argument("--top", type=int, default=5)
    e = sub.add_parser("edit"); e.add_argument("src"); e.add_argument("--start", type=float, required=True)
    e.add_argument("--end", type=float, required=True); e.add_argument("-o", "--output")
    args = ap.parse_args()
    cfg = load_config(args.config)
    if args.out: cfg["out_dir"] = args.out
    if args.cmd == "live":
        live(args.url, cfg, args.minutes)
    elif args.cmd == "file":
        file_mode(args.src, cfg, args.top)
    else:
        out = Path(cfg["out_dir"]); out.mkdir(parents=True, exist_ok=True)
        edit(args.src, args.start, args.end, args.output or out / f"{Path(args.src).stem}_{int(args.start)}s.mp4", cfg)

if __name__ == "__main__":
    main()
