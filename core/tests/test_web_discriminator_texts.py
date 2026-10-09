# SPDX-License-Identifier: BUSL-1.1
"""Rejections around a decision attribute name the attribute and the split.

Deleting the attribute that drives a decision, or changing its type, is
rejected by the core. The client used to word that as the hypothetical result
("the attribute does not exist") without naming the decision or a remedy, and
the input-mask findings of two steps read identically because the step was
missing. These tests pin the wording inputs and that identical lines collapse.
"""

from __future__ import annotations

import re
from pathlib import Path

from web_source import app_js_source
from web_vm import needs_node, run_app_js

VALIDATOR = Path(__file__).resolve().parents[1] / "src" / "procworks" / "validator.py"

_SCHEMA = r"""
state.schema = { id: "s", name: "P", version: 1, lifecycle_state: "ENTWURF",
  nodes: { start: { id: "start", type: "START" },
    e: { id: "e", type: "ACTIVITY", label: "Antrag erfassen" },
    f: { id: "f", type: "ACTIVITY", label: "Antrag prüfen" },
    split_1: { id: "split_1", type: "XOR_SPLIT", label: "Betrag hoch?" } },
  edges: [], data_elements: { betrag: { id: "betrag", name: "Betrag", data_type: "FLOAT" } },
  data_accesses: [], staff_rules: {}, service_bindings: {} };
"""


@needs_node
def test_deletion_rejection_names_attribute_branch_and_remedy(tmp_path: Path) -> None:
    res = run_app_js(
        _SCHEMA
        + r"""
const f = { rule: "K7", code: "K7.discriminator-missing", node_id: "split_1",
  params: { element: "betrag" } };
return { text: findingText(f, { withHint: true }) };
""",
        tmp_path,
    )
    text = res["text"]
    assert "„Betrag“ steuert die Verzweigung „Betrag hoch?“" in text
    assert "Erst die Entscheidung auf ein anderes Merkmal umstellen" in text
    # Nicht mehr der hypothetische Folgezustand als ganze Aussage.
    assert "gibt es nicht" not in text


@needs_node
def test_type_change_rejection_names_step_and_lines_are_unique(tmp_path: Path) -> None:
    res = run_app_js(
        _SCHEMA
        + r"""
const u2 = (node) => ({ rule: "U2", code: "U2.widget-type", node_id: node,
  params: { widget: "NUMBER", element: "betrag", type: "STRING" } });
const k7 = { rule: "K7", code: "K7.kind-mismatch", node_id: "split_1",
  params: { kind: "RANGE", type: "STRING", element: "betrag" } };
const two = describeError({ status: 422, detail: { findings: [k7, u2("e"), u2("f")] } });
const dup = describeError({ status: 422, detail: { findings: [u2("e"), u2("e")] } });
return { two: two.lines, dup: dup.lines };
""",
        tmp_path,
    )
    assert len(res["two"]) == 3
    assert "„Betrag“ steuert die Verzweigung „Betrag hoch?“" in res["two"][0]
    assert "„Antrag erfassen“" in res["two"][1] and "„Antrag prüfen“" in res["two"][2]
    # Wirklich gleich lautende Zeilen erscheinen nur einmal.
    assert len(res["dup"]) == 1


@needs_node
def test_without_element_param_texts_stay_readable(tmp_path: Path) -> None:
    """Older findings (or a split without name) still give a full sentence."""

    res = run_app_js(
        r"""
state.schema = null;
return { t: findingText({ rule: "K7", code: "K7.discriminator-missing", params: {} }) };
""",
        tmp_path,
    )
    assert res["t"].startswith("Das Merkmal steuert eine Verzweigung")


@needs_node
def test_unnamed_split_is_named_by_its_context(tmp_path: Path) -> None:
    """An unnamed split reads „die Entscheidung nach „…““, never „XOR ▶“."""

    res = run_app_js(
        _SCHEMA
        + r"""
state.schema.nodes.split_1.label = "";
state.schema.edges = [{ source: "start", target: "e" }, { source: "e", target: "split_1" }];
return { t: findingText({ rule: "K7", code: "K7.discriminator-missing", node_id: "split_1",
  params: { element: "betrag" } }) };
""",
        tmp_path,
    )
    assert "„Betrag“ steuert die Entscheidung nach „Antrag erfassen“" in res["t"]
    assert "XOR" not in res["t"] and "▶" not in res["t"]


def _codes_with_element_param() -> set[str]:
    """Codes whose ``fail``/finding in the validator passes ``element`` in params."""

    src = VALIDATOR.read_text(encoding="utf-8")
    codes = set()
    pattern = r'code="([A-Z]\d*\.[a-z-]+)",\s*(?:#[^\n]*\n\s*)*params=\{([^{}]*)\}'
    for m in re.finditer(pattern, src):
        if '"element"' in m.group(2):
            codes.add(m.group(1))
    return codes


def test_catalog_names_the_attribute_wherever_the_core_sends_it() -> None:
    """Class guard: if the core names the attribute, the text uses it.

    Otherwise the user reads "the attribute" and has to guess which one --
    exactly what made the decision rejections unclear.
    """

    codes = _codes_with_element_param()
    assert {"K7.discriminator-missing", "K7.kind-mismatch", "K6.kind-mismatch"} <= codes
    js = app_js_source()
    missing = []
    for code in sorted(codes):
        m = re.search(r'"' + re.escape(code) + r'": \(p\) => \(\{(.*?)\}\),\n', js, re.S)
        if not m or not re.search(r"p\.element|merkmalOf\(p", m.group(1)):
            missing.append(code)
    assert not missing, f"Katalogtext nennt das Element nicht: {missing}"
