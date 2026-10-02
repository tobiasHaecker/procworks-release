# SPDX-License-Identifier: BUSL-1.1
"""Jeder Aufzählungswert des Kerns hat im Web-Client eine deutsche Bezeichnung.

Die Oberfläche zeigte Knotentypen, Zustände, Zugriffsarten, Herkunft,
Lebenszyklus und Audit-Ereignisse teils roh (``COMPLETED``, ``READ_WRITE``,
``RELEASED``, ``ACTIVITY_CLAIMED``). Nach dem Vorbild des Meldungskatalog-
Wächters vergleicht dieser Test die Aufzählungen des Kerns mit den
Bezeichnungstabellen in ``web/app.js``: eine neue Ausprägung ohne Text fällt
hier auf -- mit Namen --, nicht beim Kunden.
"""

from __future__ import annotations

import re
from enum import Enum

import pytest
from web_vm import APP_JS

from procworks.audit import EventType
from procworks.model import (
    AccessMode,
    DataSourceKind,
    LifecycleState,
    MailOutboxState,
    NodeState,
    NodeType,
)
from procworks.outbox import WEBHOOK_EVENTS


def table_keys(src: str, name: str) -> set[str]:
    """Schlüssel der obersten Ebene des Objektliterals ``const name = {...}``."""
    m = re.search(rf"\bconst {name} = \{{", src)
    assert m, f"Tabelle {name} fehlt in app.js"
    depth, i, keys, token_start = 1, m.end(), set(), m.end()
    while depth:
        ch = src[i]
        if ch in "{[(":
            depth += 1
        elif ch in "}])":
            depth -= 1
        elif ch in ",\n" and depth == 1:
            token_start = i + 1
        elif ch == ":" and depth == 1:
            key = src[token_start:i].strip().strip("\"'")
            if key:
                keys.add(key)
        i += 1
    return keys


def missing_labels(src: str, table: str, values: set[str]) -> list[str]:
    """Werte ohne Eintrag in ``table`` (sortiert)."""
    return sorted(values - table_keys(src, table))


_CASES: list[tuple[str, set[str]]] = [
    ("NODE_TYPE_LABELS", {m.value for m in NodeType}),
    ("NODE_STATE_META", {m.value for m in NodeState}),
    ("ACCESS_MODE_LABELS", {m.value for m in AccessMode}),
    ("DATA_SOURCE_LABELS", {m.value for m in DataSourceKind}),
    ("LIFECYCLE_LABELS", {m.value for m in LifecycleState}),
    ("EVENT_LABELS", {m.value for m in EventType}),
    ("WEBHOOK_EVENT_LABELS", set(WEBHOOK_EVENTS)),
    ("MAIL_STATE_LABELS", {m.value for m in MailOutboxState}),
]


@pytest.mark.parametrize(("table", "values"), _CASES, ids=[c[0] for c in _CASES])
def test_every_core_enum_value_has_a_german_label(table: str, values: set[str]) -> None:
    missing = missing_labels(APP_JS.read_text(encoding="utf-8"), table, values)
    assert not missing, f"{table}: ohne deutsche Bezeichnung: {', '.join(missing)}"


def test_guard_names_the_missing_value() -> None:
    """Gegenprobe: Ein fehlender Eintrag wird mit seinem Namen gemeldet."""

    class Fake(Enum):
        A = "ALPHA"
        B = "BETA"

    src = 'const FAKE_LABELS = { ALPHA: "Alpha", nested: { BETA: "x" } };'
    assert missing_labels(src, "FAKE_LABELS", {m.value for m in Fake}) == ["BETA"]


def test_raw_enum_values_are_not_rendered_at_the_known_sites() -> None:
    """Die frueher rohen Stellen gehen ueber die Bezeichnungsfunktionen."""
    src = APP_JS.read_text(encoding="utf-8")
    assert '"pill pill-gray" }, node.type)' not in src
    assert "}, a.mode)" not in src
    assert "COMPLETED oder SKIPPED" not in src
    assert "wahr (true)" not in src and "falsch (false)" not in src
    assert "typeName(d.data_type), d.source]" not in src
    assert '" " + ev)' not in src


STYLES_CSS = APP_JS.parent / "styles.css"


def state_rule_problems(css: str, states: set[str]) -> list[str]:
    """Zustaende ohne passende ``.s-<STATE> rect``-Regel (Fuellung und Rand,
    beide nur ueber ``var(--…)``) -- je Fund mit Grund."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    rules = re.findall(r"([^{}]+)\{([^{}]*)\}", css)
    problems = []
    for state in sorted(states):
        decls = [d for sel, d in rules
                 if any(s.strip() == f".s-{state} rect" for s in sel.split(","))]
        if not decls:
            problems.append(f"{state}: keine Regel .s-{state} rect")
            continue
        body = " ".join(decls)
        for prop in ("fill", "stroke"):
            m = re.search(rf"(?<![-\w]){prop}\s*:\s*([^;]+)", body)
            if not m:
                problems.append(f"{state}: kein {prop}")
            elif not m.group(1).strip().startswith("var(--"):
                problems.append(f"{state}: {prop} nicht ueber CSS-Variable")
    return problems


def test_every_node_state_has_a_themed_map_rule() -> None:
    """Jede Knotenmarkierung des Kerns ist in der Landkarte in beiden
    Farbschemata gestaltet -- sonst faellt das SVG auf Schwarz zurueck."""
    problems = state_rule_problems(
        STYLES_CSS.read_text(encoding="utf-8"), {m.value for m in NodeState}
    )
    assert not problems, "; ".join(problems)


def test_state_rule_guard_names_the_reason() -> None:
    """Gegenprobe: fehlende Regel und hartkodierte Farbe werden benannt."""
    css = (".s-A rect { fill: var(--x); stroke: var(--y); }\n"
           ".s-B rect { fill: #000; stroke: var(--y); }")
    assert state_rule_problems(css, {"A", "B", "C"}) == [
        "B: fill nicht ueber CSS-Variable",
        "C: keine Regel .s-C rect",
    ]
