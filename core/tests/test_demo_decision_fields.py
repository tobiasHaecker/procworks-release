# SPDX-License-Identifier: BUSL-1.1
"""Felder, die eine Verzweigung entscheiden, sagen das in der Maske.

Im Forderungsmanagement hieß der Haken, der zwischen „Zahlung verbuchen“ und
der nächsten Mahnstufe entschied, neutral „Systemlauf bestätigt“: Wer nur
quittieren wollte, verbuchte eine Zahlung oder löste eine Mahnung aus.
"""

from __future__ import annotations

from test_demo_display_fields import _all_demo_schemas

from procworks.model import AccessMode, ProcessSchema, WidgetKind

_NEUTRAL = "Systemlauf bestätigt"


def decision_field_problems(schema: ProcessSchema) -> list[str]:
    """Maskenfelder eines Entscheidungsmerkmals mit neutraler Beschriftung,
    schreibende Haken davon zudem ohne Hilfetext (je mit Schritt und Feld)."""
    discriminators = {d.discriminator for d in schema.xor_decisions.values()}
    problems = []
    for node_id, form in (schema.forms or {}).items():
        for field in form.fields:
            if field.element_id not in discriminators:
                continue
            step = schema.nodes[node_id].label
            if field.label == _NEUTRAL:
                problems.append(f"{schema.name}/{step}: „{_NEUTRAL}“ entscheidet eine Verzweigung")
            # Ein Haken entscheidet still: wer ihn setzt, muss die Folge lesen
            # können. Beträge und nur gelesene Felder erklären sich selbst.
            ticks = field.widget is WidgetKind.CHECKBOX and field.mode is not AccessMode.READ
            if ticks and not field.help_text:
                problems.append(f"{schema.name}/{step}/{field.label}: kein Hilfetext zur Folge")
    return problems


def test_decision_fields_of_the_demo_are_worded_by_their_meaning() -> None:
    problems = [p for s in _all_demo_schemas() for p in decision_field_problems(s)]
    assert not problems, "; ".join(problems)


def test_payment_check_names_both_consequences() -> None:
    [forderung] = [s for s in _all_demo_schemas() if s.name == "Forderungsmanagement"]
    fields = [f for form in forderung.forms.values() for f in form.fields
              if f.element_id in ("zahlung_1", "zahlung_2", "zahlung_3")]
    assert len(fields) == 3
    assert all(f.label == "Zahlung eingegangen?" for f in fields)
    assert all("verbucht" in f.help_text and "Nicht angehakt" in f.help_text for f in fields)


def test_main_process_payment_check_is_worded_the_same_way() -> None:
    [main] = [s for s in _all_demo_schemas() if s.name == "Auftragsabwicklung (Order-to-Cash)"]
    [field] = [f for form in main.forms.values() for f in form.fields
               if f.element_id == "zahlungseingang" and f.mode is not AccessMode.READ]
    assert field.label == "Zahlung eingegangen?"
    assert "offenen Forderung" in field.help_text
