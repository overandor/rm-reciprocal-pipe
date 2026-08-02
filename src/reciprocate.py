"""
Reciprocal-visit pipeline for rentmasseur.com "Who Saw Me".

Logs in, opens /settings/whosawme, collects the profile links of people who
viewed you, and visits each of them back (reciprocal visit). Records every run
as a JSON entry in ui/runs.json so the dashboard can render it.

Env vars:
  RM_EMAIL     rentmasseur login email            (required)
  RM_PASSWORD  rentmasseur login password         (required)
  DRY_RUN      "1" to skip actually visiting back  (default "0")
  MAX_VISITS   cap on reciprocal visits per run    (default "80")
  MIN_DELAY_S  min seconds between visits          (default "2")
  MAX_DELAY_S  max seconds between visits          (default "5")
  RUNS_FILE    path to runs.json                   (default "ui/runs.json")
  HEADFUL      "1" to show the browser             (default "0")

NOTE: selectors below are best-effort against the live site and may need
tweaking if the markup changes. They are grouped in SELECTORS so you can
adjust them in one place.
"""
from __future__ import annotations

import json
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

BASE = "https://rentmasseur.com"
WHOSAWME = f"{BASE}/settings/whosawme"

SELECTORS = {
    # Login form
    "email_input": 'input[name="email"], input[type="email"], input#email',
    "pass_input": 'input[name="password"], input[type="password"], input#password',
    "submit_btn": 'button[type="submit"], input[type="submit"], button:has-text("Log in"), button:has-text("Sign in")',
    # "Who saw me" viewer entries. We look for anchors whose href contains /user/ or /profile/.
    "viewer_link": 'a[href*="/user/"], a[href*="/profile/"], a[href*="/members/"]',
    # Logged-in signal
    "logged_in_marker": 'a[href*="/logout"], a:has-text("Log out"), a:has-text("My Account")',
}


def env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except ValueError:
        return default


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}", flush=True)


def require_creds() -> tuple[str, str]:
    email, pw = env("RM_EMAIL"), env("RM_PASSWORD")
    if not email or not pw:
        sys.exit("ERROR: RM_EMAIL and RM_PASSWORD must be set.")
    return email, pw


def login(page, email: str, password: str) -> bool:
    log("Navigating to login page")
    page.goto(f"{BASE}/login", wait_until="domcontentloaded")
    try:
        page.fill(SELECTORS["email_input"], email, timeout=15000)
        page.fill(SELECTORS["pass_input"], password, timeout=15000)
        page.click(SELECTORS["submit_btn"], timeout=15000)
    except PWTimeout:
        log("Login form selectors did not match — site markup may have changed.")
        return False
    # Wait for a logged-in marker or a URL change away from /login
    try:
        page.wait_for_selector(SELECTORS["logged_in_marker"], timeout=20000)
        log("Logged in successfully")
        return True
    except PWTimeout:
        if "/login" in page.url:
            log("Still on /login after submit — credentials likely invalid.")
            return False
        log("Logged in (no explicit marker, but URL changed)")
        return True


def collect_viewers(page) -> list[dict]:
    log(f"Opening {WHOSAWME}")
    page.goto(WHOSAWME, wait_until="domcontentloaded")
    # Give lazy-loaded lists a chance to render
    try:
        page.wait_for_selector(SELECTORS["viewer_link"], timeout=20000)
    except PWTimeout:
        log("No viewer links found (page may be empty or selectors changed).")
        return []

    seen: set[str] = set()
    viewers: list[dict] = []
    # Scroll a bit to load more entries
    for _ in range(2):
        page.mouse.wheel(0, 4000)
        page.wait_for_timeout(600)

    anchors = page.query_selector_all(SELECTORS["viewer_link"])
    for a in anchors:
        href = a.get_attribute("href") or ""
        if not href:
            continue
        full = href if href.startswith("http") else BASE + href
        # Normalize to the profile root (strip query/fragments)
        key = full.split("?")[0].split("#")[0]
        if key in seen or "/settings/" in key:
            continue
        seen.add(key)
        name = (a.inner_text() or "").strip()[:80]
        viewers.append({"name": name, "url": key})
    log(f"Collected {len(viewers)} unique viewers")
    return viewers


def visit_back(page, viewers: list[dict], dry_run: bool) -> list[dict]:
    max_visits = env_int("MAX_VISITS", 80)
    min_d, max_d = env_int("MIN_DELAY_S", 2), env_int("MAX_DELAY_S", 5)
    targets = viewers[:max_visits]
    results: list[dict] = []
    if dry_run:
        log(f"DRY_RUN=1 — skipping actual visits for {len(targets)} viewers")
        for v in targets:
            results.append({**v, "visited": False, "dry_run": True})
        return results

    for i, v in enumerate(targets, 1):
        ok = False
        err = ""
        try:
            log(f"[{i}/{len(targets)}] Visiting {v['url']}")
            page.goto(v["url"], wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(700)
            ok = True
        except PWTimeout:
            err = "timeout"
        except Exception as e:  # noqa: BLE001
            err = str(e)[:120]
        results.append({**v, "visited": ok, "error": err})
        time.sleep(random.uniform(min_d, max_d))
    return results


def append_run(runs_file: Path, record: dict) -> None:
    runs_file.parent.mkdir(parents=True, exist_ok=True)
    runs: list[dict] = []
    if runs_file.exists():
        try:
            runs = json.loads(runs_file.read_text() or "[]")
        except json.JSONDecodeError:
            log("runs.json was corrupt — starting fresh")
            runs = []
    runs.append(record)
    # Keep the tail bounded so the file doesn't grow forever
    runs = runs[-500:]
    runs_file.write_text(json.dumps(runs, indent=2))
    log(f"Wrote run to {runs_file} (total {len(runs)})")


def main() -> int:
    email, password = require_creds()
    dry_run = env("DRY_RUN", "0") == "1"
    runs_file = Path(env("RUNS_FILE", "ui/runs.json"))
    headful = env("HEADFUL", "0") == "1"

    started = datetime.now(timezone.utc)
    status = "ok"
    error = ""

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headful)
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            viewport={"width": 1280, "height": 900},
        )
        page = ctx.new_page()
        try:
            if not login(page, email, password):
                status, error = "login_failed", "could not log in"
            else:
                viewers = collect_viewers(page)
                visited = visit_back(page, viewers, dry_run)
                succeeded = sum(1 for r in visited if r.get("visited"))
                log(f"Reciprocal visits done: {succeeded}/{len(visited)} succeeded")
        except Exception as e:  # noqa: BLE001
            status, error = "error", str(e)[:200]
            log(f"Run failed: {error}")
        finally:
            ctx.close()
            browser.close()

    record = {
        "id": started.strftime("%Y%m%dT%H%M%SZ") + f"-{random.randint(100,999)}",
        "started_at": started.isoformat(timespec="seconds"),
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": status,
        "error": error,
        "dry_run": dry_run,
        "viewers_found": len(viewers) if status == "ok" else 0,
        "visited_count": sum(1 for r in visited if r.get("visited")) if status == "ok" else 0,
        "visited": visited if status == "ok" else [],
    }
    append_run(runs_file, record)
    return 0 if status == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
