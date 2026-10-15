# SPDX-License-Identifier: BUSL-1.1
"""E2: activity detail states (suspended / failed overlay).

SUSPENDED/FAILED live as an overlay over the untouched base marking (V1-V4):
only the owner pauses/continues/fails, a suspended step must resume before
completion, a failed step freezes until its recovery reset -- which clears
overlay + claim + escalation ladder and re-offers the step with a freshly
stamped clock. Rule B refines the time view: once claimed, a modelled
processing duration is measured from the claim instant -- shared by the
worklist bands and the escalation sweep.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from invisible_text import INVISIBLE_REASONS, assert_reason_required
from web_vm import needs_node, run_app_js

from procworks import (
    NodeDetailState,
    NodeState,
    StaffRule,
    StaffRuleKind,
    add_agent,
    add_role,
    assign_staff_rule,
    complete_activity,
    create_empty_schema,
    fail_activity,
    instantiate,
    release,
    reset_activity,
    resume_activity,
    return_activity,
    serial_insert,
    suspend_activity,
)
from procworks import api as api_module
from procworks.audit import check_chain
from procworks.execution import ExecutionError
from procworks.model import TimeConstraint
from procworks.worklist_priority import TimeContext, assess

client = TestClient(api_module.app)

ANNA = "a-anna"
BERT = "a-bert"


def _nid(schema: object, label: str) -> str:
    return next(n.id for n in schema.nodes.values() if n.label == label)  # type: ignore[attr-defined]


def _two_agent_schema():
    schema = create_empty_schema("Detail")
    schema = serial_insert(schema, "Pruefen", after_node_id="start")
    schema = add_role(schema, "SB", role_id="r-sb")
    schema = add_agent(schema, "Anna", role_ids=["r-sb"], agent_id=ANNA)
    schema = add_agent(schema, "Bert", role_ids=["r-sb"], agent_id=BERT)
    schema = assign_staff_rule(
        schema, _nid(schema, "Pruefen"), StaffRule(kind=StaffRuleKind.ROLE, ref="r-sb")
    )
    return release(schema)


# --- engine: V1-V4 ---------------------------------------------------------


def test_suspend_blocks_completion_until_resume_v1_v2() -> None:
    schema = _two_agent_schema()
    node = _nid(schema, "Pruefen")
    inst = instantiate(schema)

    inst = suspend_activity(inst, schema, node, ANNA)  # implicit claim + start
    assert inst.node_states[node] is NodeState.RUNNING  # base marking untouched
    assert inst.node_details[node] is NodeDetailState.SUSPENDED
    assert inst.claimed_by[node] == ANNA

    with pytest.raises(ExecutionError):  # finish only from RUNNING
        complete_activity(inst, schema, node, agent_id=ANNA)
    with pytest.raises(ExecutionError):  # only the owner resumes (V2)
        resume_activity(inst, schema, node, BERT)

    inst = resume_activity(inst, schema, node, ANNA)
    assert node not in inst.node_details
    done = complete_activity(inst, schema, node, agent_id=ANNA)
    assert done.node_states[node] is NodeState.COMPLETED


def test_fail_freezes_the_step_until_reset_v3_v4() -> None:
    schema = _two_agent_schema()
    node = _nid(schema, "Pruefen")
    inst = instantiate(schema)

    inst = fail_activity(inst, schema, node, ANNA, "Unterlagen fehlen")
    assert inst.node_details[node] is NodeDetailState.FAILED
    assert inst.node_detail_reason[node] == "Unterlagen fehlen"

    with pytest.raises(ExecutionError):  # frozen: no completion
        complete_activity(inst, schema, node, agent_id=ANNA)
    with pytest.raises(ExecutionError):  # frozen: no pause either
        suspend_activity(inst, schema, node, ANNA)
    with pytest.raises(ExecutionError):  # no silent return -- reset instead
        return_activity(inst, schema, node, ANNA)
    with pytest.raises(ExecutionError) as err:  # a stranger cannot reset
        reset_activity(inst, schema, node, BERT)
    assert "V4" in str(err.value)

    back = reset_activity(inst, schema, node, ANNA)
    assert back.node_states[node] is NodeState.ACTIVATED  # fresh offer
    assert node not in back.node_details
    assert node not in back.node_detail_reason
    assert node not in back.claimed_by
    # The supervisory override works regardless of caller identity.
    failed_again = fail_activity(back, schema, node, BERT, "zweiter Versuch")
    forced = reset_activity(failed_again, schema, node, ANNA, force=True)
    assert node not in forced.node_details


def test_suspended_step_must_resume_before_failing_v3() -> None:
    schema = _two_agent_schema()
    node = _nid(schema, "Pruefen")
    inst = instantiate(schema)
    inst = suspend_activity(inst, schema, node, ANNA)

    with pytest.raises(ExecutionError):
        fail_activity(inst, schema, node, ANNA, "geht nicht")


def test_return_of_a_suspended_step_clears_the_pause() -> None:
    schema = _two_agent_schema()
    node = _nid(schema, "Pruefen")
    inst = instantiate(schema)
    inst = suspend_activity(inst, schema, node, ANNA)

    back = return_activity(inst, schema, node, ANNA)
    assert node not in back.node_details
    assert back.node_states[node] is NodeState.ACTIVATED


# --- rule B: processing clock from the claim -------------------------------


def test_rule_b_measures_the_duration_from_the_claim() -> None:
    schema = _two_agent_schema()
    node = _nid(schema, "Pruefen")
    schema = schema.model_copy(deep=True)
    schema.time_constraints[node] = TimeConstraint(
        max_duration_seconds=100, target_lead_seconds=60
    )
    t0 = datetime(2026, 8, 9, 8, 0, tzinfo=UTC)

    # Unclaimed: reaction clock from activation -> overdue after 60s.
    unclaimed = TimeContext(now=t0 + timedelta(seconds=100), activated_at={node: t0})
    assert assess(schema, node, unclaimed).criticality.value == "OVERDUE"

    # Claimed at t0+50: processing clock (100s) from the claim -> 50% -> WARNING.
    claimed = TimeContext(
        now=t0 + timedelta(seconds=100),
        activated_at={node: t0},
        claimed_at={node: t0 + timedelta(seconds=50)},
    )
    view = assess(schema, node, claimed)
    assert view.criticality.value == "WARNING"
    assert view.due_at == t0 + timedelta(seconds=150)


# --- boundary: endpoints, sweep coupling -----------------------------------


def _api_world(*, pause_stops_clock: bool = False) -> tuple[str, str]:
    sid = client.post("/schemas", json={"name": "Detail-API"}).json()["id"]
    client.post(
        f"/schemas/{sid}/serial-insert",
        json={"label": "Pruefen", "after_node_id": "start"},
    )
    nodes = client.get(f"/schemas/{sid}").json()["nodes"]
    node = next(nid for nid, n in nodes.items() if n.get("label") == "Pruefen")
    client.post(f"/schemas/{sid}/roles", json={"name": "SB", "role_id": "r-sb"})
    client.post(f"/schemas/{sid}/roles", json={"name": "Sr", "role_id": "r-senior"})
    client.post(
        f"/schemas/{sid}/agents",
        json={"name": "Anna", "role_ids": ["r-sb"], "agent_id": ANNA},
    )
    client.post(
        f"/schemas/{sid}/agents",
        json={
            "name": "Bert",
            "role_ids": ["r-senior"],
            "agent_id": BERT,
            "email": "bert@example.org",
        },
    )
    client.post(
        f"/schemas/{sid}/staff-rule",
        json={"node_id": node, "rule": {"kind": "ROLE", "ref": "r-sb"}},
    )
    client.post(
        f"/schemas/{sid}/time-constraint",
        json={
            "node_id": node,
            "constraint": {
                "target_lead_seconds": 60,
                "max_duration_seconds": 3600,
                "pause_stops_clock": pause_stops_clock,
            },
        },
    )
    client.post(
        f"/schemas/{sid}/escalation-policy",
        json={
            "node_id": node,
            "policy": {
                "stages": [
                    {
                        "after_seconds": 0,
                        "kind": "FUNCTIONAL",
                        "rule": {"kind": "ROLE", "ref": "r-senior"},
                    }
                ]
            },
        },
    )
    client.post(f"/schemas/{sid}/release")
    iid = client.post(f"/schemas/{sid}/instances").json()["id"]
    return node, iid


def test_detail_endpoints_and_audit_via_api() -> None:
    node, iid = _api_world()

    resp = client.post(f"/instances/{iid}/suspend",
                       json={"node_id": node, "agent_id": ANNA, "reason": "Rückfrage beim Kunden"})
    assert resp.status_code == 200
    assert resp.json()["node_details"][node] == "SUSPENDED"
    # The owner's list shows the pause.
    mine = [t for t in client.get(f"/agents/{ANNA}/tasks").json() if t["node_id"] == node]
    assert mine and mine[0]["detail"] == "SUSPENDED"

    resp = client.post(f"/instances/{iid}/complete", json={"node_id": node, "agent_id": ANNA})
    assert resp.status_code == 409  # resume first

    client.post(f"/instances/{iid}/resume", json={"node_id": node, "agent_id": ANNA})
    resp = client.post(f"/instances/{iid}/fail",
        json={"node_id": node, "agent_id": ANNA, "reason": "Unterlagen fehlen"})
    assert resp.status_code == 200
    assert resp.json()["node_detail_reason"][node] == "Unterlagen fehlen"

    before = client.get(f"/instances/{iid}").json()
    resp = client.post(f"/instances/{iid}/reset", json={"node_id": node, "agent_id": ANNA})
    assert resp.status_code == 200
    after = resp.json()
    assert after["node_states"][node] == "ACTIVATED"
    assert after["claimed_by"] == {}
    assert after["node_activated_at"][node] != before["node_activated_at"][node]

    events = [e["event_type"] for e in client.get(f"/instances/{iid}/audit").json()]
    suspended = next(
        e for e in client.get(f"/instances/{iid}/audit").json()
        if e["event_type"] == "ACTIVITY_SUSPENDED"
    )
    assert suspended["detail"]["reason"] == "Rückfrage beim Kunden"
    for expected in (
        "ACTIVITY_SUSPENDED",
        "ACTIVITY_RESUMED",
        "ACTIVITY_FAILED",
        "ACTIVITY_RESET",
    ):
        assert expected in events
    failed = next(
        e for e in client.get(f"/instances/{iid}/audit").json()
        if e["event_type"] == "ACTIVITY_FAILED"
    )
    assert failed["detail"]["reason"] == "Unterlagen fehlen"


def test_sweep_respects_rule_b_and_skips_failed_steps() -> None:
    node, iid = _api_world()
    inst = client.get(f"/instances/{iid}").json()
    activated = datetime.fromisoformat(inst["node_activated_at"][node])

    # Claim: the processing clock (3600s from the claim) replaces the 60s
    # reaction clock -- the stage no longer fires at activation+61. Assertions
    # are per-instance: the sweep spans the whole (shared) store.
    client.post(f"/instances/{iid}/claim", json={"node_id": node, "agent_id": ANNA})
    claimed = datetime.fromisoformat(
        client.get(f"/instances/{iid}").json()["node_claimed_at"][node]
    )
    api_module._escalation_sweep(now=activated + timedelta(seconds=61))
    assert client.get(f"/instances/{iid}").json()["escalated_stages"].get(node, 0) == 0
    # Past the processing due instant it fires.
    api_module._escalation_sweep(now=claimed + timedelta(seconds=3601))
    assert client.get(f"/instances/{iid}").json()["escalated_stages"].get(node) == 1

    # A failed step never escalates further.
    node2, iid2 = _api_world()
    client.post(
        f"/instances/{iid2}/fail",
        json={"node_id": node2, "agent_id": ANNA, "reason": "kaputt"},
    )
    api_module._escalation_sweep(now=datetime.now(UTC) + timedelta(days=30))
    tasks2 = client.get(f"/instances/{iid2}/tasks").json()
    assert [t["escalated_stage"] for t in tasks2 if t["node_id"] == node2] == [0]


# --- net time (E2 Stufe C): boundary bookkeeping + sweep coupling ----------


def test_suspend_resume_books_the_pause_at_the_boundary() -> None:
    node, iid = _api_world(pause_stops_clock=True)
    client.post(f"/instances/{iid}/claim", json={"node_id": node, "agent_id": ANNA})

    paused = client.post(
        f"/instances/{iid}/suspend",
        json={"node_id": node, "agent_id": ANNA, "reason": "Warte auf Unterlagen"},
    ).json()
    assert node in paused["node_suspended_at"]  # pause start stamped
    opened = paused["node_suspended_at"][node]

    # An idempotent re-suspend must not restart the ongoing pause.
    again = client.post(
        f"/instances/{iid}/suspend",
        json={"node_id": node, "agent_id": ANNA, "reason": "Warte auf Unterlagen"},
    ).json()
    assert again["node_suspended_at"][node] == opened

    resumed = client.post(
        f"/instances/{iid}/resume", json={"node_id": node, "agent_id": ANNA}
    ).json()
    assert node not in resumed["node_suspended_at"]  # interval closed...
    assert resumed["node_paused_seconds"][node] >= 0.0  # ...and credited

    # Completion clears the bookkeeping -- credit belongs to one activation.
    done = client.post(
        f"/instances/{iid}/complete", json={"node_id": node, "agent_id": ANNA}
    ).json()
    assert node not in done["node_paused_seconds"]
    assert node not in done["node_suspended_at"]


def test_sweep_defers_by_pause_credit_only_with_the_opt_in() -> None:
    node, iid = _api_world(pause_stops_clock=True)
    client.post(f"/instances/{iid}/claim", json={"node_id": node, "agent_id": ANNA})
    claimed = datetime.fromisoformat(
        client.get(f"/instances/{iid}").json()["node_claimed_at"][node]
    )
    # Deterministic pause credit: book 7200s directly into the store (the
    # HTTP round-trip would only accumulate milliseconds).
    inst = api_module._instances.get(iid)
    assert inst is not None
    inst.node_paused_seconds[node] = 7200.0
    api_module._instances.put(inst)

    # Past the gross due instant (claim + 3600s) nothing fires -- the pause
    # credit defers the due instant for bands AND sweep alike.
    api_module._escalation_sweep(now=claimed + timedelta(seconds=3601))
    assert client.get(f"/instances/{iid}").json()["escalated_stages"].get(node, 0) == 0
    # Past claim + 3600s + credit it fires.
    api_module._escalation_sweep(now=claimed + timedelta(seconds=3600 + 7200 + 1))
    assert client.get(f"/instances/{iid}").json()["escalated_stages"].get(node) == 1

    # Without the opt-in the same booked credit changes nothing (default).
    node2, iid2 = _api_world()
    client.post(f"/instances/{iid2}/claim", json={"node_id": node2, "agent_id": ANNA})
    claimed2 = datetime.fromisoformat(
        client.get(f"/instances/{iid2}").json()["node_claimed_at"][node2]
    )
    inst2 = api_module._instances.get(iid2)
    assert inst2 is not None
    inst2.node_paused_seconds[node2] = 7200.0
    api_module._instances.put(inst2)
    api_module._escalation_sweep(now=claimed2 + timedelta(seconds=3601))
    assert client.get(f"/instances/{iid2}").json()["escalated_stages"].get(node2) == 1


# --- a pause needs a visible reason ------------------------------------------


@pytest.mark.parametrize(
    "body",
    [{}, {"reason": None}, {"reason": ""}, {"reason": "  \t\n"},
     *({"reason": text} for _, text in INVISIBLE_REASONS)],
    ids=["fehlt", "null", "leer", "leerzeichen",
         *(name for name, _ in INVISIBLE_REASONS)],
)
def test_suspend_without_a_visible_reason_is_refused(body: dict[str, object]) -> None:
    """Negativ (der Grund): „Anhalten“ ohne Grund – 422 mit dem gemeinsamen
    Code der Begründungspflichten (``what`` ``suspend``); der Schritt bleibt
    unverändert, kein Ereignis im Verlauf. Auch eine Integration, die das Feld
    nicht kennt, bekommt diesen Code und keinen Schemafehler."""

    node, iid = _api_world()
    assert client.post(
        f"/instances/{iid}/start", json={"node_id": node, "agent_id": ANNA}
    ).status_code == 200
    before = client.get(f"/instances/{iid}").json()

    resp = client.post(
        f"/instances/{iid}/suspend", json={"node_id": node, "agent_id": ANNA, **body}
    )

    assert_reason_required(resp, "suspend", api_module.SUSPEND_REASON_REQUIRED)
    assert client.get(f"/instances/{iid}").json() == before
    events = [e["event_type"] for e in client.get(f"/instances/{iid}/audit").json()]
    assert "ACTIVITY_SUSPENDED" not in events


def test_suspend_records_the_reason_in_the_history() -> None:
    """Positiv: the reason (trimmed) stands at the event; the pause itself is
    unchanged (overlay SUSPENDED, base marking RUNNING)."""

    node, iid = _api_world()
    resp = client.post(f"/instances/{iid}/suspend",
                       json={"node_id": node, "agent_id": ANNA, "reason": "  Kunde ruft zurück "})
    assert resp.status_code == 200, resp.text
    assert resp.json()["node_details"][node] == "SUSPENDED"
    assert resp.json()["node_states"][node] == "RUNNING"
    events = [e for e in client.get(f"/instances/{iid}/audit").json()
              if e["event_type"] == "ACTIVITY_SUSPENDED"]
    assert [e["detail"]["reason"] for e in events] == ["Kunde ruft zurück"]
    _finish(iid, node)


def test_resume_needs_no_reason() -> None:
    """Grenzfall: only the pause asks why -- continuing is self-explanatory."""

    node, iid = _api_world()
    client.post(f"/instances/{iid}/suspend",
                json={"node_id": node, "agent_id": ANNA, "reason": "Pause"})
    resp = client.post(f"/instances/{iid}/resume", json={"node_id": node, "agent_id": ANNA})
    assert resp.status_code == 200, resp.text
    assert node not in resp.json()["node_details"]
    _finish(iid, node)


def _finish(iid: str, node: str) -> None:
    """Complete the case, so that no open step of it is left for the
    escalation sweep of later tests (the sweep spans the shared store)."""

    if client.get(f"/instances/{iid}").json()["node_details"].get(node) == "SUSPENDED":
        client.post(f"/instances/{iid}/resume", json={"node_id": node, "agent_id": ANNA})
    done = client.post(f"/instances/{iid}/complete", json={"node_id": node, "agent_id": ANNA})
    assert done.status_code == 200, done.text


_SUSPEND_DIALOG = r"""
state.principal = { subject: "anna", roles: ["operator"], agent_id: "a1", display_name: "Anna" };
document.body.appendChild(byId("modal-root"));
const sent = [];
let status = 200;
respond((p, m, body) => {
  if (m === "POST" && p === "/instances/i1/suspend") {
    sent.push(typeof body === "string" ? JSON.parse(body) : body);
    return status === 200 ? { body: {} } : { status, body: { detail: {
      code: "REQ.reason-required", params: { what: "suspend" }, message: "x" } } };
  }
  return { status: 404 };
});
const settle = () => new Promise((r) => setTimeout(r, 5));
const root = byId("modal-root");
const button = (label) => root.querySelectorAll("button").find((b) => textOf(b) === label);
suspendTask({ instance_id: "i1", node_id: "n1", label: "Pruefen" }, "a1");
const title = textOf(root);
"""


@needs_node
def test_the_client_asks_for_the_reason_and_sends_it(tmp_path: Path) -> None:
    """Positiv: „Anhalten“ öffnet einen Dialog mit Feld „Grund“; der Grund
    geht unverändert an den Kern, der Dialog schließt nach Erfolg."""

    res = run_app_js(_SUSPEND_DIALOG + r"""
root.querySelectorAll("input")[0].value = "Warte auf Rückruf";
await button("Anhalten").click(); await settle();
return { title, sent, open: root.children.length > 0 };
""", tmp_path)
    assert "Anhalten" in res["title"] and "Grund" in res["title"]
    assert res["sent"] == [{"node_id": "n1", "agent_id": "a1", "reason": "Warte auf Rückruf"}]
    assert res["open"] is False


@needs_node
def test_a_refused_reason_keeps_the_dialog_open_with_the_catalogue_text(tmp_path: Path) -> None:
    """Negativ (der Grund): the core refuses an empty reason -- the dialog
    stays open, the message is the catalogue text for ``suspend``."""

    res = run_app_js(_SUSPEND_DIALOG + r"""
status = 422;
await button("Anhalten").click(); await settle();
return { open: root.children.length > 0, sent,
         text: describeError({ status: 422, detail: { code: "REQ.reason-required",
           params: { what: "suspend" }, message: "x" } }).title };
""", tmp_path)
    assert res["open"] is True
    assert res["sent"] == [{"node_id": "n1", "agent_id": "a1", "reason": ""}]
    assert res["text"].startswith("Bitte einen Grund f\u00fcr das Anhalten angeben")


# --- a failure needs a visible reason -----------------------------------------


@pytest.mark.parametrize(
    "blank", ["", "   ", "\t", "\n \r\n"], ids=["leer", "leerzeichen", "tab", "umbrueche"]
)
def test_fail_without_a_visible_reason_is_refused(blank: str) -> None:
    """„Problem melden“ nahm eine Begründung aus Leerzeichen an: der Schritt
    stand als gescheitert im Verlauf, ohne erkennbaren Grund. Jetzt 422 mit
    dem Hinweis auf die fehlende Begründung; der Schritt bleibt unverändert."""

    node, iid = _api_world()
    assert client.post(
        f"/instances/{iid}/start", json={"node_id": node, "agent_id": ANNA}
    ).status_code == 200
    before = client.get(f"/instances/{iid}").json()

    resp = client.post(
        f"/instances/{iid}/fail", json={"node_id": node, "agent_id": ANNA, "reason": blank}
    )

    assert resp.status_code == 422
    assert resp.json()["detail"] == {
        "message": api_module.FAIL_REASON_REQUIRED,
        "code": "REQ.reason-required",
        "params": {"what": "fail"},
    }
    assert client.get(f"/instances/{iid}").json() == before
    events = [e["event_type"] for e in client.get(f"/instances/{iid}/audit").json()]
    assert "ACTIVITY_FAILED" not in events


def test_fail_with_one_visible_character_is_accepted() -> None:
    node, iid = _api_world()
    client.post(f"/instances/{iid}/start", json={"node_id": node, "agent_id": ANNA})

    resp = client.post(
        f"/instances/{iid}/fail", json={"node_id": node, "agent_id": ANNA, "reason": "  ?  "}
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["node_details"][node] == "FAILED"
    assert resp.json()["node_detail_reason"][node] == "?"


@needs_node
def test_the_client_words_the_missing_reason_from_its_catalogue(tmp_path: Path) -> None:
    """Die Ablehnung kommt mit Code; der Client zeigt den Katalogtext, nicht
    den technischen Rumpf."""

    res = run_app_js(
        r"""
return describeError({ status: 422, detail: { message: "x",
  code: "REQ.reason-required", params: { what: "fail" } } }).title;
""",
        tmp_path,
    )
    assert res.startswith("Bitte eine Begr\u00fcndung angeben")
    assert "Prozessverantwortung" in res


@pytest.mark.parametrize(
    "invisible", [text for _, text in INVISIBLE_REASONS], ids=[i for i, _ in INVISIBLE_REASONS]
)
def test_fail_with_an_invisible_reason_is_refused(invisible: str) -> None:
    """Zero-width characters are no reason either -- same answer as for blanks."""

    node, iid = _api_world()
    assert client.post(
        f"/instances/{iid}/start", json={"node_id": node, "agent_id": ANNA}
    ).status_code == 200
    before = client.get(f"/instances/{iid}").json()

    resp = client.post(
        f"/instances/{iid}/fail", json={"node_id": node, "agent_id": ANNA, "reason": invisible}
    )

    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "REQ.reason-required"
    assert resp.json()["detail"]["params"] == {"what": "fail"}
    assert client.get(f"/instances/{iid}").json() == before


@pytest.mark.parametrize(
    ("reason", "recorded"),
    [
        ("  Unterlagen fehlen ", "Unterlagen fehlen"),
        ("\u200bOK", "\u200bOK"),  # one visible character is enough
        ("\U0001f44d\u200d", "\U0001f44d\u200d"),  # emoji with a joiner
        ("\u00e4", "\u00e4"),
        ("OK\ue000", "OK\ue000"),  # a private-use character beside text is kept
    ],
)
def test_a_visible_reason_is_kept_as_given(reason: str, recorded: str) -> None:
    assert api_module._visible_reason(reason) == recorded


# --- an implicit claim/start is recorded like the explicit one ---------------
#
# Suspending or failing a step that is only offered (or only claimed) claims
# and starts it on the way (the engine's Started collapse). The history must
# show those transitions with the same event types as the explicit way --
# otherwise it reads as if a step nobody took was paused, and "since when is
# it being worked on" has no event to start from.


def _step_events(iid: str, node: str) -> list[dict[str, object]]:
    """The audit events of one step, oldest first."""

    return [e for e in client.get(f"/instances/{iid}/audit").json()
            if e.get("node_id") == node]


@pytest.mark.parametrize(
    ("act", "event", "body"),
    [("suspend", "ACTIVITY_SUSPENDED", {"reason": "Rückfrage"}),
     ("fail", "ACTIVITY_FAILED", {"reason": "Unterlagen fehlen"})],
)
def test_an_offered_step_records_claim_and_start_before_the_act(
        act: str, event: str, body: dict[str, str]) -> None:
    """Positiv: from the open offer -- CLAIMED, STARTED, then the act, all for
    the acting agent; the implicit ones say which act took them along, the
    reason stays at the act. The claim instant is stamped as on the explicit
    way, and the hash chain stays without defect."""

    node, iid = _api_world()
    before = [e["event_type"] for e in _step_events(iid, node)]
    resp = client.post(f"/instances/{iid}/{act}",
                       json={"node_id": node, "agent_id": ANNA, **body})
    assert resp.status_code == 200, resp.text
    assert resp.json()["node_claimed_at"].get(node)
    new = _step_events(iid, node)[len(before):]
    assert [e["event_type"] for e in new] == [
        "ACTIVITY_CLAIMED", "ACTIVITY_STARTED", event]
    assert all(e["agent_id"] == ANNA for e in new)
    assert new[0]["detail"] == {"implicit": act}
    assert new[1]["detail"] == {"implicit": act}
    assert new[2]["detail"]["reason"] == body["reason"]
    assert check_chain(api_module._audit).defects == []


def test_a_claimed_step_records_only_the_start() -> None:
    """Grenzfall: already claimed (explicitly) -- no second CLAIMED, only the
    implicit STARTED before the pause."""

    node, iid = _api_world()
    assert client.post(f"/instances/{iid}/claim",
                       json={"node_id": node, "agent_id": ANNA}).status_code == 200
    count = len(_step_events(iid, node))
    resp = client.post(f"/instances/{iid}/suspend",
                       json={"node_id": node, "agent_id": ANNA, "reason": "Rückfrage"})
    assert resp.status_code == 200, resp.text
    new = [e["event_type"] for e in _step_events(iid, node)[count:]]
    assert new == ["ACTIVITY_STARTED", "ACTIVITY_SUSPENDED"]


def test_a_started_step_records_only_the_act() -> None:
    """Grenzfall: already started -- nothing implicit happens, nothing extra
    is recorded; an idempotent re-suspend adds only its own event."""

    node, iid = _api_world()
    assert client.post(f"/instances/{iid}/start",
                       json={"node_id": node, "agent_id": ANNA}).status_code == 200
    count = len(_step_events(iid, node))
    for reason in ("erste Pause", "zweiter Grund"):
        assert client.post(
            f"/instances/{iid}/suspend",
            json={"node_id": node, "agent_id": ANNA, "reason": reason},
        ).status_code == 200
    new = [e["event_type"] for e in _step_events(iid, node)[count:]]
    assert new == ["ACTIVITY_SUSPENDED", "ACTIVITY_SUSPENDED"]


def test_a_refused_act_records_no_implicit_transition() -> None:
    """Negativ (der Grund): the step is worked on by someone else -- 409
    ``EX.claimed-by-other``; the refused pause took nothing along, so the
    history gains neither a claim nor a start for the refused agent."""

    node, iid = _api_world()
    assert client.post(f"/instances/{iid}/start",
                       json={"node_id": node, "agent_id": ANNA}).status_code == 200
    count = len(_step_events(iid, node))
    resp = client.post(f"/instances/{iid}/suspend",
                       json={"node_id": node, "agent_id": BERT, "reason": "fremd"})
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "EX.claimed-by-other"
    assert _step_events(iid, node)[count:] == []
