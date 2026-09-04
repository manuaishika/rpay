# Deploying the console to Render

`render.yaml` is a Blueprint — Render reads it straight from the repo, so
this is a few clicks, not a manual service setup.

## Steps

1. **Rotate the Sarvam key first** if you haven't (Sarvam dashboard -> API
   keys). Anything pasted into a chat or committed at any point should be
   treated as burned.
2. Push `render.yaml` to `main` (it's already in this repo).
3. On [render.com](https://render.com): **New +** -> **Blueprint** -> connect
   the `manuaishika/rpay` GitHub repo -> Render finds `render.yaml`.
4. It will ask for the one var marked `sync: false`: paste the (new,
   rotated) `SARVAM_API_KEY`. Nothing else to configure — no build step, no
   dependencies.
5. **Apply**. First deploy takes a minute or two. You get a URL like
   `https://recovery-console-xxxx.onrender.com` — that's the form link.
6. Open it, run a small sample (8-10 accounts) once to confirm the live
   agent works from Render's network before you submit the link.

## What to know about the free plan

- **Cold start.** A free service sleeps after 15 minutes idle and takes
  ~30-50s to wake on the next request. If a judge opens the link cold, the
  first load will hang briefly — that's Render waking up, not a bug. Ping
  the URL yourself a minute before anyone's expected to look at it.
- **One run at a time.** The console now refuses a second `/api/run` while
  one is in flight (`serve.py`, `_RUN_LOCK`) — otherwise a public link on a
  shared, metered key could get multiple concurrent LLM runs stacked on it
  from different visitors. A second visitor just gets "try again" until the
  first run finishes; each run is capped at 40 accounts either way.
- **The key is metered.** Every visitor who clicks "Run recovery" with the
  LLM toggle on spends real Sarvam calls. Keep an eye on usage in the Sarvam
  dashboard while the link is live, especially if it'll sit in a public form
  for a while.
- **No persistence.** Runs live in memory for the length of one stream;
  nothing is written to disk on Render, so restarts lose nothing important.

## Rolling back to local

Nothing about local usage changes: `python -m recovery.serve` still binds
`127.0.0.1:8000` by default. The Render-specific bits (`--host 0.0.0.0`,
reading `$PORT`) only activate when you pass them or when Render sets
`$PORT` itself.
