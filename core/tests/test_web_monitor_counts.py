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
  if (p === "/monitoring/instances") {
    if (visible < 0) return { status: 500, body: { detail: "kaputt" } };
    return { body: Array.from({ length: visible }, (_, i) => inst("i" + i)) };
  }
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


def _render_with_tests(tmp_path: Path, tests: int) -> dict:
    """Monitoring mit 8 echten Vorgaengen (3 laufend) und ``tests`` laufenden
    Pruefinstanzen; liefert Filterknoepfe, Tabellenzeilen und Text."""
    return run_app_js(
        r"""
const report = """ + _REPORT + r""";
const tests = """ + str(tests) + r""";
const ids = Array.from({ length: 8 }, (_, i) => "i" + i)
  .concat(Array.from({ length: tests }, (_, i) => "t" + i));
const inst = (id) => ({ id, schema_id: "s", schema_version: 1, is_test: id.startsWith("t"),
  state: id.startsWith("t") || Number(id.slice(1)) < 3 ? "RUNNING" : "COMPLETED",
  node_states: {}, data_values: {}, started_at: "2026-10-01T08:00:00Z" });
respond((p) => {
  if (p === "/monitoring/instances") return { body: ids.map(inst) };
  if (p === "/monitoring/kpis") return { body: report };
  if (p === "/monitoring/process-map") return { body: { nodes: [], edges: [] } };
  if (p.endsWith("/conformance")) return { body: { steps: [], deviations: [] } };
  if (p.startsWith("/schemas/")) {
    return { body: { id: "s", name: "P", version: 1, nodes: {}, edges: [] } };
  }
  return { body: p === "/instance-titles" ? {} : [] };
});
await viewMonitor();
const c = byId("content");
const panel = c.querySelectorAll(".panel")
  .find((x) => x.getAttribute("data-tour") === "monitor.instances");
return {
  filters: c.querySelectorAll(".seg-btn").map((b) => textOf(b)).slice(0, 3),
  rows: panel.querySelectorAll("tr").length - 1,
  listed: panel.querySelectorAll(".task-context").map((d) => textOf(d)),
  note: textOf(panel.querySelector(".monitor-test-note")),
};
""",
        tmp_path,
    )


@needs_node
def test_test_instances_are_left_out_of_list_and_filter_counts(tmp_path: Path) -> None:
    """Filterzaehler stimmen mit den Kacheln (8 / 3 laufend) ueberein: die
    Pruefinstanz steht weder in der Liste noch in den Zaehlern, ein Hinweis
    nennt sie."""
    res = _render_with_tests(tmp_path, 1)
    assert res["filters"] == ["Alle (8)", "Laufend (3)", "Abgeschlossen (5)"]
    assert res["rows"] == 8
    assert "t0" not in res["listed"] and "i0" in res["listed"]
    assert "1 Prüfinstanz" in res["note"] and "ausgeblendet" in res["note"]


@needs_node
def test_without_test_instances_counts_and_list_stay_as_before(tmp_path: Path) -> None:
    """Negativ: Ohne Pruefinstanz zaehlen die echten Vorgaenge unveraendert,
    und es gibt keinen Hinweis."""
    res = _render_with_tests(tmp_path, 0)
    assert res["filters"] == ["Alle (8)", "Laufend (3)", "Abgeschlossen (5)"]
    assert res["rows"] == 8
    assert res["note"] == ""
