# SPDX-License-Identifier: BUSL-1.1
"""Ein Serverausfall verwirft keinen Bezug -- nur „weg“ (403/404) tut das.

Beim Ausfall meldete die Pruefinstanz „ist nicht mehr verfuegbar … Bitte neu
starten“ und loeschte den Bezug dauerhaft, obwohl die Instanz auf dem Server
weiterlief. Dasselbe Muster (jeder Fehler -> Zustand verwerfen) steckte im
Monitoring (gewaehlter Vorgang) und beim Wiederherstellen des Vorgangs nach
dem Neuladen. Ebenso bot der Dialog „Geteilte Organisation“ nach einem
Fehler eine leere Auswahl an. Jetzt gilt ueberall:
403/404 (``isGoneError``) verwirft, alles andere zeigt einen Fehlerzustand und
behaelt den Bezug. Geprueft in Node (``web_vm.py``).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from web_vm import needs_node, run_app_js

_TESTRUN = r"""
state.principal = { subject: "mara", roles: ["modeler"] };
state.view = "testrun";
state.schemaId = "s";
state.schema = { id: "s", name: "Pruef", version: 1, lifecycle_state: "ENTWURF",
  nodes: {}, edges: [], data_elements: {}, data_accesses: [], forms: {},
  staff_rules: {}, service_bindings: {}, org_model: { agents: {} } };
state.testInstanceId = "instance_7"; persistTestState();
let down = __STATUS__;
respond((p) => {
  if (down) return down === 502 ? { status: 502 } : { status: down, body: { detail: "x" } };
  if (p === "/instances/instance_7") {
    return { body: { id: "instance_7", schema_id: "s", state: "RUNNING", is_test: true,
                     node_states: {}, data_values: {} } };
  }
  return { body: [] };
});
"""


@needs_node
def test_test_instance_survives_an_outage_and_comes_back(tmp_path: Path) -> None:
    """Positiv: Ausfall -> Fehlerzustand, Bezug bleibt (auch gespeichert);
    nach der Wiederkehr steht dieselbe Pruefinstanz wieder da."""
    res = run_app_js(
        _TESTRUN.replace("__STATUS__", "502")
        + r"""
await viewTestRun();
const during = textOf(byId("content"));
const kept = [state.testInstanceId, localStorage.getItem("testInstanceId")];
down = 0;
await viewTestRun();
return { during, kept, after: textOf(byId("content")) };
""",
        tmp_path,
    )
    assert "nicht mehr verfügbar" not in res["during"]
    assert "Server nicht erreichbar" in res["during"]
    assert res["kept"] == ["instance_7", "instance_7"]
    assert "TEST" in res["after"] and "Prüfinstanz starten" not in res["after"]


@needs_node
@pytest.mark.parametrize("status", [404, 403])
def test_gone_test_instance_is_still_dropped(tmp_path: Path, status: int) -> None:
    """Negativ: Gibt der Kern die Instanz nicht (mehr) heraus, faellt der
    Bezug weg -- mit dem bisherigen Hinweis."""
    res = run_app_js(
        _TESTRUN.replace("__STATUS__", str(status))
        + r"""
await viewTestRun();
return { text: textOf(byId("content")), id: state.testInstanceId,
         stored: localStorage.getItem("testInstanceId") };
""",
        tmp_path,
    )
    assert "nicht mehr verfügbar" in res["text"]
    assert res["id"] is None and res["stored"] is None


@needs_node
@pytest.mark.parametrize(("status", "kept"), [(502, True), (404, False)])
def test_monitoring_keeps_the_selected_instance_through_an_outage(
    tmp_path: Path, status: int, kept: bool
) -> None:
    """Monitoring: Der gewaehlte Vorgang bleibt bei einem Ausfall gewaehlt;
    nur 404/403 nimmt ihn aus der Ansicht."""
    res = run_app_js(
        r"""
state.principal = { subject: "mara", roles: ["modeler"] };
state.view = "monitor";
state.instanceId = "instance_3";
state.instance = { id: "instance_3", schema_id: "s", state: "RUNNING", node_states: {} };
respond((p) => p === "/instances/instance_3"
  ? (""" + str(status) + r""" === 502 ? { status: 502 } : { status: 404, body: { detail: "x" } })
  : { status: 502 });
await viewMonitor();
return { id: state.instanceId, text: textOf(byId("content")) };
""",
        tmp_path,
    )
    assert (res["id"] == "instance_3") is kept
    if kept:  # statt eines Graphen mit fremdem Schema der Hinweis
        assert "Der gewählte Vorgang konnte nicht geladen werden" in res["text"]


@needs_node
@pytest.mark.parametrize(("status", "kept"), [(502, True), (404, False)])
def test_reload_during_an_outage_keeps_the_remembered_instance(
    tmp_path: Path, status: int, kept: bool
) -> None:
    """Neuladen im Ausfall: Der fuer diesen Tab gemerkte Vorgang bleibt
    gemerkt (das naechste Laden stellt ihn her); nur 404/403 vergisst ihn."""
    res = run_app_js(
        r"""
state.principal = { subject: "erika", roles: ["operator"] };
sessionStorage.setItem(INSTANCE_KEY, JSON.stringify({ id: "instance_5", owner: "erika" }));
respond(() => (""" + str(status) + r""" === 502
  ? { status: 502 } : { status: 404, body: { detail: "x" } }));
const restored = await restoreInstance();
return { restored, stored: sessionStorage.getItem(INSTANCE_KEY) !== null };
""",
        tmp_path,
    )
    assert res["restored"] is False
    assert res["stored"] is kept


@needs_node
def test_shared_org_dialog_does_not_offer_an_empty_list_after_a_failure(
    tmp_path: Path,
) -> None:
    """Geteilte Organisation: Scheitert der Abruf, oeffnet kein Dialog mit
    leerer Auswahl (sonst legte man eine vorhandene Organisation doppelt an),
    sondern die Fehlermeldung erscheint."""
    res = run_app_js(
        r"""
state.schemaId = "s";
state.schema = { id: "s", lifecycle_state: "ENTWURF", org_model_id: null };
respond(() => ({ status: 502 }));
await manageSharedOrg();
return { dialog: byId("modal-root").children.length, toast: textOf(byId("toast-root")) };
""",
        tmp_path,
    )
    assert res["dialog"] == 0
    assert "Server nicht erreichbar" in res["toast"]
