# SPDX-License-Identifier: BUSL-1.1
"""The modelled back-off and request timeout of an automatic step take effect.

An automatic step carries ``retry_backoff_ms`` and ``request_timeout_ms`` in
its binding, and the modelling view offers both. They used to be ignored: a
failed attempt waited according to a fixed formula, and a push used the
dispatcher's fixed timeout. Now:

* the **back-off** is the wait before the first retry; every further failed
  attempt of the same task doubles it, up to 60 s or the modelled value if it
  is larger. ``0`` means "fetchable again at once". A worker that names its
  own ``retry_timeout_ms`` still decides for its report.
* the **request timeout** bounds the push delivery to the tool; ``0`` falls
  back to the dispatcher's default.
* negative values are refused at the boundary (422).
* both are bounded above in the validator -- back-off at most 24 hours,
  request timeout at most 30 seconds (the push runs inside the request that
  advanced the case, so its timeout bounds that request). A larger value
  stored before the bound is capped at run time, never a 500.
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient
from planted import plant

import procworks.api as api_module
from procworks import (
    CorrectnessError,
    add_data_element,
    assign_service,
    connect_data,
    create_empty_schema,
    instantiate,
    release,
    serial_insert,
    set_automation,
)
from procworks.api import app
from procworks.bpmn import export_bpmn, import_bpmn
from procworks.execution import ExecutionContext
from procworks.integration_runtime import ExternalTaskRuntime
from procworks.migration import check_migration
from procworks.model import (
    MAX_REQUEST_TIMEOUT_MS,
    MAX_RETRY_BACKOFF_MS,
    AccessMode,
    AutomationKind,
    DataType,
    ExternalTaskState,
    NodeType,
    ProcessInstance,
    ProcessSchema,
    ServiceBinding,
)
from procworks.store import (
    InMemoryExternalTaskStore,
    InMemoryInstanceStore,
    InMemorySchemaStore,
    make_resolver,
)

client = TestClient(app)
_refs = (f"timeout-ep-{n}" for n in itertools.count(1))


class _Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def _runtime(
    *,
    retry_max: int,
    backoff_ms: int,
    kind: AutomationKind = AutomationKind.EXTERNAL_TASK,
    on_push: object | None = None,
    age: Callable[[ProcessSchema], ProcessSchema] | None = None,
) -> tuple[ExternalTaskRuntime, _Clock]:
    """A one-step schema with an automatic step and the given robustness values.

    ``age`` changes the released schema behind the operations, as a schema
    stored before a rule existed would be found."""

    schemas = InMemorySchemaStore()
    instances = InMemoryInstanceStore()
    schema = create_empty_schema("Robust", schema_id="robust")
    schema = serial_insert(schema, "Pruefen", after_node_id="start")
    node_id = next(n.id for n in schema.nodes.values() if n.type is NodeType.ACTIVITY)
    schema = add_data_element(schema, "Ok", DataType.BOOLEAN, element_id="ok")
    schema = assign_service(schema, node_id, "Pruefen", automatic=True)
    if kind is AutomationKind.EXTERNAL_TASK:
        schema = set_automation(
            schema, node_id, kind, topic="robust",
            retry_max=retry_max, retry_backoff_ms=backoff_ms,
        )
    else:
        schema = set_automation(
            schema, node_id, kind, endpoint_ref="ep",
            retry_max=retry_max, retry_backoff_ms=backoff_ms,
        )
    schema = connect_data(schema, node_id, "ok", AccessMode.WRITE, mandatory=False)
    schema = release(schema)
    if age is not None:
        schema = age(schema)
    schemas.put(schema)
    context = ExecutionContext(make_resolver(schemas), instances)
    instances.put(instantiate(schema, context=context))
    clock = _Clock()

    def schema_for(inst: ProcessInstance) -> ProcessSchema:
        resolved = schemas.get(inst.schema_id)
        assert resolved is not None
        return resolved

    runtime = ExternalTaskRuntime(
        InMemoryExternalTaskStore(), instances, schema_for, context,
        now=clock, on_push=on_push,  # type: ignore[arg-type]
    )
    return runtime, clock


def _fail_once(runtime: ExternalTaskRuntime, clock: _Clock) -> float:
    """Fetch the task, report one failure, return its delay in seconds."""

    [task] = runtime.fetch_and_lock("w1", ["robust"], lock_ms=60_000)
    failed = runtime.failure(task.id, "w1", "upstream down")
    assert failed.state is ExternalTaskState.CREATED
    assert failed.available_at is not None
    delay = failed.available_at - clock.t
    clock.t = failed.available_at
    return delay


def test_the_modelled_back_off_decides_the_first_retry() -> None:
    runtime, clock = _runtime(retry_max=3, backoff_ms=60_000)

    assert _fail_once(runtime, clock) == pytest.approx(60.0)


def test_each_further_failure_doubles_the_modelled_back_off() -> None:
    runtime, clock = _runtime(retry_max=6, backoff_ms=1_000)

    delays = [_fail_once(runtime, clock) for _ in range(4)]

    assert delays == pytest.approx([1.0, 2.0, 4.0, 8.0])


def test_a_back_off_above_the_cap_is_not_cut_down() -> None:
    runtime, clock = _runtime(retry_max=4, backoff_ms=120_000)

    delays = [_fail_once(runtime, clock) for _ in range(2)]

    assert delays == pytest.approx([120.0, 120.0])


def test_a_zero_back_off_makes_the_task_fetchable_at_once() -> None:
    runtime, clock = _runtime(retry_max=3, backoff_ms=0)

    assert _fail_once(runtime, clock) == 0.0
    [again] = runtime.fetch_and_lock("w2", ["robust"], lock_ms=60_000)
    assert again.worker_id == "w2"


def test_a_worker_named_retry_timeout_still_wins() -> None:
    runtime, clock = _runtime(retry_max=3, backoff_ms=60_000)
    [task] = runtime.fetch_and_lock("w1", ["robust"], lock_ms=60_000)

    failed = runtime.failure(task.id, "w1", "busy", retry_timeout_ms=500)

    assert failed.available_at == pytest.approx(clock.t + 0.5)


def test_a_failed_push_waits_the_modelled_back_off() -> None:
    def refuse(binding: ServiceBinding, payload: dict[str, object]) -> bool:
        return False

    runtime, clock = _runtime(
        retry_max=3, backoff_ms=45_000, kind=AutomationKind.HTTP_PUSH, on_push=refuse
    )

    assert runtime.drive_push() == []
    [task] = runtime.list_tasks()
    assert task.available_at == pytest.approx(clock.t + 45.0)


@pytest.mark.parametrize("field", ["retry_backoff_ms", "request_timeout_ms", "retry_max"])
def test_negative_robustness_values_are_refused(field: str) -> None:
    sid = client.post("/schemas", json={"name": f"Robust negativ {field}"}).json()["id"]
    client.post(f"/schemas/{sid}/serial-insert", json={"label": "Lauf", "after_node_id": "start"})
    nodes = client.get(f"/schemas/{sid}").json()["nodes"]
    node_id = next(nid for nid, n in nodes.items() if n["type"] == "ACTIVITY")
    client.post(f"/schemas/{sid}/service", json={"node_id": node_id, "name": "Lauf",
                                                 "automatic": True})

    resp = client.post(f"/schemas/{sid}/automation", json={
        "node_id": node_id, "automation": "EXTERNAL_TASK", "topic": "neg", field: -1})

    assert resp.status_code == 422
    [error] = resp.json()["detail"]
    assert error["loc"] == ["body", field]
    assert error["type"] == "greater_than_equal"


# --- request timeout of a push --------------------------------------------


class _TimeoutTransport:
    """Records the timeout of every POST (no real network)."""

    def __init__(self) -> None:
        self.timeouts: list[float] = []

    def post(self, url: str, body: bytes, headers: dict[str, str], timeout: float) -> int:
        self.timeouts.append(timeout)
        return 200


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> Iterator[_TimeoutTransport]:
    fake = _TimeoutTransport()
    monkeypatch.setattr(api_module._outbox, "_transport", fake)
    api_module._outbox._circuit.clear()
    try:
        yield fake
    finally:
        api_module._outbox._circuit.clear()


def _push_schema(ref: str, timeout_ms: int) -> str:
    sid = client.post("/schemas", json={"name": f"Push {ref}"}).json()["id"]
    client.post(f"/schemas/{sid}/serial-insert", json={"label": "Melden", "after_node_id": "start"})
    nodes = client.get(f"/schemas/{sid}").json()["nodes"]
    node_id = next(nid for nid, n in nodes.items() if n["type"] == "ACTIVITY")
    client.post(f"/schemas/{sid}/service", json={"node_id": node_id, "name": "Melden",
                                                 "automatic": True})
    set_ok = client.post(f"/schemas/{sid}/automation", json={
        "node_id": node_id, "automation": "HTTP_PUSH", "endpoint_ref": ref,
        "request_timeout_ms": timeout_ms})
    assert set_ok.status_code == 200, set_ok.text
    assert client.post(f"/schemas/{sid}/release").status_code == 200
    return sid


@pytest.mark.parametrize(("timeout_ms", "expected_s"), [(1_500, 1.5), (0, 5.0)])
def test_the_push_uses_the_modelled_request_timeout(
    transport: _TimeoutTransport, timeout_ms: int, expected_s: float
) -> None:
    ref = next(_refs)
    api_module._push_endpoints.register(ref, "https://tool.example.com/push")
    sid = _push_schema(ref, timeout_ms)

    assert client.post(f"/v1/schemas/{sid}/instances").status_code == 201

    assert transport.timeouts == [pytest.approx(expected_s)]


# --- upper bounds ---------------------------------------------------------


def _automated(**values: int) -> ProcessSchema:
    schema = create_empty_schema("Grenzen", schema_id="grenzen")
    schema = serial_insert(schema, "Lauf", after_node_id="start")
    node_id = next(n.id for n in schema.nodes.values() if n.type is NodeType.ACTIVITY)
    schema = assign_service(schema, node_id, "Lauf", automatic=True)
    return set_automation(
        schema, node_id, AutomationKind.EXTERNAL_TASK, topic="grenzen", **values
    )


def test_the_bounds_are_one_day_and_thirty_seconds() -> None:
    assert MAX_RETRY_BACKOFF_MS == 24 * 3600 * 1000
    assert MAX_REQUEST_TIMEOUT_MS == 30_000


def test_values_up_to_the_bounds_are_accepted() -> None:
    schema = _automated(
        retry_backoff_ms=MAX_RETRY_BACKOFF_MS, request_timeout_ms=MAX_REQUEST_TIMEOUT_MS
    )
    [binding] = schema.service_bindings.values()
    assert (binding.retry_backoff_ms, binding.request_timeout_ms) == (
        MAX_RETRY_BACKOFF_MS, MAX_REQUEST_TIMEOUT_MS
    )


@pytest.mark.parametrize(
    ("field", "value", "code", "bound"),
    [
        ("retry_backoff_ms", MAX_RETRY_BACKOFF_MS + 1, "I1.backoff-too-large",
         MAX_RETRY_BACKOFF_MS),
        ("retry_backoff_ms", 10**400, "I1.backoff-too-large", MAX_RETRY_BACKOFF_MS),
        ("request_timeout_ms", MAX_REQUEST_TIMEOUT_MS + 1, "I1.timeout-too-large",
         MAX_REQUEST_TIMEOUT_MS),
        ("request_timeout_ms", 10**12, "I1.timeout-too-large", MAX_REQUEST_TIMEOUT_MS),
    ],
    ids=["backoff", "backoff-riesig", "timeout", "timeout-riesig"],
)
def test_a_value_above_the_bound_is_refused_for_that_reason(
    field: str, value: int, code: str, bound: int
) -> None:
    with pytest.raises(CorrectnessError) as exc:
        _automated(**{field: value})

    [finding] = exc.value.findings
    assert (finding.rule, finding.code) == ("I1", code)
    assert finding.params["max"] == str(bound)


def test_api_refuses_a_huge_back_off_with_the_code() -> None:
    sid = client.post("/schemas", json={"name": "Robust riesig"}).json()["id"]
    client.post(f"/schemas/{sid}/serial-insert", json={"label": "Lauf", "after_node_id": "start"})
    nodes = client.get(f"/schemas/{sid}").json()["nodes"]
    node_id = next(nid for nid, n in nodes.items() if n["type"] == "ACTIVITY")
    client.post(f"/schemas/{sid}/service", json={"node_id": node_id, "name": "Lauf",
                                                 "automatic": True})

    resp = client.post(
        f"/schemas/{sid}/automation",
        content=(
            f'{{"node_id": "{node_id}", "automation": "EXTERNAL_TASK", "topic": "x", '
            f'"retry_backoff_ms": {10**400}}}'
        ),
        headers={"content-type": "application/json"},
    )

    assert resp.status_code == 422, resp.text
    assert [f["code"] for f in resp.json()["detail"]["findings"]] == ["I1.backoff-too-large"]


def test_bpmn_import_refuses_a_too_large_timeout() -> None:
    xml = export_bpmn(release(_automated(request_timeout_ms=1_000)))
    aged, count = re.subn(
        r"(request_timeout_ms(?:&quot;|\")\s*:\s*)1000", r"\g<1>3000000", xml
    )
    assert count == 1

    with pytest.raises(CorrectnessError) as exc:
        import_bpmn(aged, schema_id="grenzen-import")

    assert [f.code for f in exc.value.findings] == ["I1.timeout-too-large"]


# --- stored values beyond the bounds ----------------------------------------


def _with(**values: int) -> Callable[[ProcessSchema], ProcessSchema]:
    def age(schema: ProcessSchema) -> ProcessSchema:
        aged = schema.model_copy(deep=True)
        for binding in aged.service_bindings.values():
            for field, value in values.items():
                setattr(binding, field, value)
        return aged

    return age


@pytest.mark.parametrize(
    "stored", [MAX_RETRY_BACKOFF_MS * 10, 10**400], ids=["zehn-tage", "riesig"]
)
def test_a_stored_huge_back_off_waits_one_day_instead_of_failing(stored: int) -> None:
    """``10**400`` ms used to end the failure report in an OverflowError (500)."""

    runtime, clock = _runtime(retry_max=3, backoff_ms=1_000, age=_with(retry_backoff_ms=stored))

    assert _fail_once(runtime, clock) == pytest.approx(MAX_RETRY_BACKOFF_MS / 1000)


@pytest.mark.parametrize("stored", [10**12, 10**400], ids=["e12", "riesig"])
def test_a_stored_huge_request_timeout_is_capped_for_the_push(
    transport: _TimeoutTransport, stored: int
) -> None:
    ref = next(_refs)
    api_module._push_endpoints.register(ref, "https://tool.example.com/push")
    sid = _push_schema(ref, 1_000)
    original = api_module._store.get(sid)
    assert original is not None
    plant(api_module._store, _with(request_timeout_ms=stored)(original))

    started = client.post(f"/v1/schemas/{sid}/instances")

    assert started.status_code == 201, started.text
    assert transport.timeouts == [pytest.approx(MAX_REQUEST_TIMEOUT_MS / 1000)]


@pytest.mark.parametrize("stored", [10**12, 10**400], ids=["e12", "riesig"])
def test_the_push_claim_of_a_stored_huge_timeout_stays_bounded(stored: int) -> None:
    """The claim must expire after the longest delivery -- capped like it."""

    binding = ServiceBinding(node_id="n", name="Melden", request_timeout_ms=stored)

    claim = ExternalTaskRuntime._push_claim_seconds(binding)

    capped = ServiceBinding(node_id="n", name="Melden", request_timeout_ms=MAX_REQUEST_TIMEOUT_MS)
    assert claim == ExternalTaskRuntime._push_claim_seconds(capped)


def test_a_case_may_move_onto_a_target_with_a_stored_huge_back_off() -> None:
    """The runtime caps the old value, so the old finding does not block M1."""

    source = release(_automated(retry_backoff_ms=1_000))
    target = _with(retry_backoff_ms=10**400)(source).model_copy(
        update={"id": "grenzen-v2", "version": 2, "revision_of": source.id}
    )
    instances = InMemoryInstanceStore()
    schemas = InMemorySchemaStore()
    schemas.put(source)
    case = instantiate(source, context=ExecutionContext(make_resolver(schemas), instances))

    assert check_migration(case, source, target) == []
