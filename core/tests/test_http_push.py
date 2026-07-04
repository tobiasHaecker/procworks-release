# SPDX-License-Identifier: BUSL-1.1
"""HTTP-Push outbound integration -- the *push* side of §6.3 (roadmap P6).

Complements the EXTERNAL_TASK *pull* path (``test_external_tasks.py``). Three
layers are covered:

* the :class:`procworks.outbox.PushEndpointRegistry` and
  :func:`procworks.outbox.build_push_endpoint_registry` resolver helpers, which
  keep concrete URLs/secrets server-side (rules I4/I6);
* :meth:`procworks.outbox.OutboxDispatcher.push` -- a subscription-less, signed,
  durable delivery that reuses the full outbox machinery; and
* :meth:`procworks.integration_runtime.ExternalTaskRuntime.drive_push` plus the
  ``/v1`` API, so activation -> push -> callback-complete is exercised end to
  end with an injected fake transport and a deterministic clock (no network,
  no sleeping). Stability is the invariant under test: a push failure must
  never advance, corrupt or block the pure engine.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import procworks.api as api_module
from procworks import (
    InMemoryWebhookStore,
    OutboxDispatcher,
    OutboxState,
    add_data_element,
    assign_service,
    build_push_endpoint_registry,
    connect_data,
    create_empty_schema,
    instantiate,
    release,
    serial_insert,
    set_automation,
    sign_body,
)
from procworks.api import app
from procworks.execution import ExecutionContext
from procworks.integration_runtime import ExternalTaskRuntime
from procworks.model import (
    AccessMode,
    AutomationKind,
    DataType,
    ExternalTaskState,
    InstanceState,
    NodeType,
    ProcessInstance,
    ProcessSchema,
    ServiceBinding,
)
from procworks.outbox import (
    PushEndpointError,
    PushEndpointRegistry,
    PushTarget,
)
from procworks.outbox import (
    build_push_endpoint_registry as _build_registry,
)
from procworks.store import (
    InMemoryExternalTaskStore,
    InMemoryInstanceStore,
    InMemorySchemaStore,
    make_resolver,
)

_ENV = "PROCWORKS_PUSH_ENDPOINTS"
_URL = "https://tool.example.com/push"

_eps = (f"ep-{n}" for n in itertools.count(1))
_topics = (f"p6-{n}" for n in itertools.count(1))


def _unique_ep() -> str:
    return next(_eps)


def _unique_tag() -> str:
    return next(_topics)


class _Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class _FakeTransport:
    """Records POSTs and returns scripted status codes (no real network)."""

    def __init__(self, status: int = 200, fail_times: int = 0) -> None:
        self.status = status
        self.fail_times = fail_times
        self.calls: list[tuple[str, bytes, dict[str, str]]] = []

    def post(
        self, url: str, body: bytes, headers: dict[str, str], timeout: float
    ) -> int:
        self.calls.append((url, body, dict(headers)))
        if self.fail_times > 0:
            self.fail_times -= 1
            return 500
        return self.status


# --- registry: build / resolve --------------------------------------------


def test_build_registry_is_empty_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_ENV, raising=False)
    registry = build_push_endpoint_registry()
    assert registry.refs() == []


def test_build_registry_from_inline_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        _ENV,
        json.dumps(
            {
                "erp": {"url": _URL, "secret_ref": "ERP_SECRET"},
                "crm": {"url": "https://crm.example.com/in"},
            }
        ),
    )
    registry = _build_registry()
    assert registry.refs() == ["crm", "erp"]
    erp = registry.resolve("erp")
    assert erp == PushTarget(url=_URL, secret_ref="ERP_SECRET")
    assert registry.resolve("crm").secret_ref == ""


def test_build_registry_from_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "endpoints.json"
    source.write_text(json.dumps({"erp": {"url": _URL}}), encoding="utf-8")
    monkeypatch.setenv(_ENV, str(source))
    registry = build_push_endpoint_registry()
    assert registry.resolve("erp").url == _URL


def test_build_registry_rejects_non_object(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_ENV, json.dumps(["not", "an", "object"]))
    with pytest.raises(PushEndpointError) as err:
        build_push_endpoint_registry()
    assert err.value.status == 422


def test_build_registry_rejects_spec_without_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_ENV, json.dumps({"erp": {"secret_ref": "X"}}))
    with pytest.raises(PushEndpointError) as err:
        build_push_endpoint_registry()
    assert err.value.status == 422


def test_registry_resolve_unknown_is_404() -> None:
    registry = PushEndpointRegistry()
    with pytest.raises(PushEndpointError) as err:
        registry.resolve("ghost")
    assert err.value.status == 404


def test_registry_refs_never_leak_targets() -> None:
    registry = PushEndpointRegistry()
    registry.register("erp", _URL, "ERP_SECRET")
    assert registry.refs() == ["erp"]
    assert registry.has("erp") and not registry.has("crm")


# --- OutboxDispatcher.push -------------------------------------------------


def _dispatcher(transport: _FakeTransport, clock: _Clock) -> OutboxDispatcher:
    return OutboxDispatcher(
        InMemoryWebhookStore(), transport=transport, now=clock
    )


def test_push_enqueues_subscriptionless_entry() -> None:
    disp = _dispatcher(_FakeTransport(), _Clock())
    entry = disp.push(_URL, "", "task.push", {"task_id": "et1"})
    assert entry.subscription_id == ""
    assert entry.state is OutboxState.PENDING
    assert entry.url == _URL


def test_push_allows_internal_target() -> None:
    # A trusted, admin-configured push URL may target a private host (the SSRF
    # allow-list does not apply); only scheme + host are enforced.
    disp = _dispatcher(_FakeTransport(), _Clock())
    entry = disp.push("http://localhost:9000/hook", "", "task.push", {"x": 1})
    assert entry.url == "http://localhost:9000/hook"


def test_push_rejects_non_http_target() -> None:
    disp = _dispatcher(_FakeTransport(), _Clock())
    with pytest.raises(Exception):  # noqa: B017 -- WebhookError on bad scheme
        disp.push("ftp://localhost/hook", "", "task.push", {})


def test_push_delivers_and_signs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUSH_SECRET", "topsecret")
    transport = _FakeTransport(status=200)
    disp = _dispatcher(transport, _Clock())
    disp.push(_URL, "PUSH_SECRET", "task.push", {"task_id": "et1"})
    assert disp.dispatch_pending() == 1
    url, body, headers = transport.calls[0]
    assert url == _URL
    assert headers["X-ProcWorks-Event"] == "task.push"
    assert headers["X-ProcWorks-Signature"] == sign_body("topsecret", body)


def test_push_without_secret_omits_signature() -> None:
    transport = _FakeTransport(status=200)
    disp = _dispatcher(transport, _Clock())
    disp.push(_URL, "", "task.push", {"task_id": "et1"})
    disp.dispatch_pending()
    _url, _body, headers = transport.calls[0]
    assert "X-ProcWorks-Signature" not in headers


# --- runtime drive_push ----------------------------------------------------


def _build_push_runtime(
    *,
    endpoint_ref: str = "ep1",
    betrag: int = 42,
    sink: object | None = None,
) -> tuple[ExternalTaskRuntime, str, _Clock, InMemoryExternalTaskStore]:
    schemas = InMemorySchemaStore()
    instances = InMemoryInstanceStore()
    schema = create_empty_schema("U", schema_id="s1")
    schema = serial_insert(schema, "Run", after_node_id="start")
    node_id = next(
        nid for nid, node in schema.nodes.items() if node.type is NodeType.ACTIVITY
    )
    schema = add_data_element(schema, "Betrag", DataType.INTEGER, element_id="betrag")
    schema = add_data_element(
        schema, "Freigabe", DataType.BOOLEAN, element_id="approved"
    )
    schema = assign_service(schema, node_id, "Run", automatic=True)
    schema = set_automation(
        schema, node_id, AutomationKind.HTTP_PUSH, endpoint_ref=endpoint_ref
    )
    schema = connect_data(schema, node_id, "betrag", AccessMode.READ, mandatory=False)
    schema = connect_data(
        schema, node_id, "approved", AccessMode.WRITE, mandatory=False
    )
    schema = release(schema)
    schemas.put(schema)

    context = ExecutionContext(make_resolver(schemas), instances)
    instance = instantiate(schema, context=context)
    instance.data_values["betrag"] = betrag
    instances.put(instance)

    tasks = InMemoryExternalTaskStore()
    clock = _Clock()

    def schema_for(inst: ProcessInstance) -> ProcessSchema:
        resolved = schemas.get(inst.schema_id)
        assert resolved is not None
        return resolved

    on_push = sink  # type: ignore[assignment]
    runtime = ExternalTaskRuntime(
        tasks, instances, schema_for, context, now=clock, on_push=on_push  # type: ignore[arg-type]
    )
    return runtime, instance.id, clock, tasks


class _Sink:
    """Records push payloads and can be told to fail on the next call."""

    def __init__(self) -> None:
        self.calls: list[tuple[ServiceBinding, dict[str, object]]] = []
        self.fail = False

    def __call__(self, binding: ServiceBinding, payload: dict[str, object]) -> None:
        if self.fail:
            raise RuntimeError("sink down")
        self.calls.append((binding, payload))


def test_drive_push_materialises_and_locks_with_token() -> None:
    sink = _Sink()
    runtime, _iid, _clock, _tasks = _build_push_runtime(sink=sink)
    pushed = runtime.drive_push()
    assert len(pushed) == 1
    task = pushed[0]
    assert task.state is ExternalTaskState.LOCKED
    assert task.topic == ""
    assert task.worker_id is not None and task.worker_id.startswith("push_")
    assert task.lock_expires_at is None
    # The payload carries the callback token and the resolved READ inputs.
    _binding, payload = sink.calls[0]
    assert payload["callback_token"] == task.worker_id
    assert payload["variables"] == {"betrag": 42}
    assert payload["task_id"] == task.id


def test_drive_push_is_idempotent() -> None:
    sink = _Sink()
    runtime, _iid, _clock, _tasks = _build_push_runtime(sink=sink)
    assert len(runtime.drive_push()) == 1
    # A second drive neither re-materialises nor re-pushes the locked task.
    assert runtime.drive_push() == []
    assert len(sink.calls) == 1


def test_drive_push_complete_via_callback_token_advances() -> None:
    sink = _Sink()
    runtime, iid, _clock, _tasks = _build_push_runtime(sink=sink)
    task = runtime.drive_push()[0]
    token = task.worker_id
    assert token is not None
    completed = runtime.complete(task.id, token, {"approved": True})
    assert completed.state is ExternalTaskState.COMPLETED
    instance = runtime._instances.get(iid)
    assert instance is not None
    assert instance.data_values["approved"] is True
    assert instance.state is InstanceState.COMPLETED


def test_drive_push_no_sink_is_noop() -> None:
    runtime, _iid, _clock, tasks = _build_push_runtime(sink=None)
    assert runtime.drive_push() == []
    assert tasks.list_tasks() == []


def test_drive_push_sink_failure_leaves_task_created() -> None:
    sink = _Sink()
    sink.fail = True
    runtime, _iid, _clock, tasks = _build_push_runtime(sink=sink)
    assert runtime.drive_push() == []
    # The task was materialised but left CREATED for a later retry; the engine
    # is untouched.
    [task] = tasks.list_tasks()
    assert task.state is ExternalTaskState.CREATED
    # Recover: once the sink is healthy the next drive pushes it.
    sink.fail = False
    pushed = runtime.drive_push()
    assert len(pushed) == 1
    assert pushed[0].state is ExternalTaskState.LOCKED


def test_drive_push_failure_backoff_then_repush() -> None:
    sink = _Sink()
    runtime, _iid, clock, _tasks = _build_push_runtime(sink=sink)
    task = runtime.drive_push()[0]
    token = task.worker_id
    assert token is not None
    failed = runtime.failure(task.id, token, "transient")
    assert failed.state is ExternalTaskState.CREATED
    # Still in back-off: a drive does not re-push yet.
    assert runtime.drive_push() == []
    assert len(sink.calls) == 1
    # After the back-off window the same task is pushed again.
    clock.t = (failed.available_at or 0.0) + 1.0
    repushed = runtime.drive_push()
    assert len(repushed) == 1
    assert repushed[0].id == task.id
    assert len(sink.calls) == 2


def test_drive_push_incident_then_resolve_repushes() -> None:
    sink = _Sink()
    runtime, _iid, _clock, _tasks = _build_push_runtime(sink=sink)
    task = runtime.drive_push()[0]
    token = task.worker_id
    assert token is not None
    failed = runtime.failure(task.id, token, "fatal", retries=0)
    assert failed.state is ExternalTaskState.INCIDENT
    # An incident task is not re-pushed.
    assert runtime.drive_push() == []
    incident = runtime.list_incidents(unresolved_only=True)[0]
    runtime.resolve_incident(incident.id)
    repushed = runtime.drive_push()
    assert [t.id for t in repushed] == [task.id]


# --- /v1 API end to end ----------------------------------------------------


client = TestClient(app)


@pytest.fixture
def push_api(monkeypatch: pytest.MonkeyPatch) -> Iterator[_FakeTransport]:
    monkeypatch.setenv("PUSH_SECRET", "api-secret")
    fake = _FakeTransport(status=200)
    monkeypatch.setattr(api_module._outbox, "_transport", fake)
    api_module._outbox._circuit.clear()
    try:
        yield fake
    finally:
        api_module._outbox._store.clear()
        api_module._outbox._circuit.clear()


def _register_endpoint(ref: str, *, secret_ref: str = "PUSH_SECRET") -> None:
    api_module._push_endpoints.register(ref, _URL, secret_ref)


def _push_schema(endpoint_ref: str) -> str:
    """Create + release a one-activity HTTP_PUSH schema; return the schema id."""

    sid = client.post("/schemas", json={"name": f"push-{endpoint_ref}"}).json()["id"]
    client.post(
        f"/schemas/{sid}/serial-insert",
        json={"label": "Run", "after_node_id": "start"},
    )
    schema = client.get(f"/schemas/{sid}").json()
    node_id = next(
        nid for nid, node in schema["nodes"].items() if node["type"] == "ACTIVITY"
    )
    client.post(
        f"/schemas/{sid}/data-elements",
        json={"name": "Betrag", "data_type": "INTEGER", "element_id": "betrag"},
    )
    client.post(
        f"/schemas/{sid}/data-elements",
        json={"name": "Freigabe", "data_type": "BOOLEAN", "element_id": "approved"},
    )
    client.post(
        f"/schemas/{sid}/service",
        json={"node_id": node_id, "name": "Run", "automatic": True},
    )
    client.post(
        f"/schemas/{sid}/automation",
        json={
            "node_id": node_id,
            "automation": "HTTP_PUSH",
            "endpoint_ref": endpoint_ref,
        },
    )
    client.post(
        f"/schemas/{sid}/data-access",
        json={"node_id": node_id, "element_id": "betrag", "mode": "READ",
              "mandatory": False},
    )
    client.post(
        f"/schemas/{sid}/data-access",
        json={"node_id": node_id, "element_id": "approved", "mode": "WRITE",
              "mandatory": False},
    )
    assert client.post(f"/schemas/{sid}/release").status_code == 200
    return sid


def test_api_start_pushes_and_callback_completes(push_api: _FakeTransport) -> None:
    ref = _unique_ep()
    _register_endpoint(ref)
    sid = _push_schema(ref)

    iid = client.post(f"/v1/schemas/{sid}/instances").json()["id"]
    client.put(f"/v1/instances/{iid}/data", json={"values": {"betrag": 1200}})
    # Starting populated the step; the very next advance is the data write, but
    # the push already fired when the step activated on instance start. Drive
    # once more to be explicit (idempotent) and capture the locked task.
    pushed = client.post("/v1/external-tasks/drive-push").json()
    # The push was delivered to the configured endpoint.
    assert len(push_api.calls) >= 1
    url, body, _headers = push_api.calls[0]
    assert url == _URL
    envelope = json.loads(body)
    payload = envelope["data"]
    assert payload["instance_id"] == iid
    token = payload["callback_token"]
    task_id = payload["task_id"]

    # The materialised task is LOCKED on the callback token (not fetch-able).
    fetched = client.post(
        "/v1/external-tasks/fetch-and-lock",
        json={"worker_id": "w1", "topics": [""], "lock_ms": 1000},
    )
    assert fetched.json() == []  # push tasks are never pulled
    assert pushed == [] or pushed[0]["state"] == "LOCKED"

    # The tool reports back through the regular completion endpoint.
    done = client.post(
        f"/v1/external-tasks/{task_id}/complete",
        json={"worker_id": token, "variables": {"approved": True}},
    )
    assert done.status_code == 200, done.text
    assert done.json()["state"] == "COMPLETED"
    instance = client.get(f"/v1/instances/{iid}").json()
    assert instance["state"] == "COMPLETED"
    assert instance["data_values"]["approved"] is True


def test_api_list_push_endpoints(push_api: _FakeTransport) -> None:
    ref = _unique_ep()
    _register_endpoint(ref)
    refs = client.get("/v1/push-endpoints").json()
    assert ref in refs


def test_api_drive_push_is_safe_when_idle(push_api: _FakeTransport) -> None:
    assert client.post("/v1/external-tasks/drive-push").json() == []
