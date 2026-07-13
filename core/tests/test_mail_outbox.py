# SPDX-License-Identifier: BUSL-1.1
"""Durable mail outbox for the modelled e-mail notification (rule group N).

Covers the follow-up beyond the best-effort v0.1 sender:

* the transactional :class:`MailOutboxDispatcher` -- idempotent per-activation
  enqueue, back-off retry and dead-letter (mirrors ``outbox.py`` on SMTP),
* the ``mail.sent`` / ``mail.failed`` audit events (metadata only),
* the ``GET/POST /admin/mail-outbox`` ops view,
* the extra runtime-advance triggers (external-task completion), so a task made
  ready off the human mainline is notified too.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from procworks import (
    add_agent,
    add_role,
    assign_service,
    assign_staff_rule,
    create_empty_schema,
    instantiate,
    release,
    serial_insert,
    set_automation,
    set_mail_binding,
)
from procworks import api as api_module
from procworks.api import app
from procworks.mail_runtime import (
    MailMessage,
    MailOutboxDispatcher,
    activation_dedup_key,
    enqueue_ready_tasks,
)
from procworks.model import (
    AutomationKind,
    MailBinding,
    MailOutboxState,
    StaffRule,
    StaffRuleKind,
)
from procworks.store import InMemoryMailOutboxStore


# --------------------------------------------------------------------------- #
# Fixtures / builders
# --------------------------------------------------------------------------- #
def _activity(schema, label):
    return next(n.id for n in schema.nodes.values() if n.label == label)


def _role_rule(role_id: str) -> StaffRule:
    return StaffRule(kind=StaffRuleKind.ROLE, ref=role_id)


def _mail(**kw) -> MailBinding:
    kw.setdefault("subject", "Neue Aufgabe")
    return MailBinding(**kw)


def _msg(dedup: str = "k") -> MailMessage:
    return MailMessage(
        to=["erika@firma.de"],
        subject="Neue Aufgabe",
        body="Bitte prüfen",
        instance_id="i1",
        node_id="n1",
        schema_id="sc1",
        message_id=dedup,
    )


def _first_activity_mail_schema(schema_id: str):
    """Released schema whose first activity after start carries a notification."""

    schema = create_empty_schema("Out", schema_id=schema_id)
    schema = serial_insert(schema, "Prüfen", after_node_id="start")
    schema = add_role(schema, "SB", role_id="sb")
    schema = add_agent(
        schema, "Erika", role_ids=["sb"], agent_id="a1", email="erika@firma.de"
    )
    schema = assign_staff_rule(schema, _activity(schema, "Prüfen"), _role_rule("sb"))
    schema = set_mail_binding(schema, _activity(schema, "Prüfen"), _mail())
    return release(schema)


class _Collector:
    """A ``MailSender`` that records every message instead of transmitting it."""

    def __init__(self) -> None:
        self.sent: list[MailMessage] = []

    def send(self, message: MailMessage) -> None:
        self.sent.append(message)


class _Failing:
    """A ``MailSender`` that always raises, to drive the retry/dead-letter path."""

    def send(self, message: MailMessage) -> None:
        raise RuntimeError("smtp down")


class _Clock:
    """A movable monotonic clock for deterministic back-off assertions."""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


# --------------------------------------------------------------------------- #
# Dispatcher: idempotent enqueue
# --------------------------------------------------------------------------- #
def test_enqueue_is_idempotent_per_dedup_key() -> None:
    store = InMemoryMailOutboxStore()
    disp = MailOutboxDispatcher(store, now=lambda: 100.0)

    first = disp.enqueue(_msg(), "key1")
    assert first is not None and first.state is MailOutboxState.PENDING
    # A re-observed activation (same key) must not be queued twice.
    assert disp.enqueue(_msg(), "key1") is None
    assert len(store.list_entries()) == 1
    # A different activation is a distinct entry.
    assert disp.enqueue(_msg(), "key2") is not None
    assert len(store.list_entries()) == 2


# --------------------------------------------------------------------------- #
# Dispatcher: delivery / retry / dead-letter
# --------------------------------------------------------------------------- #
def test_dispatch_delivers_and_marks_sent() -> None:
    store = InMemoryMailOutboxStore()
    disp = MailOutboxDispatcher(store, now=lambda: 100.0)
    entry = disp.enqueue(_msg("key1"), "key1")
    assert entry is not None

    collector = _Collector()
    results = disp.dispatch_pending(collector)
    assert len(results) == 1 and results[0].delivered and not results[0].dead
    assert store.get_entry(entry.id).state is MailOutboxState.SENT
    # The dedup key rides along as the Message-ID for receiver idempotency.
    assert len(collector.sent) == 1 and collector.sent[0].message_id == "key1"
    # A delivered entry is not dispatched again.
    assert disp.dispatch_pending(collector) == []


def test_dispatch_retries_with_backoff_then_dead_letters() -> None:
    clock = _Clock(1000.0)
    store = InMemoryMailOutboxStore()
    disp = MailOutboxDispatcher(store, now=clock, max_attempts=3)
    entry = disp.enqueue(_msg(), "k")
    assert entry is not None
    failing = _Failing()

    # Attempt 1 -> FAILED with a future back-off.
    r1 = disp.dispatch_pending(failing)
    assert len(r1) == 1 and not r1[0].delivered and not r1[0].dead
    stored = store.get_entry(entry.id)
    assert stored.state is MailOutboxState.FAILED and stored.attempts == 1
    assert stored.next_attempt_at > clock.t

    # Not retried before the back-off elapses.
    assert disp.dispatch_pending(failing) == []

    # Attempt 2 after the back-off -> still FAILED.
    clock.t = store.get_entry(entry.id).next_attempt_at + 1
    r2 = disp.dispatch_pending(failing)
    assert len(r2) == 1 and store.get_entry(entry.id).attempts == 2

    # Attempt 3 exhausts the budget -> DEAD dead-letter.
    clock.t = store.get_entry(entry.id).next_attempt_at + 1
    r3 = disp.dispatch_pending(failing)
    dead = store.get_entry(entry.id)
    assert dead.state is MailOutboxState.DEAD and dead.attempts == 3
    assert r3[0].dead and dead.last_error == "smtp down"

    # A dead-letter is never retried.
    clock.t += 10_000
    assert disp.dispatch_pending(failing) == []


# --------------------------------------------------------------------------- #
# Per-activation idempotency key + enqueue_ready_tasks
# --------------------------------------------------------------------------- #
def test_activation_dedup_key_is_stable_per_activation() -> None:
    schema = _first_activity_mail_schema("dedup")
    instance = instantiate(schema)
    node = _activity(schema, "Prüfen")
    instance.node_activated_at[node] = datetime(2026, 1, 1, tzinfo=UTC)

    key1 = activation_dedup_key(instance, node)
    assert activation_dedup_key(instance, node) == key1
    assert instance.id in key1 and node in key1
    # A loop re-activation (later stamp) yields a fresh key -> a fresh mail.
    instance.node_activated_at[node] = datetime(2026, 1, 2, tzinfo=UTC)
    assert activation_dedup_key(instance, node) != key1


def test_enqueue_ready_tasks_dedupes_same_activation() -> None:
    schema = _first_activity_mail_schema("enq")
    instance = instantiate(schema)  # 'Prüfen' is ready immediately
    node = _activity(schema, "Prüfen")
    instance.node_activated_at[node] = datetime.now(UTC)
    store = InMemoryMailOutboxStore()
    disp = MailOutboxDispatcher(store)

    first = enqueue_ready_tasks(schema, None, instance, disp)
    assert len(first) == 1 and first[0].recipients == ["erika@firma.de"]
    # Re-running the same observation (same activation stamp) queues nothing more.
    assert enqueue_ready_tasks(schema, None, instance, disp) == []
    assert len(store.list_entries()) == 1


# --------------------------------------------------------------------------- #
# API: end-to-end durable delivery + audit + admin view
# --------------------------------------------------------------------------- #
def _reset_api() -> None:
    api_module._store.clear()
    api_module._instances.clear()
    api_module._mail_outbox_store.clear()
    api_module._audit.clear()


def test_api_instantiate_delivers_via_outbox_and_audits(monkeypatch) -> None:
    schema = _first_activity_mail_schema("apiout")
    collector = _Collector()
    monkeypatch.setattr(api_module, "_mail_sender", collector)
    _reset_api()
    api_module._store.put(schema)
    try:
        client = TestClient(app)
        iid = client.post(f"/schemas/{schema.id}/instances").json()["id"]
        assert len(collector.sent) == 1

        status = client.get("/admin/mail-outbox").json()
        assert status["total"] == 1 and status["sent"] == 1
        assert status["configured"] is False  # collector is not an SMTP sender
        entry = status["entries"][0]
        assert entry["instance_id"] == iid
        assert entry["recipient_count"] == 1
        assert entry["state"] == "SENT"
        # Data minimisation: no address list, no body in the ops view.
        assert "recipients" not in entry and "body" not in entry

        audit = client.get("/audit").json()
        sent_events = [e for e in audit if e["event_type"] == "MAIL_SENT"]
        assert len(sent_events) == 1
        assert sent_events[0]["detail"]["recipients"] == "1"
    finally:
        _reset_api()


def test_api_failing_sender_dead_letters_and_audits(monkeypatch) -> None:
    schema = _first_activity_mail_schema("apifail")
    monkeypatch.setattr(api_module, "_mail_sender", _Failing())
    # A one-shot dispatcher so the single failed attempt dead-letters at once.
    monkeypatch.setattr(
        api_module,
        "_mail_outbox",
        MailOutboxDispatcher(api_module._mail_outbox_store, max_attempts=1),
    )
    _reset_api()
    api_module._store.put(schema)
    try:
        client = TestClient(app)
        # The failing delivery must never break the triggering request.
        assert client.post(f"/schemas/{schema.id}/instances").status_code == 201

        status = client.get("/admin/mail-outbox").json()
        assert status["dead"] == 1 and status["sent"] == 0
        assert status["entries"][0]["last_error"] == "smtp down"

        audit = client.get("/audit").json()
        failed = [e for e in audit if e["event_type"] == "MAIL_FAILED"]
        assert len(failed) == 1 and failed[0]["detail"]["attempts"] == "1"
        assert not any(e["event_type"] == "MAIL_SENT" for e in audit)
    finally:
        _reset_api()


def test_api_admin_dispatch_retries_pending(monkeypatch) -> None:
    """After an SMTP outage a manual dispatch flushes the queued notification."""

    schema = _first_activity_mail_schema("apiretry")
    monkeypatch.setattr(api_module, "_mail_sender", _Failing())
    _reset_api()
    api_module._store.put(schema)
    try:
        client = TestClient(app)
        client.post(f"/schemas/{schema.id}/instances")
        # First attempt failed (default budget not yet spent): still retryable.
        assert client.get("/admin/mail-outbox").json()["failed"] == 1

        # SMTP recovers; the queued mail is redelivered on the manual flush.
        collector = _Collector()
        monkeypatch.setattr(api_module, "_mail_sender", collector)
        # The queued entry is only due again after its back-off; a dispatcher
        # with a far-future clock would skip it, so drive with real time by
        # resetting next_attempt_at to the past for the test.
        for entry in api_module._mail_outbox_store.list_entries():
            entry.next_attempt_at = 0.0
            api_module._mail_outbox_store.put_entry(entry)

        status = client.post("/admin/mail-outbox/dispatch").json()
        assert status["sent"] == 1 and status["failed"] == 0
        assert len(collector.sent) == 1
    finally:
        _reset_api()


# --------------------------------------------------------------------------- #
# Extra trigger: external-task completion notifies a downstream mail task
# --------------------------------------------------------------------------- #
def _external_then_mail_schema(schema_id: str, topic: str):
    """start -> Run (EXTERNAL_TASK) -> Prüfen (human, notified)."""

    schema = create_empty_schema("ExtMail", schema_id=schema_id)
    schema = serial_insert(schema, "Run", after_node_id="start")
    schema = serial_insert(schema, "Prüfen", after_node_id=_activity(schema, "Run"))
    schema = add_role(schema, "SB", role_id="sb")
    schema = add_agent(
        schema, "Erika", role_ids=["sb"], agent_id="a1", email="erika@firma.de"
    )
    schema = assign_staff_rule(schema, _activity(schema, "Prüfen"), _role_rule("sb"))
    schema = assign_service(schema, _activity(schema, "Run"), "Run", automatic=True)
    schema = set_automation(
        schema, _activity(schema, "Run"), AutomationKind.EXTERNAL_TASK, topic=topic
    )
    schema = set_mail_binding(schema, _activity(schema, "Prüfen"), _mail())
    return release(schema)


def test_external_task_completion_triggers_notification(monkeypatch) -> None:
    schema = _external_then_mail_schema("extmail", "extmail-topic")
    collector = _Collector()
    monkeypatch.setattr(api_module, "_mail_sender", collector)
    _reset_api()
    api_module._external_tasks.clear()
    api_module._store.put(schema)
    try:
        client = TestClient(app)
        iid = client.post(f"/v1/schemas/{schema.id}/instances").json()["id"]
        # 'Run' is the automatic first step; the human 'Prüfen' is not ready yet.
        assert collector.sent == []

        locked = client.post(
            "/v1/external-tasks/fetch-and-lock",
            json={"worker_id": "w1", "topics": ["extmail-topic"], "max_tasks": 1},
        ).json()
        assert len(locked) == 1 and locked[0]["instance_id"] == iid

        resp = client.post(
            f"/v1/external-tasks/{locked[0]['id']}/complete",
            json={"worker_id": "w1", "variables": {}},
        )
        assert resp.status_code == 200
        # Completing the external step activated 'Prüfen' -> the mail went out.
        assert len(collector.sent) == 1
        assert collector.sent[0].to == ["erika@firma.de"]
        assert client.get("/admin/mail-outbox").json()["sent"] == 1
    finally:
        _reset_api()
        api_module._external_tasks.clear()
