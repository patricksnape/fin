"""VIAC (pillar 3a) from the web app's own responses, recorded from a logged-in browser.

`fetch` opens a real browser window with a persistent profile; you log in (2FA included). fin then
shows the 3a pages and writes down every JSON answer the app itself receives from `/rest/web/`
(`sources/viac/<day>_<login>_capture.json`). Nothing but the response bodies is kept: no request
header, cookie or token, and nothing passes through an LLM. The app answers its endpoints only with
headers its own scripts add, so fin reads what the page loads instead of calling them.

The parser is not written yet: this module only records, so the real shape of the answers can be
studied before anything is booked. `parse_files` yields nothing.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import date, datetime
from pathlib import Path

from fin.importers.base import ImportFailed, RawTxn

SOURCE = "viac"
HOME = "https://app.viac.ch/"
PAGES = (  # the app's routes worth recording, in the order they are shown
    "#/p3a/portfolio/overview",
    "#/documents",
)
API = "/rest/web/"
IN_SYNC = False  # a login of its own (2FA): run `fin fetch viac`
LOGGED_IN = re.compile(r"#/(products|p3a|documents|hub)")
BROWSE_SECONDS = 90  # time to open each portfolio yourself before fin stops recording


def profile_dir() -> Path:
    base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "fin" / "browser" / SOURCE


def source_files(root: Path) -> list[Path]:
    return sorted((root / "sources" / SOURCE).glob("*_capture.json"))


def parse_files(files: list[Path]) -> list[RawTxn]:
    return []


def fetch(root: Path, start: date, end: date, log=print, login: str = "main") -> list[Path]:
    """Record what the web app loads. `login` names whose account it is (a partner has their own)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise ImportFailed("playwright is not installed: uv sync --extra browser") from e

    seen: dict[tuple[str, str], dict] = {}

    def on_response(resp) -> None:
        url = resp.url
        if API not in url or "application/json" not in (resp.headers.get("content-type") or ""):
            return
        key = (resp.request.method, url)
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001  a body that is not JSON (or is gone) is not recorded
            return
        seen[key] = {"method": key[0], "url": url, "status": resp.status, "body": body}

    out_dir = root / "sources" / SOURCE
    out_dir.mkdir(parents=True, exist_ok=True)
    profile_dir().mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(profile_dir()), channel="chrome", headless=False, no_viewport=True
        )
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.on("response", on_response)
            page.goto(HOME)
            log("Log in in the browser window (including 2FA). Waiting for the overview...")
            deadline = time.time() + 600
            while not LOGGED_IN.search(page.url) and time.time() < deadline:
                page.wait_for_timeout(500)
            if not LOGGED_IN.search(page.url):
                raise ImportFailed("timed out waiting for you to log in")
            for route in PAGES:
                page.goto(HOME + route)
                page.wait_for_timeout(6000)
            log(
                f"Recording for {BROWSE_SECONDS} s more: open each portfolio and its "
                "transactions in the window now."
            )
            page.wait_for_timeout(BROWSE_SECONDS * 1000)
        finally:
            ctx.close()
    if not seen:
        raise ImportFailed("the app answered nothing fin could record; has the site changed?")
    path = out_dir / f"{date.today()}_{login}_capture.json"
    path.write_text(
        json.dumps(
            {
                "fetched": datetime.now().astimezone().isoformat(timespec="seconds"),
                "login": login,
                "responses": sorted(seen.values(), key=lambda r: r["url"]),
            },
            indent=1,
        )
    )
    log(f"  recorded {len(seen)} answers")
    return [path]
