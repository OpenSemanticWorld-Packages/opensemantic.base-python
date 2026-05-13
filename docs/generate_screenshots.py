"""Generate screenshots and demo GIF for the DataToolView README.

Prerequisites:
    pip install playwright imageio
    playwright install chromium

Usage:
    python docs/generate_screenshots.py
"""

import io
import os
import subprocess
import sys
import time

import imageio.v3 as iio
from playwright.sync_api import sync_playwright

DOCS_DIR = os.path.dirname(os.path.abspath(__file__))
PACKAGE_DIR = os.path.dirname(DOCS_DIR)
EXAMPLE = os.path.join(PACKAGE_DIR, "examples", "datatool_dashboard.py")
PORT = 5010
URL = f"http://localhost:{PORT}/datatool_dashboard"
VIEWPORT = {"width": 1400, "height": 900}


def find_wunderbaum_js():
    """JS snippet to find the Wunderbaum shadow root."""
    return """
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


def click_checkbox(page, idx):
    """Click the idx-th Wunderbaum checkbox."""
    page.evaluate(
        f"""() => {{
        {find_wunderbaum_js()}
        const wbRoot = findWbShadow(document);
        if (wbRoot) {{
            const cbs = wbRoot.querySelectorAll('i.wb-checkbox');
            if (cbs[{idx}]) cbs[{idx}].click();
        }}
    }}"""
    )


def switch_temperature_unit(page):
    """Switch the Temperature unit dropdown to Celsius."""
    page.evaluate(
        """() => {
        function findAll(root) {
            const sels = root.querySelectorAll('select.bk-input');
            for (const s of sels) {
                const label = s.closest('.bk-input-group')
                    ?.querySelector('label')?.textContent || '';
                if (label.includes('Temperature')) {
                    for (let i = 0; i < s.options.length; i++) {
                        const t = s.options[i].text;
                        if (t.includes('C') && !t.includes('K') && t.length < 5) {
                            s.value = s.options[i].value;
                            s.dispatchEvent(new Event('change', {bubbles: true}));
                            return true;
                        }
                    }
                }
            }
            const all = root.querySelectorAll('*');
            for (const el of all) {
                if (el.shadowRoot) {
                    const r = findAll(el.shadowRoot);
                    if (r) return r;
                }
            }
            return false;
        }
        return findAll(document);
    }"""
    )


def capture(page, frames, delay=500):
    """Capture a screenshot frame."""
    page.wait_for_timeout(delay)
    buf = page.screenshot()
    frames.append(iio.imread(io.BytesIO(buf)))


def start_server():
    """Start the Panel server as a subprocess."""
    # Run from package root so relative DB paths resolve correctly
    cwd = PACKAGE_DIR

    # Clean up old DB so example creates fresh data
    db_path = os.path.join(cwd, "example_db.sqlite")
    if os.path.exists(db_path):
        os.remove(db_path)

    proc = subprocess.Popen(
        [sys.executable, "-m", "panel", "serve", EXAMPLE, "--port", str(PORT)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=cwd,
    )
    # Wait for server to be ready
    time.sleep(15)
    return proc


def stop_server(proc):
    """Stop the Panel server subprocess."""
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def main():
    print("Starting Panel server...")
    proc = start_server()

    try:
        frames = []

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport=VIEWPORT)
            page.goto(URL, timeout=20000)
            page.wait_for_timeout(5000)

            # Frames 0-1: Initial empty view
            capture(page, frames, 500)
            capture(page, frames, 500)

            # Select Sensor A (parent checkbox index 0)
            click_checkbox(page, 0)
            capture(page, frames, 3000)
            capture(page, frames, 1000)

            # Select Sensor B (parent checkbox index 5)
            click_checkbox(page, 5)
            capture(page, frames, 3000)
            capture(page, frames, 1500)

            # Switch Temperature unit to Celsius
            switch_temperature_unit(page)
            capture(page, frames, 2000)
            capture(page, frames, 1500)

            # Scroll down to show log console
            page.evaluate("window.scrollBy(0, 350)")
            capture(page, frames, 1000)
            capture(page, frames, 1500)

            # Hold final frame
            capture(page, frames, 500)

            browser.close()

        # Write GIF
        gif_path = os.path.join(DOCS_DIR, "archive_demo.gif")
        iio.imwrite(gif_path, frames, duration=1500, loop=0)
        print(f"archive_demo.gif: {len(frames)} frames")

        # Save key static screenshots
        iio.imwrite(os.path.join(DOCS_DIR, "screenshot_archive_plots.png"), frames[3])
        iio.imwrite(os.path.join(DOCS_DIR, "screenshot_unit_switch.png"), frames[7])
        iio.imwrite(os.path.join(DOCS_DIR, "screenshot_log_console.png"), frames[9])
        print("Static screenshots saved")

    finally:
        print("Stopping server...")
        stop_server(proc)


if __name__ == "__main__":
    main()
