# SPDX-License-Identifier: BUSL-1.1
"""Die Meldungen zur Vier-Augen-Regel erklären den Grund und führen nicht in die Irre.

* ``SUP.four-eyes`` (Aufsichtsabschluss ohne Bearbeiter vor einem
  Vier-Augen-Schritt) riet früher, den Schritt „etwa nach einer
  Vertretungsregel“ erledigen zu lassen. Eine Vertretung ist kein Weg um die
  Regel herum – der Hinweis nennt sie deshalb nicht mehr.
* ``EX.not-eligible`` (Übernehmen/Abschließen abgelehnt) sagte einer Person,
  die die Rolle hat, nur „nicht zuständig“. Mit ``params.reason``
  ``four-eyes`` und ``other_step`` erklärt der Text jetzt die Regel.

Geprüft im Node-``vm`` (``web_vm.py``) über ``findingText`` aus dem einen
Meldungskatalog.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from web_vm import needs_node, run_app_js


def _text(code: str, params: dict[str, str], tmp_path: Path) -> str:
    """Katalogtext samt Hinweis für ``code``/``params``."""

    import json

    return str(run_app_js(
        f"return findingText({{ code: {json.dumps(code)}, params: {json.dumps(params)} }},"
        " { withHint: true });",
        tmp_path,
    ))


@needs_node
def test_four_eyes_supervision_hint_does_not_suggest_a_deputy(tmp_path: Path) -> None:
    text = _text(
        "SUP.four-eyes",
        {"step": "Erfassen", "dependants": "Prüfen (nicht wer erfasst hat)"},
        tmp_path,
    )
    assert "Vier-Augen-Prinzip" in text and "„Erfassen“" in text
    assert "Vertretung" not in text
    # Was stattdessen zu tun ist: eine zuständige Person, nicht die Aufsicht.
    assert "zuständigen Person" in text
    assert "nicht per Aufsicht" in text


@needs_node
def test_performer_needed_hint_keeps_its_advice(tmp_path: Path) -> None:
    """Gegenstück: Ohne Vier-Augen-Bezug bleibt der Hinweis unverändert."""

    text = _text(
        "SUP.performer-needed",
        {"step": "Erfassen", "dependants": "Genehmigen"},
        tmp_path,
    )
    assert "Den Schritt von einer zuständigen Person erledigen lassen" in text


@needs_node
def test_not_eligible_explains_the_four_eyes_rule(tmp_path: Path) -> None:
    """``EX.not-eligible`` mit ``reason`` four-eyes nennt die Regel und den früheren Schritt."""

    text = _text(
        "EX.not-eligible",
        {"step": "Prüfen", "reason": "four-eyes", "other_step": "Erfassen"},
        tmp_path,
    )
    assert "„Prüfen“" in text and "„Erfassen“" in text
    assert "Vier-Augen-Prinzip" in text
    assert "nicht zuständig" not in text


@needs_node
def test_not_eligible_without_reason_stays_plain(tmp_path: Path) -> None:
    """Negativ: ohne Grund bleibt der knappe Text, keine erfundene Vier-Augen-Erklärung."""

    text = _text("EX.not-eligible", {"step": "Prüfen"}, tmp_path)
    assert text.startswith("Für „Prüfen“ bist du nicht zuständig.")
    assert "Vier-Augen" not in text


@needs_node
def test_not_eligible_four_eyes_without_other_step_still_reads(tmp_path: Path) -> None:
    """Grenzfall: fehlt ``other_step`` (älterer Server), bleibt der Satz vollständig."""

    text = _text("EX.not-eligible", {"step": "Prüfen", "reason": "four-eyes"}, tmp_path)
    assert "einen früheren Schritt" in text
    assert "undefined" not in text


@needs_node
@pytest.mark.parametrize(
    ("action", "verb"),
    [("claim", "übernehmen"), ("complete", "abschließen"), (None, "bearbeiten")],
)
def test_not_eligible_names_the_refused_act(tmp_path: Path, action: str | None, verb: str) -> None:
    """Ein abgelehnter Abschluss heißt nicht „übernehmen“; ohne ``action``
    (älterer Server) bleibt ein neutrales „bearbeiten“."""

    params = {"step": "Prüfen", "reason": "four-eyes", "other_step": "Erfassen"}
    if action:
        params["action"] = action
    text = _text("EX.not-eligible", params, tmp_path)
    assert text.startswith(f"„Prüfen“ darfst du nicht {verb}:"), text
