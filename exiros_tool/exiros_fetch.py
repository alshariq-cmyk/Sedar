"""Collect RFQs from the Exiros supplier portal into the local RFQ platform.

Runs only when the user starts it (the "Update from Exiros" button). It opens a
fresh browser window on the login page, waits for the user to log in, reads the
RFQ list (numbers and deadlines), downloads each RFQ's quotations report,
saves everything locally, then logs out and closes the browser. No login is
kept between runs.

    python exiros_fetch.py capture   # one-time: save portal pages so the settings can be filled in

Portal-specific details (addresses, which links and buttons to click) live in
exiros_config.json.
"""

import json
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urljoin

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

import store

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "exiros_config.json"
CAPTURE_DIR = HERE / "captures"
LOGIN_TIMEOUT_MINUTES = 10

DEFAULTS = {
    "portal_url": "",           # login page
    "logged_in_selector": "",   # element only present once logged in
    "rfq_list_url": "",         # RFQ list; empty = stay on the page shown after login
    "rfq_row": "",              # one row per RFQ in the list (optional)
    "rfq_link": "",             # link that opens the RFQ (inside the row if rfq_row is set)
    "rfq_number_regex": r"\d{6,}",
    "deadline": "",             # deadline text inside the row
    "deadline_formats": ["%d/%m/%Y %H:%M", "%d/%m/%Y", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%d-%b-%Y %H:%M", "%d-%b-%Y", "%m/%d/%Y %H:%M", "%m/%d/%Y"],
    "title": "",                # RFQ title/subject inside the row (optional)
    "next_page": "",            # next-page button on the list (optional)
    "download_button": "",      # on the RFQ page: triggers the quotations report download
    "logout_selector": "",      # clicked at the end (optional)
    "logout_url": "",           # or opened at the end (optional)
    "delay_seconds": 2,
    "download_timeout_seconds": 60,
    "max_pages": 200,
}


def load_config(path: Path = CONFIG_PATH) -> dict:
    config = dict(DEFAULTS)
    if Path(path).exists():
        config.update({k: v for k, v in json.loads(Path(path).read_text(encoding="utf-8")).items() if not k.startswith("_")})
    return config


def config_problem(config: dict) -> str | None:
    missing = [k for k in ("portal_url", "logged_in_selector", "rfq_link", "download_button") if not config.get(k)]
    if missing:
        return ("The Exiros connection isn't set up yet. It needs one look at the portal pages "
                "(the one-time screenshots), then this button will work.")
    return None


def open_browser(playwright, headless: bool = False):
    """A fresh window in the Chrome or Edge already on the computer (nothing is remembered after it closes)."""
    for channel in ("chrome", "msedge", None):
        try:
            options = {"headless": headless}
            if channel:
                options["channel"] = channel
            return playwright.chromium.launch(**options)
        except PlaywrightError:
            continue
    raise RuntimeError("Couldn't start a browser. Install Google Chrome or Microsoft Edge.")


def parse_deadline(text: str | None, formats: list[str]) -> str | None:
    if not text:
        return None
    text = re.sub(r"\s+", " ", text).strip()
    candidates = [text] + re.findall(r"\d{1,4}[/\-.][\w]{1,3}[/\-.]\d{2,4}(?: \d{1,2}:\d{2})?", text)
    for candidate in candidates:
        for fmt in formats:
            try:
                parsed = datetime.strptime(candidate.strip(), fmt)
            except ValueError:
                continue
            if "%H" not in fmt:
                parsed = parsed.replace(hour=23, minute=59)
            return parsed.strftime("%Y-%m-%d %H:%M")
    return None


def rfq_number(text: str, pattern: str) -> str | None:
    match = re.search(pattern, text or "")
    return match.group(0) if match else None


def _href(base: str, href: str | None) -> str | None:
    return urljoin(base, href) if href and not href.lower().startswith("javascript") else None


def _text(locator) -> str | None:
    return locator.first.inner_text().strip() if locator.count() else None


def wait_for_login(page, config: dict, progress) -> None:
    page.goto(config["portal_url"])
    progress("Please log in to Exiros in the browser window that just opened.")
    deadline = time.monotonic() + LOGIN_TIMEOUT_MINUTES * 60
    while time.monotonic() < deadline:
        try:
            if page.locator(config["logged_in_selector"]).count():
                progress("Logged in.")
                return
        except PlaywrightError:
            pass  # page navigating during login
        if page.is_closed():
            raise RuntimeError("The browser window was closed before logging in.")
        page.wait_for_timeout(1000)
    raise RuntimeError(f"Not logged in after {LOGIN_TIMEOUT_MINUTES} minutes, so the update was stopped.")


def collect_rfqs(page, config: dict, progress) -> list[dict]:
    """Walk the RFQ list (all pages) and return [{rfq, href, index, deadline, title}]."""
    if config.get("rfq_list_url"):
        page.goto(config["rfq_list_url"])
    found: dict[str, dict] = {}
    for page_no in range(1, config["max_pages"] + 1):
        page.wait_for_load_state()
        rows = page.locator(config["rfq_row"]) if config.get("rfq_row") else page.locator(config["rfq_link"])
        new = 0
        for i in range(rows.count()):
            row = rows.nth(i)
            link = row.locator(config["rfq_link"]).first if config.get("rfq_row") else row
            if config.get("rfq_row") and not row.locator(config["rfq_link"]).count():
                continue
            href = link.get_attribute("href")
            text = link.inner_text().strip()
            number = rfq_number(text, config["rfq_number_regex"]) or rfq_number(href or "", config["rfq_number_regex"])
            if not number or number in found:
                continue
            item = {"rfq": number, "href": _href(page.url, href), "index": i, "page": page_no, "deadline": None, "title": None}
            if config.get("rfq_row"):
                if config.get("deadline"):
                    item["deadline"] = parse_deadline(_text(row.locator(config["deadline"])), config["deadline_formats"])
                if config.get("title"):
                    item["title"] = _text(row.locator(config["title"]))
            found[number] = item
            new += 1
        progress(f"Reading the RFQ list… page {page_no}, {len(found)} RFQs so far")
        next_button = page.locator(config["next_page"]) if config.get("next_page") else None
        if not new or next_button is None or next_button.count() == 0 or not next_button.first.is_enabled():
            break
        next_button.first.click()
        page.wait_for_timeout(config["delay_seconds"] * 1000)
    return list(found.values())


def download_report(page, item: dict, config: dict, download_dir: Path) -> Path:
    if item["href"]:
        page.goto(item["href"])
    else:  # JavaScript-only links: open the list again and click the same row
        page.goto(config["rfq_list_url"])
        for _ in range(item["page"] - 1):
            page.locator(config["next_page"]).first.click()
            page.wait_for_load_state()
        rows = page.locator(config["rfq_row"] or config["rfq_link"])
        target = rows.nth(item["index"])
        (target.locator(config["rfq_link"]).first if config.get("rfq_row") else target).click()
    page.wait_for_load_state()
    with page.expect_download(timeout=config["download_timeout_seconds"] * 1000) as info:
        page.locator(config["download_button"]).first.click()
    download = info.value
    suffix = Path(download.suggested_filename).suffix or ".xls"
    download_dir.mkdir(parents=True, exist_ok=True)
    path = download_dir / f"{item['rfq']}_quotationsReport{suffix}"
    download.save_as(path)
    return path


def logout(page, config: dict) -> None:
    try:
        if config.get("logout_selector") and page.locator(config["logout_selector"]).count():
            page.locator(config["logout_selector"]).first.click()
            page.wait_for_timeout(1000)
        elif config.get("logout_url"):
            page.goto(config["logout_url"])
    except PlaywrightError:
        pass  # the browser is closed right after anyway, which ends the session


def needs_download(conn, item: dict) -> bool:
    """Skip RFQs we already have whose deadline has passed: they can't change any more."""
    row = conn.execute("SELECT deadline FROM rfqs WHERE rfq = ?", (item["rfq"],)).fetchone()
    if row is None:
        return True
    deadline = item["deadline"] or row["deadline"]
    return deadline is None or deadline >= datetime.now().strftime("%Y-%m-%d %H:%M")


def update_from_exiros(conn, config: dict, download_dir: Path, progress=print, *, browser_factory=None) -> tuple[int, int, list[str]]:
    """Returns (RFQs found, new RFQs, RFQ numbers that failed)."""
    new, failed = 0, []
    with sync_playwright() as p:
        browser = (browser_factory or open_browser)(p)
        try:
            page = browser.new_context(accept_downloads=True).new_page()
            wait_for_login(page, config, progress)
            rfqs = collect_rfqs(page, config, progress)
            progress(f"Found {len(rfqs)} RFQs. Downloading…")
            for n, item in enumerate(rfqs, 1):
                if not needs_download(conn, item):
                    if item["deadline"]:
                        conn.execute("UPDATE rfqs SET deadline = ?, deadline_source = 'portal' WHERE rfq = ?", (item["deadline"], item["rfq"]))
                        conn.commit()
                    continue
                try:
                    path = download_report(page, item, config, download_dir)
                    _, is_new = store.import_report(conn, path, deadline=item["deadline"], title=item["title"], rfq=item["rfq"])
                    new += is_new
                    progress(f"[{n}/{len(rfqs)}] RFQ {item['rfq']} saved{' (new)' if is_new else ''}")
                except Exception as exc:
                    failed.append(item["rfq"])
                    progress(f"[{n}/{len(rfqs)}] RFQ {item['rfq']} failed: {str(exc).splitlines()[0]}")
                page.wait_for_timeout(config["delay_seconds"] * 1000)
            progress("Logging out of Exiros…")
            logout(page, config)
        finally:
            browser.close()
    progress("Done.")
    return len(rfqs), new, failed


def capture() -> Path:
    """One-time helper: the user browses the portal and each Enter saves the page."""
    folder = CAPTURE_DIR / date.today().isoformat()
    folder.mkdir(parents=True, exist_ok=True)
    config = load_config()
    with sync_playwright() as p:
        browser = open_browser(p)
        page = browser.new_page()
        if config.get("portal_url"):
            page.goto(config["portal_url"])
        print("\nLog in to Exiros in the browser, then open each page below and press Enter here to save it:")
        print("  1. the list of your RFQs   2. one quoted RFQ   3. one RFQ you didn't quote")
        n = 0
        while input(f"[{n} saved] Enter = save this page, q = finish: ").strip().lower() != "q":
            n += 1
            page = browser.contexts[0].pages[-1]
            page.wait_for_load_state()
            (folder / f"page{n}.html").write_text(page.content(), encoding="utf-8")
            (folder / f"page{n}.url.txt").write_text(page.url, encoding="utf-8")
            page.screenshot(path=str(folder / f"page{n}.png"), full_page=True)
            for i, frame in enumerate(page.frames[1:], 1):
                try:
                    (folder / f"page{n}_frame{i}.html").write_text(frame.content(), encoding="utf-8")
                except PlaywrightError:
                    pass
            print(f"   saved page {n}: {page.url}")
        browser.close()
    print(f"\nSaved {n} page(s) in {folder}. Zip that folder and send it.")
    return folder


if __name__ == "__main__":
    if sys.argv[1:] == ["capture"]:
        capture()
    else:
        print(__doc__)
