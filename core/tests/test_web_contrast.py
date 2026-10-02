# SPDX-License-Identifier: BUSL-1.1
"""Light theme: every text/background pair of the variables meets WCAG AA.

Measured in the light theme: green pills 2.93:1, the warning banner 3.23:1,
the OK banner 3.49:1, the danger button 3.70:1 -- below the 4.5:1 that body
text needs. The pairs below are the ones the stylesheet actually combines
(``.pill-*``, banners, buttons, links); the guard computes their contrast from
the variables, so a later colour change cannot silently fall back.
"""

from __future__ import annotations

import re

from web_vm import APP_JS

STYLES = (APP_JS.parent / "styles.css").read_text(encoding="utf-8")

#: (text variable, background variable) pairs used for text in the stylesheet.
PAIRS = [
    *[(c, bg) for c in ("green", "amber", "red", "gray", "accent")
      for bg in ("bg", "bg-elev", "bg-elev2")],
    ("green", "green-soft"), ("amber", "amber-soft"), ("red", "red-soft"),
    ("gray", "gray-soft"), ("accent-txt", "accent-soft"), ("purple-txt", "purple-soft"),
    ("on-accent", "accent"), ("on-green", "green"),
    ("txt", "bg"), ("txt", "bg-elev"), ("txt", "gray-soft"),
    ("txt-dim", "bg"), ("txt-dim", "bg-elev"), ("txt-dim", "bg-elev2"),
]


def _vars(selector: str) -> dict[str, str]:
    m = re.search(re.escape(selector) + r"\s*\{(.*?)\}", STYLES, re.S)
    assert m, selector
    return dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6})", m.group(1)))


def _lum(hex_colour: str) -> float:
    chans = [int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in chans]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a: str, b: str) -> float:
    la, lb = _lum(a), _lum(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def low_pairs(colours: dict[str, str]) -> list[str]:
    """Pairs below 4.5:1, named so a failure says which one."""

    return [f"--{f} auf --{b}: {contrast(colours[f], colours[b]):.2f}"
            for f, b in PAIRS if contrast(colours[f], colours[b]) < 4.5]


def test_light_theme_text_pairs_meet_wcag_aa() -> None:
    light = _vars(':root[data-theme="light"]')
    assert not low_pairs(light)


def test_finder_names_a_failing_pair() -> None:
    """Counter-check of the guard with the earlier green."""

    light = _vars(':root[data-theme="light"]')
    light["green"] = "#1f9d57"
    assert "--green auf --green-soft: 2.93" in low_pairs(light)


def test_filled_buttons_take_their_text_colour_from_a_variable() -> None:
    """A fixed text colour on a variable background escapes the pair check --
    the green button kept dark text when the light green became darker."""

    for cls in (".btn.primary", ".btn.green"):
        m = re.search(re.escape(cls) + r" \{([^}]*)\}", STYLES)
        assert m and re.search(r"(?<!-)color: var\(--on-", m.group(1)), cls


def test_dark_theme_green_button_keeps_its_contrast() -> None:
    dark = _vars(":root")
    assert contrast(dark["on-green"], dark["green"]) >= 4.5


def test_contrast_formula() -> None:
    assert round(contrast("#000000", "#ffffff"), 1) == 21.0
    assert round(contrast("#777777", "#ffffff"), 2) == 4.48
