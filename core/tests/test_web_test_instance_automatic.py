# SPDX-License-Identifier: BUSL-1.1
"""Die Pruefinstanz bietet ihre automatischen Schritte zum simulierten Abschluss an.

Der Kern legt fuer eine Pruefinstanz keine externe Aufgabe mehr an und pusht
nichts -- kein echtes System soll fuer einen Probelauf taetig werden. Ein
automatischer Schritt steht damit in keiner Arbeitsliste des Cockpits; ohne
eigenes Angebot liesse sich die Pruefinstanz nicht zu Ende spielen. Das Panel
„Automatische Schritte“ listet die bereiten automatischen Schritte und schliesst
sie ueber die Aufgabenmaske ab (ohne Bearbeiter). Geprueft in Node
(``web_vm.py``).
"""

from __future__ import annotations

from pathlib import Path

from web_vm import needs_node, run_app_js

_SETUP = r"""
state.principal = { subject: "mara", roles: ["modeler"] };
state.view = "testrun";
state.schemaId = "s";
state.schema = { id: "s", name: "Pruef", version: 1, lifecycle_state: "ENTWURF",
  nodes: {
    n_auto: { id: "n_auto", type: "ACTIVITY", label: "Buchen" },
    n_hand: { id: "n_hand", type: "ACTIVITY", label: "Pruefen" },
  },
  edges: [], data_elements: {}, data_accesses: [], forms: {}, staff_rules: {},
  service_bindings: { n_auto: { node_id: "n_auto", name: "Buchen", automatic: true,
                                automation: "EXTERNAL_TASK", topic: "buchung" } },
  org_model: { agents: {} } };
state.testInstanceId = "instance_7"; persistTestState();
let instState = "RUNNING";
let posted = null;
document.body.appendChild(byId("content"));
document.body.appendChild(byId("modal-root"));
respond((p, m, body) => {
  if (p === "/instances/instance_7") {
    return { body: { id: "instance_7", schema_id: "s", state: instState, is_test: true,
                     node_states: { n_auto: "ACTIVATED", n_hand: "ACTIVATED" },
                     data_values: {} } };
  }
  if (m === "POST" && p === "/instances/instance_7/complete") {
    posted = body; instState = "COMPLETED";
    return { body: { id: "instance_7", state: "COMPLETED" } };
  }
  return { body: [] };
});
const settle = async () => {
  do { await new Promise((r) => setTimeout(r, 5)); } while (renderBusy);
};
const panelText = () => {
  const panel = byId("content").querySelectorAll(".panel")
    .find((p) => textOf(p).startsWith("Automatische Schritte"));
  return panel ? textOf(panel) : null;
};
"""


@needs_node
def test_a_ready_automatic_step_is_offered_and_completed_without_an_agent(
    tmp_path: Path,
) -> None:
    res = run_app_js(
        _SETUP
        + r"""
await viewTestRun();
const offered = panelText();
const button = byId("content").querySelectorAll("button")
  .find((b) => textOf(b) === "Ergebnis simulieren");
await button.click(); await settle();
const confirm = byId("modal-root").querySelectorAll("button")
  .find((b) => textOf(b) === "Abschließen");
await confirm.click(); await settle();
return { offered, posted };
""",
        tmp_path,
    )
    assert res["offered"] is not None and "Buchen" in res["offered"]
    assert "Pruefen" not in res["offered"], "ein Schritt ohne Automatik gehoert nicht hierher"
    assert res["posted"]["node_id"] == "n_auto"
    assert "agent_id" not in res["posted"] or res["posted"]["agent_id"] is None


@needs_node
def test_no_panel_once_the_test_instance_is_completed(tmp_path: Path) -> None:
    res = run_app_js(
        _SETUP
        + r"""
instState = "COMPLETED";
await viewTestRun();
return { offered: panelText() };
""",
        tmp_path,
    )
    assert res["offered"] is None
