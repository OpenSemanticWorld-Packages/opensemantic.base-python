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
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
