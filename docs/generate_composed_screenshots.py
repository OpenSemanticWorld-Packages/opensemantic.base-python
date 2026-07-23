"""Generate a screenshot + GIF for the composed (multi-view) dashboard README.

Serves examples/composed_dashboard.py, selects a channel in each tab, and
captures with a synthetic address bar - the composed app URL-syncs one aggregate
config in PLAIN_KEYS mode, so the (readable) ``?app=...`` params are visible.

Prerequisites:
    pip install playwright imageio pillow
    playwright install chromium

Usage:
    python docs/generate_composed_screenshots.py
"""

import os

import imageio.v3 as iio
from _screenshot_utils import capture_with_address_bar as capture
from _screenshot_utils import (
    click_tree_checkbox,
    start_server,
    stop_server,
)
from playwright.sync_api import sync_playwright

DOCS_DIR = os.path.dirname(os.path.abspath(__file__))
PACKAGE_DIR = os.path.dirname(DOCS_DIR)
EXAMPLE = os.path.join(PACKAGE_DIR, "examples", "composed_dashboard.py")
PORT = 5015
URL = f"http://localhost:{PORT}/composed_dashboard"
VIEWPORT = {"width": 1400, "height": 900}


def main():
    print("Starting Panel server...")
    proc = start_server(EXAMPLE, PORT, clean_sqlite=True, settle=15)
    try:
        frames = []
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport=VIEWPORT)
            page.goto(URL, timeout=30000)
            page.wait_for_timeout(6000)
            capture(page, frames, 500)  # Reactor tab, empty

            # Select the Reactor tool (tree 0) -> plots + URL updates.
            click_tree_checkbox(page, 0, 0)
            capture(page, frames, 4000)

            # Switch to the Furnace tab; the sidebar swaps to its tree (now the
            # only tree in the DOM, so index 0), then select its tool.
            page.get_by_text("Furnace", exact=True).first.click()
            page.wait_for_timeout(2000)
            click_tree_checkbox(page, 0, 0)
            capture(page, frames, 4000)

            # Full-page static shot (shows the plot below the control stack).
            static = []
            capture(page, static, 800, full_page=True)

            browser.close()

        iio.imwrite(
            os.path.join(DOCS_DIR, "composed_demo.gif"),
            frames,
            duration=1600,
            loop=0,
        )
        iio.imwrite(os.path.join(DOCS_DIR, "screenshot_composed.png"), static[0])
        print(f"composed_demo.gif: {len(frames)} frames; screenshot saved")
    finally:
        print("Stopping server...")
        stop_server(proc)


if __name__ == "__main__":
    main()
