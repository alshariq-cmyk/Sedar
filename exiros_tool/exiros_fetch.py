"""Download all RFQ quotation reports from the Exiros supplier portal, then build one Excel file.

You log in yourself in the browser window this opens; the program does the rest.

    python exiros_fetch.py capture   # save the portal pages so the settings can be filled in
    python exiros_fetch.py run       # download every quotations report and build the Excel

Portal-specific details (which links and buttons to click) live in exiros_config.json.
"""

import json
import re
import sys
import time
from datetime import date
from pathlib import Path
from urllib.parse import urljoin

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

import rfq_to_excel

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "exiros_config.json"
PROFILE_DIR = HERE / "browser_profile"  # keeps you logged in between runs; delete it to log out
DOWNLOAD_DIR = HERE / "downloads"
CAPTURE_DIR = HERE / "captures"
OUTPUT_DIR = HERE / "output"


def load_config(path: Path = CONFIG_PATH) -> dict:
    config = json.loads(path.read_text(encoding="utf-8"))
    config.setdefault("delay_seconds", 3)
    config.setdefault("rfq_number_regex", r"\d{6,}")
    return config


def open_browser(playwright, headless: bool = False):
    """Use the Chrome or Edge already installed on the computer, falling back to Playwright's own."""
    options = dict(user_data_dir=str(PROFILE_DIR), headless=headless, accept_downloads=True, viewport=None)
    for channel in ("chrome", "msedge", None):
        try:
            return playwright.chromium.launch_persistent_context(channel=channel, **options) if channel else playwright.chromium.launch_persistent_context(**options)
        except PlaywrightError:
            continue
    raise SystemExit("Couldn't start a browser. Install Google Chrome or run: python -m playwright install chromium")


def wait_for_login(page, config: dict) -> None:
    if config.get("portal_url"):
        page.goto(config["portal_url"])
    input("\nLog in to Exiros in the browser window, then come back here and press Enter... ")


def capture(page) -> Path:
    """Save the HTML, a screenshot and the address of each page the user wants to show."""
    folder = CAPTURE_DIR / date.today().isoformat()
    folder.mkdir(parents=True, exist_ok=True)
    print("\nOpen each page I should learn, then press Enter here to save it:")
    print("  1. the list of your RFQs / quotations")
    print("  2. one RFQ, where the quotations report download button is")
    print("Type q and press Enter when done.")
    n = 0
    while input(f"[{n} saved] Enter = save this page, q = finish: ").strip().lower() != "q":
        n += 1
        page.wait_for_load_state()
        (folder / f"page{n}.html").write_text(page.content(), encoding="utf-8")
        (folder / f"page{n}.url.txt").write_text(page.url, encoding="utf-8")
        page.screenshot(path=str(folder / f"page{n}.png"), full_page=True)
        for i, frame in enumerate(page.frames[1:], 1):  # portals often put content in frames
            try:
                (folder / f"page{n}_frame{i}.html").write_text(frame.content(), encoding="utf-8")
            except PlaywrightError:
                pass
        print(f"   saved page {n}: {page.url}")
    print(f"\nSaved {n} page(s) in {folder}\nZip that folder and send it, so the settings file can be filled in.")
    return folder


def _join(base: str, href: str | None) -> str | None:
    return urljoin(base, href) if href and not href.lower().startswith("javascript") else None


def rfq_number(text: str, pattern: str) -> str | None:
    match = re.search(pattern, text or "")
    return match.group(0) if match else None


def collect_rfqs(page, config: dict) -> list[dict]:
    """Walk the RFQ list (all pages) and return [{rfq, href}] in list order."""
    if config.get("rfq_list_url"):
        page.goto(config["rfq_list_url"])
    found: dict[str, dict] = {}
    for _ in range(config.get("max_pages", 200)):
        page.wait_for_load_state()
        links = page.locator(config["rfq_link"])
        new = 0
        for i in range(links.count()):
            link = links.nth(i)
            text = link.inner_text().strip()
            href = link.get_attribute("href")
            number = rfq_number(text, config["rfq_number_regex"]) or rfq_number(href or "", config["rfq_number_regex"])
            if number and number not in found:
                found[number] = {"rfq": number, "href": _join(page.url, href), "index": i}
                new += 1
        next_button = page.locator(config["next_page"]) if config.get("next_page") else None
        if not new or next_button is None or next_button.count() == 0 or not next_button.first.is_enabled():
            break
        next_button.first.click()
        time.sleep(config["delay_seconds"])
    return list(found.values())


def already_downloaded(rfq: str) -> Path | None:
    return next(iter(sorted(DOWNLOAD_DIR.glob(f"{rfq}_quotationsReport.*"))), None)


def download_report(page, item: dict, config: dict) -> Path:
    if item["href"]:
        page.goto(item["href"])
    else:  # JavaScript-only links: go back to the list and click the row instead
        page.goto(config["rfq_list_url"])
        page.locator(config["rfq_link"]).nth(item["index"]).click()
    page.wait_for_load_state()
    with page.expect_download(timeout=config.get("download_timeout_seconds", 60) * 1000) as info:
        page.locator(config["download_button"]).first.click()
    download = info.value
    suffix = Path(download.suggested_filename).suffix or ".xls"
    target = DOWNLOAD_DIR / f"{item['rfq']}_quotationsReport{suffix}"
    download.save_as(target)
    return target


def run(page, config: dict) -> Path | None:
    missing = [k for k in ("rfq_link", "download_button") if not config.get(k)]
    if missing:
        raise SystemExit(f"exiros_config.json isn't filled in yet ({', '.join(missing)}). Run 'capture' first and send the pages.")
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    rfqs = collect_rfqs(page, config)
    print(f"\nFound {len(rfqs)} RFQ(s) in the list.")
    failed = []
    for n, item in enumerate(rfqs, 1):
        if already_downloaded(item["rfq"]):
            print(f"  [{n}/{len(rfqs)}] {item['rfq']}: already downloaded, skipping")
            continue
        try:
            path = download_report(page, item, config)
            print(f"  [{n}/{len(rfqs)}] {item['rfq']}: saved {path.name}")
        except PlaywrightError as exc:
            failed.append(item["rfq"])
            print(f"  [{n}/{len(rfqs)}] {item['rfq']}: FAILED ({str(exc).splitlines()[0]})")
        time.sleep(config["delay_seconds"])

    OUTPUT_DIR.mkdir(exist_ok=True)
    output = OUTPUT_DIR / f"Exiros_RFQs_{date.today().isoformat()}.xlsx"
    if rfq_to_excel.main([str(output), str(DOWNLOAD_DIR)]) != 0:
        return None
    if failed:
        print(f"\nCouldn't download {len(failed)} RFQ(s): {', '.join(failed)}. Run again to retry just those.")
    print(f"\nDone. Your Excel file: {output}")
    return output


def main(argv: list[str]) -> int:
    mode = argv[0] if argv else "run"
    if mode not in ("run", "capture"):
        print(__doc__)
        return 2
    config = load_config()
    with sync_playwright() as p:
        browser = open_browser(p)
        page = browser.pages[0] if browser.pages else browser.new_page()
        try:
            wait_for_login(page, config)
            capture(page) if mode == "capture" else run(page, config)
        finally:
            browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
