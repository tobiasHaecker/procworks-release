# SPDX-License-Identifier: BUSL-1.1
"""The monitoring loads with a fixed number of requests, whatever the stock.

It used to fetch the id list, then every case on its own, then the tasks of
every running case on its own -- two requests per case, and each task request
swept every case for escalations on the server. With a few hundred running
cases the view took minutes and blocked navigation meanwhile. Now it reads
the cases and the open tasks in one request each (``GET /monitoring/instances``,
``GET /monitoring/tasks``).
"""

from __future__ import annotations

from pathlib import Path

from web_vm import needs_node, run_app_js


def _load(tmp_path: Path, running: int) -> dict:
    """Render the monitoring over ``running`` running cases; return the requests."""

    return run_app_js(
        r"""
state.principal = { subject: "vera", roles: ["viewer"] };
state.view = "monitor";
const running = """ + str(running) + r""";
const inst = (i) => ({ id: "i" + i, schema_id: "s", schema_version: 1, state: "RUNNING",
  node_states: {}, data_values: {}, started_at: "2026-10-01T08:00:00Z" });
const task = (i) => ({ instance_id: "i" + i, node_id: "n", label: "Pruefen",
  eligible_agents: ["a1"], time_criticality: i % 2 ? "OVERDUE" : "NONE",
  escalated_stage: i === 0 ? 1 : 0, claimed_by: null });
const seen = [];
const each = (make) => Array.from({ length: running }, (_, i) => make(i));
respond((p) => {
  seen.push(p);
  if (p === "/monitoring/instances") return { body: each(inst) };
  if (p === "/monitoring/tasks") return { body: each(task) };
  if (p === "/monitoring/kpis") {
    return { body: { total_instances: running, running, completed: 0, activity_stats: [] } };
  }
  if (p === "/monitoring/process-map") return { body: { nodes: [], edges: [] } };
  if (p.endsWith("/conformance")) return { body: { steps: [], deviations: [] } };
  const schema = { id: "s", name: "P", version: 1, nodes: {}, edges: [] };
  if (p.startsWith("/schemas/")) return { body: schema };
  return { body: p === "/instance-titles" ? {} : [] };
});
await viewMonitor();
const kpis = Object.fromEntries(byId("content").querySelectorAll(".kpi")
  .map((k) => [textOf(k.querySelector(".label")), textOf(k.querySelector(".value"))]));
return { seen, kpis, text: textOf(byId("content")) };
""",
        tmp_path,
    )


@needs_node
def test_the_number_of_requests_does_not_grow_with_the_running_cases(tmp_path: Path) -> None:
    few = _load(tmp_path, 3)
    many = _load(tmp_path, 40)
    assert len(many["seen"]) == len(few["seen"]), (few["seen"], many["seen"])
    per_case = [p for p in many["seen"] if p.startswith("/instances/")]
    assert per_case == [], f"per-case requests: {per_case[:5]}"
    assert "/monitoring/instances" in many["seen"] and "/monitoring/tasks" in many["seen"]


@needs_node
def test_overdue_and_escalated_figures_come_from_the_collective_task_read(
    tmp_path: Path,
) -> None:
    res = _load(tmp_path, 6)
    assert res["kpis"]["Überfällige Aufgaben"] == "3"
    assert res["kpis"]["Eskalierte Aufgaben"] == "1"
    assert "Eskalationen" in res["text"]
