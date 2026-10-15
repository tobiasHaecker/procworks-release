# SPDX-License-Identifier: BUSL-1.1
"""An outbox entry is delivered by one dispatcher at a time -- once per lease.

Deliveries run after every commit. The webhook dispatcher used to read the
due entries and deliver each of them without reserving it first, and the mail
dispatcher did the same. Any other request that committed while a delivery
was still running therefore delivered the same entry again, until one of
them stored it as delivered -- a subscriber received one event many times
with the same ``delivery_id``, a mail recipient one notification several
times, and a push tool the same work package twice (also when the tool
reported back while the push call was still running: its report is a
committing request too).

Pinned here, on the in-memory stores and on the SQLAlchemy stores of an
SQLite file:

* the store contract -- a due entry is reserved by exactly one caller, a
  reserved entry is due again only once its lease has run out, and an outcome
  is recorded only by the holder of the current lease;
* the races: a dispatch that runs while another one delivers (same process
  and "another process" with its own store on the same database), two
  dispatches at the same time held together after their read, and a push
  during which a dispatch runs;
* the deliberate at-least-once limit: a delivery abandoned by a dying process
  is delivered again after its lease (same ``delivery_id``), unless that was
  its last attempt -- a push is never repeated by the outbox;
* the API: a tool that reports back during the push, and requests that
  commit while a webhook is being delivered;
* the mail outbox, which follows the same rules.
"""

from __future__ import annotations

import itertools
import json
import threading
from collections import Counter
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sql_backed_api import use_sql_backend
from staffing import staff_via_api

import procworks.api as api_module
from procworks import db
from procworks.api import app
from procworks.mail_runtime import MailMessage, MailOutboxDispatcher
from procworks.model import MailOutboxState, OutboxState
from procworks.outbox import OutboxDispatcher
from procworks.store import InMemoryMailOutboxStore, InMemoryWebhookStore

client = TestClient(app)

_HOOK = "https://hooks.example.org/procworks"
_PUSH = "https://tool.example.com/push"
_BARRIER_TIMEOUT = 5.0


class _Clock:
    """A movable clock, so lease and back-off are deterministic."""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class _Receiver:
    """A transport that records every delivery and can act while it "runs".

    ``during`` is called inside the first delivery, before it answers -- the
    moment at which, in production, another request commits and dispatches.
    """

    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.bodies: list[dict[str, Any]] = []
        self.during: Callable[[], object] | None = None
        self._lock = threading.Lock()

    def post(self, url: str, body: bytes, headers: dict[str, str], timeout: float) -> int:
        with self._lock:
            self.bodies.append(json.loads(body))
            first = len(self.bodies) == 1
        if first and self.during is not None:
            self.during()
        return self.status

    def delivery_ids(self) -> Counter[str]:
        return Counter(str(b["delivery_id"]) for b in self.bodies)


@pytest.fixture(params=["memory", "sqlite"])
def backend(request: pytest.FixtureRequest, tmp_path: Path) -> str:
    """The store backend: in memory, or an SQLite file (its URL)."""

    if request.param == "memory":
        return "memory"
    return f"sqlite:///{tmp_path / 'outbox.db'}"


def _webhook_store(backend: str, shared: InMemoryWebhookStore | None = None) -> Any:
    """A webhook store on ``backend``; on SQLite each call is "another process"."""

    if backend == "memory":
        return shared if shared is not None else InMemoryWebhookStore()
    return db.SqlAlchemyWebhookStore(backend, create_tables=True)


def _mail_store(backend: str, shared: InMemoryMailOutboxStore | None = None) -> Any:
    if backend == "memory":
        return shared if shared is not None else InMemoryMailOutboxStore()
    return db.SqlAlchemyMailOutboxStore(backend, create_tables=True)


def _two_dispatchers(
    backend: str, transport: _Receiver, clock: _Clock
) -> tuple[OutboxDispatcher, OutboxDispatcher]:
    """Two dispatchers on one outbox -- two API processes on one database."""

    first_store = _webhook_store(backend)
    second_store = first_store if backend == "memory" else _webhook_store(backend)
    return (
        OutboxDispatcher(first_store, transport=transport, now=clock),
        OutboxDispatcher(second_store, transport=transport, now=clock),
    )


def _hold_first_reads(
    monkeypatch: pytest.MonkeyPatch, targets: list[object], name: str, parties: int
) -> None:
    """Hold the first ``parties`` calls of ``name`` on ``targets`` until all have read.

    Placed on the read of the due entries: every dispatcher then works on the
    list it read before any of them delivers -- the interleaving that
    delivered one entry several times. One barrier spans all targets (one
    store in memory, one store per "process" on SQLite).
    """

    barrier = threading.Barrier(parties, timeout=_BARRIER_TIMEOUT)
    counter = itertools.count()
    lock = threading.Lock()

    def wrap(original: Callable[..., Any]) -> Callable[..., Any]:
        def held(*args: Any, **kwargs: Any) -> Any:
            result = original(*args, **kwargs)
            with lock:
                call = next(counter)
            if call < parties:
                try:
                    barrier.wait()
                except threading.BrokenBarrierError:
                    pass
            return result

        return held

    for target in {id(t): t for t in targets}.values():
        monkeypatch.setattr(target, name, wrap(getattr(target, name)))


# --- the store contract -------------------------------------------------------


def test_a_due_entry_is_reserved_by_exactly_one_caller(backend: str) -> None:
    clock = _Clock()
    disp, _ = _two_dispatchers(backend, _Receiver(), clock)
    entry = disp.push(_PUSH, "", "task.push", {"n": 1})
    store_a = disp._store
    store_b = _webhook_store(backend, store_a)

    won = store_a.claim_entry(entry.id, now=clock.t, lease_until=clock.t + 90)
    lost = store_b.claim_entry(entry.id, now=clock.t, lease_until=clock.t + 90)

    assert won is not None and lost is None
    # The reservation is the attempt: counted, and not due until the lease ends.
    assert won.attempts == 1 and won.next_attempt_at == clock.t + 90
    stored = store_b.get_entry(entry.id)
    assert stored.attempts == 1 and stored.next_attempt_at == clock.t + 90
    assert stored.state is OutboxState.PENDING


def test_a_reserved_entry_is_due_again_only_after_its_lease(backend: str) -> None:
    clock = _Clock()
    disp, _ = _two_dispatchers(backend, _Receiver(), clock)
    entry = disp.push(_PUSH, "", "task.push", {"n": 1})
    store = disp._store
    assert store.claim_entry(entry.id, now=clock.t, lease_until=clock.t + 90) is not None

    # Just before the end of the lease nobody else gets it ...
    assert store.claim_entry(entry.id, now=clock.t + 89.9, lease_until=clock.t + 200) is None
    # ... at its end it is due again (the holder is presumed dead).
    again = store.claim_entry(entry.id, now=clock.t + 90, lease_until=clock.t + 200)
    assert again is not None and again.attempts == 2


def test_only_the_holder_of_the_current_lease_records_the_outcome(backend: str) -> None:
    clock = _Clock()
    disp, _ = _two_dispatchers(backend, _Receiver(), clock)
    entry = disp.push(_PUSH, "", "task.push", {"n": 1})
    store = disp._store
    slow = store.claim_entry(entry.id, now=clock.t, lease_until=clock.t + 90)
    fast = store.claim_entry(entry.id, now=clock.t + 100, lease_until=clock.t + 190)
    assert slow is not None and fast is not None

    # The new holder fails and backs off -- the entry stays in a due state.
    fast.state, fast.next_attempt_at = OutboxState.FAILED, clock.t + 500
    assert store.finish_entry(fast, lease_until=clock.t + 190) is True
    # The holder of the expired lease comes back late: its outcome is refused
    # and does not overwrite the newer one.
    slow.state, slow.next_attempt_at = OutboxState.DELIVERED, clock.t + 50
    assert store.finish_entry(slow, lease_until=clock.t + 90) is False
    stored = store.get_entry(entry.id)
    assert stored.state is OutboxState.FAILED and stored.next_attempt_at == clock.t + 500


def test_a_delivered_entry_cannot_be_reserved(backend: str) -> None:
    clock = _Clock()
    transport = _Receiver()
    disp, _ = _two_dispatchers(backend, transport, clock)
    entry = disp.push(_PUSH, "", "task.push", {"n": 1})
    assert disp.dispatch_pending() == 1
    clock.t += 10_000
    assert disp._store.claim_entry(entry.id, now=clock.t, lease_until=clock.t + 90) is None
    assert disp.dispatch_pending() == 0
    assert len(transport.bodies) == 1


# --- the races ------------------------------------------------------------------


def test_a_dispatch_during_a_delivery_does_not_deliver_the_entry_again(
    backend: str,
) -> None:
    clock = _Clock()
    transport = _Receiver()
    first, second = _two_dispatchers(backend, transport, clock)
    first.push(_PUSH, "", "task.push", {"n": 1})
    # While the first delivery runs, "another request" commits and dispatches.
    nested: list[int] = []
    transport.during = lambda: nested.append(second.dispatch_pending())

    assert first.dispatch_pending() == 1

    assert nested == [0]
    assert len(transport.bodies) == 1
    [entry] = first._store.list_entries()
    assert entry.state is OutboxState.DELIVERED and entry.attempts == 1


def test_two_dispatches_at_the_same_time_deliver_every_entry_once(
    backend: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock()
    transport = _Receiver()
    first, second = _two_dispatchers(backend, transport, clock)
    for n in range(8):
        first.push(_PUSH, "", "task.push", {"n": n})
    _hold_first_reads(monkeypatch, [first._store, second._store], "list_entries", 2)

    with ThreadPoolExecutor(2) as pool:
        sent = list(pool.map(lambda d: d.dispatch_pending(), [first, second]))

    assert sum(sent) == 8
    assert sorted(transport.delivery_ids().values()) == [1] * 8
    assert all(e.state is OutboxState.DELIVERED for e in first._store.list_entries())


def test_a_dispatch_during_a_push_does_not_push_again(backend: str) -> None:
    clock = _Clock()
    transport = _Receiver()
    first, second = _two_dispatchers(backend, transport, clock)
    nested: list[int] = []
    transport.during = lambda: nested.append(second.dispatch_pending())

    delivery = first.push_once(_PUSH, "", "task.push", {"task_id": "et1"})

    assert delivery.ok is True
    assert nested == [0]
    assert len(transport.bodies) == 1
    [entry] = first._store.list_entries()
    assert entry.state is OutboxState.DELIVERED and entry.attempts == 1


def test_a_test_ping_is_not_delivered_again_by_a_dispatch_during_it(
    backend: str,
) -> None:
    clock = _Clock()
    transport = _Receiver()
    first, second = _two_dispatchers(backend, transport, clock)
    sub = first.subscribe(_HOOK, ["task.ready"], "")
    nested: list[int] = []
    transport.during = lambda: nested.append(second.dispatch_pending())

    assert first.test_delivery(sub.id).ok is True

    assert nested == [0] and len(transport.bodies) == 1


# --- the at-least-once limit ------------------------------------------------------


class _ProcessDied(BaseException):
    """Stands for the process ending in the middle of a delivery."""


def _dying(transport: _Receiver) -> None:
    def die() -> None:
        raise _ProcessDied

    transport.during = die


def test_an_abandoned_delivery_is_delivered_again_after_its_lease(backend: str) -> None:
    clock = _Clock()
    transport = _Receiver()
    first, second = _two_dispatchers(backend, transport, clock)
    first.push(_PUSH, "", "task.push", {"n": 1})
    _dying(transport)
    with pytest.raises(_ProcessDied):
        first.dispatch_pending()
    transport.during = None

    # The lease still runs: no second delivery yet.
    clock.t += 30
    assert second.dispatch_pending() == 0
    # After it, the entry is delivered again -- the same delivery_id, so a
    # receiver can tell it is the same event.
    clock.t += 60
    assert second.dispatch_pending() == 1
    ids = transport.delivery_ids()
    assert len(ids) == 1 and list(ids.values()) == [2]
    [entry] = second._store.list_entries()
    assert entry.state is OutboxState.DELIVERED and entry.attempts == 2


def test_an_abandoned_last_attempt_becomes_dead_without_a_delivery(backend: str) -> None:
    clock = _Clock()
    transport = _Receiver()
    first, second = _two_dispatchers(backend, transport, clock)
    first.push(_PUSH, "", "task.push", {"n": 1}, max_attempts=1)
    _dying(transport)
    with pytest.raises(_ProcessDied):
        first.dispatch_pending()
    transport.during = None

    clock.t += 1000
    assert second.dispatch_pending() == 0
    assert len(transport.bodies) == 1
    [entry] = second._store.list_entries()
    assert entry.state is OutboxState.DEAD
    assert entry.attempts == 1
    assert entry.last_error == "delivery abandoned (lease expired)"


def test_an_abandoned_push_is_never_repeated_by_the_outbox(backend: str) -> None:
    """The push task owns its retries (new callback token); the outbox must not."""

    clock = _Clock()
    transport = _Receiver()
    first, second = _two_dispatchers(backend, transport, clock)
    _dying(transport)
    with pytest.raises(_ProcessDied):
        first.push_once(_PUSH, "", "task.push", {"task_id": "et1"})
    transport.during = None

    clock.t += 10_000
    assert second.dispatch_pending() == 0
    assert len(transport.bodies) == 1
    [entry] = second._store.list_entries()
    assert entry.state is OutboxState.DEAD
    assert entry.last_error == "delivery abandoned (lease expired)"


def test_the_lease_outlasts_the_delivery_timeout(backend: str) -> None:
    clock = _Clock()
    transport = _Receiver()
    first, second = _two_dispatchers(backend, transport, clock)
    first.push(_PUSH, "", "task.push", {"n": 1})
    _dying(transport)
    with pytest.raises(_ProcessDied):
        first.dispatch_pending()
    transport.during = None
    [entry] = second._store.list_entries()
    # The dispatcher's timeout is 5 s; a delivery still within it (and a
    # margin for clock differences between processes) is never taken over.
    assert entry.next_attempt_at >= 1000 + 5 + 60


# --- the API ----------------------------------------------------------------------


@pytest.fixture(params=["memory", "sqlite"])
def api_backend(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[_Receiver]:
    """The API on in-memory stores or on SQLite, with a recording transport."""

    if request.param == "sqlite":
        use_sql_backend(monkeypatch, tmp_path)
    receiver = _Receiver()
    monkeypatch.setattr(api_module._outbox, "_transport", receiver)
    api_module._outbox._circuit.clear()
    try:
        yield receiver
    finally:
        if request.param == "memory":
            api_module._outbox._store.clear()
        api_module._outbox._circuit.clear()


_refs = (f"once-{n}" for n in itertools.count(1))


def _push_schema() -> str:
    """A released one-step ``HTTP_PUSH`` schema on a freshly registered endpoint."""

    ref = next(_refs)
    api_module._push_endpoints.register(ref, _PUSH, "")
    sid = client.post("/schemas", json={"name": f"Einmal {ref}"}).json()["id"]
    client.post(f"/schemas/{sid}/serial-insert", json={"label": "Run", "after_node_id": "start"})
    schema = client.get(f"/schemas/{sid}").json()
    node_id = next(nid for nid, n in schema["nodes"].items() if n["type"] == "ACTIVITY")
    client.post(f"/schemas/{sid}/service", json={"node_id": node_id, "name": "Run",
                                                 "automatic": True})
    client.post(f"/schemas/{sid}/automation",
                json={"node_id": node_id, "automation": "HTTP_PUSH", "endpoint_ref": ref})
    assert client.post(f"/schemas/{sid}/release").status_code == 200
    return sid


def test_a_tool_reporting_during_the_push_receives_the_push_once(
    api_backend: _Receiver,
) -> None:
    receiver = api_backend
    sid = _push_schema()
    answers: list[int] = []

    def report() -> None:
        package = receiver.bodies[0]["data"]
        answers.append(
            client.post(
                f"/v1/external-tasks/{package['task_id']}/complete",
                json={"worker_id": package["callback_token"], "variables": {}},
            ).status_code
        )

    receiver.during = report
    started = client.post(f"/v1/schemas/{sid}/instances")
    assert started.status_code == 201, started.text
    iid = started.json()["id"]

    pushes = [b for b in receiver.bodies if b["event"] == "task.push"]
    assert len(pushes) == 1
    assert answers == [200]
    assert client.get(f"/v1/instances/{iid}").json()["state"] == "COMPLETED"


def test_requests_committing_during_a_webhook_delivery_do_not_repeat_it(
    api_backend: _Receiver,
) -> None:
    receiver = api_backend
    sid = client.post("/schemas", json={"name": "Abo-Ziel"}).json()["id"]
    client.post(f"/schemas/{sid}/serial-insert", json={"label": "A", "after_node_id": "start"})
    staff_via_api(client, sid)
    assert client.post(f"/schemas/{sid}/release").status_code == 200
    created = client.post(
        "/v1/webhooks", json={"url": _HOOK, "events": ["instance.started"], "secret_ref": ""}
    )
    assert created.status_code in (200, 201), created.text
    # Three other requests commit while the first event is being delivered.
    receiver.during = lambda: [client.post(f"/v1/schemas/{sid}/instances") for _ in range(3)]

    assert client.post(f"/v1/schemas/{sid}/instances").status_code == 201

    ids = receiver.delivery_ids()
    assert len(ids) == 4
    assert set(ids.values()) == {1}


# --- the mail outbox ----------------------------------------------------------


class _Sender:
    """A mail sender that records and can act while the first send "runs"."""

    def __init__(self) -> None:
        self.sent: list[MailMessage] = []
        self.during: Callable[[], object] | None = None

    def send(self, message: MailMessage) -> None:
        self.sent.append(message)
        if len(self.sent) == 1 and self.during is not None:
            self.during()


def _message() -> MailMessage:
    return MailMessage(
        to=["erika@firma.example"], subject="Neue Aufgabe", body="Bitte prüfen",
        instance_id="i1", node_id="n1", schema_id="s1", message_id="k1",
    )


def _two_mail_dispatchers(
    backend: str, clock: _Clock
) -> tuple[MailOutboxDispatcher, MailOutboxDispatcher]:
    first_store = _mail_store(backend)
    second_store = first_store if backend == "memory" else _mail_store(backend)
    return (
        MailOutboxDispatcher(first_store, now=clock),
        MailOutboxDispatcher(second_store, now=clock),
    )


def test_a_mail_dispatch_during_a_send_does_not_send_again(backend: str) -> None:
    clock = _Clock()
    first, second = _two_mail_dispatchers(backend, clock)
    first.enqueue(_message(), "k1")
    sender = _Sender()
    nested: list[int] = []
    sender.during = lambda: nested.append(len(second.dispatch_pending(sender)))

    [result] = first.dispatch_pending(sender)

    assert result.delivered and nested == [0]
    assert len(sender.sent) == 1
    [entry] = first._store.list_entries()
    assert entry.state is MailOutboxState.SENT and entry.attempts == 1


def test_two_mail_dispatches_at_the_same_time_send_every_mail_once(
    backend: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock()
    first, second = _two_mail_dispatchers(backend, clock)
    for n in range(6):
        first.enqueue(_message(), f"k{n}")
    sender = _Sender()
    _hold_first_reads(monkeypatch, [first._store, second._store], "list_entries", 2)

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda d: d.dispatch_pending(sender), [first, second]))

    assert sum(len(r) for r in results) == 6
    assert len(sender.sent) == 6


def test_an_abandoned_mail_is_sent_again_after_its_lease(backend: str) -> None:
    clock = _Clock()
    first, second = _two_mail_dispatchers(backend, clock)
    first.enqueue(_message(), "k1")
    sender = _Sender()

    def die() -> None:
        raise _ProcessDied

    sender.during = die
    with pytest.raises(_ProcessDied):
        first.dispatch_pending(sender)
    sender.during = None

    clock.t += 60
    assert second.dispatch_pending(sender) == []
    clock.t += 300
    [result] = second.dispatch_pending(sender)
    assert result.delivered
    assert len(sender.sent) == 2
    assert {m.message_id for m in sender.sent} == {"k1"}
    [entry] = second._store.list_entries()
    assert entry.state is MailOutboxState.SENT and entry.attempts == 2


def test_an_abandoned_last_mail_attempt_is_dead_and_reported(backend: str) -> None:
    clock = _Clock()
    store = _mail_store(backend)
    first = MailOutboxDispatcher(store, now=clock, max_attempts=1)
    first.enqueue(_message(), "k1")
    sender = _Sender()

    def die() -> None:
        raise _ProcessDied

    sender.during = die
    with pytest.raises(_ProcessDied):
        first.dispatch_pending(sender)
    sender.during = None

    clock.t += 10_000
    [result] = first.dispatch_pending(sender)
    assert result.dead and not result.delivered
    assert len(sender.sent) == 1
    entry = store.get_entry(result.entry.id)
    assert entry.state is MailOutboxState.DEAD
    assert entry.last_error == "Versand abgebrochen (Reservierung abgelaufen)"
