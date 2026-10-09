# SPDX-License-Identifier: BUSL-1.1
"""Die klassische Modellier-Ansicht ist der Karten-Ansicht gleichwertig.

Beide Oberflächen bleiben dauerhaft bestehen und müssen dieselben Wege bieten.
Die klassische zeigte keine Freigabe-Hinweise (Schritte ohne Bearbeiter), ihr
Bearbeiter-Dialog bot weder „Erweiterte Regel“ noch „Auch weiteren Schritten
zuordnen“, Esc hob die Auswahl nicht auf, und Enter führte nicht ins Feld.
"""

from __future__ import annotations

from pathlib import Path

from web_source import app_js_source
from web_vm import needs_node, run_app_js


def _body(src: str, head: str) -> str:
    start = src.index(head)
    return src[start:src.index("\n}\n", start)]


def test_classic_surface_uses_the_shared_operations() -> None:
    src = app_js_source()
    drop = _body(src, "function dropResourceOnNode(")
    assert "otherStepsBox(" in drop and "addStaffRule(nodeId)" in drop
    assert "applyStaffRuleTo(" in drop
    assert "releaseFindings()" in _body(src, "function findingsPanel(")
    assert "bindStaffDialog(node.id)" in _body(src, "function nodeInspectorPanel(")


def test_escape_handler_is_not_limited_to_one_surface() -> None:
    wire = _body(app_js_source(), "function wireNav(")
    assert 'modelUx() === "card" && state.selectedNode' not in wire


def test_revision_button_is_named_the_same_everywhere() -> None:
    src = app_js_source()
    assert '"Neue Revision erstellen"' not in src
    assert "Ausführungs-/Monitoring-Sicht" not in src


_SCHEMA = r"""
state.schema = { id: "s1", name: "P", version: 1, lifecycle_state: "ENTWURF",
  nodes: { start: { id: "start", type: "START" }, a: { id: "a", type: "ACTIVITY", label: "A" },
           end: { id: "end", type: "END" } },
  edges: [], data_elements: {}, data_accesses: [], staff_rules: {}, service_bindings: {},
  org_model: { roles: {}, org_units: {}, agents: {} } };
state.schemaId = "s1"; state.view = "model"; state.modelUx = "classic";
state.validation = { correct: true, findings: [] };
"""


@needs_node
def test_escape_clears_the_selection_in_the_classic_surface(tmp_path: Path) -> None:
    res = run_app_js(
        _SCHEMA
        + r"""
let renders = 0; globalThis.render = () => { renders += 1; };
wireNav();
state.selectedNode = "a";
document.activeElement = null;
await document.dispatch("keydown", { key: "Escape" });
return { selected: state.selectedNode, renders };
""",
        tmp_path,
    )
    assert res == {"selected": None, "renders": 1}


@needs_node
def test_enter_focuses_the_name_field_of_the_classic_inspector(tmp_path: Path) -> None:
    res = run_app_js(
        _SCHEMA
        + r"""
state.selectedNode = "a";
const panel = nodeInspectorPanel();
document.body.appendChild(panel);
document.activeElement = null;
const ok = focusStepCard();
return { ok, focused: document.activeElement && document.activeElement.getAttribute("id") };
""",
        tmp_path,
    )
    assert res == {"ok": True, "focused": "insp-name-input"}


@needs_node
def test_classic_findings_panel_shows_steps_without_staff(tmp_path: Path) -> None:
    """Grund wie in der Statusleiste: Schritte ohne Bearbeiter verhindern die Freigabe."""
    res = run_app_js(
        _SCHEMA
        + r"""
globalThis.releaseFindings = () => [{ rule: "B2", code: "B2.no-staff", params: { step: "A" },
  message: "x", node_id: "a" }];
return textOf(findingsPanel());
""",
        tmp_path,
    )
    assert "1 Schritt ohne Bearbeiter" in res  # Einzahl (countLabel)
