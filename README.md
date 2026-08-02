# RM Reciprocal Visit Pipeline

A 24/7 CI/CD pipeline that reciprocally visits the profiles of people who
viewed you on **rentmasseur.com** ("Who Saw Me" → visit back), plus a web UI
dashboard to monitor it.

> ⚠️ **ToS warning.** Automating a third-party site with a bot almost certainly
> violates rentmasseur.com's Terms of Service and can get your account banned.
> This automates **your own** account's "visit back" action only — it does not
> scrape other people's private data or create fake accounts. Review the site's
> ToS before enabling the schedule. You are responsible for how you use this.

## Architecture

```
GitHub Actions cron (every 10 min, 24/7)  ← runs in the cloud
   └─ src/reciprocate.py  (Playwright)
        ├─ login(email, password)
        ├─ open /settings/whosawme
        ├─ collect viewer profile links
        ├─ visit each one back (random delay, capped)
        └─ append run record to ui/runs.json
   └─ commit ui/runs.json back to main

Local dashboard  ← runs on your machine
   └─ src/sync-runs.sh pulls runs.json from the repo via gh CLI
   └─ python -m http.server serves ui/ at http://127.0.0.1:8099
   └─ index.html fetches runs.json and renders stats + history
```

The pipeline runs entirely in GitHub Actions (no local execution needed).
The dashboard is a static page served locally that reads the cloud-committed
`runs.json`.

## Repo layout

- `src/reciprocate.py` — the Playwright reciprocate script
- `src/requirements.txt` — Python deps
- `src/sync-runs.sh` — pulls latest runs.json from the repo for the local dashboard
- `.github/workflows/reciprocate.yml` — 24/7 cron pipeline (also `workflow_dispatch` with a dry-run toggle)
- `ui/index.html` — monitoring dashboard
- `ui/runs.json` — run history (written by the pipeline, read by the dashboard)

## Setup

1. **Add repository secrets** (Settings → Secrets and variables → Actions):
   - `RM_EMAIL` — your rentmasseur login email
   - `RM_PASSWORD` — your rentmasseur login password
2. **Run a dry run first.** Actions tab → "Reciprocal Visit Pipeline" → Run
   workflow → tick "Dry run". This verifies login + collection without visiting
   anyone back.
3. Once the dry run looks good, let the cron schedule take over (every 10 min).
4. **View the dashboard locally** (see below).

## Configuration (env vars in the workflow)

| Var          | Default     | Meaning                                   |
|--------------|-------------|-------------------------------------------|
| `DRY_RUN`    | `0`         | `1` skips actually visiting back          |
| `MAX_VISITS` | `80`        | Cap on reciprocal visits per run          |
| `MIN_DELAY_S`| `2`         | Min seconds between visits                |
| `MAX_DELAY_S`| `5`         | Max seconds between visits                |
| `RUNS_FILE`  | `ui/runs.json` | Where the run record is appended        |
| `HEADFUL`    | `0`         | `1` shows the browser (local only)        |

## Local dashboard (cloud pipeline)

The pipeline runs in GitHub Actions — no local execution needed. To view the
dashboard, serve `ui/` locally and sync `runs.json` from the repo:

```bash
cd ~/projects/rm-reciprocal-pipe

# Terminal 1: serve the dashboard
python3 -m http.server 8099 --bind 127.0.0.1 --directory ui

# Terminal 2: keep runs.json in sync with the cloud
./src/sync-runs.sh --watch        # polls every 30s
```

Then open http://127.0.0.1:8099 in your browser.

## Local run (optional, for testing)

If you want to run the pipeline locally instead of in GitHub Actions:

```bash
cd ~/projects/rm-reciprocal-pipe
python -m venv .venv && source .venv/bin/activate
pip install -r src/requirements.txt
python -m playwright install chromium
export RM_EMAIL="you@example.com"
export RM_PASSWORD="yourpassword"
export DRY_RUN=1            # try dry first
python src/reciprocate.py
```

## Selectors

DOM selectors live in `SELECTORS` at the top of `src/reciprocate.py`. If the
site's markup changes and login/collection breaks, adjust them there. The
script is defensive: a selector miss is logged and fails the run gracefully
rather than crashing silently.

## Notes / caveats

- **GitHub Actions cron is best-effort.** For public/low-activity repos it can
  drift or be delayed. For a true 24/7 cadence, run the workflow on a
  self-hosted runner with a real cron, or use a scheduled external trigger
  (e.g. a cron-job.org webhook hitting the repository dispatch endpoint).
- **Run history is bounded** to the last 500 runs in `ui/runs.json` to keep the
  file small.
- **Use a dedicated/throwaway account** if you're worried about a ban.
