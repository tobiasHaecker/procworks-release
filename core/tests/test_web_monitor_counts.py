# SPDX-License-Identifier: BUSL-1.1
"""Monitoring tiles count all instances, as the guide promises.

For an operator who only reads the instances they take part in, "Instanzen
gesamt" came from the filtered list while cycle time and bottlenecks came from
the unfiltered report -- contradicting figures without a hint. The list also
counted test instances, which never count in the monitoring. The tiles now
take their counts from the report, and a note appears whenever the list shows
fewer real instances than the report counts.
"""

from __future__ import annotations

from pathlib import Path

from web_vm import needs_node, run_app_js

_REPORT = (
    "{ total_instances: 8, running: 3, completed: 5, avg_cycle_seconds: 3600, activity_stats: [] }"
)


@needs_node
def test_counts_come_from_the_report_and_ignore_test_instances(tmp_path: Path) -> None:
    res = run_app_js(
        r"""
const report = """ + _REPORT + r""";
const all = Array.from({ length: 8 },
  (_, i) => ({ id: "i" + i, state: i < 3 ? "RUNNING" : "COMPLETED" }));
const withTest = all.concat([{ id: "t", state: "RUNNING", is_test: true }]);
return {
  full: monitorCounts(withTest, report),
  partial: monitorCounts(all.slice(0, 2), report),
  noReport: monitorCounts(withTest, null),
  loadFailed: monitorCounts([], report, false),
};
""",
        tmp_path,
    )
    assert res["full"] == {"total": 8, "running": 3, "done": 5, "listIsPartial": False}
    # Grund des Hinweises: die Liste zeigt weniger echte Vorgaenge als der Bericht.
    assert res["partial"]["total"] == 8 and res["partial"]["listIsPartial"] is True
    # Ohne Bericht: aus der Liste, aber ohne Test-Instanz; Teilmenge nicht feststellbar.
    assert res["noReport"] == {"total": 8, "running": 3, "done": 5, "listIsPartial": False}
    # Ein Ladefehler der Liste ist keine Einschraenkung -- kein Hinweis.
    assert res["loadFailed"]["total"] == 8 and res["loadFailed"]["listIsPartial"] is False


def _render(tmp_path: Path, visible: int) -> dict:
    return run_app_js(
        r"""
const report = """ + _REPORT + r""";
const visible = """ + str(visible) + r""";
const inst = (i) => ({ id: "i" + i, schema_id: "s", schema_version: 1,
  state: "COMPLETED", node_states: {}, data_values: {}, started_at: "2026-10-01T08:00:00Z" });
respond((p) => {
  if (p === "/instances") {
    if (visible < 0) return { status: 500, body: { detail: "kaputt" } };
    return { body: Array.from({ length: visible }, (_, i) => "i" + i) };
  }
  if (p.startsWith("/instances/")) return { body: inst(p.split("/")[2]) };
  if (p === "/monitoring/kpis") return { body: report };
  if (p === "/monitoring/process-map") return { body: { nodes: [], edges: [] } };
  if (p.endsWith("/conformance")) return { body: { steps: [], deviations: [] } };
  const schema = { id: "s", name: "P", version: 1, nodes: {}, edges: [] };
  if (p.startsWith("/schemas/")) return { body: schema };
  return { body: p === "/instance-titles" ? {} : [] };
});
await viewMonitor();
const c = byId("content");
const tiles = c.querySelectorAll(".kpi").map((k) => textOf(k));
return { tiles, note: !!c.querySelector(".monitor-scope-note"), text: textOf(c) };
""",
        tmp_path,
    )


@needs_node
def test_restricted_list_shows_report_totals_and_a_scope_note(tmp_path: Path) -> None:
    res = _render(tmp_path, 2)
    assert any(t.startswith("Instanzen gesamt") and "8" in t for t in res["tiles"]), res["tiles"]
    assert res["note"] is True
    assert "nur Vorgänge, an denen du beteiligt bist" in res["text"]


@needs_node
def test_full_list_shows_no_scope_note(tmp_path: Path) -> None:
    res = _render(tmp_path, 8)
    assert res["note"] is False


@needs_node
def test_failed_list_load_shows_no_scope_note(tmp_path: Path) -> None:
    res = _render(tmp_path, -1)
    assert res["note"] is False
    assert any(t.startswith("Instanzen gesamt") and "8" in t for t in res["tiles"])
