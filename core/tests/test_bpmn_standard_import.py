# SPDX-License-Identifier: BUSL-1.1
"""Standard BPMN imports without the ProcWorks extension.

The import ignored the standard ``conditionExpression`` (-> "XOR split has no
branch decision"), refused implicit merges (a task with two incoming flows),
and ProcWorks' *own* export was not re-importable once its extension was gone.
Now the import normalises implicit gateways, reads the conditions ProcWorks
writes (and the ``${ … }`` form of other tools) into the partition, and lets
the step before a decision write its discriminator when nothing does.

The other half matters as much: the deliberately broken models the validation
threw at the import must **still** be rejected -- the
normalisation must never talk a defect into a valid model. ``validate()``
stays the only judge.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from web_vm import needs_node, run_app_js

from procworks.bpmn import BpmnError, export_bpmn, import_bpmn
from procworks.demo import _build_org, _build_urlaubsantrag
from procworks.model import NodeType, XorDecisionKind
from procworks.validator import CorrectnessError

_NS = 'xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"'


def _doc(body: str) -> str:
    return (
        f'<?xml version="1.0"?><bpmn:definitions {_NS}>'
        f'<bpmn:process id="p" name="P">{body}</bpmn:process></bpmn:definitions>'
    )


def _flow(fid: str, s: str, t: str, cond: str | None = None) -> str:
    inner = f"<bpmn:conditionExpression>{cond}</bpmn:conditionExpression>" if cond else ""
    head = f'<bpmn:sequenceFlow id="{fid}" sourceRef="{s}" targetRef="{t}">'
    return f"{head}{inner}</bpmn:sequenceFlow>"


def _nodes(**kinds: str) -> str:
    return "".join(f'<bpmn:{tag} id="{nid}" name="{nid}"/>' for nid, tag in kinds.items())


# --- now accepted ------------------------------------------------------------


def test_own_export_without_extension_is_reimportable() -> None:
    urlaub = _build_urlaubsantrag(_build_org())
    stripped = re.sub(
        r"<bpmn:extensionElements>.*?</bpmn:extensionElements>", "", export_bpmn(urlaub), flags=re.S
    )

    schema = import_bpmn(stripped, schema_id="reimport")

    [decision] = schema.xor_decisions.values()
    assert decision.kind is XorDecisionKind.ENUM
    element = schema.data_elements[decision.discriminator]
    writers = [a.node_id for a in schema.data_accesses if a.element_id == element.id]
    assert len(writers) == 1  # the step before the decision now writes it


def test_foreign_conditions_and_an_implicit_merge_are_understood() -> None:
    body = _nodes(s="startEvent", a="task", g="exclusiveGateway", b="task", c="task",
                  d="task", e="endEvent")
    body += _flow("1", "s", "a") + _flow("2", "a", "g")
    body += _flow("3", "g", "b", "${Betrag &lt; 1000}")
    body += _flow("4", "g", "c", "${Betrag &gt;= 1000}")
    body += _flow("5", "b", "d") + _flow("6", "c", "d") + _flow("7", "d", "e")

    schema = import_bpmn(_doc(body), schema_id="foreign")

    decision = schema.xor_decisions["g"]
    assert decision.kind is XorDecisionKind.THRESHOLD
    assert [b.upper for b in decision.branches] == [1000, None]
    joins = [n for n in schema.nodes.values() if n.type is NodeType.XOR_JOIN]
    assert len(joins) == 1  # the implicit merge before "d" became an explicit join


def test_implicit_parallel_split_and_merge_become_an_and_block() -> None:
    body = _nodes(s="startEvent", a="task", b="task", c="task", d="task", e="endEvent")
    body += _flow("1", "s", "a") + _flow("2", "a", "b") + _flow("3", "a", "c")
    body += _flow("4", "b", "d") + _flow("5", "c", "d") + _flow("6", "d", "e")

    schema = import_bpmn(_doc(body), schema_id="implicit-and")

    kinds = sorted(
        n.type.value for n in schema.nodes.values() if "SPLIT" in n.type or "JOIN" in n.type
    )
    assert kinds == ["AND_JOIN", "AND_SPLIT"]


# --- still rejected (regression against the validation's defect set) ----------


def _gateway_block(split: str, join: str) -> str:
    body = _nodes(s="startEvent", g=split, b="task", c="task", j=join, e="endEvent")
    return body + _flow("1", "s", "g") + _flow("2", "g", "b") + _flow("3", "g", "c") \
        + _flow("4", "b", "j") + _flow("5", "c", "j") + _flow("6", "j", "e")


@pytest.mark.parametrize(
    ("name", "body", "rule"),
    [
        ("XOR split, AND join", _gateway_block("exclusiveGateway", "parallelGateway"), "K"),
        ("AND split, XOR join", _gateway_block("parallelGateway", "exclusiveGateway"), "K1"),
        ("two starts",
         _nodes(s="startEvent", t="startEvent", a="task", e="endEvent")
         + _flow("1", "s", "a") + _flow("2", "t", "a") + _flow("3", "a", "e"), "K2"),
        ("dead end",
         _nodes(s="startEvent", a="task", b="task", e="endEvent")
         + _flow("1", "s", "a") + _flow("2", "a", "b") + _flow("3", "a", "e"), "K"),
        ("unreachable step",
         _nodes(s="startEvent", a="task", x="task", e="endEvent")
         + _flow("1", "s", "a") + _flow("2", "a", "e") + _flow("3", "x", "e"), "K"),
        ("raw cycle",
         _nodes(s="startEvent", a="task", b="task", e="endEvent")
         + _flow("1", "s", "a") + _flow("2", "a", "b") + _flow("3", "b", "a")
         + _flow("4", "b", "e"), "K"),
    ],
)
def test_broken_models_stay_rejected(name: str, body: str, rule: str) -> None:
    with pytest.raises((CorrectnessError, BpmnError)) as exc:
        import_bpmn(_doc(body), schema_id="broken")
    if isinstance(exc.value, CorrectnessError):
        assert any(f.rule.startswith(rule) for f in exc.value.findings), (name, exc.value.findings)


def test_inclusive_gateway_stays_unsupported() -> None:
    body = _nodes(s="startEvent", g="inclusiveGateway", e="endEvent")
    body += _flow("1", "s", "g") + _flow("2", "g", "e")
    with pytest.raises(BpmnError, match="inclusiveGateway"):
        import_bpmn(_doc(body), schema_id="or")


def test_threshold_gap_is_not_guessed_away() -> None:
    body = _nodes(s="startEvent", a="task", g="exclusiveGateway", b="task", c="task",
                  j="exclusiveGateway", e="endEvent")
    body += _flow("1", "s", "a") + _flow("2", "a", "g")
    body += _flow("3", "g", "b", "Betrag &lt; 500") + _flow("4", "g", "c", "Betrag &gt;= 1000")
    body += _flow("5", "b", "j") + _flow("6", "c", "j") + _flow("7", "j", "e")
    with pytest.raises(BpmnError) as exc:
        import_bpmn(_doc(body), schema_id="gap")
    assert exc.value.code == "BPMN.condition-gap"
    assert exc.value.params == {"element": "Betrag", "from": "500", "to": "1000", "step": "c"}


# --- bounds the partition cannot express are refused with their own code -------
#
# A ProcWorks threshold cell is half-open: ``x < n`` / ``n <= x < m`` / ``x >= n``,
# the boundary value always belongs to the upper branch. ``x > n`` / ``x <= n``
# put it into the *lower* branch. The import does not convert (that would move
# the boundary value silently into another branch); it names the condition and
# the notation it understands instead of the generic "no decision".


def _decision_doc(*conditions: str) -> str:
    """A start -> task -> XOR split with one branch per condition -> join -> end."""

    targets = [f"b{i}" for i in range(len(conditions))]
    body = _nodes(s="startEvent", a="task", g="exclusiveGateway",
                  **{t: "task" for t in targets}, j="exclusiveGateway", e="endEvent")
    body += _flow("1", "s", "a") + _flow("2", "a", "g")
    for i, (target, cond) in enumerate(zip(targets, conditions, strict=True)):
        body += _flow(f"c{i}", "g", target, cond) + _flow(f"m{i}", target, "j")
    return _doc(body + _flow("9", "j", "e"))


@pytest.mark.parametrize(
    ("conditions", "offending"),
    [
        (("${betrag &gt; 1000}", "${betrag &lt;= 1000}"), "${betrag &gt; 1000}"),
        (("betrag &lt;= 1000", "betrag &gt;= 1000"), "betrag &lt;= 1000"),
        (("betrag &lt; 1000", "betrag &gt; 1000"), "betrag &gt; 1000"),
        (("${betrag gt 1000}", "${betrag le 1000}"), "${betrag gt 1000}"),
        (("1000 &lt; betrag", "1000 &gt;= betrag"), "1000 &lt; betrag"),
        (("1000 &gt; betrag", "1000 &lt;= betrag"), "1000 &gt; betrag"),
        (("betrag &lt; 0", "0 &lt;= betrag &lt;= 10", "betrag &gt;= 10"),
         "0 &lt;= betrag &lt;= 10"),
        (("betrag &lt; 0", "0 &lt; betrag &lt; 10", "betrag &gt;= 10"),
         "0 &lt; betrag &lt; 10"),
        (("betrag &lt; 0", "10 &gt; betrag &gt;= 0", "betrag &gt;= 10"),
         "10 &gt; betrag &gt;= 0"),
    ],
)
def test_lower_inclusive_bounds_are_refused_with_their_own_code(
    conditions: tuple[str, ...], offending: str
) -> None:
    with pytest.raises(BpmnError) as exc:
        import_bpmn(_decision_doc(*conditions), schema_id="bounds")

    assert exc.value.code == "BPMN.condition-operator"
    expected = offending.replace("&gt;", ">").replace("&lt;", "<")
    assert exc.value.params["condition"] == expected
    index = conditions.index(offending)
    assert exc.value.params["step"] == f"b{index}"  # the branch it guards


def test_half_open_bounds_still_import_into_a_partition() -> None:
    schema = import_bpmn(
        _decision_doc("betrag &lt; 1000", "1000 &lt;= betrag &lt; 5000", "betrag &gt;= 5000"),
        schema_id="half-open",
    )

    decision = schema.xor_decisions["g"]
    assert decision.kind is XorDecisionKind.THRESHOLD
    assert [b.upper for b in decision.branches] == [1000, 5000, None]


@pytest.mark.parametrize(
    ("conditions", "code", "params"),
    [
        (("betrag &lt; 1000", "betrag &gt;= 2000"), "BPMN.condition-gap",
         {"element": "betrag", "from": "1000", "to": "2000", "step": "b1"}),
        (("betrag &gt;= 1000", "betrag &gt;= 2000"), "BPMN.condition-gap",
         {"element": "betrag", "from": "", "to": "1000", "step": "b0"}),
        (("betrag &lt; 2000", "betrag &gt;= 1000"), "BPMN.condition-overlap",
         {"element": "betrag", "from": "1000", "to": "2000", "step": "b1"}),
        (("betrag &lt; 1000", "betrag &lt; 2000", "betrag &gt;= 2000"), "BPMN.condition-overlap",
         {"element": "betrag", "from": "", "to": "1000", "step": "b1"}),
        (("betrag &lt; 0", "0 &lt;= betrag &lt; 10.5", "5 &lt;= betrag &lt; 20", "betrag &gt;= 20"),
         "BPMN.condition-overlap",
         {"element": "betrag", "from": "5", "to": "10.5", "step": "b2"}),
    ],
    ids=["gap-between", "gap-below", "overlap", "overlap-below", "overlap-middle"],
)
def test_gap_and_overlap_between_bounds_are_named(
    conditions: tuple[str, ...], code: str, params: dict[str, str]
) -> None:
    """Conditions that do not tile the number line used to end in the generic
    "has no condition to choose a branch by" -- although every flow had one."""

    with pytest.raises(BpmnError) as exc:
        import_bpmn(_decision_doc(*conditions), schema_id="tiling")

    assert exc.value.code == code
    assert exc.value.params == params


@pytest.mark.parametrize(
    ("conditions", "params"),
    [
        (("betrag &lt; 1000", "2000 &lt;= betrag &lt; 1000", "betrag &gt;= 1000"),
         {"element": "betrag", "from": "2000", "to": "1000", "step": "b1"}),
        (("betrag &lt; 1000", "1000 &lt;= betrag &lt; 1000", "betrag &gt;= 1000"),
         {"element": "betrag", "from": "1000", "to": "1000", "step": "b1"}),
    ],
    ids=["vertauscht", "gleich"],
)
def test_a_branch_whose_range_is_empty_is_named_with_both_bounds(
    conditions: tuple[str, ...], params: dict[str, str]
) -> None:
    """``2000 <= x < 1000`` takes no value at all. Compared with its
    neighbours it looked like an overlap "from 2000 to 1000" -- a range that
    does not exist. It is refused for what it is, with both bounds as written."""

    with pytest.raises(BpmnError) as exc:
        import_bpmn(_decision_doc(*conditions), schema_id="empty-range")

    assert exc.value.code == "BPMN.condition-empty"
    assert exc.value.params == params


def test_a_bounded_last_branch_keeps_its_validator_reason() -> None:
    """A gap above the last bound was already named (K7) -- unchanged."""

    with pytest.raises(CorrectnessError) as exc:
        import_bpmn(_decision_doc("betrag &lt; 1000", "1000 &lt;= betrag &lt; 2000"),
                    schema_id="last")
    assert "K7.threshold-last-unbounded" in {f.code for f in exc.value.findings}


def test_comparison_without_a_number_keeps_the_generic_refusal() -> None:
    """Rewriting ``>`` would not help here -- a variable bound is not mappable at
    all, so the import must not suggest it (K7 decides as before)."""

    with pytest.raises(CorrectnessError) as exc:
        import_bpmn(_decision_doc("betrag &gt; limit", "betrag &lt;= limit"), schema_id="var")
    assert ("K7", "K7.no-decision") in {(f.rule, f.code) for f in exc.value.findings}


def test_conditions_are_only_captions_when_the_extension_decides() -> None:
    """With the ProcWorks extension the structured decision is the source of
    truth -- a caption with ``>`` beside it is never parsed, never refused."""

    urlaub = _build_urlaubsantrag(_build_org())
    xml = re.sub(
        r"<bpmn:conditionExpression([^>]*)>[^<]*</bpmn:conditionExpression>",
        r"<bpmn:conditionExpression\1>tage &gt; 10</bpmn:conditionExpression>",
        export_bpmn(urlaub),
    )
    assert "tage &gt; 10" in xml

    schema = import_bpmn(xml, schema_id="captions")

    assert schema.xor_decisions  # taken from the extension


@needs_node
def test_the_catalog_names_the_range(tmp_path: Path) -> None:
    texts = run_app_js(
        "return ["
        "findingText({code: 'BPMN.condition-gap', params: {element: 'Betrag', from: '1000',"
        " to: '2000', step: 'Groß'}}),"
        "findingText({code: 'BPMN.condition-overlap', params: {element: 'Betrag', from: '',"
        " to: '1000', step: 'Mittel'}}),"
        "findingText({code: 'BPMN.condition-empty', params: {element: 'Betrag', from: '2000',"
        " to: '1000', step: 'Mittel'}}),"
        "];",
        tmp_path,
    )
    assert texts[0].startswith(
        "Die Bedingungen der Verzweigung über „Betrag“ lassen eine Lücke: "
        "Werte ab 1000 und unter 2000 führen in keinen Zweig (ab „Groß“)."
    )
    assert "Werte unter 1000 passen zu mehreren Zweigen" in texts[1]
    assert texts[2].startswith(
        "Die Bedingung auf dem Weg zu „Mittel“ lässt keinen Wert zu: "
        "Die Untergrenze 2000 liegt nicht unter der Obergrenze 1000."
    )
