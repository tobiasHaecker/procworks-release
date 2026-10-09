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

import pytest

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
    with pytest.raises(CorrectnessError) as exc:
        import_bpmn(_doc(body), schema_id="gap")
    assert ("K7", "K7.no-decision") in {(f.rule, f.code) for f in exc.value.findings}
