# SPDX-License-Identifier: BUSL-1.1
"""Tour popup never lands on top of its target when there is room elsewhere.

With a wide, tall target (task list, absence panel, backups) the popup fitted
neither below nor above and was clamped into the target -- right onto the row
the step was about. ``autoPosition`` is pure; this runs it in Node.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parents[2] / "web" / "tour" / "engine.js"
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="Node.js not available")


def _place(cases: list[dict[str, object]], tmp_path: Path) -> list[dict[str, float]]:
    script = tmp_path / "place.js"
    script.write_text(
        "const vm = require('vm'); const fs = require('fs');\n"
        "const ctx = { window: {}, document: {}, localStorage: {}, console };\n"
        "vm.createContext(ctx);\n"
        f"vm.runInContext(fs.readFileSync({json.dumps(str(ENGINE))}, 'utf8') + "
        "'\\n;globalThis.Tour = Tour;', ctx);\n"
        f"const cases = {json.dumps(cases)};\n"
        "console.log(JSON.stringify(cases.map((c) => ctx.Tour._autoPosition("
        "c.r, c.w, c.h, c.viewW, c.ub, 12, c.top))));\n",
        encoding="utf-8",
    )
    out = subprocess.run([NODE or "node", str(script)], capture_output=True, text=True,
                         check=True, timeout=30)
    return json.loads(out.stdout)


def _rect(left: float, top: float, right: float, bottom: float) -> dict[str, float]:
    return {"left": left, "top": top, "right": right, "bottom": bottom,
            "width": right - left, "height": bottom - top}


def _overlap(r: dict[str, float], pos: dict[str, float], w: float, h: float) -> float:
    dx = max(0.0, min(r["right"], pos["left"] + w) - max(r["left"], pos["left"]))
    dy = max(0.0, min(r["bottom"], pos["top"] + h) - max(r["top"], pos["top"]))
    return dx * dy


@needs_node
def test_room_below_or_above_is_used(tmp_path: Path) -> None:
    small = _rect(100, 100, 400, 140)
    low = _rect(100, 500, 400, 560)
    below, above, forced = _place([
        {"r": small, "w": 300, "h": 200, "viewW": 1054, "ub": 676},
        {"r": low, "w": 300, "h": 200, "viewW": 1054, "ub": 676},
        {"r": low, "w": 300, "h": 200, "viewW": 1054, "ub": 676, "top": True},
    ], tmp_path)
    assert below["top"] == 152
    assert above["top"] == 500 - 200 - 12
    assert forced["top"] == 288


@needs_node
def test_wide_tall_target_is_covered_as_little_as_possible(tmp_path: Path) -> None:
    """Nearly full-width target, no room above, below or beside."""

    w, h = 360, 260
    # Panel beginnt oben im Bild, reicht weit nach unten: Popup an den unteren
    # Rand, die oberen Zeilen bleiben frei.
    tall = _rect(240, 60, 1040, 600)
    # Panel mittig, unten viel davon: oben frei lassen ist hier besser.
    mid = _rect(240, 250, 1040, 660)
    p_tall, p_mid = _place([
        {"r": tall, "w": w, "h": h, "viewW": 1054, "ub": 676},
        {"r": mid, "w": w, "h": h, "viewW": 1054, "ub": 676},
    ], tmp_path)
    assert p_tall["top"] == 676 - h - 12          # unten
    assert p_mid["top"] == 12                     # oben, kaum ueber dem Panel
    bottom_alt = {"left": p_mid["left"], "top": 676 - h - 12}
    assert _overlap(mid, p_mid, w, h) < _overlap(mid, bottom_alt, w, h)
    # Die erste Zeile des hohen Panels (oberste 120 px) bleibt sichtbar.
    assert p_tall["top"] >= tall["top"] + 120


@needs_node
def test_side_is_used_when_the_target_leaves_room_beside_it(tmp_path: Path) -> None:
    r = _rect(500, 50, 700, 640)
    [pos] = _place([{"r": r, "w": 300, "h": 300, "viewW": 1054, "ub": 676}], tmp_path)
    assert _overlap(r, pos, 300, 300) == 0


@needs_node
def test_position_stays_inside_the_window_for_off_screen_targets(tmp_path: Path) -> None:
    above = _rect(100, -400, 400, -300)
    below = _rect(100, 900, 400, 1000)
    for pos in _place([
        {"r": above, "w": 300, "h": 200, "viewW": 1054, "ub": 676},
        {"r": below, "w": 300, "h": 200, "viewW": 1054, "ub": 676},
    ], tmp_path):
        assert 12 <= pos["top"] <= 676 - 200 - 12, pos
