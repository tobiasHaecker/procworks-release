# SPDX-License-Identifier: BUSL-1.1
"""Navigation fuer Screenreader und Tastatur: Name, aktueller Eintrag, Sprung.

Die Seitenleiste war ein unbenanntes ``<nav>``; der aktive Eintrag war nur per
Klasse hervorgehoben, ein Screenreader erfuhr nicht, wo man steht. Wer per
Tastatur arbeitet, musste bei jeder Sicht erst durch die ganze Navigation.
Jetzt: ``aria-label`` am ``<nav>``, ``aria-current="page"`` am aktiven Eintrag
(``setActiveNav``) und als erster Tab-Stopp „Zum Inhalt“, der den Fokus in den
Inhaltsbereich setzt.
"""

from __future__ import annotations

import re
from pathlib import Path

from web_source import INDEX_HTML
from web_vm import needs_node, run_app_js

HTML = INDEX_HTML.read_text(encoding="utf-8")


def test_nav_is_named_and_the_skip_link_is_the_first_tab_stop() -> None:
    nav = re.search(r"<nav\b[^>]*>", HTML)
    assert nav and re.search(r'aria-label="[^"]+"', nav.group(0)), nav
    body = HTML[HTML.index("<body>"):]
    first = re.search(r"<(a|button|input|select|textarea)\b[^>]*>", body)
    assert first and 'id="skip-link"' in first.group(0), first
    assert 'href="#content"' in first.group(0)
    assert re.search(r'id="content"[^>]*tabindex="-1"', HTML), "Sprungziel muss fokussierbar sein"


@needs_node
def test_active_entry_carries_aria_current_and_only_it(tmp_path: Path) -> None:
    res = run_app_js(
        r"""
const nav = byId("nav");
["model", "tasks", "monitor"].forEach((v) => nav.appendChild(el("button", { "data-view": v }, v)));
// dataset wie im Browser aus data-*-Attributen
nav.children.forEach((b) => { b.dataset.view = b.getAttribute("data-view"); });
state.view = "tasks";
setActiveNav();
const first = nav.children.map((b) => b.getAttribute("aria-current"));
state.view = "monitor";
setActiveNav();
return { first, second: nav.children.map((b) => b.getAttribute("aria-current")) };
""",
        tmp_path,
    )
    assert res["first"] == [None, "page", None]
    assert res["second"] == [None, None, "page"], "der vorige Eintrag verliert die Markierung"


@needs_node
def test_skip_link_moves_focus_into_the_content(tmp_path: Path) -> None:
    res = run_app_js(
        r"""
const content = byId("content");
document.body.appendChild(content);
wireNav();
let prevented = false;
await byId("skip-link").dispatch("click", { preventDefault() { prevented = true; } });
return { focused: document.activeElement === content, prevented };
""",
        tmp_path,
    )
    # Der Sprung aendert die Adresse nicht (kein „#content“ in der URL).
    assert res == {"focused": True, "prevented": True}
