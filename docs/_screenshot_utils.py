"""Shared Playwright/Panel helpers for the docs screenshot generators.

Used by generate_downsample_screenshots.py and generate_process_screenshots.py
so the Wunderbaum-checkbox clicking, frame capture and panel-serve lifecycle
live in one place.
"""

import glob
import io
import os
import subprocess
import sys
import time

import imageio.v3 as iio

DOCS_DIR = os.path.dirname(os.path.abspath(__file__))
PACKAGE_DIR = os.path.dirname(DOCS_DIR)


def all_wb_shadows_js():
    """JS defining allWbShadows(root): every Wunderbaum shadow root, in order."""
    return """
    function allWbShadows(root) {
        const out = [];
        function rec(r) {
            for (const el of r.querySelectorAll('*')) {
                if (el.shadowRoot) {
                    if (el.shadowRoot.querySelectorAll('.wb-row').length > 0)
                        out.push(el.shadowRoot);
                    rec(el.shadowRoot);
                }
            }
        }
        rec(root);
        return out;
    }
    """


def click_tree_checkbox(page, tree_idx, cb_idx):
    """Click the cb_idx-th checkbox of the tree_idx-th Wunderbaum (0 = first)."""
    page.evaluate(
        f"""() => {{
        {all_wb_shadows_js()}
        const sh = allWbShadows(document)[{tree_idx}];
        if (sh) {{
            const cbs = sh.querySelectorAll('i.wb-checkbox');
            if (cbs[{cb_idx}]) cbs[{cb_idx}].click();
        }}
    }}"""
    )


def capture(page, frames, delay=800):
    """Wait `delay` ms, then append a screenshot to `frames`."""
    page.wait_for_timeout(delay)
    frames.append(iio.imread(io.BytesIO(page.screenshot())))


def _address_bar(url, width, height=52):
    """Render a synthetic browser address bar showing ``url`` (headless has
    no chrome, so we draw one to showcase URL-synced state)."""
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont

    bar = Image.new("RGB", (width, height), (241, 243, 244))
    d = ImageDraw.Draw(bar)
    cx = 20
    for _ in range(3):  # back / forward / reload glyphs
        d.ellipse(
            [cx - 7, height // 2 - 7, cx + 7, height // 2 + 7],
            outline=(150, 150, 150),
            width=2,
        )
        cx += 28
    x0 = cx + 6
    d.rounded_rectangle(
        [x0, 9, width - 12, height - 9],
        radius=(height - 18) // 2,
        fill=(255, 255, 255),
        outline=(205, 205, 205),
    )
    try:
        font = ImageFont.truetype("arial.ttf", 15)
    except Exception:
        font = ImageFont.load_default()
    text, maxw = url, width - 12 - (x0 + 16)
    if d.textlength(text, font=font) > maxw:
        while len(text) > 12 and d.textlength(text + "…", font=font) > maxw:
            text = text[:-1]
        text += "…"
    d.text((x0 + 14, height // 2 - 9), text, fill=(50, 50, 50), font=font)
    return np.asarray(bar)


def capture_with_address_bar(page, frames, delay=800, full_page=False):
    """Like :func:`capture`, but prepend a synthetic address bar with page.url.

    ``full_page=True`` captures the whole scrollable page (so content below the
    fold - e.g. a plot under a tall control stack - is included).
    """
    import numpy as np

    page.wait_for_timeout(delay)
    shot = iio.imread(io.BytesIO(page.screenshot(full_page=full_page)))
    bar = _address_bar(page.url, shot.shape[1])
    if shot.shape[2] == 4:  # match RGBA of the page screenshot
        alpha = np.full(bar.shape[:2] + (1,), 255, dtype=bar.dtype)
        bar = np.concatenate([bar, alpha], axis=2)
    frames.append(np.vstack([bar, shot]))


def start_server(example, port, env=None, clean_sqlite=False, settle=12):
    """Start ``panel serve <example> --port <port>`` and return the process.

    ``clean_sqlite`` first removes leftover ``*_db.sqlite`` in the package dir
    (so a SQLite demo reseeds fresh). ``settle`` is the seconds to wait for the
    first module execution before returning.
    """
    if clean_sqlite:
        for db in glob.glob(os.path.join(PACKAGE_DIR, "*_db.sqlite")):
            os.remove(db)
    logf = open(os.path.join(PACKAGE_DIR, f"_gen_server_{port}.log"), "w")
    proc_env = dict(os.environ)
    if env:
        proc_env.update(env)
    proc = subprocess.Popen(
        [sys.executable, "-m", "panel", "serve", example, "--port", str(port)],
        stdout=logf,
        stderr=subprocess.STDOUT,
        cwd=PACKAGE_DIR,
        env=proc_env,
    )
    time.sleep(settle)
    return proc


def stop_server(proc):
    # On Windows, terminate() leaves the `panel serve` child alive (it keeps the
    # port and leaks servers across runs); kill the whole process tree.
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
