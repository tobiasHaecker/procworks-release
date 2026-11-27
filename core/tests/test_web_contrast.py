# SPDX-License-Identifier: BUSL-1.1
"""Both themes: every text/background pair of the variables meets WCAG AA.

Measured in the light theme: green pills 2.93:1, the warning banner 3.23:1,
the OK banner 3.49:1, the danger button 3.70:1 -- below the 4.5:1 that body
text needs. In the dark default theme white on the primary button (and the
skip link) reached only 3.39:1. The pairs below are the ones the stylesheet
actually combines (``.pill-*``, banners, buttons, links); the guard computes
their contrast from the variables of both themes, so a later colour change
cannot silently fall back.
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
    ("on-accent", "accent"), ("on-green", "green"), ("on-red", "red"),
    ("txt", "bg"), ("txt", "bg-elev"), ("txt", "gray-soft"),
    ("txt-dim", "bg"), ("txt-dim", "bg-elev"), ("txt-dim", "bg-elev2"),
]


def _strip_comments(css: str) -> str:
    """Entfernt Kommentare so, wie der Browser sie liest.

    Ein Kommentar endet am **ersten** ``*/`` -- auch mitten in einem Satz wie
    ``(tl-*/tree-Badges)``. Der Rest danach ist dann kein Kommentar mehr,
    sondern Teil der naechsten Deklaration und macht sie ungueltig.

    :param css: Stylesheet-Text.
    :returns: der Text ohne Kommentare.
    """

    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def _declared(selector: str, css: str = STYLES) -> dict[str, str]:
    """Die Variablen, die ein Browser im Block ``selector`` tatsaechlich setzt.

    Wie der CSS-Parser: Kommentare raus, dann je ``;``-Abschnitt genau eine
    Deklaration ``--name: wert``. Ein Abschnitt mit fremdem Text davor (etwa
    der Rest eines zu frueh beendeten Kommentars) ist ungueltig und wird --
    wie im Browser -- samt seiner Variable verworfen. Eine reine Textsuche
    nach ``--name:`` saehe die Variable dagegen weiter als gesetzt.

    :param selector: Selektor des Blocks, z. B. ``":root"``; der erste
        Treffer zaehlt (``:root[data-theme=...]`` trifft ``":root"`` nicht).
    :param css: Stylesheet-Text (Standard: ``web/styles.css``).
    :returns: Name ohne ``--`` -> Wert.
    """

    m = re.search(re.escape(selector) + r"\s*\{(.*?)\}", _strip_comments(css), re.S)
    assert m, selector
    out: dict[str, str] = {}
    for chunk in m.group(1).split(";"):
        decl = re.fullmatch(r"\s*--([\w-]+)\s*:\s*(.+?)\s*", chunk, re.S)
        if decl:
            out[decl.group(1)] = decl.group(2)
    return out


def _vars(selector: str) -> dict[str, str]:
    """Die Hex-Farbvariablen des Blocks ``selector`` (siehe ``_declared``)."""

    return {k: v for k, v in _declared(selector).items()
            if re.fullmatch(r"#[0-9a-fA-F]{6}", v)}


def _undefined_in_default_theme(css: str) -> list[str]:
    """Variablen, die das Stylesheet ohne Rueckfallwert nutzt, die das dunkle
    Standardschema (``:root``) aber nicht setzt.

    Eine solche Regel faellt im dunklen Schema still auf ``transparent`` bzw.
    den geerbten Wert -- ein Primaerknopf ohne Hintergrund. Nutzungen mit
    Rueckfallwert (``var(--x, 0px)``, von Skripten gesetzte Masse) zaehlen
    nicht.

    :param css: Stylesheet-Text.
    :returns: die fehlenden Namen (mit ``--``), sortiert.
    """

    body = _strip_comments(css)
    used = set(re.findall(r"var\(--([\w-]+)\s*\)", body))
    return sorted("--" + v for v in used - set(_declared(":root", css)))


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
    the green button kept dark text when the light green became darker. The
    same holds for the danger button on hover and for the skip link, which
    share the filled look."""

    for cls in (".btn.primary", ".btn.green", ".btn.danger:hover", ".skip-link"):
        m = re.search(re.escape(cls) + r" \{([^}]*)\}", STYLES)
        assert m and re.search(r"(?<!-)color: var\(--on-", m.group(1)), cls


#: Paare, die im dunklen Schema keine Textpaare sind: ``--gray`` faerbt dort
#: nur Kanten und uebersprungene Schritte im Graphen (Text auf grauen Flaechen
#: nimmt ``--gray-txt``, siehe ``test_gray_is_never_a_text_colour``). Fuer sie
#: gilt die Grenze fuer Bedienelemente und Grafik (3:1, WCAG 1.4.11).
DARK_NON_TEXT = {("gray", "bg"), ("gray", "bg-elev"), ("gray", "bg-elev2"), ("gray", "gray-soft")}


def test_dark_theme_text_pairs_meet_wcag_aa() -> None:
    """Das dunkle Standardschema erreicht fuer jedes Textpaar 4,5:1 -- vorher
    war nur das helle geprueft, und Weiss auf dem Primaerknopf („Anmelden“,
    Sprunglink) kam dunkel auf 3,39:1."""

    dark = _vars(":root")
    low = [f"--{f} auf --{b}: {contrast(dark[f], dark[b]):.2f}"
           for f, b in PAIRS
           if (f, b) not in DARK_NON_TEXT and contrast(dark[f], dark[b]) < 4.5]
    assert not low, low


def test_dark_theme_graphic_pairs_meet_the_non_text_minimum() -> None:
    """Grenzfall: Die vom Textpaar ausgenommenen Grautoene erreichen als Kante
    bzw. Symbol auf ihren Flaechen mindestens 3:1."""

    dark = _vars(":root")
    low = [f"--{f} auf --{b}: {contrast(dark[f], dark[b]):.2f}"
           for f, b in sorted(DARK_NON_TEXT) if contrast(dark[f], dark[b]) < 3.0]
    assert not low, low


def test_gray_is_never_a_text_colour() -> None:
    """Voraussetzung der Ausnahme oben: keine Regel setzt Text in ``--gray``."""

    assert not re.search(r"(?<![\w-])color: var\(--gray\)", _strip_comments(STYLES))


def test_dark_finder_names_the_former_white_on_primary() -> None:
    """Gegenprobe: Mit dem frueheren Weiss auf dem frueheren Blau meldet die
    Pruefung genau dieses Paar mit dem gemessenen Wert."""

    dark = {**_vars(":root"), "on-accent": "#ffffff", "accent": "#4f86ff"}
    low = [f"--{f} auf --{b}: {contrast(dark[f], dark[b]):.2f}"
           for f, b in PAIRS if (f, b) not in DARK_NON_TEXT and contrast(dark[f], dark[b]) < 4.5]
    assert "--on-accent auf --accent: 3.39" in low


def test_dark_theme_green_button_keeps_its_contrast() -> None:
    dark = _vars(":root")
    assert contrast(dark["on-green"], dark["green"]) >= 4.5


def test_contrast_formula() -> None:
    assert round(contrast("#000000", "#ffffff"), 1) == 21.0
    assert round(contrast("#777777", "#ffffff"), 2) == 4.48


def _pill_pairs() -> list[tuple[str, str, str]]:
    """``(Klasse, Textvariable, Flaechenvariable)`` jeder ``.pill-*``-Regel."""

    rules = re.findall(
        r"\.(pill-[\w-]+) \{ background: var\(--([\w-]+)\); color: var\(--([\w-]+)\); \}", STYLES)
    return [(cls, fg, bg) for cls, bg, fg in rules]


def test_every_pill_meets_wcag_aa_in_both_themes() -> None:
    """Jede Pille (11 px, fett) erreicht in beiden Schemata 4,5:1. Gemessen war
    „angeboten“ (``pill-gray``) im dunklen Schema mit 3,11:1."""

    dark = _vars(":root")
    light = {**dark, **_vars(':root[data-theme="light"]')}
    pills = _pill_pairs()
    assert {"pill-gray", "pill-green", "pill-red"} <= {p[0] for p in pills}
    low = [f"{name}: .{cls} --{fg} auf --{bg}: {contrast(theme[fg], theme[bg]):.2f}"
           for name, theme in (("dunkel", dark), ("hell", light))
           for cls, fg, bg in pills if contrast(theme[fg], theme[bg]) < 4.5]
    assert not low, low


def test_pill_finder_names_the_former_dark_gray() -> None:
    """Gegenprobe: mit dem frueheren Grau als Pillentext meldet die Pruefung
    genau dieses Paar."""

    dark = _vars(":root")
    assert round(contrast("#6b7794", dark["gray-soft"]), 2) == 3.11


def test_dark_default_theme_defines_every_variable_it_uses() -> None:
    """Jede ohne Rueckfallwert benutzte Variable ist im dunklen Standardschema
    gesetzt. ``--accent`` fehlte dort, weil ein Kommentar mit ``*/`` im Text
    zu frueh endete und die folgende Deklaration verschluckte: Primaerknoepfe
    und der Sprunglink hatten im dunklen Schema keinen Hintergrund."""

    assert not _undefined_in_default_theme(STYLES)


def test_swallowed_declaration_is_reported_as_missing() -> None:
    """Gegenprobe: Der frueher fehlerhafte Kommentar verschluckt genau die
    folgende Variable, und die Pruefung nennt sie."""

    broken = (":root {\n  --muted: #9aa6bf;  /* Alias (tl-*/tree-Badges) */\n"
              "  --accent: #4f86ff;\n  --line: #2a3550;\n}\n"
              ".btn.primary { background: var(--accent); border-color: var(--line); }\n")
    assert "accent" not in _declared(":root", broken)
    assert _declared(":root", broken)["line"] == "#2a3550"
    assert _undefined_in_default_theme(broken) == ["--accent"]


def test_the_skip_link_keeps_its_colours_in_every_link_state() -> None:
    """A visited ``#content`` made the skip link invisible: ``a:visited``
    (specificity 0,1,1) beat ``.skip-link`` (0,1,0), and text and background
    were both ``--accent``. Every link state the stylesheet colours for ``a``
    needs a skip-link rule of its own with the text colour on the accent."""

    states = set(re.findall(r"(?m)^a:(\w+)[^{]*\{[^}]*\bcolor:", STYLES))
    assert "visited" in states  # the rule that hid the link is still there
    for state in sorted(states):
        rules = [body for selectors, body in re.findall(r"([^{}]+)\{([^}]*)\}", STYLES)
                 if re.search(rf"a\.skip-link:{state}\b", selectors)]
        assert rules, f"no a.skip-link:{state} rule"
        assert any(re.search(r"(?<!-)color: var\(--on-accent\)", body) for body in rules), state
