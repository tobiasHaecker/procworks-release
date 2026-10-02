# SPDX-License-Identifier: BUSL-1.1
"""Unumkehrbare Bestätigungen lassen sich nicht versehentlich per Enter auslösen.

„Auf Null zurücksetzen“ öffnete mit dem Fokus auf dem blauen Knopf
„Endgültig löschen“ -- ein Enter löschte alles. Zerstörende Dialoge nutzen
``openModal(…, { danger: true })``: Gefahr-Knopf, Fokus auf „Abbrechen“.
"""

from __future__ import annotations

import re
from pathlib import Path

from web_vm import APP_JS, needs_node, run_app_js

#: Bestätigungsbeschriftungen, die etwas unwiderruflich entfernen.
_DESTRUCTIVE = re.compile(r'"(Entfernen|L\\u00F6schen|Löschen|Zur\\u00FCcksetzen|Endg[^"]*)"')


def _modal_calls(src: str) -> list[tuple[int, str]]:
    calls = []
    for m in re.finditer(r"\bopenModal\(", src):
        if src[m.start() - 9:m.start()] == "function ":
            continue
        depth = 0
        for i in range(m.end() - 1, len(src)):
            if src[i] == "(":
                depth += 1
            elif src[i] == ")":
                depth -= 1
                if depth == 0:
                    calls.append((src.count("\n", 0, m.start()) + 1, src[m.start():i + 1]))
                    break
    return calls


def test_every_destructive_confirmation_is_marked_dangerous() -> None:
    """Wächter über die Klasse: endet ein Dialog auf „Entfernen“, „Löschen“,
    „Zurücksetzen“ o. ä., trägt er ``{ danger: true }``."""
    missing = [
        f"Zeile {line}"
        for line, call in _modal_calls(APP_JS.read_text(encoding="utf-8"))
        if _DESTRUCTIVE.search(call[-80:]) and "danger: true" not in call[-80:]
    ]
    assert not missing, "zerstörender Dialog ohne danger-Option: " + ", ".join(missing)


def test_maintenance_dialogs_are_marked_dangerous() -> None:
    src = APP_JS.read_text(encoding="utf-8")
    reset = src[src.index("function confirmReset("):]
    reset = reset[:reset.index("\n}\n")]
    assert "spec.confirm, { danger: true })" in reset


@needs_node
def test_reset_dialog_focuses_cancel_and_shows_a_danger_button(tmp_path: Path) -> None:
    res = run_app_js(
        r"""
confirmReset("wipe");
const root = byId("modal-root");
const confirm = root.querySelectorAll("button").find((b) => textOf(b) === "Endgültig löschen");
return { focused: textOf(document.activeElement), danger: confirm.classList.contains("danger"),
  primary: confirm.classList.contains("primary") };
""",
        tmp_path,
    )
    assert res == {"focused": "Abbrechen", "danger": True, "primary": False}


@needs_node
def test_dangerous_dialog_with_fields_starts_in_the_first_field(tmp_path: Path) -> None:
    """Grenzfall: Ein Gefahr-Dialog mit Pflicht-Anlass beginnt im Feld, nicht auf
    „Abbrechen“ -- sonst müsste man erst zurück zum Feld."""
    res = run_app_js(
        r"""
const reason = el("input", { type: "text", id: "why" });
openModal("Schritt entfernen", el("div", null, reason), async () => true, "Entfernen",
  { danger: true });
return document.activeElement === reason;
""",
        tmp_path,
    )
    assert res is True
