#!/usr/bin/env python3
"""Clipping Machine: runs a clipping page on a loop.

  python scripts/machine.py scout      # find fresh clips for your game on Twitch
  python scripts/machine.py make       # download, edit (hook + captions + credit), QA
  python scripts/machine.py post       # schedule finished clips through Zernio (dry run unless --live)
  python scripts/machine.py stats      # pull views/likes/shares, health check
  python scripts/machine.py report     # write state/report.md + state/dashboard.html
  python scripts/machine.py cycle      # all of the above, in order

Settings live in machine.json, keys in .env. Claude Code runs CYCLE.md after every cycle to
learn from the stats and tune machine.json. Only clip streamers who allow it, and credit them.
"""
import argparse, csv, datetime as dt, html, json, os, re, shutil, sys, tempfile, time, urllib.error, urllib.parse, urllib.request, uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import clipper  # noqa: E402  (reuses make_vertical, loudness_series, probe, find_font)

STATE = ROOT / "state"
MDEFAULTS = {
    "game": "Grand Theft Auto VI",
    "languages": ["en"],
    "lookback_hours": 24,
    "min_views": 300,
    "min_seconds": 12,
    "max_seconds": 60,
    "allowed_broadcasters": [],      # empty = any; fill this in if you only clip streamers who opted in
    "blocked_broadcasters": [],
    "blocked_words": ["nsfw", "leak", "spoiler"],
    "clips_per_cycle": 2,
    "hook_seconds": 2.5,
    "hook_text": "WAIT FOR IT...",
    "facecam_box": None,
    "platforms": {"tiktok": "", "instagram": ""},   # Zernio account ids
    "timezone": "Australia/Melbourne",
    "post_slots": ["08:00", "12:00", "16:00", "19:00", "22:00"],
    "max_posts_per_day": 5,
    "stuck_views": 20,
    "stuck_after_minutes": 60,
    "pause_hours_after_stuck": 24,
    "hashtags": ["#GTA6", "#GTAVI", "#gaming"],
}

# --------------------------------------------------------------------------- plumbing

def log(*a):
    print(time.strftime("[%H:%M:%S]"), *a, flush=True)

def load_env():
    p = ROOT / ".env"
    if p.exists():
        for line in p.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

def mcfg():
    c = dict(MDEFAULTS)
    p = ROOT / "machine.json"
    if p.exists():
        c.update(json.loads(p.read_text()))
    return c

def jload(name, default):
    p = STATE / name
    return json.loads(p.read_text()) if p.exists() else default

def jsave(name, data):
    STATE.mkdir(exist_ok=True)
    tmp = STATE / (name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(STATE / name)

def http(method, url, headers=None, body=None, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    h = dict(headers or {})
    if body is not None:
        h.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            txt = r.read().decode() if raw is None else ""
            return json.loads(txt) if txt.strip() else {}
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{method} {url.split('?')[0]} -> {e.code}: {e.read().decode()[:300]}")

def now():
    return dt.datetime.now(dt.timezone.utc)

def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")

# --------------------------------------------------------------------------- scout (Twitch Helix)

def twitch_headers():
    cid, sec = os.environ.get("TWITCH_CLIENT_ID"), os.environ.get("TWITCH_CLIENT_SECRET")
    if not cid or not sec:
        sys.exit("Add TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET to .env (free app at dev.twitch.tv/console).")
    q = urllib.parse.urlencode({"client_id": cid, "client_secret": sec, "grant_type": "client_credentials"})
    tok = http("POST", f"https://id.twitch.tv/oauth2/token?{q}")["access_token"]
    return {"Client-Id": cid, "Authorization": f"Bearer {tok}"}

def scout():
    c = mcfg(); h = twitch_headers()
    seen = set(jload("seen.json", []))
    g = http("GET", "https://api.twitch.tv/helix/games?" + urllib.parse.urlencode({"name": c["game"]}), h)["data"]
    if not g:
        sys.exit(f"Twitch has no category called '{c['game']}'.")
    gid = g[0]["id"]
    end = now(); start = end - dt.timedelta(hours=c["lookback_hours"])
    clips, cursor = [], None
    for _ in range(3):  # up to 300 clips; Twitch returns them most-viewed first
        q = {"game_id": gid, "started_at": iso(start), "ended_at": iso(end), "first": 100}
        if cursor: q["after"] = cursor
        r = http("GET", "https://api.twitch.tv/helix/clips?" + urllib.parse.urlencode(q), h)
        clips += r.get("data", [])
        cursor = r.get("pagination", {}).get("cursor")
        if not cursor: break
    allow = {b.lower() for b in c["allowed_broadcasters"]}
    block = {b.lower() for b in c["blocked_broadcasters"]}
    out, why = [], {}
    def skip(reason):
        why[reason] = why.get(reason, 0) + 1
    for k in clips:
        name = k["broadcaster_name"].lower()
        if k["id"] in seen: skip("already used"); continue
        if allow and name not in allow: skip("not on allow list"); continue
        if name in block: skip("blocked streamer"); continue
        if c["languages"] and k.get("language") not in c["languages"]: skip("language"); continue
        if k["view_count"] < c["min_views"]: skip("too few views"); continue
        if not (c["min_seconds"] <= k["duration"] <= c["max_seconds"]): skip("length"); continue
        if any(w in k["title"].lower() for w in c["blocked_words"]): skip("blocked word"); continue
        out.append({"id": k["id"], "url": k["url"], "title": k["title"], "broadcaster": k["broadcaster_name"],
                    "views": k["view_count"], "duration": k["duration"], "created_at": k["created_at"]})
    # one clip per streamer per cycle keeps the page varied
    picked, names = [], set()
    for k in sorted(out, key=lambda x: -x["views"]):
        if k["broadcaster"] not in names:
            picked.append(k); names.add(k["broadcaster"])
    jsave("candidates.json", picked)
    log(f"scout: {len(clips)} clips in the last {c['lookback_hours']}h, {len(picked)} candidates")
    for r, n in sorted(why.items(), key=lambda x: -x[1]):
        log(f"   skipped {n}: {r}")
    return picked

# --------------------------------------------------------------------------- make (edit + hook + QA)

def text_png(text, path, size=86, w=1080, h=200, color="#FFFFFF"):
    from PIL import Image, ImageDraw, ImageFont
    fp = clipper.find_font()
    f = ImageFont.truetype(fp, size) if fp else ImageFont.load_default(size)
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(im, "RGBA")
    while d.textlength(text, font=f) > w - 80 and f.size > 30:
        f = ImageFont.truetype(fp, f.size - 4) if fp else ImageFont.load_default(f.size - 4)
    d.text((w / 2, h / 2), text, font=f, fill=color, anchor="mm", stroke_width=8, stroke_fill="#000000")
    im.save(path)

def loudest_window(path, secs):
    pts = clipper.loudness_series(path, step=0.25)
    n = max(1, int(secs / 0.25))
    best, bt = -999, 0.0
    for i in range(min(8, len(pts)), max(0, len(pts) - n)):   # skip the first 2s
        m = sum(p[1] for p in pts[i:i + n]) / n
        if m > best: best, bt = m, pts[i][0]
    return bt, best

def add_hook_and_credit(src, dst, credit, c):
    """Play the loudest moment first with a hook line, then the full clip; credit on screen throughout."""
    dur = clipper.probe(src)["duration"]
    hs = float(c["hook_seconds"])
    t, _ = loudest_window(src, hs)
    a = max(0.0, min(t - 0.5, dur - hs))
    tmp = Path(tempfile.mkdtemp(prefix="machine_"))
    try:
        text_png(c["hook_text"], tmp / "hook.png", size=96, color="#FFD23F")
        text_png(credit, tmp / "credit.png", size=46, h=90)
        # hook (zoomed, with hook line) then the full clip, credit overlaid on both
        fc = ("[0:v]trim=start={a:.2f}:duration={hs},setpts=PTS-STARTPTS,scale=1188:2112,crop=1080:1920[h0];"
              "[0:a]atrim=start={a:.2f}:duration={hs},asetpts=PTS-STARTPTS[ha];"
              "[1:v]format=rgba[hp];[h0][hp]overlay=0:300:shortest=1[hv];"
              "[0:v]setpts=PTS-STARTPTS[fv];[0:a]asetpts=PTS-STARTPTS[fa];"
              "[hv][ha][fv][fa]concat=n=2:v=1:a=1[cv][ca];"
              "[2:v]format=rgba[cp];[cv][cp]overlay=0:1760:shortest=0:eof_action=repeat[v]").format(a=a, hs=hs)
        clipper.run(["ffmpeg", "-v", "error", "-y", "-i", str(src),
                     "-loop", "1", "-t", str(hs), "-i", str(tmp / "hook.png"),
                     "-loop", "1", "-t", f"{dur + hs:.2f}", "-i", str(tmp / "credit.png"),
                     "-filter_complex", fc, "-map", "[v]", "-map", "[ca]",
                     "-r", "30", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                     "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", "-t", f"{dur + hs:.2f}", str(dst)])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return a

def qa(path, c):
    """Return a list of problems; empty means it passes."""
    probs = []
    try:
        info = clipper.probe(path)
    except Exception as e:
        return [f"unreadable: {e}"]
    if (info["w"], info["h"]) != (1080, 1920): probs.append(f"size {info['w']}x{info['h']}")
    if not info["audio"]: probs.append("no audio")
    if not (c["min_seconds"] <= info["duration"] <= c["max_seconds"] + c["hook_seconds"] + 1):
        probs.append(f"length {info['duration']:.1f}s")
    if info["audio"]:
        pts = clipper.loudness_series(path, step=1.0)
        if pts and max(p[1] for p in pts) < -35: probs.append("near-silent audio")
    if path.stat().st_size < 300_000: probs.append("file too small")
    return probs

def caption_for(k, c):
    title = re.sub(r"\s+", " ", k["title"]).strip()
    return f"{title}\n\n🎥 {k['broadcaster']} on Twitch (twitch.tv/{k['broadcaster'].lower()})\n\n" + " ".join(c["hashtags"])

def make():
    c = mcfg()
    cands = jload("candidates.json", [])
    queue = jload("queue.json", [])
    seen = set(jload("seen.json", []))
    out = ROOT / "clips"; out.mkdir(exist_ok=True)
    ccfg = clipper.load_config(None)
    ccfg["facecam_box"] = c["facecam_box"]
    made = 0
    for k in cands:
        if made >= c["clips_per_cycle"]: break
        if k["id"] in seen: continue
        seen.add(k["id"]); jsave("seen.json", sorted(seen))
        tmp = Path(tempfile.mkdtemp(prefix="machine_dl_"))
        try:
            log(f"make: {k['broadcaster']} - {k['title'][:60]} ({k['views']} views)")
            clipper.run([*clipper.ytdlp_cmd(), "-q", "--no-warnings", "-f", "best[ext=mp4]/best",
                         "-o", str(tmp / "raw.%(ext)s"), k["url"]])
            raw = next(tmp.glob("raw.*"))
            vert = tmp / "vert.mp4"
            clipper.make_vertical(raw, vert, ccfg)
            dst = out / f"{dt.date.today():%Y%m%d}_{re.sub(r'[^a-z0-9]+', '', k['broadcaster'].lower())}_{k['id'][:10]}.mp4"
            hook_at = add_hook_and_credit(vert, dst, f"twitch.tv/{k['broadcaster'].lower()}", c)
            probs = qa(dst, c)
            item = {**k, "file": str(dst.relative_to(ROOT)), "hook_at": round(hook_at, 2),
                    "caption": caption_for(k, c), "made_at": iso(now()),
                    "status": "ready" if not probs else "rejected", "qa": probs, "posts": {}}
            queue.append(item); jsave("queue.json", queue)
            log(f"   {'READY' if not probs else 'REJECTED ' + ', '.join(probs)} -> {dst.name}")
            made += not probs
        except Exception as e:
            log(f"   failed: {e}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    log(f"make: {made} clip(s) ready")

# --------------------------------------------------------------------------- post (Zernio)

ZERNIO = "https://zernio.com/api/v1"

def zh():
    key = os.environ.get("ZERNIO_API_KEY")
    if not key:
        sys.exit("Add ZERNIO_API_KEY to .env (Zernio dashboard > API keys).")
    return {"Authorization": f"Bearer {key}"}

def next_slots(c, posted_today, n):
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(c["timezone"])
    taken = {p for p in jload("slots.json", [])}
    local = now().astimezone(tz)
    out, day = [], 0
    while len(out) < n and day < 7:
        d = (local + dt.timedelta(days=day)).date()
        used = posted_today if day == 0 else 0
        for s in c["post_slots"]:
            hh, mm = map(int, s.split(":"))
            t = dt.datetime(d.year, d.month, d.day, hh, mm, tzinfo=tz)
            key = t.isoformat()
            if t > local + dt.timedelta(minutes=10) and key not in taken and used < c["max_posts_per_day"]:
                out.append(t); used += 1
                if len(out) >= n: break
        day += 1
    return out

def post(live=False):
    c = mcfg(); queue = jload("queue.json", []); state = jload("health.json", {})
    ready = [q for q in queue if q["status"] == "ready"]
    if not ready:
        log("post: nothing ready"); return
    paused = {p for p, until in state.get("paused", {}).items() if until > iso(now())}
    targets = {p: a for p, a in c["platforms"].items() if a and p not in paused}
    if paused: log(f"post: paused (view jail guard): {', '.join(sorted(paused))}")
    if not targets:
        log("post: no platform account ids set in machine.json (or all paused)"); return
    today = dt.date.today().isoformat()
    posted_today = sum(1 for q in queue if q.get("scheduled_for", "").startswith(today))
    slots = next_slots(c, posted_today, len(ready))
    taken = jload("slots.json", [])
    for item, slot in zip(ready, slots):
        f = ROOT / item["file"]
        if not live:
            log(f"post (dry run): {f.name} -> {', '.join(targets)} at {slot:%a %H:%M}")
            continue
        h = zh()
        pre = http("POST", f"{ZERNIO}/media/presign", h, {"filename": f.name, "contentType": "video/mp4", "size": f.stat().st_size})
        http("PUT", pre["uploadUrl"], {"Content-Type": "video/mp4"}, raw=f.read_bytes())
        body = {"content": item["caption"], "mediaItems": [{"type": "video", "url": pre["publicUrl"]}],
                "platforms": [{"platform": p, "accountId": a} for p, a in targets.items()],
                "scheduledFor": slot.strftime("%Y-%m-%dT%H:%M:%S"), "timezone": c["timezone"]}
        if "tiktok" in targets:
            body["tiktokSettings"] = {"privacy_level": "PUBLIC_TO_EVERYONE", "allow_comment": True, "allow_duet": True,
                                      "allow_stitch": True, "content_preview_confirmed": True, "express_consent_given": True}
        r = http("POST", f"{ZERNIO}/posts", {**h, "Idempotency-Key": str(uuid.uuid5(uuid.NAMESPACE_URL, item["id"]))}, body)
        pid = r.get("post", r).get("_id")
        item.update(status="scheduled", zernio_post_id=pid, scheduled_for=slot.isoformat())
        taken.append(slot.isoformat())
        jsave("queue.json", queue); jsave("slots.json", taken)
        log(f"post: scheduled {f.name} for {slot:%a %H:%M} ({pid})")

# --------------------------------------------------------------------------- stats + health

def stats():
    c = mcfg(); queue = jload("queue.json", []); health = jload("health.json", {"paused": {}, "stuck": {}})
    ids = {q["zernio_post_id"]: q for q in queue if q.get("zernio_post_id")}
    if not ids:
        log("stats: nothing posted yet"); return
    since = (now() - dt.timedelta(days=30)).date().isoformat()
    rows, page = [], 1
    while True:
        r = http("GET", f"{ZERNIO}/analytics?" + urllib.parse.urlencode({"fromDate": since, "limit": 100, "page": page}), zh())
        posts = r.get("posts", [])
        rows += posts
        if len(posts) < 100: break
        page += 1
    snap = STATE / "stats.csv"
    new = not snap.exists()
    with snap.open("a", newline="") as fh:
        w = csv.writer(fh)
        if new: w.writerow(["checked_at", "clip_id", "broadcaster", "platform", "published_at", "views", "likes", "comments", "shares", "url"])
        for p in rows:
            q = ids.get(p.get("_id")) or ids.get(p.get("postId"))
            if not q: continue
            a = p.get("analytics", p)
            plat = p.get("platform", "")
            views = int(a.get("views") or 0)
            q.setdefault("posts", {})[plat] = {"views": views, "likes": a.get("likes", 0), "comments": a.get("comments", 0),
                                               "shares": a.get("shares", 0), "url": p.get("platformPostUrl", ""),
                                               "published_at": p.get("publishedAt", "")}
            q["status"] = "live" if p.get("status") == "published" else q["status"]
            w.writerow([iso(now()), q["id"], q["broadcaster"], plat, p.get("publishedAt", ""), views,
                        a.get("likes", 0), a.get("comments", 0), a.get("shares", 0), p.get("platformPostUrl", "")])
            pub = p.get("publishedAt")
            if pub:
                age = (now() - dt.datetime.fromisoformat(pub.replace("Z", "+00:00"))).total_seconds() / 60
                key = f"{q['id']}:{plat}"
                if age > c["stuck_after_minutes"] and views < c["stuck_views"]:
                    health["stuck"][key] = iso(now())
                else:
                    health["stuck"].pop(key, None)
    # two stuck posts in a row on a platform -> pause that platform (view jail guard)
    for plat in c["platforms"]:
        recent = sorted([q for q in queue if plat in q.get("posts", {}) and q["posts"][plat].get("published_at")],
                        key=lambda q: q["posts"][plat]["published_at"])[-2:]
        if len(recent) == 2 and all(f"{q['id']}:{plat}" in health["stuck"] for q in recent):
            until = iso(now() + dt.timedelta(hours=c["pause_hours_after_stuck"]))
            if health["paused"].get(plat, "") < iso(now()):
                health["paused"][plat] = until
                log(f"health: 2 stuck posts in a row on {plat}, pausing until {until}")
    jsave("queue.json", queue); jsave("health.json", health)
    log(f"stats: updated {sum(len(q.get('posts', {})) for q in queue)} post(s)")

# --------------------------------------------------------------------------- report + dashboard

def report():
    queue = jload("queue.json", []); health = jload("health.json", {"paused": {}, "stuck": {}})
    live = [q for q in queue if q.get("posts")]
    views = sum(p.get("views", 0) for q in live for p in q["posts"].values())
    best = sorted(live, key=lambda q: -sum(p.get("views", 0) for p in q["posts"].values()))
    lessons = (ROOT / "learnings.md").read_text().strip().splitlines()[-5:] if (ROOT / "learnings.md").exists() else []
    lines = [f"# Clipping Machine report ({dt.datetime.now():%a %d %b %H:%M})", "",
             f"- Clips made: {len(queue)} (rejected by QA: {sum(q['status'] == 'rejected' for q in queue)})",
             f"- Scheduled: {sum(q['status'] == 'scheduled' for q in queue)} | Live: {len(live)} | Total views: {views}",
             f"- Paused platforms: {', '.join(p for p, u in health['paused'].items() if u > iso(now())) or 'none'}",
             f"- Stuck posts (under the view threshold after an hour): {len(health['stuck'])}", "", "## Top clips"]
    for q in best[:5]:
        s = " | ".join(f"{p}: {v.get('views', 0)} views, {v.get('shares', 0)} shares" for p, v in q["posts"].items())
        lines.append(f"- {q['broadcaster']}: {q['title'][:70]} ({s})")
    lines += ["", "## Latest lessons", *(lessons or ["- none yet"])]
    (STATE).mkdir(exist_ok=True)
    (STATE / "report.md").write_text("\n".join(lines) + "\n")
    rows = "".join(
        f"<tr><td>{html.escape(q['broadcaster'])}</td><td>{html.escape(q['title'][:60])}</td><td>{q['status']}</td>"
        f"<td>{sum(p.get('views', 0) for p in q.get('posts', {}).values())}</td>"
        f"<td>{sum(p.get('shares', 0) for p in q.get('posts', {}).values())}</td></tr>" for q in reversed(queue[-50:]))
    (STATE / "dashboard.html").write_text(f"""<!doctype html><meta charset="utf-8"><title>Clipping Machine</title>
<meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="300">
<style>body{{font:15px system-ui;background:#0d0d10;color:#eee;margin:24px}}.k{{display:flex;gap:16px;flex-wrap:wrap}}
.k div{{background:#18181d;border-radius:12px;padding:16px 20px}}.k b{{display:block;font-size:28px;color:#ff7825}}
table{{width:100%;border-collapse:collapse;margin-top:24px}}td,th{{padding:8px;border-bottom:1px solid #2a2a30;text-align:left}}</style>
<h1>Clipping Machine</h1><div class="k"><div><b>{len(queue)}</b>clips made</div><div><b>{len(live)}</b>live</div>
<div><b>{views}</b>total views</div><div><b>{len(health['stuck'])}</b>stuck</div></div>
<table><tr><th>Streamer</th><th>Clip</th><th>Status</th><th>Views</th><th>Shares</th></tr>{rows}</table>""")
    log(f"report: state/report.md, state/dashboard.html")

# --------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["scout", "make", "post", "stats", "report", "cycle"])
    ap.add_argument("--live", action="store_true", help="really schedule posts (default is a dry run)")
    a = ap.parse_args()
    load_env(); STATE.mkdir(exist_ok=True)
    steps = {"scout": scout, "make": make, "post": lambda: post(a.live), "stats": stats, "report": report}
    if a.cmd != "cycle":
        steps[a.cmd](); return
    for name in ["stats", "scout", "make", "post", "report"]:
        try:
            if name == "stats" and not os.environ.get("ZERNIO_API_KEY"):
                continue
            steps[name]()
        except SystemExit as e:
            log(f"{name}: {e}")
        except Exception as e:
            log(f"{name} failed: {e}")

if __name__ == "__main__":
    main()
