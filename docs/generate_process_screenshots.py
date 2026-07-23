"""Generate screenshots and demo GIF for the ProcessObjectView README.

Prerequisites:
    pip install playwright imageio
    playwright install chromium

Usage:
    python docs/generate_process_screenshots.py
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
EXAMPLE = os.path.join(PACKAGE_DIR, "examples", "process_dashboard.py")
PORT = 5012
URL = f"http://localhost:{PORT}/process_dashboard"
VIEWPORT = {"width": 1400, "height": 900}


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
            for (const el of root.querySelectorAll('*')) {
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


def main():
    print("Starting Panel server...")
    proc = start_server(EXAMPLE, PORT, clean_sqlite=True, settle=10)
    try:
        frames = []
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport=VIEWPORT)
            # panel serve rebuilds the demo data per session -> slow first load.
            page.goto(URL, timeout=120000, wait_until="domcontentloaded")
            page.wait_for_timeout(12000)

            # Frame 0-1: initial view (both trees, process tree shows
            # per-instance Evacuation entries and merged Heating entries).
            capture(page, frames, 800)
            capture(page, frames, 500)
            tree_shot = len(frames) - 1

            # Select object "Sample 1" (objects tree, checkbox 0).
            click_tree_checkbox(page, 0, 0)
            capture(page, frames, 1500)

            # Select Evacuation / FurnaceProbe-A / temp
            # (process tree: 0=Evac root, 1=A/temp, 2=B/temp, 3=A/press, ...).
            click_tree_checkbox(page, 1, 1)
            capture(page, frames, 3000)
            overlay_shot = len(frames) - 1  # Sample 1: two Evacuation runs

            # Add FurnaceProbe-B / temp -> co-present fan-out.
            click_tree_checkbox(page, 1, 2)
            capture(page, frames, 2500)

            # Add object "Sample 2" -> compare across objects.
            click_tree_checkbox(page, 0, 1)
            capture(page, frames, 3000)
            fanout_shot = len(frames) - 1

            # Switch Temperature unit to Celsius.
            switch_temperature_unit(page)
            capture(page, frames, 2500)
            units_shot = len(frames) - 1
            capture(page, frames, 1000)

            # Also add the Heating process so BOTH processes are selected at the
            # end. Evacuation stays selected; the merged Heating entry is a
            # drop-in pool resolving to probe A for Sample 1 and probe B for
            # Sample 2 (process tree: 6 = Heating DataTool/temp).
            click_tree_checkbox(page, 1, 6)  # check Heating DataTool/temp
            capture(page, frames, 3000)

            # Finally add the Heating pressure channel (process tree: 7) -> a
            # second plot group (Pressure) appears alongside Temperature.
            click_tree_checkbox(page, 1, 7)  # check Heating DataTool/pressure
            capture(page, frames, 3000)
            heating_shot = len(frames) - 1
            capture(page, frames, 1500)

            browser.close()

        gif_path = os.path.join(DOCS_DIR, "process_demo.gif")
        iio.imwrite(gif_path, frames, duration=1500, loop=0)
        print(f"process_demo.gif: {len(frames)} frames")

        iio.imwrite(
            os.path.join(DOCS_DIR, "screenshot_process_trees.png"), frames[tree_shot]
        )
        iio.imwrite(
            os.path.join(DOCS_DIR, "screenshot_process_overlay.png"),
            frames[overlay_shot],
        )
        iio.imwrite(
            os.path.join(DOCS_DIR, "screenshot_process_fanout.png"),
            frames[fanout_shot],
        )
        iio.imwrite(
            os.path.join(DOCS_DIR, "screenshot_process_units.png"),
            frames[units_shot],
        )
        iio.imwrite(
            os.path.join(DOCS_DIR, "screenshot_process_heating.png"),
            frames[heating_shot],
        )
        print("Static screenshots saved")
    finally:
        print("Stopping server...")
        stop_server(proc)


if __name__ == "__main__":
    main()
