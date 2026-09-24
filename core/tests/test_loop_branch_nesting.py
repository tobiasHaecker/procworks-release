# SPDX-License-Identifier: BUSL-1.1
"""K6a mutual nesting: a loop block must never straddle a branch block.

K1 pairs splits with joins, K6a pairs LOOP_START with LOOP_END -- and each
pairing treats the other kind as ordinary serial nodes. Until 2026-09-24 that
let a loop that *crosses* a branch block pass both, via ``POST /bpmn-import``:
the model could be released, and an instance taking the branch without the
loop start either stuck on an unwritten discriminator or spun forever inside
``execution._advance`` (a request that never returns). The operations never
build this shape; the import does, because it maps a foreign graph as is.

The tests pin the *reason* (``code == "K6.crosses-branch"``), not just any
rejection, and keep two properly nested counter-examples so the check cannot
silently grow into rejecting legitimate models. The engine and simulation
tests cover models stored *before* the fix: they must fail fast, not hang.
"""

from __future__ import annotations

import json
import signal
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from procworks import bpmn as bpmn_mod
from procworks import execution, simulation
from procworks.bpmn import BPMN_NS, import_bpmn
from procworks.model import (
    Agent,
    LifecycleState,
    OrgModel,
    ProcessSchema,
    Role,
    StaffRule,
    StaffRuleKind,
)
from procworks.validator import CorrectnessError, validate

#: Data layer shared by every variant: ``t0`` writes the XOR discriminator ``d``
#: and a stale value of the loop discriminator ``r``; the listed body steps
#: write ``r`` again (K6c).
_ELEMENTS = [
    {"id": "d", "name": "Weg A", "data_type": "BOOLEAN"},
    {"id": "r", "name": "Wiederholen", "data_type": "BOOLEAN"},
]


def _xml(
    tasks: list[str],
    gateways: list[str],
    flows: list[tuple[str, str]],
    *,
    parallel: bool = False,
    loop_writers: tuple[str, ...] = ("a",),
    xor_branches: tuple[str, str] | None = None,
) -> str:
    """BPMN document for one variant; the loop-back flow is ``le -> ls``.

    ``gateways`` are the split/join ids (``xs``/``xj`` or ``ps``/``pj``);
    ``ls``/``le`` are always the loop pair. ``xor_branches`` names the first
    node of the ``true`` and ``false`` branch of ``xs``.
    """

    accesses = [
        {"node_id": "t0", "element_id": "d", "mode": "WRITE"},
        {"node_id": "t0", "element_id": "r", "mode": "WRITE"},
    ] + [{"node_id": w, "element_id": "r", "mode": "WRITE"} for w in loop_writers]
    ext: dict[str, object] = {
        "data_elements": _ELEMENTS,
        "data_accesses": accesses,
        "loop_decisions": {"le": {"discriminator": "r"}},
    }
    if xor_branches is not None:
        yes, no = xor_branches
        ext["xor_decisions"] = {
            "xs": {
                "discriminator": "d",
                "kind": "BOOLEAN",
                "branches": [
                    {"target": yes, "bool_value": True},
                    {"target": no, "bool_value": False},
                ],
            }
        }
    kind = "parallelGateway" if parallel else "exclusiveGateway"
    nodes = "".join(f'<task id="{t}" name="{t}"/>' for t in ["t0", *tasks])
    nodes += "".join(f'<{kind} id="{g}"/>' for g in gateways)
    nodes += '<exclusiveGateway id="ls"/><exclusiveGateway id="le"/>'
    all_flows = [("start", "t0"), *flows, ("le", "ls")]
    seq = "".join(
        f'<sequenceFlow id="f{i}" sourceRef="{s}" targetRef="{t}"/>'
        for i, (s, t) in enumerate(all_flows)
    )
    return (
        f'<?xml version="1.0"?><definitions xmlns="{BPMN_NS}"><process id="p" name="P">'
        f'<startEvent id="start"/>{nodes}<endEvent id="end"/>{seq}'
        '<extensionElements><model xmlns="https://procworks/bpmn/ext">'
        f"{json.dumps(ext)}</model></extensionElements></process></definitions>"
    )


# Loop start inside the XOR branch, loop end behind the join.
_START_IN_BRANCH = _xml(
    ["a", "b"],
    ["xs", "xj"],
    [("t0", "xs"), ("xs", "ls"), ("xs", "b"), ("ls", "a"), ("a", "xj"),
     ("b", "xj"), ("xj", "le"), ("le", "end")],
    xor_branches=("ls", "b"),
)
# Loop start before the split, loop end inside one branch.
_END_IN_BRANCH = _xml(
    ["a", "b"],
    ["xs", "xj"],
    [("t0", "ls"), ("ls", "xs"), ("xs", "a"), ("xs", "b"), ("a", "le"),
     ("le", "xj"), ("b", "xj"), ("xj", "end")],
    xor_branches=("a", "b"),
)
# The same straddling with a parallel block.
_START_IN_AND_BRANCH = _xml(
    ["a", "b"],
    ["ps", "pj"],
    [("t0", "ps"), ("ps", "ls"), ("ps", "b"), ("ls", "a"), ("a", "pj"),
     ("b", "pj"), ("pj", "le"), ("le", "end")],
    parallel=True,
)


@pytest.mark.parametrize(
    "xml",
    [_START_IN_BRANCH, _END_IN_BRANCH, _START_IN_AND_BRANCH],
    ids=["start-in-branch", "end-in-branch", "start-in-and-branch"],
)
def test_import_rejects_a_loop_that_crosses_a_branch_block(xml: str) -> None:
    with pytest.raises(CorrectnessError) as exc:
        import_bpmn(xml)
    codes = {f.code for f in exc.value.findings}
    assert codes == {"K6.crosses-branch"}, exc.value.findings


def test_loop_inside_one_branch_still_validates() -> None:
    """Gegenprobe: a loop lying wholly in one XOR branch is block-structured."""

    xml = _xml(
        ["a", "b"],
        ["xs", "xj"],
        [("t0", "xs"), ("xs", "ls"), ("xs", "b"), ("ls", "a"), ("a", "le"),
         ("le", "xj"), ("b", "xj"), ("xj", "end")],
        xor_branches=("ls", "b"),
    )
    assert validate(import_bpmn(xml)) == []


def test_loop_enclosing_a_whole_branch_block_still_validates() -> None:
    """Gegenprobe: a loop around a complete XOR block is block-structured."""

    xml = _xml(
        ["a", "b"],
        ["xs", "xj"],
        [("t0", "ls"), ("ls", "xs"), ("xs", "a"), ("xs", "b"), ("a", "xj"),
         ("b", "xj"), ("xj", "le"), ("le", "end")],
        loop_writers=("a", "b"),
        xor_branches=("a", "b"),
    )
    assert validate(import_bpmn(xml)) == []


@contextmanager
def _deadline(seconds: int) -> Iterator[None]:
    """Fail the test instead of hanging the suite if the engine loops forever."""

    def _timeout(*_: object) -> None:
        raise AssertionError(f"did not return within {seconds}s (endless loop)")

    previous = signal.signal(signal.SIGALRM, _timeout)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def _legacy_crossed(monkeypatch: pytest.MonkeyPatch) -> ProcessSchema:
    """The crossed model as it could be stored before K6a checked nesting.

    The import is run with validation switched off -- exactly the state of a
    model imported and released on an older version. Staff rules are set
    directly, because every operation now (rightly) refuses to touch it.
    """

    monkeypatch.setattr(bpmn_mod, "raise_if_invalid", lambda schema, resolver=None: schema)
    schema = import_bpmn(_START_IN_BRANCH)
    schema.org_model = OrgModel(
        roles={"r": Role(id="r", name="Rolle")},
        agents={"x": Agent(id="x", name="X", role_ids=["r"])},
    )
    for node_id in ("t0", "a", "b"):
        schema.staff_rules[node_id] = StaffRule(kind=StaffRuleKind.ROLE, ref="r")
    schema.lifecycle_state = LifecycleState.RELEASED
    return schema


def test_engine_stops_a_stored_crossed_loop_instead_of_hanging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Taking the branch without the loop start used to spin ``_advance`` forever."""

    schema = _legacy_crossed(monkeypatch)
    instance = execution.instantiate(schema)
    instance = execution.complete_activity(instance, schema, "t0", {"d": False, "r": True})
    with _deadline(5), pytest.raises(execution.ExecutionError, match="repeated without any work"):
        execution.complete_activity(instance, schema, "b", {})
    # The caller's instance is untouched: the step is still waiting for work.
    assert instance.node_states["b"].value == "ACTIVATED"


def test_simulation_of_a_stored_crossed_loop_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The simulation ran into the same endless loop; it must end with a finding."""

    schema = _legacy_crossed(monkeypatch)
    with _deadline(5):
        result = simulation.simulate(schema, {"d": False, "r": True})
    assert "repeated without any work" in json.dumps(result.model_dump(mode="json"))
