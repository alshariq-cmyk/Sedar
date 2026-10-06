"""Run an Exiros update against a small fake portal served locally."""

import http.server
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path

import pytest

playwright_api = pytest.importorskip("playwright.sync_api")
import exiros_fetch  # noqa: E402
import store  # noqa: E402

from test_rfq_to_excel import make_report  # noqa: E402

CHROMIUM = os.environ.get("CHROMIUM_PATH", "/opt/pw-browsers/chromium")
needs_chromium = pytest.mark.skipif(not Path(CHROMIUM).exists(), reason="no local Chromium")

FUTURE = (datetime.now() + timedelta(days=3)).strftime("%m/%d/%Y")
PAST = (datetime.now() - timedelta(days=3)).strftime("%m/%d/%Y")
RFQS = {"5400796": f"{FUTURE} 14:00", "5400801": FUTURE, "5400815": f"{PAST} 10:00"}


def build_portal(root: Path, logouts: list) -> None:
    (root / "login.html").write_text(
        '<html><body><form action="/list1.html"><input name="user"><button id="login">Log in</button></form></body></html>')
    pages = [list(RFQS)[:2], list(RFQS)[2:]]
    for n, numbers in enumerate(pages, 1):
        rows = "".join(
            f'<tr class="rfq-row"><td><a class="rfq" href="/rfq/{r}.html">{r}</a></td><td class="title">Office supplies {r}</td>'
            f'<td class="deadline">Closes {RFQS[r]}</td></tr>' for r in numbers)
        nxt = f'<a id="next" href="/list{n + 1}.html">Next</a>' if n < len(pages) else ""
        (root / f"list{n}.html").write_text(
            f'<html><body><a id="logout" href="/logout.html">Log out</a><table>{rows}</table>{nxt}</body></html>')
    (root / "logout.html").write_text("<html><body>Bye</body></html>")
    (root / "rfq").mkdir()
    (root / "files").mkdir()
    for r in RFQS:
        (root / "rfq" / f"{r}.html").write_text(
            f'<html><body><a id="logout" href="/logout.html">Log out</a><a id="export" href="/files/{r}.xlsx" download="quotationsReport.xlsx">Export</a></body></html>')
        price = "" if r == "5400801" else 100
        make_report(root / "files" / f"{r}.xlsx",
                    [[1, "Item", "Item", "Tenaris", "2.0", "PZA", "11/30/2026", price, 2 if price else "", 30, "", ""]])


@pytest.fixture
def portal(tmp_path):
    site = tmp_path / "site"
    site.mkdir()
    requests: list[str] = []
    build_portal(site, requests)

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=str(site), **k)

        def log_message(self, *a):
            requests.append(self.path)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", requests
    server.shutdown()


def config_for(base: str) -> dict:
    config = exiros_fetch.load_config(Path("/nonexistent"))
    config.update({
        "portal_url": f"{base}/login.html", "logged_in_selector": "#logout",
        "rfq_row": "tr.rfq-row", "rfq_link": "a.rfq", "deadline": ".deadline", "title": ".title",
        "next_page": "#next", "download_button": "#export", "logout_selector": "#logout", "delay_seconds": 0,
    })
    return config


def headless(p):
    return p.chromium.launch(executable_path=CHROMIUM)


def test_parse_deadline():
    formats = exiros_fetch.DEFAULTS["deadline_formats"]
    # As the Exiros tender list shows it (month first).
    assert exiros_fetch.parse_deadline("10/07/2026 01:00 | 0d 19h 1m", formats) == "2026-10-07 01:00"
    assert exiros_fetch.parse_deadline("10/07/2026", formats) == "2026-10-07 23:59"
    assert exiros_fetch.parse_deadline("12-Oct-2026 09:30", formats) == "2026-10-12 09:30"
    assert exiros_fetch.parse_deadline("", formats) is None


def test_unconfigured_portal_is_reported():
    assert "isn't set up yet" in exiros_fetch.config_problem(exiros_fetch.load_config(Path("/nonexistent")))


@needs_chromium
def test_update_waits_for_login_then_collects_everything(portal, tmp_path):
    base, requests = portal
    conn = store.connect(tmp_path / "rfqs.db")
    config = config_for(base)
    log: list[str] = []

    def browser_factory(p):
        browser = headless(p)
        original_new_context = browser.new_context

        def new_context(**kwargs):
            context = original_new_context(**kwargs)
            # Stand in for the user: press "Log in" once the login page loads.
            context.on("page", lambda page: page.on("load", lambda: page.click("#login") if page.url.endswith("login.html") else None))
            return context

        browser.new_context = new_context
        return browser

    found, new, failed = exiros_fetch.update_from_exiros(conn, config, tmp_path / "downloads", log.append, browser_factory=browser_factory)
    assert (found, new, failed) == (3, 3, [])
    rows = {r["rfq"]: dict(r) for r in conn.execute("SELECT * FROM rfqs")}
    assert rows["5400796"]["deadline"].endswith("14:00")
    assert rows["5400801"]["deadline"].endswith("23:59")
    assert rows["5400801"]["status"] == "Not quoted"
    assert rows["5400796"]["status"] == "Quoted" and rows["5400796"]["total"] == 200
    assert rows["5400815"]["title"] == "Office supplies 5400815"
    assert any(path.startswith("/logout.html") for path in requests), "should log out at the end"

    # Second run: nothing new, and the closed RFQ isn't downloaded again.
    requests.clear()
    found, new, failed = exiros_fetch.update_from_exiros(conn, config, tmp_path / "downloads", log.append, browser_factory=browser_factory)
    assert (found, new) == (3, 0)
    assert not any("5400815.xlsx" in path for path in requests)
    assert any("5400796.xlsx" in path for path in requests)


@needs_chromium
def test_gives_up_if_never_logged_in(portal, tmp_path, monkeypatch):
    base, _ = portal
    monkeypatch.setattr(exiros_fetch, "LOGIN_TIMEOUT_MINUTES", 0.03)
    conn = store.connect(tmp_path / "rfqs.db")
    with pytest.raises(RuntimeError, match="Not logged in"):
        exiros_fetch.update_from_exiros(conn, config_for(base), tmp_path / "downloads", lambda m: None, browser_factory=headless)
