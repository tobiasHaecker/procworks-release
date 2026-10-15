# SPDX-License-Identifier: BUSL-1.1
"""Ein Serverausfall erscheint als Ausfall, nie als leerer Bestand.

Steht die API, antwortet der vorgeschaltete Proxy mit 502/503/504 ohne
JSON-Rumpf. Der Client hielt das fuer eine gewoehnliche Antwort: Statusanzeige
„verbunden“, Meldung nur „Fehler“, und die Sichten zeigten ihren Leerzustand
(„Keine offenen Aufgaben“, „Keine Instanzen“) -- als waere der Bestand leer.
Nach der Wiederkehr blieb die Sicht bis zum Sichtwechsel leer.

Geprueft wird das Verhalten des Clients in Node (``web_vm.py``).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from web_vm import needs_node, run_app_js

_CATCH = r"""
async function attempt(path) {
  try { await api.get(path); return { ok: true }; }
  catch (err) {
    return { ok: false, status: err.status, unreachable: !!err.unreachable,
      code: err.detail && err.detail.code, title: describeError(err).title };
  }
}
const pill = () => byId("conn-pill").textContent;
"""


@needs_node
@pytest.mark.parametrize("status", [502, 503, 504])
def test_a_proxy_error_without_json_means_disconnected(tmp_path: Path, status: int) -> None:
    res = run_app_js(
        _CATCH
        + f"respond(() => ({{ status: {status} }}));\n"
        + r"""
const r = await attempt("/schemas");
return { r, pill: pill() };
""",
        tmp_path,
    )
    assert res["pill"] == "getrennt"
    assert res["r"]["unreachable"] is True
    assert res["r"]["code"] == "NET.unreachable"
    assert res["r"]["status"] == status
    assert "Server nicht erreichbar" in res["r"]["title"]


@needs_node
def test_a_network_error_means_disconnected_with_the_catalog_text(tmp_path: Path) -> None:
    res = run_app_js(
        _CATCH
        + r"""
respond(() => { throw new Error("connection refused"); });
const r = await attempt("/schemas");
return { r, pill: pill() };
""",
        tmp_path,
    )
    assert res["pill"] == "getrennt"
    assert res["r"]["unreachable"] is True
    assert res["r"]["code"] == "NET.unreachable"
    assert "Server nicht erreichbar" in res["r"]["title"]


@needs_node
def test_a_json_502_of_the_api_itself_stays_connected(tmp_path: Path) -> None:
    """Negativfall: Ein angebundenes Fremdsystem scheitert (502 mit Rumpf der
    API) -- der Server antwortet, die Meldung ist die des Kerns."""

    res = run_app_js(
        _CATCH
        + r"""
respond(() => ({ status: 502, body: { detail: { message: "ERP antwortet nicht" } } }));
const r = await attempt("/v1/connectors/erp/test");
return { r, pill: pill() };
""",
        tmp_path,
    )
    assert res["pill"] == "verbunden"
    assert res["r"]["unreachable"] is False
    assert res["r"]["title"] == "ERP antwortet nicht"


@needs_node
@pytest.mark.parametrize(
    ("status", "detail", "expected"),
    [
        (403, '"forbidden"', "Berechtigung"),
        (409, '{ code: "IDEM.key-reused", message: "x" }', "Idempotency-Key"),
        (422, '{ code: "REQ.nul-character", params: { where: "body" } }', "NUL"),
    ],
)
def test_real_rejections_keep_the_status_connected(
    tmp_path: Path, status: int, detail: str, expected: str
) -> None:
    res = run_app_js(
        _CATCH
        + f"respond(() => ({{ status: {status}, body: {{ detail: {detail} }} }}));\n"
        + r"""
const r = await attempt("/schemas");
return { r, pill: pill() };
""",
        tmp_path,
    )
    assert res["pill"] == "verbunden"
    assert res["r"]["unreachable"] is False
    assert expected in res["r"]["title"]


_TASKS = r"""
state.principal = { subject: "erika", roles: ["operator"], agent_id: "a1", display_name: "Erika" };
state.agentDirectory = { a1: { agent_id: "a1", name: "Erika" } };
state.view = "tasks";
byId("auth-overlay").style.display = "none";
"""


@needs_node
def test_my_tasks_show_an_outage_not_an_empty_list(tmp_path: Path) -> None:
    res = run_app_js(
        _TASKS
        + r"""
respond((p) => p === "/directory/agents"
  ? { body: [{ agent_id: "a1", name: "Erika" }] }
  : { status: 502 });
await viewTasks();
return textOf(byId("content"));
""",
        tmp_path,
    )
    assert "Keine offenen Aufgaben" not in res
    assert "Server nicht erreichbar" in res


@needs_node
def test_my_tasks_still_say_empty_when_the_list_is_empty(tmp_path: Path) -> None:
    res = run_app_js(
        _TASKS
        + r"""
respond((p) => p === "/directory/agents"
  ? { body: [{ agent_id: "a1", name: "Erika" }] }
  : p === "/me/tasks" ? { body: [] } : { status: 404 });
await viewTasks();
return textOf(byId("content"));
""",
        tmp_path,
    )
    assert "Keine offenen Aufgaben" in res
    assert "Server nicht erreichbar" not in res


@needs_node
def test_monitoring_shows_an_outage_not_no_instances(tmp_path: Path) -> None:
    res = run_app_js(
        r"""
state.principal = { subject: "mara", roles: ["modeler"] };
state.view = "monitor";
respond(() => ({ status: 503 }));
await viewMonitor();
return textOf(byId("content"));
""",
        tmp_path,
    )
    assert "Keine Instanzen" not in res
    assert "Server nicht erreichbar" in res


@needs_node
def test_monitoring_still_says_no_instances_for_an_empty_list(tmp_path: Path) -> None:
    res = run_app_js(
        r"""
state.principal = { subject: "mara", roles: ["modeler"] };
state.view = "monitor";
respond((p) => p === "/monitoring/instances" ? { body: [] } : { status: 404 });
await viewMonitor();
return textOf(byId("content"));
""",
        tmp_path,
    )
    assert "Keine Instanzen" in res
    assert "Server nicht erreichbar" not in res


@needs_node
def test_every_view_carries_an_outage_banner_while_disconnected(tmp_path: Path) -> None:
    """Auch eine Sicht mit zwischengespeicherten Daten (Ausfuehrung) sagt, dass
    sie gerade nicht nachladen kann -- und ohne Ausfall steht kein Banner."""

    res = run_app_js(
        r"""
state.view = "help";
VIEW_META.help.fn = async () => {
  clear(byId("content"));
  byId("content").appendChild(el("div", null, "Inhalt"));
};
setConnected(false);
render();
await new Promise((r) => setTimeout(r, 0));
const down = textOf(byId("content"));
setConnected(true);
render();
await new Promise((r) => setTimeout(r, 0));
return { down, up: textOf(byId("content")) };
""",
        tmp_path,
    )
    assert "Server nicht erreichbar" in res["down"] and "Inhalt" in res["down"]
    assert "Server nicht erreichbar" not in res["up"]


@needs_node
def test_the_open_view_reloads_itself_when_the_server_is_back(tmp_path: Path) -> None:
    """Nach dem Ausfall zeichnet der naechste Live-Poll neu, auch wenn sich
    die Revision nicht geaendert hat."""

    res = run_app_js(
        r"""
state.principal = { subject: "erika", roles: ["operator"] };
state.view = "tasks"; state.revision = 5;
byId("auth-overlay").style.display = "none";
let renders = 0;
globalThis.render = () => { renders += 1; };
let down = true;
respond((p) => down ? { status: 502 }
  : p === "/monitoring/revision" ? { body: { revision: 5 } } : { status: 404 });
await pollLiveUpdates();
const whileDown = { renders, pill: byId("conn-pill").textContent };
down = false;
await pollLiveUpdates();
const afterBack = renders;
await pollLiveUpdates();
return { whileDown, afterBack, steady: renders, pill: byId("conn-pill").textContent };
""",
        tmp_path,
    )
    assert res["whileDown"] == {"renders": 0, "pill": "getrennt"}
    assert res["afterBack"] == 1
    assert res["steady"] == 1  # unchanged revision, no outage: no redraw
    assert res["pill"] == "verbunden"


@needs_node
def test_a_rejection_at_start_up_is_no_outage(tmp_path: Path) -> None:
    """Grenzfall: scheitert der Start an einer Ablehnung des Kerns (401), bleibt
    die Anzeige „verbunden“ und es steht kein Ausfall-Banner."""

    res = run_app_js(
        r"""
state.view = "help";
VIEW_META.help.fn = async () => { clear(byId("content")); };
respond((p) => p === "/health" ? { body: { status: "ok", version: "x" } }
  : { status: 401, body: { detail: "not authenticated" } });
await boot();
await new Promise((r) => setTimeout(r, 0));
return { pill: byId("conn-pill").textContent,
  banner: !!byId("content").querySelector(".outage-banner") };
""",
        tmp_path,
    )
    assert res == {"pill": "verbunden", "banner": False}


@needs_node
def test_a_failed_start_up_shows_disconnected(tmp_path: Path) -> None:
    res = run_app_js(
        r"""
state.view = "help";
VIEW_META.help.fn = async () => { clear(byId("content")); };
respond(() => ({ status: 502 }));
await boot();
await new Promise((r) => setTimeout(r, 0));
return { pill: byId("conn-pill").textContent,
  banner: textOf(byId("content").querySelector(".outage-banner")) };
""",
        tmp_path,
    )
    assert res["pill"] == "getrennt"
    assert "Server nicht erreichbar" in res["banner"]


@needs_node
def test_a_modelling_view_only_drops_the_banner_when_the_server_is_back(
    tmp_path: Path,
) -> None:
    """Modellier-Sichten zeichnen nie von selbst neu (Eingaben!); nach der
    Wiederkehr verschwindet nur der Ausfall-Hinweis."""

    res = run_app_js(
        r"""
state.principal = { subject: "mara", roles: ["modeler"] };
state.view = "model"; state.revision = 3;
byId("auth-overlay").style.display = "none";
let renders = 0;
globalThis.render = () => { renders += 1; };
setConnected(false);
showOutageBanner();
respond((p) => p === "/monitoring/revision" ? { body: { revision: 3 } } : { status: 404 });
await pollLiveUpdates();
return { renders, banner: !!byId("content").querySelector(".outage-banner") };
""",
        tmp_path,
    )
    assert res == {"renders": 0, "banner": False}


# --- Nebenbereiche: Kennzahlen, Zaehler, Inzidente, Abwesenheiten ----------
#
# Nicht nur die Haupttabellen: jede Kachel, jeder Filterzaehler und jedes
# Panel, dessen Daten nicht geladen werden konnten, zeigt einen Fehlerzustand
# bzw. „–“ statt „0“, „keine …“ oder „✓“. Gegenprobe je Bereich: mit leerem,
# aber geladenem Bestand bleiben die Leerzustaende.

_MONITOR = r"""
state.principal = { subject: "mara", roles: ["modeler"] };
state.view = "monitor";
const kpiValues = () => Object.fromEntries(byId("content").querySelectorAll(".kpi")
  .map((k) => [textOf(k.querySelector(".label")), textOf(k.querySelector(".value"))]));
const filterLabels = () => byId("content").querySelectorAll(".seg-btn").map(textOf);
"""

_EMPTY_MONITOR = {
    "/monitoring/instances": "[]",
    "/monitoring/tasks": "[]",
    "/monitoring/kpis": (
        "{ total_instances: 0, running: 0, completed: 0, activity_stats: [], "
        "avg_cycle_seconds: null }"
    ),
    "/monitoring/process-map": "{ nodes: [], edges: [] }",
    "/monitoring/unstaffed": "[]",
    "/instance-titles": "{}",
    "/v1/incidents?unresolved_only=true": "[]",
}


def _monitor_responder(failing: set[str]) -> str:
    """``respond``-Rumpf: leerer Bestand, die Pfade in ``failing`` mit 502."""

    cases = "".join(
        f'  if (p === "{path}") return {{ status: 502 }};\n'
        if path in failing
        else f'  if (p === "{path}") return {{ body: {body} }};\n'
        for path, body in _EMPTY_MONITOR.items()
    )
    return "respond((p) => {\n" + cases + "  return { status: 404 };\n});\n"


@needs_node
def test_monitoring_tiles_and_counters_claim_nothing_during_an_outage(tmp_path: Path) -> None:
    res = run_app_js(
        _MONITOR
        + r"""
respond(() => ({ status: 502 }));
await viewMonitor();
return { kpis: kpiValues(), filters: filterLabels(), text: textOf(byId("content")) };
""",
        tmp_path,
    )
    assert res["kpis"] and set(res["kpis"].values()) == {"–"}, res["kpis"]
    assert res["filters"] and not any("(0)" in f for f in res["filters"]), res["filters"]
    for claim in ("Keine offenen Inzidente", "Noch keine abgeschlossenen",
                  "Noch keine Abläufe", "Keine Instanzen"):
        assert claim not in res["text"], claim
    assert res["text"].count("Server nicht erreichbar") >= 3


@needs_node
def test_monitoring_still_shows_zero_and_empty_for_an_empty_inventory(tmp_path: Path) -> None:
    res = run_app_js(
        _MONITOR
        + _monitor_responder(set())
        + r"""
await viewMonitor();
return { kpis: kpiValues(), filters: filterLabels(), text: textOf(byId("content")) };
""",
        tmp_path,
    )
    assert res["kpis"]["Instanzen gesamt"] == "0"
    assert res["kpis"]["Überfällige Aufgaben"] == "0"
    assert res["kpis"]["Niemand zuständig"] == "0"
    assert "Alle (0)" in res["filters"]
    assert "Keine offenen Inzidente" in res["text"]
    assert "Noch keine abgeschlossenen" in res["text"]
    assert "Server nicht erreichbar" not in res["text"]


@pytest.mark.parametrize(
    ("failing", "claim", "tile"),
    [
        ("/v1/incidents?unresolved_only=true", "Keine offenen Inzidente", None),
        ("/monitoring/kpis", "Noch keine abgeschlossenen", "Ø Durchlaufzeit"),
        ("/monitoring/unstaffed", None, "Niemand zuständig"),
        ("/monitoring/tasks", None, "Überfällige Aufgaben"),
    ],
)
@needs_node
def test_monitoring_marks_exactly_the_part_that_failed(
    tmp_path: Path, failing: str, claim: str | None, tile: str | None
) -> None:
    res = run_app_js(
        _MONITOR
        + _monitor_responder({failing})
        + r"""
await viewMonitor();
return { kpis: kpiValues(), filters: filterLabels(), text: textOf(byId("content")) };
""",
        tmp_path,
    )
    if claim:  # das Panel nennt den Fehler statt des Leerzustands
        assert claim not in res["text"]
        assert "Server nicht erreichbar" in res["text"]
    if failing == "/monitoring/unstaffed":
        assert "Niemand zuständig (–)" in res["filters"]
    if tile:
        assert res["kpis"][tile] == "–"
    # Was geladen wurde, bleibt: die (leere) Liste zaehlt weiter 0.
    assert "Alle (0)" in res["filters"]


@needs_node
def test_my_tasks_absences_and_counter_claim_nothing_during_an_outage(tmp_path: Path) -> None:
    res = run_app_js(
        _TASKS
        + r"""
respond(() => ({ status: 502 }));
await viewTasks();
return textOf(byId("content"));
""",
        tmp_path,
    )
    assert "Keine Abwesenheiten eingetragen" not in res
    assert "0 Einträge" not in res
    assert "Die Abwesenheiten konnten nicht geladen werden" in res


@needs_node
def test_my_tasks_still_say_no_absences_when_there_are_none(tmp_path: Path) -> None:
    res = run_app_js(
        _TASKS
        + r"""
respond((p) => p === "/directory/agents" ? { body: [{ agent_id: "a1", name: "Erika" }] }
  : (p === "/me/tasks" || p === "/agents/a1/absences") ? { body: [] } : { status: 404 });
await viewTasks();
return textOf(byId("content"));
""",
        tmp_path,
    )
    assert "Keine Abwesenheiten eingetragen" in res
    assert "0 Einträge" in res
    assert "Server nicht erreichbar" not in res


@needs_node
def test_my_tasks_do_not_claim_an_empty_organisation_when_the_directory_failed(
    tmp_path: Path,
) -> None:
    res = run_app_js(
        r"""
state.principal = { subject: "mara", roles: ["modeler"] };
state.agentDirectory = {};
state.view = "tasks";
byId("auth-overlay").style.display = "none";
respond(() => ({ status: 503 }));
await viewTasks();
return textOf(byId("content"));
""",
        tmp_path,
    )
    assert "Keine Agenten" not in res
    assert "Server nicht erreichbar" in res


_INSTANCE = r"""
state.principal = { subject: "mara", roles: ["modeler"] };
state.schema = { id: "s1", name: "S", version: 1, lifecycle_state: "FREIGEGEBEN",
  nodes: { start: { id: "start", type: "START", label: "Start" },
           a1: { id: "a1", type: "ACTIVITY", label: "Pruefen" },
           end: { id: "end", type: "END", label: "Ende" } },
  edges: [{ source: "start", target: "a1", type: "CONTROL" },
          { source: "a1", target: "end", type: "CONTROL" }],
  data_elements: {}, data_accesses: [], staff_rules: { a1: { kind: "ROLE", ref: "r1" } },
  org_model: { roles: {}, org_units: {}, agents: {} } };
state.schemaId = "s1";
state.instance = { id: "i1", schema_id: "s1", schema_version: 1, state: "RUNNING",
  node_states: { start: "COMPLETED", a1: "ACTIVATED", end: "NOT_ACTIVATED" }, data_values: {} };
state.worklist = { ready_activities: ["a1"] };
"""


@needs_node
def test_the_instance_detail_claims_no_empty_history_and_no_unstaffed_step_on_failure(
    tmp_path: Path,
) -> None:
    res = run_app_js(
        _INSTANCE
        + r"""
respond(() => ({ status: 502 }));
const box = el("div");
await renderInstanceDetail(box, false);
return textOf(box);
""",
        tmp_path,
    )
    assert "Noch keine Ereignisse" not in res
    assert "0 Ereignisse" not in res
    assert "niemand zuständig" not in res
    assert "Server nicht erreichbar" in res


@needs_node
def test_the_instance_detail_still_reports_an_empty_history_and_unstaffed_step(
    tmp_path: Path,
) -> None:
    res = run_app_js(
        _INSTANCE
        + r"""
respond((p) => (p === "/instances/i1/audit" || p === "/instances/i1/tasks")
  ? { body: [] } : { status: 404 });
const box = el("div");
await renderInstanceDetail(box, false);
return textOf(box);
""",
        tmp_path,
    )
    assert "Noch keine Ereignisse" in res
    assert "niemand zuständig" in res
    assert "Server nicht erreichbar" not in res


@needs_node
@pytest.mark.parametrize("fail", [True, False])
def test_the_integration_lists_claim_no_empty_inventory_on_failure(
    tmp_path: Path, fail: bool
) -> None:
    res = run_app_js(
        r"""
state.principal = { subject: "mara", roles: ["modeler"] };
"""
        + ("respond(() => ({ status: 502 }));\n" if fail else "respond(() => ({ body: [] }));\n")
        + r"""
const conn = await connectorRegistryPanel();
const hooks = await webhookPanel();
return textOf(conn) + " | " + textOf(hooks);
""",
        tmp_path,
    )
    claims = ("Keine Connectoren konfiguriert", "Noch keine Webhook-Abonnements")
    if fail:
        assert not any(c in res for c in claims), res
        assert res.count("Server nicht erreichbar") == 2
    else:
        assert all(c in res for c in claims), res
        assert "Server nicht erreichbar" not in res


@needs_node
@pytest.mark.parametrize("fail", [True, False])
def test_a_test_instance_panel_claims_no_empty_task_list_on_failure(
    tmp_path: Path, fail: bool
) -> None:
    part = (
        "{ ok: false, value: [], error: unreachableError(502) }"
        if fail
        else "{ ok: true, value: [], error: null }"
    )
    res = run_app_js(
        r"""
state.testAgentA = "a1";
const schema = { nodes: {}, org_model: { roles: {}, org_units: {},
  agents: { a1: { id: "a1", name: "Erika", role_ids: [] } } } };
const inst = { id: "i1", state: "RUNNING" };
"""
        + 'const people = [{ id: "a1", name: "Erika" }];\n'
        + f'const panel = testAgentPanel("A", inst, schema, people, {part});\n'
        + "return textOf(panel);\n",
        tmp_path,
    )
    if fail:
        assert "Keine offenen Aufgaben" not in res
        assert "Server nicht erreichbar" in res
    else:
        assert "Keine offenen Aufgaben für Erika" in res
