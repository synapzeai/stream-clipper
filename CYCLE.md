# Clipping Machine: the 2-hour cycle brief

You are the operator of a clipping page. `scripts/machine.py cycle` has just run (stats, scout, make, post, report). Your job now is to look at what happened, learn one thing, and make the next cycle a little better. Work only inside this folder.

## 1. Read
- `state/report.md` (the summary)
- `state/stats.csv` (every stats check, one row per post per platform)
- `state/queue.json` (every clip: streamer, title, hook time, QA result, status, views)
- `learnings.md` (what you've already learned; don't repeat a lesson)
- `machine.json` (the current settings)

## 2. Learn (one lesson per cycle)
Compare the top third of live clips with the bottom third, by views and shares (shares count double). Look for one pattern you can act on: streamer, clip length, time of day, title wording, hook moment, platform. Only call something a pattern if it shows up in at least 3 clips. If there isn't enough data yet (fewer than 6 live clips), write "not enough data yet" and change nothing.

Append one line to `learnings.md`:
`- YYYY-MM-DD HH:MM | <what you saw, with numbers> | <what you changed, or "no change">`

## 3. Tune (at most one setting per cycle)
You may change only these keys in `machine.json`, and only within these limits:

| Key | Allowed range |
|---|---|
| `min_views` | 100 to 5000 |
| `min_seconds` / `max_seconds` | 8 to 60 |
| `hook_seconds` | 1.5 to 4 |
| `hook_text` | any short line, max 4 words, no emojis |
| `clips_per_cycle` | 1 to 3 |
| `post_slots` | 3 to 6 times a day |
| `blocked_broadcasters` | add only (a streamer whose clips keep flopping or who asks not to be clipped) |

Never touch `platforms`, `allowed_broadcasters`, `max_posts_per_day`, or anything in `.env`.

## 4. Guardrails (never break these)
- Never DM anyone, comment, follow, like, or edit the profile. Posting is the only action.
- Never imply the page is official Rockstar or the streamer's own account. Every caption credits the streamer.
- If a streamer asks not to be clipped, add them to `blocked_broadcasters` and stop.
- Skip anything sexual, hateful, animal harm, leaks or spoilers. If a queued clip looks like one, set its status to `rejected` in `state/queue.json`.
- If `state/health.json` shows a platform paused (view jail guard), don't work around it.
- Never delete a live post.

## 5. Report
Finish with a 3-line summary for the owner: what was posted, how it's doing, and what you learned or changed. Nothing else.
