# SPDX-License-Identifier: BUSL-1.1
"""The start of the web client asks only for what the first view needs.

Measured after a login as a clerk: before "Meine Aufgaben" stood, the client
loaded the migration report of the preselected process (about 1 s on a large
store) -- which only the modelling view shows, a view a clerk cannot open --
and the person directory twice (once at the start, once in the task view,
about 0.5 s each).

Pinned here, by running ``boot()`` in the Node ``vm`` and recording the
requests:

* a clerk (operator) never loads a migration report; a modeller still does
  (the header button "migrate running instances" needs it);
* the person directory is loaded once per start, also when the task view
  follows right after; a later view after the freshness window loads it anew;
* two loads at the same time share one request.
"""

from __future__ import annotations

from pathlib import Path

from web_vm import needs_node, run_app_js

_BOOT = r"""
state.token = "sitzung";
respond((p) => {
  if (p === "/health") return { body: { status: "ok", version: "x" } };
  if (p === "/auth/config") return { body: { mode: "password", password_login: true } };
  if (p === "/auth/me") return { body: { subject: "u", roles: __ROLES__, agent_id: "a1" } };
  if (p === "/schema-summaries") {
    return { body: [{ id: "s1", name: "P", version: 2, lifecycle_state: "RELEASED",
                      revision_of: null }] };
  }
  if (p === "/schemas/s1") {
    return { body: { id: "s1", name: "P", version: 2, lifecycle_state: "RELEASED",
                     nodes: {}, edges: [], data_elements: {}, staff_rules: {},
                     org_model: { agents: {}, roles: {}, org_units: {} } } };
  }
  if (p === "/schemas/s1/validation") return { body: { correct: true, findings: [] } };
  if (p === "/schemas/s1/metrics") return { body: { hints: [] } };
  if (p.startsWith("/schemas/s1/migration-report")) {
    return { body: { target_schema_id: "s1", candidates: [] } };
  }
  if (p === "/directory/agents") return { body: [{ agent_id: "a1", name: "Erika" }] };
  if (p === "/monitoring/revision") return { body: { revision: 1 } };
  if (p.startsWith("/me/tasks")) return { body: [] };
  if (p.startsWith("/me/absences")) return { body: [] };
  return { body: [] };
});
await boot();
await new Promise((r) => setTimeout(r, 50));
return { calls: calls.slice(), view: state.view };
"""


def _boot(roles: str, tmp_path: Path) -> dict[str, object]:
    return run_app_js(_BOOT.replace("__ROLES__", roles), tmp_path)


@needs_node
def test_a_clerk_starts_without_migration_report_and_one_directory_load(
    tmp_path: Path,
) -> None:
    snap = _boot('["operator"]', tmp_path)
    calls = snap["calls"]
    assert snap["view"] == "tasks"
    assert not any("migration-report" in c for c in calls), calls
    assert calls.count("GET /directory/agents") == 1, calls
    assert any(c.startswith("GET /me/tasks") for c in calls), calls


@needs_node
def test_a_modeller_still_gets_the_migration_report(tmp_path: Path) -> None:
    """Negativ: the modelling view shows the migration button -- keep loading it."""

    snap = _boot('["modeler"]', tmp_path)
    assert any("migration-report" in c for c in snap["calls"]), snap["calls"]


@needs_node
def test_directory_loads_share_one_request_and_expire(tmp_path: Path) -> None:
    """Grenzfall: zwei Abrufe zugleich -> eine Anfrage; nach Ablauf der
    Frische-Frist laedt die naechste Sicht neu."""

    res = run_app_js(
        r"""
const people = [{ agent_id: "a", name: "A" }];
respond((p) => (p === "/directory/agents" ? { body: people } : { body: [] }));
await Promise.all([loadAgentDirectory(), loadAgentDirectory({ maxAgeMs: 5000 })]);
const together = calls.filter((c) => c === "GET /directory/agents").length;
await loadAgentDirectory({ maxAgeMs: 5000 });
const fresh = calls.filter((c) => c === "GET /directory/agents").length;
agentDirectoryLoad.at -= 6000;
await loadAgentDirectory({ maxAgeMs: 5000 });
const stale = calls.filter((c) => c === "GET /directory/agents").length;
await loadAgentDirectory();
const forced = calls.filter((c) => c === "GET /directory/agents").length;
return { together, fresh, stale, forced, name: state.agentDirectory.a.name };
""",
        tmp_path,
    )
    assert (res["together"], res["fresh"], res["stale"], res["forced"]) == (1, 1, 2, 3)
    assert res["name"] == "A"
