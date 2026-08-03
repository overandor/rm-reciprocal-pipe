"""
Reciprocal-visit pipeline for rentmasseur.com "Who Saw Me".

Logs in, opens /settings/whosawme, collects the profile links of people who
viewed you, visits each of them back (reciprocal visit), then opens the
"Who Did I See" tab and verifies the visits registered. If "Who Did I See"
doesn't reflect the "Who Saw Me" entries, the run is marked as failed.

Records every run as a JSON entry in ui/runs.json for the dashboard.

Env vars:
  RM_EMAIL     rentmasseur login email            (required)
  RM_PASSWORD  rentmasseur login password         (required)
  DRY_RUN      "1" to skip actually visiting back  (default "0")
  MAX_VISITS   cap on reciprocal visits per run    (default "80")
  MIN_DELAY_S  min seconds between visits          (default "2")
  MAX_DELAY_S  max seconds between visits          (default "5")
  RUNS_FILE    path to runs.json                   (default "ui/runs.json")
  HEADFUL      "1" to show the browser             (default "0")
  RECORD_DIR   directory for session video         (default "recordings")

Selectors were verified against the live site on 2026-08-02:
  - Login:   input#email, input#password, button:has-text("Login")
  - Viewers: a.username (href="/{username}")
  - Tabs:    button:has-text("Who Saw Me") / button:has-text("Who Did I See")
  - Cookie:  button:has-text("Accept all")
"""
from __future__ import annotations

import json
import os
import random
import time
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

BASE = "https://rentmasseur.com"
LOGIN_URL = f"{BASE}/login"
WHOSAWME_URL = f"{BASE}/settings/whosawme"

# Verified selectors — do not change without testing against the live site.
EMAIL_SEL = "input#email"
PASS_SEL = "input#password"
LOGIN_BTN = 'button:has-text("Login")'
LOGOUT_LINK = 'a:has-text("Logout")'
COOKIE_ACCEPT = 'button:has-text("Accept all")'
# Viewer links on "Who Saw Me" — class contains "username", href is /{username}
VIEWER_LINK = "a.username"
# Tab labels on the visits page (buttons with mixed case)
WHOSAWME_TAB = 'button:has-text("Who Saw Me")'
WHODIDISEE_TAB = 'button:has-text("Who Did I See")'


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
        raise RuntimeError("RM_EMAIL and RM_PASSWORD must be set.")
    return email, pw


def dismiss_cookie_banner(page) -> None:
    try:
        btn = page.locator(COOKIE_ACCEPT)
        if btn.count() > 0:
            btn.first.click(timeout=3000)
            log("Dismissed cookie banner")
    except (PWTimeout, Exception):
        pass  # Banner may not appear


def login(page, email: str, password: str) -> bool:
    log(f"Navigating to {LOGIN_URL}")
    page.goto(LOGIN_URL, wait_until="domcontentloaded")
    dismiss_cookie_banner(page)

    try:
        page.fill(EMAIL_SEL, email, timeout=15000)
        page.fill(PASS_SEL, password, timeout=15000)
        log("Submitting login form")
        page.click(LOGIN_BTN, timeout=15000)
    except PWTimeout:
        log("Login form selectors did not match — site markup may have changed.")
        return False

    # Wait for either: URL changes away from /login, or logout link appears.
    # The site is a SPA so the URL may not change immediately.
    log("Waiting for login to complete...")
    for attempt in range(20):  # up to ~20s
        page.wait_for_timeout(1000)
        current_url = page.url
        if "/login" not in current_url:
            log(f"URL changed to {current_url} — login likely succeeded")
            # Double-check for logout link
            try:
                page.wait_for_selector(LOGOUT_LINK, timeout=5000)
                log("Logged in successfully (logout link found)")
            except PWTimeout:
                log("URL changed but no logout link — assuming logged in")
            return True
        # Check if logout link appeared even though URL hasn't changed
        try:
            page.wait_for_selector(LOGOUT_LINK, timeout=500)
            log("Logged in successfully (logout link found while on /login)")
            return True
        except PWTimeout:
            pass

    log(f"Still on /login after 20s — credentials likely invalid.")
    return False


def collect_viewers(page) -> list[dict]:
    log(f"Opening {WHOSAWME_URL}")
    page.goto(WHOSAWME_URL, wait_until="domcontentloaded")
    dismiss_cookie_banner(page)

    # Make sure we're on the "WHO SAW ME" tab
    try:
        page.click(WHOSAWME_TAB, timeout=10000)
        log("Clicked 'WHO SAW ME' tab")
    except (PWTimeout, Exception):
        log("Could not click 'WHO SAW ME' tab (may already be active)")

    # Wait for viewer links to render
    try:
        page.wait_for_selector(VIEWER_LINK, timeout=20000)
    except PWTimeout:
        log("No viewer links found (page may be empty or not logged in).")
        return []

    seen: set[str] = set()
    viewers: list[dict] = []
    # Scroll to load more entries
    for _ in range(3):
        page.mouse.wheel(0, 4000)
        page.wait_for_timeout(800)

    anchors = page.query_selector_all(VIEWER_LINK)
    for a in anchors:
        href = a.get_attribute("href") or ""
        if not href:
            continue
        full = href if href.startswith("http") else BASE + href
        key = full.split("?")[0].split("#")[0].rstrip("/")
        # Skip non-profile links (settings, advertise, etc.)
        if key in seen or "/settings/" in key or "/advertise/" in key:
            continue
        seen.add(key)
        name = (a.inner_text() or "").strip()[:80]
        viewers.append({"name": name, "url": key})
    log(f"Collected {len(viewers)} unique viewers from 'Who Saw Me'")
    return viewers


def visit_back(page, viewers: list[dict], dry_run: bool, record_dir: Path, run_id: str) -> list[dict]:
    max_visits = env_int("MAX_VISITS", 80)
    min_d, max_d = env_int("MIN_DELAY_S", 2), env_int("MAX_DELAY_S", 5)
    targets = viewers[:max_visits]
    results: list[dict] = []
    if dry_run:
        log(f"DRY_RUN=1 — skipping actual visits for {len(targets)} viewers")
        for v in targets:
            results.append({**v, "visited": False, "dry_run": True})
        return results

    shot_dir = record_dir / "screenshots" / run_id
    shot_dir.mkdir(parents=True, exist_ok=True)

    for i, v in enumerate(targets, 1):
        ok = False
        err = ""
        screenshot = None
        try:
            log(f"[{i}/{len(targets)}] Visiting {v['url']}")
            page.goto(v["url"], wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(700)
            ok = True
            try:
                safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in v.get("name", "unknown"))
                shot_file = shot_dir / f"{i:03d}_{safe_name}.png"
                page.screenshot(path=str(shot_file), full_page=False)
                screenshot = f"recordings/screenshots/{run_id}/{i:03d}_{safe_name}.png"
                log(f"  Screenshot saved: {shot_file.name}")
            except Exception as e:  # noqa: BLE001
                log(f"  Screenshot failed: {e}")
        except PWTimeout:
            err = "timeout"
        except Exception as e:  # noqa: BLE001
            err = str(e)[:120]
        results.append({**v, "visited": ok, "error": err, "screenshot": screenshot})
        time.sleep(random.uniform(min_d, max_d))
    return results


def verify_whodidisee(page, expected_viewers: list[dict]) -> dict:
    """
    Open the 'Who Did I See' tab and check that the viewers we just visited
    appear there. If they don't, the visits didn't register and the pipe
    didn't actually work.

    Returns a dict with:
      - saw_count: how many of the visited profiles appear in 'Who Did I See'
      - matched: list of matched viewer names
      - missing: list of viewer names not found in 'Who Did I See'
    """
    log("Verifying visits via 'WHO DID I SEE' tab")
    # Navigate back to the visits page
    page.goto(WHOSAWME_URL, wait_until="domcontentloaded")
    dismiss_cookie_banner(page)

    # Click the "WHO DID I SEE" tab
    try:
        page.click(WHODIDISEE_TAB, timeout=10000)
        log("Clicked 'WHO DID I SEE' tab")
    except (PWTimeout, Exception) as e:
        log(f"Could not click 'WHO DID I SEE' tab: {e}")
        return {"saw_count": 0, "matched": [], "missing": [v["name"] for v in expected_viewers], "error": "tab not found"}

    # Wait for the list to render
    try:
        page.wait_for_selector(VIEWER_LINK, timeout=15000)
    except PWTimeout:
        log("No entries in 'Who Did I See' — visits may not have registered.")
        return {"saw_count": 0, "matched": [], "missing": [v["name"] for v in expected_viewers]}

    # Scroll to load entries
    for _ in range(3):
        page.mouse.wheel(0, 4000)
        page.wait_for_timeout(800)

    # Collect who appears in "Who Did I See"
    seen_urls: set[str] = set()
    anchors = page.query_selector_all(VIEWER_LINK)
    for a in anchors:
        href = a.get_attribute("href") or ""
        if href:
            full = href if href.startswith("http") else BASE + href
            seen_urls.add(full.split("?")[0].split("#")[0].rstrip("/"))

    expected_urls = {v["url"] for v in expected_viewers if v.get("visited")}
    matched = [v for v in expected_viewers if v.get("visited") and v["url"] in seen_urls]
    missing = [v for v in expected_viewers if v.get("visited") and v["url"] not in seen_urls]

    log(f"Verification: {len(matched)}/{len(expected_urls)} visited profiles appear in 'Who Did I See'")
    if missing:
        log(f"Missing from 'Who Did I See': {[v['name'] for v in missing[:10]]}")

    return {
        "saw_count": len(matched),
        "expected_count": len(expected_urls),
        "matched": [v["name"] for v in matched],
        "missing": [v["name"] for v in missing],
    }


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
    runs = runs[-500:]
    runs_file.write_text(json.dumps(runs, indent=2))
    log(f"Wrote run to {runs_file} (total {len(runs)})")


def main() -> int:
    dry_run = env("DRY_RUN", "0") == "1"
    runs_file = Path(env("RUNS_FILE", "ui/runs.json"))
    headful = env("HEADFUL", "0") == "1"
    record_dir = Path(env("RECORD_DIR", "recordings"))
    record_dir.mkdir(parents=True, exist_ok=True)

    started = datetime.now(timezone.utc)
    run_id = started.strftime("%Y%m%dT%H%M%SZ") + f"-{random.randint(100,999)}"
    status = "ok"
    error = ""
    viewers: list[dict] = []
    visited: list[dict] = []
    verification: dict = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headful)
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            viewport={"width": 1280, "height": 900},
            record_video_dir=str(record_dir),
            record_video_size={"width": 1280, "height": 900},
        )
        page = ctx.new_page()
        try:
            email, password = require_creds()
            if not login(page, email, password):
                status, error = "login_failed", "could not log in"
            else:
                viewers = collect_viewers(page)
                if not viewers:
                    status, error = "no_viewers", "no viewers found on Who Saw Me"
                else:
                    visited = visit_back(page, viewers, dry_run, record_dir, run_id)
                    succeeded = sum(1 for r in visited if r.get("visited"))
                    log(f"Reciprocal visits done: {succeeded}/{len(visited)} succeeded")

                    if not dry_run and succeeded > 0:
                        verification = verify_whodidisee(page, visited)
                        # If any visited profile is missing from "Who Did I See",
                        # the pipe didn't work — visits didn't register.
                        missing = verification.get("missing", [])
                        if missing:
                            status = "verification_failed"
                            error = f"{len(missing)} visit(s) did not register in 'Who Did I See'"
                            log(f"VERIFICATION FAILED: {error}")
        except Exception as e:  # noqa: BLE001
            status, error = "error", str(e)[:200]
            log(f"Run failed: {error}")
        finally:
            # Close context first so the video file is finalized
            video = page.video
            ctx.close()
            browser.close()
            # Rename the video file to our run ID
            if video:
                try:
                    original = video.path()
                    if original and Path(original).exists():
                        target = record_dir / f"{run_id}.webm"
                        Path(original).rename(target)
                        log(f"Session video saved: {target}")
                except Exception as e:  # noqa: BLE001
                    log(f"Could not save video: {e}")

    record = {
        "id": run_id,
        "started_at": started.isoformat(timespec="seconds"),
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": status,
        "error": error,
        "dry_run": dry_run,
        "viewers_found": len(viewers),
        "visited_count": sum(1 for r in visited if r.get("visited")),
        "verified_count": verification.get("saw_count", 0),
        "verification": verification if verification else None,
        "video": f"recordings/{run_id}.webm",
        "visited": visited,
    }
    append_run(runs_file, record)
    return 0 if status == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
