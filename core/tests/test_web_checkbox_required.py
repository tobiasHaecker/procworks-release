# SPDX-License-Identifier: BUSL-1.1
"""No required star on check boxes.

A check box always has a value (ticked = yes, empty = no). The star promised
"must be ticked", yet completing without the tick went through -- the star had
no effect. Decision: check boxes show no star, and the mask designer does not
offer "Pflicht" for them. Mask and designer preview share ``showsRequiredMark``.
"""

from __future__ import annotations

from pathlib import Path

from web_vm import APP_JS, needs_node, run_app_js

_SCHEMA = r"""
state.schema = { id: "s", name: "P", version: 1, lifecycle_state: "ENTWURF",
  nodes: { a: { id: "a", type: "ACTIVITY", label: "Kommissionieren" } }, edges: [],
  data_elements: {
    ok: { id: "ok", name: "Vollständig", data_type: "BOOLEAN", source: "INSTANCE" },
    menge: { id: "menge", name: "Menge", data_type: "INTEGER", source: "INSTANCE" } },
  data_accesses: [], staff_rules: {}, service_bindings: {},
  forms: { a: { node_id: "a", title: "", columns: 1, fields: [
    { id: "f1", element_id: "ok", widget: "CHECKBOX", label: "Vollständig", mode: "WRITE",
      required: true, options: [] },
    { id: "f2", element_id: "menge", widget: "NUMBER", label: "Menge", mode: "WRITE",
      required: true, options: [] } ] } } };
state.schemaId = "s";
"""


@needs_node
def test_task_mask_marks_the_number_but_not_the_check_box(tmp_path: Path) -> None:
    res = run_app_js(
        _SCHEMA
        + r"""
respond(() => ({ body: {} }));
promptComplete(state.schema, "i1", "a", "Kommissionieren", null, () => {}, {});
const labels = byId("modal-root").querySelectorAll("label.field").map((l) => textOf(l));
return { labels };
""",
        tmp_path,
    )
    assert any(t.startswith("Menge *") for t in res["labels"]), res["labels"]
    assert any(t.startswith("Vollständig") and "*" not in t for t in res["labels"]), res["labels"]


@needs_node
def test_designer_offers_no_required_toggle_for_a_check_box(tmp_path: Path) -> None:
    res = run_app_js(
        _SCHEMA
        + r"""
openFormDesigner("a");
const root = byId("modal-root");
const rows = root.querySelectorAll(".fd-field");
const reqCell = (row) => row.querySelectorAll(".fd-cell")
  .find((c) => c.classList.contains("fd-req"));
return {
  box: textOf(reqCell(rows[0])), boxHasInput: !!reqCell(rows[0]).querySelector("input"),
  num: textOf(reqCell(rows[1])), numHasInput: !!reqCell(rows[1]).querySelector("input"),
  preview: textOf(root.querySelector(".fd-preview")),
};
""",
        tmp_path,
    )
    assert res["boxHasInput"] is False and "immer einen Wert" in res["box"]
    assert res["numHasInput"] is True and "Pflicht" in res["num"]
    assert "Menge *" in res["preview"] and "Vollständig *" not in res["preview"]


def test_mask_and_preview_use_the_shared_rule() -> None:
    src = APP_JS.read_text(encoding="utf-8")
    assert src.count("showsRequiredMark(f)") >= 2
    assert '(f.required ? " *"' not in src and '(f.required && writable ? " *"' not in src
