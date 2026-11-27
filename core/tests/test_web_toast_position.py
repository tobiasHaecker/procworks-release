# SPDX-License-Identifier: BUSL-1.1
"""Meldungen verdecken die Knöpfe eines offenen Dialogs nicht.

Eine Ablehnung erschien unten rechts -- genau über „Abbrechen“/„Speichern“.
Bei offenem Dialog stehen Meldungen jetzt oben (im Browser nachgemessen:
beide Dialogknöpfe frei).
"""

from __future__ import annotations

import re

from web_vm import APP_JS

STYLES = (APP_JS.parent / "styles.css").read_text(encoding="utf-8")


def test_toasts_move_to_the_top_while_a_dialog_is_open() -> None:
    rule = re.search(r"body:has\(#modal-root \.modal-backdrop\) \.toast-root \{([^}]*)\}", STYLES)
    assert rule, "Regel für Meldungen bei offenem Dialog fehlt"
    assert "top: 18px" in rule.group(1) and "bottom: auto" in rule.group(1)
