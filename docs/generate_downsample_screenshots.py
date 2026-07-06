"""Generate screenshots and a GIF for the downsampling demo README.

Serves examples/downsample_demo.py (which reads from a running pgstack with the
downsampling RPC applied and pre-seeded data) and drives the UI with Playwright.

The script tells the whole story on a single full-window server:
  1. Full window: select each channel (one strategy each). minmax keeps the
     spikes, sample/average lose them at the coarse full-window resolution, raw
     shows full detail (slow 100k load).
  2. Zoom range: narrow the shared x-range to a window around the first spike
     (this is what a horizontal box-zoom produces) - the spikes are still
     missing because only the already-loaded coarse points are shown.
  3. Load current range: click the button - the zoomed window is re-fetched at
     finer buckets and the hidden spikes reappear on sample/average.

Steps 2-3 exercise the real interactive feature (set zoom range -> "Load
current range" re-fetch), so this script doubles as an end-to-end validation of
the zoom path: if the zoomed window were loaded at the wrong position (e.g. a
timezone offset bug), the spike would not land inside the narrowed axis.

Prerequisites:
    pip install playwright imageio
    playwright install chromium
    # a running pgstack on localhost:3000 with the demo data seeded
    #   (run `python examples/downsample_demo.py` once to seed)

Usage:
    python docs/generate_downsample_screenshots.py
"""

import datetime as dt
import os

import imageio.v3 as iio
from _screenshot_utils import (
    capture,
    click_tree_checkbox,
    start_server,
    stop_server,
)
from playwright.sync_api import sync_playwright

DOCS_DIR = os.path.dirname(os.path.abspath(__file__))
PACKAGE_DIR = os.path.dirname(DOCS_DIR)
EXAMPLE = os.path.join(PACKAGE_DIR, "examples", "downsample_demo.py")
VIEWPORT = {"width": 1400, "height": 1300}

# Tree checkbox order: 0 = DataTool root, then channels in declaration order
# (raw, sample, average, minmax).
CB_RAW = 1
CB_SAMPLE = 2
CB_AVERAGE = 3
CB_MINMAX = 4

# Demo series constants (mirror examples/downsample_demo.py) used to compute the
# zoom window directly, instead of reading possibly-stale Bokeh range models.
BASE_TS = dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)
# Zoom window in data seconds, around the first spike (at 12 000 s) so the finer
# buckets reveal it on sample/average after "Load current range".
ZOOM_SEC = (4_000, 20_000)

LOAD_RANGE_LABEL = "Load current range"


def axis_ms(sec):
    """Epoch-ms x-axis position of data second ``sec``.

    The plot draws timestamps in local wall time (naive), so a box-zoom range is
    expressed as the local-wall-clock epoch. Mirror that: take the UTC instant
    ``BASE_TS + sec``, read it as local wall time, and return that as an epoch.
    """
    utc = BASE_TS + dt.timedelta(seconds=sec)
    naive_local = utc.astimezone().replace(tzinfo=None)
    return naive_local.replace(tzinfo=dt.timezone.utc).timestamp() * 1000.0


def click_button(page, label):
    """Click the first <button> whose text contains label (across shadow DOMs)."""
    return page.evaluate(
        """(l) => {
            function f(r) {
                for (const b of r.querySelectorAll('button')) {
                    if ((b.textContent || '').includes(l)) { b.click(); return true; }
                }
                for (const e of r.querySelectorAll('*')) {
                    if (e.shadowRoot && f(e.shadowRoot)) return true;
                }
                return false;
            }
            return f(document);
        }""",
        label,
    )


def read_time_xrange(page):
    """Return [start, end] (epoch ms) of the shared datetime x-range, or None.

    The time axis is the DataRange1d with epoch-ms bounds (>1e11); the value
    axis has small bounds and is filtered out.
    """
    return page.evaluate(
        """() => {
            const docs = (window.Bokeh && window.Bokeh.documents) || [];
            for (const doc of docs) {
                const models = doc._all_models
                    ? Array.from(doc._all_models.values()) : [];
                for (const m of models) {
                    if (m.type === 'DataRange1d' && m.start != null
                        && m.end != null && m.end > 1e11) {
                        return [m.start, m.end];
                    }
                }
            }
            return null;
        }"""
    )


def set_time_xrange(page, start, end):
    """Set every datetime DataRange1d to [start, end] (simulates a box-zoom)."""
    page.evaluate(
        """([s, e]) => {
            const docs = (window.Bokeh && window.Bokeh.documents) || [];
            for (const doc of docs) {
                const models = doc._all_models
                    ? Array.from(doc._all_models.values()) : [];
                for (const m of models) {
                    if (m.type === 'DataRange1d' && m.end > 1e11) {
                        m.setv({start: s, end: e});
                    }
                }
            }
        }""",
        [start, end],
    )


def click_reset_tool(page):
    """Real pointer click on the Bokeh reset tool icon (across shadow DOMs).

    figure.on_event(Reset) does not propagate through Panel, and a synthetic
    el.click() does not trigger the Bokeh pointer handler, so locate the reset
    icon and dispatch a real mouse down/up on its center.
    """
    box = page.evaluate(
        """() => {
            function f(r) {
                for (const el of r.querySelectorAll('.bk-tool-icon-reset')) {
                    const b = el.getBoundingClientRect();
                    return {x: b.x + b.width / 2, y: b.y + b.height / 2};
                }
                for (const e of r.querySelectorAll('*')) {
                    if (e.shadowRoot) { const g = f(e.shadowRoot); if (g) return g; }
                }
                return null;
            }
            return f(document);
        }"""
    )
    if not box:
        return False
    page.mouse.move(box["x"], box["y"])
    page.mouse.down()
    page.wait_for_timeout(60)
    page.mouse.up()
    return True


def max_line_points(page):
    """Largest point count across line data sources (0 if no data yet)."""
    return page.evaluate(
        """() => {
            let mx = 0;
            const docs = window.Bokeh.documents || [];
            for (const d of docs)
                for (const m of (d._all_models
                    ? Array.from(d._all_models.values()) : []))
                    if (m.type === 'ColumnDataSource' && m.data
                        && m.data.x && m.data.x.length)
                        mx = Math.max(mx, m.data.x.length);
            return mx;
        }"""
    )


def wait_for_data(page, min_points=100, timeout_ms=30000, settle_ms=1500):
    """Poll until a line source has >= min_points, then let it settle.

    The demo loads asynchronously (lazy PostgREST client, serialized reloads);
    capturing or zooming before the load settles races with a late figure
    rebuild that reverts the view. Returns the final point count.
    """
    waited = 0
    while waited < timeout_ms:
        if max_line_points(page) >= min_points:
            page.wait_for_timeout(settle_ms)
            return max_line_points(page)
        page.wait_for_timeout(500)
        waited += 500
    return max_line_points(page)


def set_zoom_range(page, zs, ze, tries=4):
    """Set the zoom window and confirm no figure rebuild reverted it."""
    for _ in range(tries):
        set_time_xrange(page, zs, ze)
        page.wait_for_timeout(700)
        cur = read_time_xrange(page)
        if cur and abs(cur[0] - zs) < 1e6 and abs(cur[1] - ze) < 1e6:
            return True
    return False


def main():
    frames = []
    compare_frame = raw_frame = zoom_select_frame = zoom_frame = None

    proc = start_server(EXAMPLE, 5013)
    try:
        with sync_playwright() as p:
            page = p.chromium.launch(headless=True).new_page(viewport=VIEWPORT)
            page.goto(
                "http://localhost:5013/downsample_demo",
                timeout=120000,
                wait_until="domcontentloaded",
            )
            page.wait_for_timeout(9000)
            capture(page, frames, 800)  # empty

            # -- Pass 1: full-window comparison, one strategy per channel --
            # minmax keeps the spikes; sample/average lose them at the coarse
            # full-window resolution; raw shows full detail (slow 100k load).
            print("Pass 1: full-window comparison...")
            click_tree_checkbox(page, 0, CB_MINMAX)  # spikes kept
            capture(page, frames, 5000)
            click_tree_checkbox(page, 0, CB_SAMPLE)  # spikes lost
            capture(page, frames, 5000)
            click_tree_checkbox(page, 0, CB_AVERAGE)  # spikes smoothed
            print(f"  loaded points: {wait_for_data(page)}")
            capture(page, frames, 1500)
            compare_frame = frames[-1]

            click_tree_checkbox(page, 0, CB_RAW)  # full detail, slow
            raw_pts = wait_for_data(page, min_points=50000, timeout_ms=90000)
            print(f"  raw points: {raw_pts}")
            capture(page, frames, 1500)
            raw_frame = frames[-1]

            # -- Pass 2: interactive zoom on a single channel --
            # Driven on one figure (sample) on a fresh session: with a single
            # x-range there are no linked/stale-range races, so the box-zoom ->
            # "Load current range" -> reset flow is captured reliably and the
            # spike-reveal is unambiguous.
            print("Pass 2: interactive zoom on 'sample'...")
            page.goto(
                "http://localhost:5013/downsample_demo",
                timeout=120000,
                wait_until="domcontentloaded",
            )
            page.wait_for_timeout(9000)
            click_tree_checkbox(page, 0, CB_SAMPLE)
            print(f"  sample points: {wait_for_data(page)}")
            capture(page, frames, 1500)  # full window, spike hidden in coarse data

            # Zoom range (what a horizontal box-zoom produces); spikes still
            # missing because only the coarse full-window points are shown.
            zs, ze = axis_ms(ZOOM_SEC[0]), axis_ms(ZOOM_SEC[1])
            print(f"  zoom x-range (epoch ms): [{zs:.0f}, {ze:.0f}]")
            print(f"  zoom set confirmed: {set_zoom_range(page, zs, ze)}")
            capture(page, frames, 2500)
            zoom_select_frame = frames[-1]

            # Load current range: re-fetch the zoomed window at finer buckets;
            # the hidden spike reappears.
            clicked = click_button(page, LOAD_RANGE_LABEL)
            print(f"  '{LOAD_RANGE_LABEL}' clicked: {clicked}")
            if not clicked:
                raise RuntimeError(f"button '{LOAD_RANGE_LABEL}' not found")
            print(f"  reloaded points: {wait_for_data(page)}")
            capture(page, frames, 1500)
            zoom_frame = frames[-1]

            # Toolbar reset returns to the full window.
            reset_ok = click_reset_tool(page)
            print(f"  reset tool clicked: {reset_ok}")
            wait_for_data(page)
            capture(page, frames, 2000)
    finally:
        stop_server(proc)

    iio.imwrite(
        os.path.join(DOCS_DIR, "downsample_demo.gif"), frames, duration=1400, loop=0
    )
    print(f"downsample_demo.gif: {len(frames)} frames")
    if compare_frame is not None:
        iio.imwrite(
            os.path.join(DOCS_DIR, "screenshot_downsample_compare.png"), compare_frame
        )
    if raw_frame is not None:
        iio.imwrite(os.path.join(DOCS_DIR, "screenshot_downsample_raw.png"), raw_frame)
    if zoom_select_frame is not None:
        iio.imwrite(
            os.path.join(DOCS_DIR, "screenshot_downsample_zoom_select.png"),
            zoom_select_frame,
        )
    if zoom_frame is not None:
        iio.imwrite(
            os.path.join(DOCS_DIR, "screenshot_downsample_zoom.png"), zoom_frame
        )
    print("Screenshots saved")


if __name__ == "__main__":
    main()
