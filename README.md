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
GitHub Actions cron (every 10 min, 24/7)
   └─ src/reciprocate.py  (Playwright)
        ├─ login(email, password)
        ├─ open /settings/whosawme
        ├─ collect viewer profile links
        ├─ visit each one back (random delay, capped)
        └─ append run record to ui/runs.json
   └─ commit ui/runs.json back to main
   └─ GitHub Pages serves ui/ as the dashboard
        └─ index.html fetches runs.json and renders stats + history
```

## Repo layout

- `src/reciprocate.py` — the Playwright reciprocate script
- `src/requirements.txt` — Python deps
- `.github/workflows/reciprocate.yml` — 24/7 cron pipeline (also `workflow_dispatch` with a dry-run toggle)
- `.github/workflows/pages.yml` — publishes `ui/` to GitHub Pages
- `ui/index.html` — monitoring dashboard
- `ui/runs.json` — run history (written by the pipeline, read by the dashboard)

## Setup

1. **Create the repo** and push this project to it.
2. **Add repository secrets** (Settings → Secrets and variables → Actions):
   - `RM_EMAIL` — your rentmasseur login email
   - `RM_PASSWORD` — your rentmasseur login password
3. **Enable GitHub Pages** (Settings → Pages → Source: GitHub Actions). The
   `pages.yml` workflow will publish `ui/` on push.
4. **Run a dry run first.** Actions tab → "Reciprocal Visit Pipeline" → Run
   workflow → tick "Dry run". This verifies login + collection without visiting
   anyone back.
5. Once the dry run looks good, let the cron schedule take over (every 30 min).

## Configuration (env vars in the workflow)

| Var          | Default     | Meaning                                   |
|--------------|-------------|-------------------------------------------|
| `DRY_RUN`    | `0`         | `1` skips actually visiting back          |
| `MAX_VISITS` | `80`        | Cap on reciprocal visits per run          |
| `MIN_DELAY_S`| `2`         | Min seconds between visits                |
| `MAX_DELAY_S`| `5`         | Max seconds between visits                |
| `RUNS_FILE`  | `ui/runs.json` | Where the run record is appended        |
| `HEADFUL`    | `0`         | `1` shows the browser (local only)        |

## Local run

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

Then open `ui/index.html` in a browser (or `python -m http.server -d ui 8080`)
to see the dashboard read `ui/runs.json`.

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
