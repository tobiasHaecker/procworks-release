# SPDX-License-Identifier: BUSL-1.1
"""Help, tour and admin texts match what the interface really offers.

The help overview missed views (the test-run cockpit) and still promised that
"Ausführung" lets you pick decision branches by hand; the hint about the
modeller tour appeared for roles without that tour; the admin tour spoke of
two buttons where there are three, of a "view only" backup panel that has a
"back up now" button, and the maintenance text gave instance counts the
example data no longer has. These guards tie the texts to their source.
"""

from __future__ import annotations

import re
from pathlib import Path

from web_source import app_js_source
from web_vm import APP_JS, needs_node, run_app_js

from procworks import demo, demo_o2c
from procworks.audit import InMemoryAuditLog
from procworks.store import InMemoryInstanceStore, InMemoryOrgStore, InMemorySchemaStore

SRC = app_js_source()
TOURS = (APP_JS.parent / "tour" / "tours.js").read_text(encoding="utf-8")
GUIDE = (APP_JS.parents[1] / "docs" / "Mitarbeiter-Anleitung.md")


@needs_node
def test_help_overview_names_every_view_and_no_manual_branching(tmp_path: Path) -> None:
    res = run_app_js(
        r"""
return { titles: Object.entries(VIEW_META).map(([k, m]) => [k, m.title]),
  rows: HELP_VIEWS.map(([n, d]) => [n, d]) };
""",
        tmp_path,
    )
    names = " ".join(n for n, _ in res["rows"])
    missing = [t for k, t in res["titles"] if t not in names]
    assert not missing, f"Hilfe-Übersicht ohne: {missing}"
    run = next(d for n, d in res["rows"] if "Ausführung" in n)
    assert "XOR" not in run and "wählen" not in run
    assert "entscheidet das System" in run


@needs_node
def test_modeller_tour_hint_only_where_that_tour_is_offered(tmp_path: Path) -> None:
    res = run_app_js(
        r"""
globalThis.Tour = { availableTours: () => tours, savedProgress: () => 0, isDone: () => false };
let tours = [{ id: "operator", title: "A", subtitle: "s", steps: [1], sandbox: false }];
const op = textOf(tourPanel());
tours = [{ id: "modeler", title: "M", subtitle: "s", steps: [1], sandbox: true }];
const mod = textOf(tourPanel());
return { op, mod };
""",
        tmp_path,
    )
    assert "Modellierer-Tour" not in res["op"] and "Esc" in res["op"]
    assert "Modellierer-Tour" in res["mod"]


def _seed_counts() -> tuple[dict[str, tuple[int, int, int, int]], int]:
    """Per data set (schemas, instances, running, completed), plus O2C main orders."""

    out: dict[str, tuple[int, int, int, int]] = {}
    main = 0
    for name, fn in (("demo", demo.load_demo), ("o2c", demo_o2c.load_o2c)):
        ss, ins = InMemorySchemaStore(), InMemoryInstanceStore()
        fn(schema_store=ss, instance_store=ins, org_store=InMemoryOrgStore(),
           audit_log=InMemoryAuditLog())
        insts = [ins.get(i) for i in ins.list_ids()]
        states = [i.state.value for i in insts]
        out[name] = (len(ss.list_ids()), len(states), states.count("RUNNING"),
                     states.count("COMPLETED"))
        if name == "o2c":
            main = sum(1 for i in insts if i.schema_id == "o2c-auftragsabwicklung")
    return out, main


def test_maintenance_and_tour_counts_match_the_example_data() -> None:
    counts, o2c_main = _seed_counts()
    assert counts["demo"] == (2, 3, 2, 1)
    total = counts["o2c"][1]
    maint = re.search(r'"Setzt das gesamte System zur[^"]*"', SRC)
    assert maint
    text = maint.group(0)
    assert "drei Vorg\\u00E4ngen (zwei laufend, einer abgeschlossen)" in text
    assert o2c_main == 9
    assert "neun Auftragsvorg\\u00E4ngen" in text and f"{total} Vorg\\u00E4nge)" in text
    assert "drei Vorgänge – zwei laufend, einer abgeschlossen" in TOURS


def test_admin_texts_name_the_buttons_that_exist() -> None:
    body = SRC[SRC.index("async function viewAdmin(") if "async function viewAdmin(" in SRC
               else SRC.index("function viewAdmin("):]
    buttons = re.findall(r'onClick: \(\) => confirmReset\("(\w+)"\)', body)
    assert buttons == ["demo", "o2c", "wipe"]
    assert "Drei Knöpfe" in TOURS and "Zwei Knöpfe" not in TOURS
    # Panels mit Bedienknopf heissen nicht „nur Ansicht“.
    assert "Datensicherung \\u00B7 nur Ansicht" not in SRC
    assert "Benachrichtigungen · nur Ansicht" not in SRC
    assert "Jetzt sichern" in TOURS


def test_staff_guide_lists_the_views_an_operator_sees() -> None:
    text = GUIDE.read_text(encoding="utf-8")
    roles = re.search(r"const VIEW_ROLES = \{(.*?)\};", SRC, re.S)
    assert roles
    seen = [k for k, v in re.findall(r"(\w+): \[([^\]]*)\]", roles.group(1)) if '"operator"' in v]
    names = {"tasks": "Meine Aufgaben", "run": "Ausführung", "monitor": "Monitoring",
             "help": "Hilfe"}
    assert set(seen) == set(names), seen
    para = text[text.index("Mit der Rolle **Bearbeiter**"):][:400]
    for name in names.values():
        assert name in para, name
