# SPDX-License-Identifier: BUSL-1.1
"""Modellhinweise nennen Schritte beim Namen und wirken nicht wie Befunde.

„G2 Das Gateway 'split_482' hat einen Grad von 6 … [split_482]“ stand rot wie
ein Regelverstoß im Panel „Korrektheit“ -- mit interner Kennung, zweimal.
"""

from __future__ import annotations

import re
from pathlib import Path

from web_vm import APP_JS, needs_node, run_app_js

_SCHEMA = r"""
state.schema = { id: "s", name: "P", version: 1, lifecycle_state: "ENTWURF",
  nodes: { start: { id: "start", type: "START" },
    e: { id: "e", type: "ACTIVITY", label: "Betrag erfassen" },
    split_482: { id: "split_482", type: "XOR_SPLIT" } },
  edges: [{ source: "start", target: "e" }, { source: "e", target: "split_482" }],
  data_elements: {}, data_accesses: [], staff_rules: {}, service_bindings: {} };
state.validation = { correct: true, findings: [] };
state.hints = [{ code: "G2", node_id: "split_482",
  message: "Das Gateway 'split_482' hat einen Grad von 6. Gateways mit hohem Grad "
    + "sollten aufgeteilt werden." }];
"""


@needs_node
def test_hint_names_the_gateway_and_shows_no_id(tmp_path: Path) -> None:
    res = run_app_js(
        _SCHEMA
        + r"""
const panel = findingsPanel();
const code = panel.querySelectorAll("span").find((sp) => textOf(sp) === "G2");
return { text: textOf(panel), neutral: code.classList.contains("rule-hint"),
  bar: modelStatusBar(state.schema, true).querySelectorAll("span")
    .map((sp) => sp.getAttribute("title") || "").join(" ") };
""",
        tmp_path,
    )
    assert "split_482" not in res["text"] and "split_482" not in res["bar"]
    assert "„Entscheidung nach „Betrag erfassen““" in res["text"]
    assert res["neutral"] is True


def test_hints_are_styled_neutrally() -> None:
    styles = (APP_JS.parent / "styles.css").read_text(encoding="utf-8")
    assert re.search(r"\.finding \.rule\.rule-hint \{ color: var\(--txt-dim\); \}", styles)


@needs_node
def test_unnamed_steps_are_told_apart_and_hints_select_them(tmp_path: Path) -> None:
    """Grenzfall: mehrere unbenannte Schritte -- jeder heißt nach seinem
    Vorgänger, und ein Klick auf den Hinweis wählt ihn aus."""
    res = run_app_js(
        r"""
state.schema = { id: "s", name: "P", version: 1, lifecycle_state: "ENTWURF",
  nodes: { start: { id: "start", type: "START" },
    e: { id: "e", type: "ACTIVITY", label: "Erfassen" },
    u1: { id: "u1", type: "ACTIVITY", label: "" }, u2: { id: "u2", type: "ACTIVITY", label: "" } },
  edges: [{ source: "start", target: "e" }, { source: "e", target: "u1" },
    { source: "u1", target: "u2" }],
  data_elements: {}, data_accesses: [], staff_rules: {}, service_bindings: {} };
state.validation = { correct: true, findings: [] };
state.hints = ["u1", "u2"].map((id) => ({ code: "G7", node_id: id,
  message: `Die Aktivitaet '${id}' hat kein Label.` }));
globalThis.render = () => {};
const panel = findingsPanel();
const links = panel.querySelectorAll("a");
await links[1].click();
return { texts: links.map((a) => textOf(a)), selected: state.selectedNode };
""",
        tmp_path,
    )
    assert res["texts"][0] != res["texts"][1]
    assert "„Schritt ohne Bezeichnung nach „Erfassen““" in res["texts"][0]
    # Kette unbenannter Schritte: gezählt, nicht verschachtelt.
    assert "„2. Schritt ohne Bezeichnung nach „Erfassen““" in res["texts"][1]
    assert res["selected"] == "u2"


@needs_node
def test_subprocess_and_activity_count_together_and_no_symbol_names(tmp_path: Path) -> None:
    res = run_app_js(
        r"""
const schema = { nodes: { start: { id: "start", type: "START" },
    e: { id: "e", type: "ACTIVITY", label: "Erfassen" },
    t: { id: "t", type: "SUBPROCESS", label: "" }, a: { id: "a", type: "ACTIVITY", label: "" },
    sp: { id: "sp", type: "XOR_SPLIT" }, x: { id: "x", type: "ACTIVITY", label: "" },
    jn: { id: "jn", type: "XOR_JOIN" } },
  edges: [{ source: "start", target: "e" }, { source: "e", target: "t" },
    { source: "t", target: "a" }, { source: "a", target: "sp" },
    { source: "sp", target: "x" }, { source: "sp", target: "jn" }, { source: "x", target: "jn" }] };
schema.nodes.y = { id: "y", type: "ACTIVITY", label: "" };
schema.edges.push({ source: "jn", target: "y" });
const c = (id) => nodeCaptionInContext(schema, schema.nodes[id]);
return { t: c("t"), a: c("a"), jn: c("jn"), y: c("y") };
""",
        tmp_path,
    )
    assert res["t"] == "Schritt ohne Bezeichnung nach „Erfassen“"
    assert res["a"] == "2. Schritt ohne Bezeichnung nach „Erfassen“"
    assert res["jn"] == "Ende der Entscheidung"  # zwei unbenannte Vorgänger: kein „nach XOR ▶“
    # Hinter dem Verzweigungsende ist dieses der Bezug.
    assert res["y"] == "Schritt ohne Bezeichnung nach „Ende der Entscheidung“"
