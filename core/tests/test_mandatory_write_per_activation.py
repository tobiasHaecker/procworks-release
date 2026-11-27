# SPDX-License-Identifier: BUSL-1.1
"""Once a loop repeats a step, its mandatory write is supplied by *this* round.

The completion check (``validator.completion_value_findings``) counted every
value the case had stored. In the second round of a loop the step's mandatory
values were therefore always "there" -- left over from round one -- and a
completion without any value passed:

* the loop's discriminator kept its old value, so a loop whose completions
  send nothing repeated without end (each round decides on fresh data only on
  paper);
* a following step or decision read the value of the previous round as if the
  step had just written it.

The rule now (``validator.is_repeated_activation``):

* in the **first activation** of a step every stored value counts, as before
  (start data, values of earlier steps, corrections before the step became
  ready) -- plus what the completion brings;
* once the **step itself ran before** (a loop repeats it -- not merely the
  loop around it: a branch first taken in a later round runs there for the
  first time), a mandatory value counts only when the completion brings it,
  or when a data correction (``PUT …/data``) set it while this activation of
  the step was open. The value of the earlier round
  stays stored and readable; the step must only write it anew.

The same for every completion path -- web, ``/v1``, supervision, external task
(pull and push).
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from staffing import staff_via_api

import procworks.api as api_module
from procworks import (
    NodeState,
    ProcessSchema,
    complete_activity,
    instantiate,
    record_data_correction,
)
from procworks.api import app
from procworks.auth_password import InMemoryCredentialStore, PasswordAuthBackend, hash_password

client = TestClient(app)

_topics = (f"per-activation-topic-{n}" for n in itertools.count(1))


def _loop_schema(
    name: str, *, topic: str | None = None, push_ref: str | None = None
) -> tuple[str, str]:
    """Released REPEAT-UNTIL loop: body "Erfassen" repeats while ``nochmal`` is true.

    The body mandatorily writes ``nochmal`` (BOOLEAN, the discriminator) and
    ``x`` (STRING). With ``topic`` the body is an automatic external task
    (pull), with ``push_ref`` an automatic ``HTTP_PUSH`` step.
    Returns ``(schema_id, body node id)``.
    """

    sid = client.post("/schemas", json={"name": name}).json()["id"]
    for elem, dtype in (("nochmal", "BOOLEAN"), ("x", "STRING")):
        made = client.post(
            f"/schemas/{sid}/data-elements",
            json={"name": elem, "data_type": dtype, "element_id": elem},
        )
        assert made.status_code in (200, 201), made.text
    looped = client.post(
        f"/schemas/{sid}/loop-insert",
        json={"label": "Erfassen", "after_node_id": "start", "discriminator": "nochmal"},
    )
    assert looped.status_code == 200, looped.text
    body = next(
        nid for nid, n in looped.json()["nodes"].items() if n["label"] == "Erfassen"
    )
    for elem in ("nochmal", "x"):
        bound = client.post(
            f"/schemas/{sid}/data-access",
            json={"node_id": body, "element_id": elem, "mode": "WRITE", "mandatory": True},
        )
        assert bound.status_code == 200 or "already" in bound.text, bound.text
    if topic is not None or push_ref is not None:
        client.post(
            f"/schemas/{sid}/service", json={"node_id": body, "name": "W", "automatic": True}
        )
        automation: dict[str, object] = (
            {"automation": "EXTERNAL_TASK", "topic": topic}
            if topic is not None
            else {"automation": "HTTP_PUSH", "endpoint_ref": push_ref}
        )
        auto = client.post(
            f"/schemas/{sid}/automation", json={"node_id": body, **automation}
        )
        assert auto.status_code == 200, auto.text
    else:
        staff_via_api(client, sid)
    released = client.post(f"/schemas/{sid}/release")
    assert released.status_code == 200, released.text
    return sid, body


def _start(sid: str) -> str:
    resp = client.post(f"/v1/schemas/{sid}/instances")
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


def _web(iid: str, node: str, data: dict[str, object]) -> Any:
    return client.post(f"/instances/{iid}/complete", json={"node_id": node, "data": data})


def _v1(iid: str, node: str, data: dict[str, object]) -> Any:
    return client.post(f"/v1/instances/{iid}/nodes/{node}/complete", json={"data": data})


def _missing(resp: Any) -> list[str]:
    """The elements a refusal names as missing mandatory writes (``D1.write-missing``)."""

    assert resp.status_code == 422, resp.text
    findings = resp.json()["detail"]["findings"]
    assert {f["code"] for f in findings} == {"D1.write-missing"}, findings
    return [f["params"]["element"] for f in findings]


def _case(iid: str) -> dict[str, Any]:
    return dict(client.get(f"/instances/{iid}").json())


# --- loops: every round writes its mandatory values ---------------------------------


def test_round_two_without_values_is_refused_on_every_human_path() -> None:
    sid, body = _loop_schema("Runde zwei ohne Werte")
    for complete in (_web, _v1):
        iid = _start(sid)
        assert complete(iid, body, {"nochmal": True, "x": "Runde-1"}).status_code == 200

        refused = complete(iid, body, {})

        assert _missing(refused) == ["nochmal", "x"]
        case = _case(iid)
        assert case["node_states"][body] in ("ACTIVATED", "RUNNING")
        # Old values stay stored and readable -- they just do not count anew.
        assert case["data_values"] == {"nochmal": True, "x": "Runde-1"}


def test_a_value_left_over_from_the_last_round_does_not_count() -> None:
    sid, body = _loop_schema("Runde zwei ohne x")
    iid = _start(sid)
    assert _web(iid, body, {"nochmal": True, "x": "Runde-1"}).status_code == 200

    refused = _web(iid, body, {"nochmal": False})

    assert _missing(refused) == ["x"]
    assert _web(iid, body, {"nochmal": False, "x": "Runde-2"}).status_code == 200
    assert _case(iid)["state"] == "COMPLETED"
    assert _case(iid)["data_values"]["x"] == "Runde-2"


def test_a_loop_whose_completions_send_nothing_cannot_spin() -> None:
    sid, body = _loop_schema("Keine Endlosschleife")
    iid = _start(sid)
    assert _v1(iid, body, {"nochmal": True, "x": "Runde-1"}).status_code == 200

    answers = [_v1(iid, body, {}) for _ in range(3)]

    assert [_missing(a) for a in answers] == [["nochmal", "x"]] * 3
    assert list(_case(iid)["loop_iterations"].values()) == [1]  # never repeated again


def test_a_correction_in_this_round_counts() -> None:
    sid, body = _loop_schema("Korrektur in Runde zwei")
    iid = _start(sid)
    assert _web(iid, body, {"nochmal": True, "x": "Runde-1"}).status_code == 200

    corrected = client.put(f"/instances/{iid}/data", json={"values": {"x": "korrigiert"}})
    assert corrected.status_code == 200, corrected.text
    done = _web(iid, body, {"nochmal": False})

    assert done.status_code == 200, done.text
    assert _case(iid)["data_values"]["x"] == "korrigiert"


def test_a_correction_of_the_last_round_does_not_carry_over() -> None:
    sid, body = _loop_schema("Korrektur aus Runde eins")
    iid = _start(sid)
    corrected = client.put(f"/instances/{iid}/data", json={"values": {"x": "Runde-1"}})
    assert corrected.status_code == 200, corrected.text
    assert _web(iid, body, {"nochmal": True}).status_code == 200  # x from the correction

    refused = _web(iid, body, {"nochmal": False})

    assert _missing(refused) == ["x"]


def test_the_same_value_corrected_again_counts_as_set_now() -> None:
    """A correction that confirms the stored value is still a value set in this round."""

    sid, body = _loop_schema("Gleicher Wert bestätigt")
    iid = _start(sid)
    assert _web(iid, body, {"nochmal": True, "x": "gleich"}).status_code == 200

    assert client.put(f"/instances/{iid}/data", json={"values": {"x": "gleich"}}).status_code == 200

    assert _web(iid, body, {"nochmal": False}).status_code == 200


def test_an_external_task_in_round_two_must_report_its_outputs_again() -> None:
    topic = next(_topics)
    sid, _body = _loop_schema("Runde zwei extern", topic=topic)
    iid = _start(sid)

    def fetch() -> dict[str, Any]:
        tasks = client.post(
            "/v1/external-tasks/fetch-and-lock",
            json={"worker_id": "w", "topics": [topic], "lock_ms": 60_000},
        ).json()
        assert len(tasks) == 1, tasks
        return dict(tasks[0])

    first = fetch()
    assert client.post(
        f"/v1/external-tasks/{first['id']}/complete",
        json={"worker_id": "w", "variables": {"nochmal": True, "x": "Runde-1"}},
    ).status_code == 200
    second = fetch()

    refused = client.post(
        f"/v1/external-tasks/{second['id']}/complete", json={"worker_id": "w", "variables": {}}
    )

    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"]["code"] == "D1.write-missing"
    assert refused.json()["detail"]["params"]["element"] == "nochmal"
    assert _case(iid)["state"] == "RUNNING"
    done = client.post(
        f"/v1/external-tasks/{second['id']}/complete",
        json={"worker_id": "w", "variables": {"nochmal": False, "x": "Runde-2"}},
    )
    assert done.status_code == 200, done.text
    assert _case(iid)["state"] == "COMPLETED"


# --- without a loop -------------------------------------------------------------------


def _two_steps(name: str) -> tuple[str, str, str]:
    """Released "Vorbereiten" → "Erfassen"; "Erfassen" mandatorily writes ``x``."""

    sid = client.post("/schemas", json={"name": name}).json()["id"]
    first = client.post(
        f"/schemas/{sid}/serial-insert", json={"label": "Vorbereiten", "after_node_id": "start"}
    ).json()
    prep = next(nid for nid, n in first["nodes"].items() if n["label"] == "Vorbereiten")
    second = client.post(
        f"/schemas/{sid}/serial-insert", json={"label": "Erfassen", "after_node_id": prep}
    ).json()
    act = next(nid for nid, n in second["nodes"].items() if n["label"] == "Erfassen")
    client.post(
        f"/schemas/{sid}/data-elements",
        json={"name": "x", "data_type": "STRING", "element_id": "x"},
    )
    bound = client.post(
        f"/schemas/{sid}/data-access",
        json={"node_id": act, "element_id": "x", "mode": "WRITE", "mandatory": True},
    )
    assert bound.status_code == 200, bound.text
    staff_via_api(client, sid)
    assert client.post(f"/schemas/{sid}/release").status_code == 200
    return sid, prep, act


def test_a_correction_while_the_step_is_open_counts_without_a_loop() -> None:
    sid, prep, act = _two_steps("Ohne Schleife – Korrektur offen")
    iid = _start(sid)
    assert _web(iid, prep, {}).status_code == 200

    assert client.put(f"/instances/{iid}/data", json={"values": {"x": "vorab"}}).status_code == 200

    assert _v1(iid, act, {}).status_code == 200
    assert _case(iid)["data_values"] == {"x": "vorab"}


def test_in_the_first_activation_a_value_set_before_the_step_became_ready_counts() -> None:
    """Without a repetition every stored value counts, as before (start data, earlier steps)."""

    sid, prep, act = _two_steps("Ohne Schleife – vorher gesetzt")
    iid = _start(sid)
    early = client.put(f"/instances/{iid}/data", json={"values": {"x": "vorher"}})
    assert early.status_code == 200, early.text
    assert _web(iid, prep, {}).status_code == 200

    done = _v1(iid, act, {})

    assert done.status_code == 200, done.text
    assert _case(iid)["data_values"] == {"x": "vorher"}


def _loop_with_two_steps(name: str) -> tuple[str, str, str]:
    """Loop body "Erfassen" → "Prüfen"; "Prüfen" mandatorily writes ``y``.

    "Erfassen" writes the discriminator ``nochmal`` and ``x`` (see
    :func:`_loop_schema`). Returns ``(schema_id, "Erfassen", "Prüfen")``.
    """

    sid = client.post("/schemas", json={"name": name}).json()["id"]
    for elem, dtype in (("nochmal", "BOOLEAN"), ("x", "STRING"), ("y", "STRING")):
        client.post(
            f"/schemas/{sid}/data-elements",
            json={"name": elem, "data_type": dtype, "element_id": elem},
        )
    looped = client.post(
        f"/schemas/{sid}/loop-insert",
        json={"label": "Erfassen", "after_node_id": "start", "discriminator": "nochmal"},
    ).json()
    first = next(nid for nid, n in looped["nodes"].items() if n["label"] == "Erfassen")
    inserted = client.post(
        f"/schemas/{sid}/serial-insert", json={"label": "Prüfen", "after_node_id": first}
    )
    assert inserted.status_code == 200, inserted.text
    second = next(
        nid for nid, n in inserted.json()["nodes"].items() if n["label"] == "Prüfen"
    )
    for node, elem in ((first, "x"), (second, "y")):
        bound = client.post(
            f"/schemas/{sid}/data-access",
            json={"node_id": node, "element_id": elem, "mode": "WRITE", "mandatory": True},
        )
        assert bound.status_code == 200, bound.text
    staff_via_api(client, sid)
    released = client.post(f"/schemas/{sid}/release")
    assert released.status_code == 200, released.text
    return sid, first, second


def test_in_the_first_round_of_a_loop_a_value_set_before_the_step_was_ready_counts() -> None:
    """Round 1: ``y`` corrected while only "Erfassen" was open still supplies "Prüfen"."""

    sid, first, second = _loop_with_two_steps("Runde eins – vorher gesetzt")
    iid = _start(sid)
    assert client.put(f"/instances/{iid}/data", json={"values": {"y": "vorab"}}).status_code == 200
    assert _web(iid, first, {"nochmal": False, "x": "1"}).status_code == 200

    done = _web(iid, second, {})

    assert done.status_code == 200, done.text
    assert _case(iid)["state"] == "COMPLETED"


def test_in_round_two_a_value_from_the_last_round_set_before_the_step_was_ready_does_not_count(
) -> None:
    """Round 2: ``y`` was last written in round 1; "Prüfen" is not open yet while
    "Erfassen" runs -- nothing supplies ``y`` for this round."""

    sid, first, second = _loop_with_two_steps("Runde zwei – vor der Bereitschaft")
    iid = _start(sid)
    assert _web(iid, first, {"nochmal": True, "x": "1"}).status_code == 200
    assert _web(iid, second, {"y": "Runde-1"}).status_code == 200  # repeats
    assert _web(iid, first, {"nochmal": False, "x": "2"}).status_code == 200

    refused = _web(iid, second, {})

    assert _missing(refused) == ["y"]
    assert _case(iid)["data_values"]["y"] == "Runde-1"


def test_in_round_two_a_correction_before_the_step_was_ready_does_not_count() -> None:
    sid, first, second = _loop_with_two_steps("Runde zwei – Korrektur zu früh")
    iid = _start(sid)
    assert _web(iid, first, {"nochmal": True, "x": "1"}).status_code == 200
    assert _web(iid, second, {"y": "Runde-1"}).status_code == 200
    # Round 2, only "Erfassen" open: the correction is not "Prüfen"'s activation.
    assert client.put(f"/instances/{iid}/data", json={"values": {"y": "früh"}}).status_code == 200
    assert _web(iid, first, {"nochmal": False, "x": "2"}).status_code == 200

    assert _missing(_web(iid, second, {})) == ["y"]


def test_in_round_two_a_correction_while_the_step_is_open_counts() -> None:
    sid, first, second = _loop_with_two_steps("Runde zwei – Korrektur offen")
    iid = _start(sid)
    assert _web(iid, first, {"nochmal": True, "x": "1"}).status_code == 200
    assert _web(iid, second, {"y": "Runde-1"}).status_code == 200
    assert _web(iid, first, {"nochmal": False, "x": "2"}).status_code == 200
    assert client.put(f"/instances/{iid}/data", json={"values": {"y": "jetzt"}}).status_code == 200

    done = _web(iid, second, {})

    assert done.status_code == 200, done.text
    assert _case(iid)["state"] == "COMPLETED"


def _loop_with_a_branch(name: str) -> tuple[str, str, str]:
    """Loop body "Erfassen" → XOR on ``weg``: ``False`` → "Kurz", ``True`` → "Nachtragen".

    "Erfassen" mandatorily writes ``nochmal`` (the loop's discriminator), ``x``
    and ``weg`` (the branch's discriminator); "Nachtragen" mandatorily writes
    ``z``. Returns ``(schema_id, "Erfassen", "Nachtragen")``.
    """

    sid = client.post("/schemas", json={"name": name}).json()["id"]
    for elem, dtype in (("nochmal", "BOOLEAN"), ("x", "STRING"), ("weg", "BOOLEAN"),
                        ("z", "STRING")):
        made = client.post(
            f"/schemas/{sid}/data-elements",
            json={"name": elem, "data_type": dtype, "element_id": elem},
        )
        assert made.status_code in (200, 201), made.text
    looped = client.post(
        f"/schemas/{sid}/loop-insert",
        json={"label": "Erfassen", "after_node_id": "start", "discriminator": "nochmal"},
    ).json()
    first = next(nid for nid, n in looped["nodes"].items() if n["label"] == "Erfassen")
    for elem in ("x", "weg"):
        bound = client.post(
            f"/schemas/{sid}/data-access",
            json={"node_id": first, "element_id": elem, "mode": "WRITE", "mandatory": True},
        )
        assert bound.status_code == 200, bound.text
    branched = client.post(
        f"/schemas/{sid}/conditional-insert",
        json={
            "after_node_id": first,
            "discriminator": "weg",
            "branches": [
                {"label": "Kurz", "bool_value": False},
                {"label": "Nachtragen", "bool_value": True},
            ],
        },
    )
    assert branched.status_code == 200, branched.text
    late = next(
        nid for nid, n in branched.json()["nodes"].items() if n["label"] == "Nachtragen"
    )
    bound = client.post(
        f"/schemas/{sid}/data-access",
        json={"node_id": late, "element_id": "z", "mode": "WRITE", "mandatory": True},
    )
    assert bound.status_code == 200, bound.text
    staff_via_api(client, sid)
    released = client.post(f"/schemas/{sid}/release")
    assert released.status_code == 200, released.text
    return sid, first, late


def _open_step(iid: str, label: str) -> str:
    """The id of the open step labelled ``label`` (the branch's step)."""

    case = _case(iid)
    schema = client.get(f"/schemas/{case['schema_id']}").json()
    return next(
        nid for nid, n in schema["nodes"].items()
        if n["label"] == label and case["node_states"][nid] in ("ACTIVATED", "RUNNING")
    )


def test_a_branch_step_first_active_in_round_two_is_in_its_first_activation() -> None:
    """Round 1 takes "Kurz"; "Nachtragen" runs for the first time in round 2.

    Its activation is no repetition: the value set in round 1 (before the
    step was ever ready) supplies its mandatory write, as for any first
    activation. That the loop around it repeated does not make the step
    repeated.
    """

    sid, first, late = _loop_with_a_branch("Zweig erst in Runde zwei")
    iid = _start(sid)
    assert client.put(f"/instances/{iid}/data", json={"values": {"z": "vorab"}}).status_code == 200
    assert _web(iid, first, {"nochmal": True, "x": "1", "weg": False}).status_code == 200
    assert _web(iid, _open_step(iid, "Kurz"), {}).status_code == 200  # repeats
    assert _web(iid, first, {"nochmal": False, "x": "2", "weg": True}).status_code == 200

    done = _web(iid, late, {})

    assert done.status_code == 200, done.text
    assert _case(iid)["state"] == "COMPLETED"
    assert _case(iid)["data_values"]["z"] == "vorab"


def test_a_branch_step_that_ran_in_round_one_needs_its_value_anew_in_round_three() -> None:
    """The same step on its second run: round 1 and round 3 take "Nachtragen"."""

    sid, first, late = _loop_with_a_branch("Zweig wiederholt")
    iid = _start(sid)
    assert _web(iid, first, {"nochmal": True, "x": "1", "weg": True}).status_code == 200
    assert _web(iid, late, {"z": "Runde-1"}).status_code == 200  # repeats
    assert _web(iid, first, {"nochmal": True, "x": "2", "weg": False}).status_code == 200
    assert _web(iid, _open_step(iid, "Kurz"), {}).status_code == 200  # repeats
    assert _web(iid, first, {"nochmal": False, "x": "3", "weg": True}).status_code == 200

    refused = _web(iid, late, {})

    assert _missing(refused) == ["z"]
    assert _web(iid, late, {"z": "Runde-3"}).status_code == 200


def _nested_loops(name: str) -> tuple[str, str, str]:
    """Outer loop over "Erfassen", an inner loop over "Innen" inside its body.

    "Erfassen" writes ``nochmal`` (outer discriminator) and ``x``; "Innen"
    mandatorily writes ``innen`` (inner discriminator) and ``w``.
    Returns ``(schema_id, "Erfassen", "Innen")``.
    """

    sid, first = _loop_schema_draft(name, ("innen", "BOOLEAN"), ("w", "STRING"))
    inner = client.post(
        f"/schemas/{sid}/loop-insert",
        json={"label": "Innen", "after_node_id": first, "discriminator": "innen"},
    )
    assert inner.status_code == 200, inner.text
    step = next(nid for nid, n in inner.json()["nodes"].items() if n["label"] == "Innen")
    for elem in ("innen", "w"):
        bound = client.post(
            f"/schemas/{sid}/data-access",
            json={"node_id": step, "element_id": elem, "mode": "WRITE", "mandatory": True},
        )
        assert bound.status_code == 200 or "already" in bound.text, bound.text
    staff_via_api(client, sid)
    released = client.post(f"/schemas/{sid}/release")
    assert released.status_code == 200, released.text
    return sid, first, step


def _loop_schema_draft(name: str, *extra: tuple[str, str]) -> tuple[str, str]:
    """The loop of :func:`_loop_schema` as a draft, with ``extra`` data elements."""

    sid = client.post("/schemas", json={"name": name}).json()["id"]
    for elem, dtype in (("nochmal", "BOOLEAN"), ("x", "STRING"), *extra):
        made = client.post(
            f"/schemas/{sid}/data-elements",
            json={"name": elem, "data_type": dtype, "element_id": elem},
        )
        assert made.status_code in (200, 201), made.text
    looped = client.post(
        f"/schemas/{sid}/loop-insert",
        json={"label": "Erfassen", "after_node_id": "start", "discriminator": "nochmal"},
    )
    assert looped.status_code == 200, looped.text
    body = next(nid for nid, n in looped.json()["nodes"].items() if n["label"] == "Erfassen")
    for elem in ("nochmal", "x"):
        bound = client.post(
            f"/schemas/{sid}/data-access",
            json={"node_id": body, "element_id": elem, "mode": "WRITE", "mandatory": True},
        )
        assert bound.status_code == 200 or "already" in bound.text, bound.text
    return sid, body


def test_nested_loops_the_inner_step_in_its_first_run_uses_stored_values() -> None:
    sid, first, inner = _nested_loops("Verschachtelt – erster Lauf")
    iid = _start(sid)
    assert client.put(
        f"/instances/{iid}/data", json={"values": {"innen": False, "w": "vorab"}}
    ).status_code == 200
    assert _web(iid, first, {"nochmal": False, "x": "1"}).status_code == 200

    done = _web(iid, inner, {})

    assert done.status_code == 200, done.text
    assert _case(iid)["state"] == "COMPLETED"


def test_nested_loops_the_inner_step_of_the_next_outer_round_is_a_repetition() -> None:
    """Outer round 2: the inner loop starts its first round, but "Innen" ran before."""

    sid, first, inner = _nested_loops("Verschachtelt – äußere Runde zwei")
    iid = _start(sid)
    assert _web(iid, first, {"nochmal": True, "x": "1"}).status_code == 200
    assert _web(iid, inner, {"innen": False, "w": "Runde-1"}).status_code == 200  # outer repeats
    assert _web(iid, first, {"nochmal": False, "x": "2"}).status_code == 200

    assert _missing(_web(iid, inner, {})) == ["innen", "w"]
    assert _web(iid, inner, {"innen": False, "w": "Runde-2"}).status_code == 200
    assert _case(iid)["state"] == "COMPLETED"


def test_claiming_and_returning_does_not_make_a_step_repeated() -> None:
    sid, body = _loop_schema("Übernehmen und Zurücklegen")
    iid = _start(sid)
    step = {"node_id": body, "agent_id": "a-test"}
    assert client.put(
        f"/instances/{iid}/data", json={"values": {"nochmal": True, "x": "vorab"}}
    ).status_code == 200
    assert client.post(f"/instances/{iid}/claim", json=step).status_code == 200
    assert client.post(f"/instances/{iid}/return", json=step).status_code == 200

    # Round 1, after a claim and a return: still the first activation.
    assert _web(iid, body, {}).status_code == 200  # repeats on the stored values
    assert client.post(f"/instances/{iid}/claim", json=step).status_code == 200
    assert client.post(f"/instances/{iid}/return", json=step).status_code == 200

    # Round 2, after a claim and a return: still a repetition.
    assert _missing(_web(iid, body, {})) == ["nochmal", "x"]


def _released_revision(sid: str) -> str:
    """A released revision of ``sid`` with the same content."""

    draft = client.post(f"/schemas/{sid}/revision", json={})
    assert draft.status_code == 200, draft.text
    rid = str(draft.json()["id"])
    released = client.post(f"/schemas/{rid}/release")
    assert released.status_code == 200, released.text
    return rid


def test_a_migrated_case_keeps_what_its_steps_ran() -> None:
    sid, body = _loop_schema("Migration in Runde zwei")
    iid = _start(sid)
    assert _web(iid, body, {"nochmal": True, "x": "Runde-1"}).status_code == 200
    rid = _released_revision(sid)

    migrated = client.post(f"/instances/{iid}/migrate", json={"target_schema_id": rid})

    assert migrated.status_code == 200, migrated.text
    assert migrated.json()["schema_id"] == rid
    assert _missing(_web(iid, body, {})) == ["nochmal", "x"]
    assert _web(iid, body, {"nochmal": False, "x": "Runde-2"}).status_code == 200


def test_a_migrated_case_in_the_first_round_still_uses_stored_values() -> None:
    sid, body = _loop_schema("Migration in Runde eins")
    iid = _start(sid)
    assert client.put(
        f"/instances/{iid}/data", json={"values": {"nochmal": False, "x": "vorab"}}
    ).status_code == 200
    rid = _released_revision(sid)
    assert client.post(
        f"/instances/{iid}/migrate", json={"target_schema_id": rid}
    ).status_code == 200

    assert _web(iid, body, {}).status_code == 200
    assert _case(iid)["state"] == "COMPLETED"


def test_an_ad_hoc_change_keeps_what_the_steps_ran() -> None:
    """Round 2 on the case's own variant: the body stays a repetition, the new
    step (no writes of its own) is in its first activation."""

    sid, body = _loop_schema("Ad-hoc in Runde zwei")
    iid = _start(sid)
    assert _web(iid, body, {"nochmal": True, "x": "Runde-1"}).status_code == 200
    changed = client.post(
        f"/instances/{iid}/adhoc/insert",
        json={
            "after_node_id": body,
            "label": "Zusatz",
            "staff_rule": {"kind": "ROLE", "ref": "r-test"},
            "reason": "Zusatzprüfung",
        },
    )
    assert changed.status_code == 200, changed.text
    extra = next(
        nid for nid, n in changed.json()["ad_hoc_schema"]["nodes"].items()
        if n["label"] == "Zusatz"
    )

    assert _missing(_web(iid, body, {})) == ["nochmal", "x"]
    assert _web(iid, body, {"nochmal": False, "x": "Runde-2"}).status_code == 200
    assert _web(iid, extra, {}).status_code == 200
    assert _case(iid)["state"] == "COMPLETED"


def test_an_external_task_in_its_first_activation_uses_stored_values() -> None:
    topic = next(_topics)
    sid, _body = _loop_schema("Extern – erster Lauf", topic=topic)
    iid = _start(sid)
    assert client.put(
        f"/instances/{iid}/data", json={"values": {"nochmal": False, "x": "vorab"}}
    ).status_code == 200
    tasks = client.post(
        "/v1/external-tasks/fetch-and-lock",
        json={"worker_id": "w", "topics": [topic], "lock_ms": 60_000},
    ).json()
    assert len(tasks) == 1, tasks

    done = client.post(
        f"/v1/external-tasks/{tasks[0]['id']}/complete", json={"worker_id": "w", "variables": {}}
    )

    assert done.status_code == 200, done.text
    assert _case(iid)["state"] == "COMPLETED"


def test_a_value_sent_with_the_completion_always_counts() -> None:
    sid, prep, act = _two_steps("Ohne Schleife – mitgeschickt")
    iid = _start(sid)
    assert _web(iid, prep, {}).status_code == 200

    assert _web(iid, act, {"x": "jetzt"}).status_code == 200


# --- the engine's bookkeeping -----------------------------------------------------------


def _engine_loop() -> tuple[Any, Any, str]:
    """The released loop schema of the API, as an engine object, and a fresh case."""

    sid, body = _loop_schema("Buchführung")
    model = ProcessSchema.model_validate(client.get(f"/schemas/{sid}").json())
    return model, instantiate(model), body


def test_a_correction_is_recorded_only_for_the_open_steps() -> None:
    model, case, body = _engine_loop()
    assert case.node_states[body] in (NodeState.ACTIVATED, NodeState.RUNNING)

    corrected = record_data_correction(case, model, ["x"])

    assert corrected.data_corrected_in_activation == {body: ["x"]}
    assert case.data_corrected_in_activation == {}  # the input is not changed
    again = record_data_correction(corrected, model, ["x", "nochmal"])
    assert again.data_corrected_in_activation == {body: ["nochmal", "x"]}


def test_a_correction_is_recorded_only_for_the_open_steps_that_write_the_element() -> None:
    """Round 1, only "Erfassen" open: ``z`` belongs to "Nachtragen" alone.

    The record lists, per open step, only the corrected elements that step
    writes -- an element of another step is no value this step supplies.
    A step not open yet gets no entry at all: in a repetition only a
    correction made while the step is open counts for it.
    """

    sid, first, late = _loop_with_a_branch("Buchführung nur für Schreibende")
    model = ProcessSchema.model_validate(client.get(f"/schemas/{sid}").json())
    case = instantiate(model)
    assert case.node_states[first] in (NodeState.ACTIVATED, NodeState.RUNNING)

    corrected = record_data_correction(case, model, ["x", "z"])

    assert corrected.data_corrected_in_activation == {first: ["x"]}
    nothing = record_data_correction(case, model, ["z"])
    assert nothing.data_corrected_in_activation == {}


def test_completion_and_loop_reset_clear_the_record() -> None:
    model, case, body = _engine_loop()
    corrected = record_data_correction(case, model, ["x"])

    after = complete_activity(corrected, model, body, {"nochmal": True})

    # Repeated: the body is open again, for a fresh round without a record.
    assert after.node_states[body] in (NodeState.ACTIVATED, NodeState.RUNNING)
    assert after.data_corrected_in_activation == {}


def test_the_recovery_of_a_failed_step_keeps_the_correction_that_repaired_it() -> None:
    """Round 2: fail, correct the value, reset -- the repaired value still counts.

    The recovery reset keeps the step in its round: the correction made while
    it was open (and failed) supplies its second attempt.
    """

    sid, body = _loop_schema("Wiederanlauf nach Korrektur")
    iid = _start(sid)
    assert _web(iid, body, {"nochmal": True, "x": "Runde-1"}).status_code == 200
    step = {"node_id": body, "agent_id": "a-test"}
    assert client.post(f"/instances/{iid}/start", json=step).status_code == 200
    failed = client.post(f"/instances/{iid}/fail", json={**step, "reason": "x falsch"})
    assert failed.status_code == 200, failed.text
    repaired = client.put(f"/instances/{iid}/data", json={"values": {"x": "repariert"}})
    assert repaired.status_code == 200, repaired.text
    assert client.post(f"/instances/{iid}/reset", json=step).status_code == 200

    done = _web(iid, body, {"nochmal": False})

    assert done.status_code == 200, done.text
    assert _case(iid)["data_values"]["x"] == "repariert"


def test_every_completion_counts_and_the_loop_reset_keeps_the_count() -> None:
    model, case, body = _engine_loop()
    assert case.node_completions == {}

    round_two = complete_activity(case, model, body, {"nochmal": True, "x": "1"})

    # Open again for round two -- and known to have run once.
    assert round_two.node_states[body] in (NodeState.ACTIVATED, NodeState.RUNNING)
    assert round_two.node_completions == {body: 1}
    done = complete_activity(round_two, model, body, {"nochmal": False, "x": "2"})
    assert done.node_completions == {body: 2}
    assert case.node_completions == {}  # the input is not changed


def test_a_case_stored_without_the_count_still_knows_a_performed_step_ran() -> None:
    """A case from before the counter: the performer of round one tells it."""

    sid, body = _loop_schema("Altbestand ohne Zähler")
    iid = _start(sid)
    assert _web(iid, body, {"nochmal": True, "x": "Runde-1"}).status_code == 200
    stored = api_module._instances.get(iid)
    assert stored is not None
    stored.node_completions.clear()
    stored.performed_by[body] = "agent-runde-1"
    api_module._instances.put(stored)

    assert _missing(_web(iid, body, {})) == ["nochmal", "x"]


# --- supervision and push answer in round two ---------------------------------------


@pytest.fixture
def modeller() -> Iterator[Callable[[], dict[str, str]]]:
    """``enable()`` switches to password mode and returns a modeller's auth header.

    A callable, so the test can build its model in the open mode first.
    """

    original = api_module._auth_backend
    backend = PasswordAuthBackend(InMemoryCredentialStore())
    backend.create_user(subject="mara", login="mara", roles=["modeler"])
    user = backend.store.get_user("mara")
    assert user is not None
    backend.store.put_user(
        user.model_copy(update={"password_hash": hash_password("secret-pw1"), "must_change": False})
    )

    def enable() -> dict[str, str]:
        api_module._auth_backend = backend
        return {"Authorization": f"Bearer {backend.login('mara', 'secret-pw1').token}"}

    try:
        yield enable
    finally:
        api_module._auth_backend = original


def test_a_supervision_completion_in_round_two_needs_the_values_too(
    modeller: Callable[[], dict[str, str]],
) -> None:
    sid, body = _loop_schema("Runde zwei – Aufsicht")
    headers = modeller()

    def supervise(iid: str, data: dict[str, object]) -> Any:
        return client.post(
            f"/instances/{iid}/complete",
            json={"node_id": body, "data": data, "supervision_reason": "Vertretung"},
            headers=headers,
        )

    started = client.post(f"/v1/schemas/{sid}/instances", headers=headers)
    assert started.status_code == 201, started.text
    iid = started.json()["id"]
    assert supervise(iid, {"nochmal": True, "x": "Runde-1"}).status_code == 200

    refused = supervise(iid, {})

    assert refused.status_code == 422, refused.text
    findings = refused.json()["detail"]["findings"]
    assert [(f["code"], f["params"]["element"]) for f in findings] == [
        ("D1.write-missing", "nochmal"),
        ("D1.write-missing", "x"),
    ]
    assert supervise(iid, {"nochmal": False, "x": "Runde-2"}).status_code == 200


class _Recorder:
    """Stands in for the HTTP transport of the outbox: records pushes, answers 200."""

    def __init__(self) -> None:
        self.calls: list[bytes] = []

    def post(self, url: str, body: bytes, headers: dict[str, str], timeout: float) -> int:
        self.calls.append(body)
        return 200


def test_a_push_answer_in_round_two_must_report_its_outputs_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sink = _Recorder()
    monkeypatch.setattr(api_module._outbox, "_transport", sink)
    api_module._outbox._circuit.clear()
    ref = f"round-two-{next(_topics)}"
    api_module._push_endpoints.register(ref, "https://tool.example.com/push", "")
    sid, _body = _loop_schema("Runde zwei – Push", push_ref=ref)
    iid = _start(sid)

    def last_push() -> dict[str, Any]:
        client.post("/v1/external-tasks/drive-push")
        assert sink.calls, "no push was delivered"
        return dict(json.loads(sink.calls[-1])["data"])

    def answer(push: dict[str, Any], variables: dict[str, object]) -> Any:
        return client.post(
            f"/v1/external-tasks/{push['task_id']}/complete",
            json={"worker_id": push["callback_token"], "variables": variables},
        )

    first = last_push()
    assert answer(first, {"nochmal": True, "x": "Runde-1"}).status_code == 200
    second = last_push()
    assert second["task_id"] != first["task_id"]

    refused = answer(second, {})

    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"]["code"] == "D1.write-missing"
    assert refused.json()["detail"]["params"]["element"] == "nochmal"
    assert answer(second, {"nochmal": False, "x": "Runde-2"}).status_code == 200
    assert _case(iid)["state"] == "COMPLETED"
