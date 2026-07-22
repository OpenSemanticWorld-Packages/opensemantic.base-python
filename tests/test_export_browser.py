"""Playwright browser tests for the archive-view export features.

Serves ``examples/datatool_dashboard.py`` with ``panel serve`` and drives it
with a headless Chromium: selects channels, expands the Export card in the Plot
Controls sidebar, and downloads the data (CSV) and plot (HTML), asserting the
downloaded bytes.

Skipped unless playwright (and a Chromium build) plus the export deps are
available; the server/browser self-skip if they cannot start.
"""

import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import time

import pytest

pytest.importorskip("playwright")
pytest.importorskip("pandas")
pytest.importorskip("pint_pandas")
from playwright.sync_api import sync_playwright  # noqa: E402

PACKAGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLE = os.path.join(PACKAGE_DIR, "examples", "datatool_dashboard.py")
PORT = 5021
URL = f"http://localhost:{PORT}/datatool_dashboard"


# JS: find the Wunderbaum shadow root (the tree grid lives in a shadow DOM).
_WB_SHADOW_JS = """
function findWbShadow(root) {
    const all = root.querySelectorAll('*');
    for (const el of all) {
        if (el.shadowRoot) {
            const rows = el.shadowRoot.querySelectorAll('.wb-row');
            if (rows.length > 0) return el.shadowRoot;
            const deeper = findWbShadow(el.shadowRoot);
            if (deeper) return deeper;
        }
    }
    return null;
}
"""


def _click_checkbox(page, idx):
    """Click the idx-th Wunderbaum checkbox (piercing the shadow root)."""
    page.evaluate(
        f"""() => {{
        {_WB_SHADOW_JS}
        const wbRoot = findWbShadow(document);
        if (wbRoot) {{
            const cbs = wbRoot.querySelectorAll('i.wb-checkbox');
            if (cbs[{idx}]) cbs[{idx}].click();
        }}
    }}"""
    )


def _download(page, label):
    """Click a FileDownload button and return the downloaded file path.

    Uses the button role (Playwright pierces Bokeh's shadow DOM); the download
    is generated lazily server-side on click.
    """
    btn = page.get_by_role("button", name=label)
    btn.scroll_into_view_if_needed()
    with page.expect_download(timeout=20000) as dl:
        btn.click()
    return dl.value.path()


def _wait_ready(timeout=40):
    # A raw TCP connect (not an HTTP GET) avoids corporate-proxy routing that
    # would otherwise divert a localhost request. The app is imported before
    # the port opens, so an accepting socket means the dashboard is ready.
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", PORT), timeout=2):
                return True
        except OSError:
            time.sleep(1)
    return False


def _kill_port(port):
    """Free a TCP port on Windows (leftover panel servers block re-runs).

    Parses ``netstat`` by columns rather than the state word, which is
    localized (e.g. German "ABHOEREN" instead of "LISTENING").
    """
    if os.name != "nt":
        return
    try:
        out = subprocess.run(
            ["netstat", "-ano"], capture_output=True, text=True, timeout=10
        ).stdout
    except Exception:
        return
    pids = set()
    for line in out.splitlines():
        parts = line.split()
        # proto local foreign state pid  -> a listening socket's local address
        # ends with :port and its pid is non-zero.
        if len(parts) >= 5 and parts[1].endswith(f":{port}") and parts[-1] != "0":
            pids.add(parts[-1])
    for pid in pids:
        subprocess.run(["taskkill", "/F", "/T", "/PID", pid], capture_output=True)


def _terminate(proc):
    """Kill the whole panel server process tree (Windows-safe)."""
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
        )
    else:
        proc.terminate()
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture(scope="module")
def server():
    # Serve an isolated copy of the example: its SQLite DB path is
    # ``Path(__file__).parent``, so a copy in a fresh temp dir gets its own DB
    # and never contends with a running demo on the shared example file.
    _kill_port(PORT)  # clear any leftover server from an aborted run
    workdir = tempfile.mkdtemp(prefix="export_browser_")
    # Keep the filename so the served route stays /datatool_dashboard.
    app = os.path.join(workdir, "datatool_dashboard.py")
    shutil.copyfile(EXAMPLE, app)
    # Log to a file (not PIPE): a full PIPE buffer would stall the child during
    # the verbose startup and the server would never come up.
    log = tempfile.NamedTemporaryFile(
        prefix="panel_export_test_", suffix=".log", delete=False
    )
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "panel",
            "serve",
            app,
            "--port",
            str(PORT),
            "--allow-websocket-origin",
            f"localhost:{PORT}",
        ],
        stdout=log,
        stderr=subprocess.STDOUT,
        cwd=workdir,
    )
    try:
        if not _wait_ready():
            proc.terminate()
            log.flush()
            with open(log.name, "r", encoding="utf-8", errors="replace") as fh:
                out = fh.read()
            pytest.skip(f"panel serve did not become ready:\n{out[-1200:]}")
        yield URL
    finally:
        _terminate(proc)
        log.close()
        try:
            os.remove(log.name)
        except OSError:
            pass
        shutil.rmtree(workdir, ignore_errors=True)


@pytest.fixture(scope="module")
def page(server):
    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=True)
            except Exception as e:
                pytest.skip(f"Chromium not available: {e}")
            context = browser.new_context(
                viewport={"width": 1400, "height": 900}, accept_downloads=True
            )
            pg = context.new_page()
            pg.goto(server, timeout=30000)
            pg.wait_for_timeout(6000)
            # Select "Sensor A" parent (checkbox 0) -> all its channels, incl.
            # the text "Status" channel. auto_fetch is on, so data loads.
            _click_checkbox(pg, 0)
            pg.wait_for_timeout(5000)
            # Expand the collapsed "Export" card in Plot Controls.
            header = pg.get_by_text("Export", exact=True).first
            header.scroll_into_view_if_needed()
            header.click()
            pg.wait_for_timeout(1500)
            yield pg
            browser.close()
    except Exception as e:  # pragma: no cover - environment guard
        pytest.skip(f"Playwright session failed: {e}")


def test_download_csv_has_units_and_text_channel(page):
    path = _download(page, "Download CSV")
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    # Numeric channel with a unit header row, plus the text "Status" channel.
    assert "Temperature" in text
    assert "unit" in text
    assert "Status" in text
    # More than just the two header rows (real data present).
    assert len(text.splitlines()) > 3


# Recurse through open shadow roots (Bokeh 3 renders its canvas in one).
_CANVAS_W_JS = """
() => {
    let maxw = 0;
    const walk = (root) => {
        for (const c of root.querySelectorAll('canvas')) {
            maxw = Math.max(maxw, Math.round(c.getBoundingClientRect().width));
        }
        for (const el of root.querySelectorAll('*')) {
            if (el.shadowRoot) walk(el.shadowRoot);
        }
    };
    walk(document);
    return maxw;
}
"""


def test_download_plot_html(page):
    path = _download(page, "Download plot (HTML)")
    with open(path, "rb") as fh:
        low = fh.read().lower()
    assert b"<html" in low and b"bokeh" in low
    # The log console (text-log channel) is embedded below the plots.
    assert b"log console" in low and b"[status]" in low
    # Open the standalone file and confirm the plot actually renders (guards the
    # regression where stretch_width figures collapse to zero width in a
    # container-less HTML page).
    viewer = page.context.new_page()
    try:
        dst = os.path.join(tempfile.mkdtemp(prefix="plot_html_"), "plot.html")
        shutil.copyfile(path, dst)
        viewer.goto(pathlib.Path(dst).as_uri(), timeout=20000)
        viewer.wait_for_timeout(3000)
        assert viewer.evaluate(_CANVAS_W_JS) > 200
    finally:
        viewer.close()
