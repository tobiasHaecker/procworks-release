# SPDX-License-Identifier: BUSL-1.1
"""Tests for the built-in demo data and the admin reset endpoint.

Covers the pure :func:`procworks.demo.load_demo` loader (org, two schemas, three
instances at different points, monitoring KPIs) and the ``POST /admin/reset``
maintenance endpoint that wipes the system to zero and optionally reloads the
demo -- including the RBAC gate and the login-preservation guarantee.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

import procworks.api as api_module
from procworks import demo
from procworks.api import app
from procworks.audit import InMemoryAuditLog, compute_kpis, discover_process_map
from procworks.auth_password import (
    InMemoryCredentialStore,
    PasswordAuthBackend,
    User,
    hash_password,
)
from procworks.execution import ExecutionContext, complete_activity, instantiate
from procworks.model import (
    AccessMode,
    DataSourceKind,
    DataType,
    InstanceState,
    LifecycleState,
    StaffRuleKind,
    ValueClass,
)
from procworks.store import (
    InMemoryInstanceStore,
    InMemoryOrgStore,
    InMemorySchemaStore,
    hydrate_org,
    make_org_resolver,
    make_resolver,
)

client = TestClient(app)


# --- pure loader ----------------------------------------------------------


def _fresh_stores() -> tuple[
    InMemorySchemaStore, InMemoryInstanceStore, InMemoryOrgStore, InMemoryAuditLog
]:
    return (
        InMemorySchemaStore(),
        InMemoryInstanceStore(),
        InMemoryOrgStore(),
        InMemoryAuditLog(),
    )


def test_load_demo_builds_two_schemas_and_one_org() -> None:
    ss, ins, orgs, log = _fresh_stores()
    demo.load_demo(schema_store=ss, instance_store=ins, org_store=orgs, audit_log=log)

    assert set(ss.list_ids()) == {demo.SCHEMA_URLAUB, demo.SCHEMA_BESCHAFFUNG}
    assert orgs.list_ids() == [demo.ORG_ID]

    org_resolver = make_org_resolver(orgs)
    urlaub = hydrate_org(ss.get(demo.SCHEMA_URLAUB), org_resolver)  # type: ignore[arg-type]
    beschaffung = ss.get(demo.SCHEMA_BESCHAFFUNG)
    assert urlaub.lifecycle_state is LifecycleState.RELEASED
    assert beschaffung is not None
    assert beschaffung.lifecycle_state is LifecycleState.ENTWURF
    # The released schema resolves its staffing against the shared org.
    assert urlaub.org_model_id == demo.ORG_ID
    assert "a-erika" in urlaub.org_model.agents


def test_load_demo_creates_three_instances_at_different_points() -> None:
    ss, ins, orgs, log = _fresh_stores()
    demo.load_demo(schema_store=ss, instance_store=ins, org_store=orgs, audit_log=log)

    states = {iid: ins.get(iid).state for iid in ins.list_ids()}  # type: ignore[union-attr]
    assert len(states) == 3
    assert sum(s is InstanceState.RUNNING for s in states.values()) == 2
    assert sum(s is InstanceState.COMPLETED for s in states.values()) == 1
    # None of the demo instances is a throw-away test instance.
    assert all(not ins.get(iid).is_test for iid in ins.list_ids())  # type: ignore[union-attr]


def test_load_demo_feeds_monitoring_kpis_and_process_map() -> None:
    ss, ins, orgs, log = _fresh_stores()
    demo.load_demo(schema_store=ss, instance_store=ins, org_store=orgs, audit_log=log)

    report = compute_kpis(log.list_all())
    assert report.total_instances == 3
    assert report.running == 2
    assert report.completed == 1
    pmap = discover_process_map(log.list_all())
    assert len(pmap.edges) >= 1


def test_load_demo_seeds_logins_only_with_password_backend() -> None:
    ss, ins, orgs, log = _fresh_stores()
    backend = PasswordAuthBackend(InMemoryCredentialStore())
    seeded = demo.load_demo(
        schema_store=ss,
        instance_store=ins,
        org_store=orgs,
        audit_log=log,
        password_backend=backend,
    )
    assert seeded == len(demo.DEMO_USERS)
    assert backend.store.get_user("erika.sander") is not None


def test_load_demo_is_idempotent_for_users() -> None:
    ss, ins, orgs, log = _fresh_stores()
    backend = PasswordAuthBackend(InMemoryCredentialStore())
    demo.load_demo(
        schema_store=ss, instance_store=ins, org_store=orgs, audit_log=log,
        password_backend=backend,
    )
    # A second load over the same backend must not duplicate logins.
    again = demo.load_demo(
        schema_store=ss, instance_store=ins, org_store=orgs, audit_log=log,
        password_backend=backend,
    )
    assert again == 0


def _node_id(schema, label):  # type: ignore[no-untyped-def]
    return next(n.id for n in schema.nodes.values() if n.label == label)


def _accessors(schema, element_id, mode):  # type: ignore[no-untyped-def]
    return {
        a.node_id
        for a in schema.data_accesses
        if a.element_id == element_id and a.mode is mode
    }


def test_demo_urlaub_carries_enriched_decision_object() -> None:
    # The "entscheidung" object is filled by whichever XOR branch runs and read
    # by the notification afterwards -> a data object that travels and is
    # enriched across activities (D1 holds via the XOR-join intersection).
    ss, ins, orgs, log = _fresh_stores()
    demo.load_demo(schema_store=ss, instance_store=ins, org_store=orgs, audit_log=log)
    urlaub = ss.get(demo.SCHEMA_URLAUB)
    assert urlaub is not None

    assert urlaub.data_elements["entscheidung"].data_type is DataType.STRING
    genehmigung = _node_id(urlaub, "Genehmigung durch Leitung")
    ablehnung = _node_id(urlaub, "Ablehnung dokumentieren")
    benachrichtigen = _node_id(urlaub, "Mitarbeiter benachrichtigen")
    # Both branches write it, the notification reads it.
    assert {genehmigung, ablehnung} <= _accessors(urlaub, "entscheidung", AccessMode.WRITE)
    assert benachrichtigen in _accessors(urlaub, "entscheidung", AccessMode.READ)


def test_demo_completed_instance_holds_enriched_values() -> None:
    # The finished, rejected instance must carry both the captured "tage" and
    # the "entscheidung" written by the rejection step (object passed along).
    ss, ins, orgs, log = _fresh_stores()
    demo.load_demo(schema_store=ss, instance_store=ins, org_store=orgs, audit_log=log)

    finished = ins.get("urlaub-2026-003")
    assert finished is not None
    assert finished.state is InstanceState.COMPLETED
    assert finished.data_values.get("tage") == 20
    assert "Abgelehnt" in str(finished.data_values.get("entscheidung", ""))


def test_demo_beschaffung_wires_parallel_data_objects() -> None:
    # Two objects filled on parallel branches and merged at the final activity:
    # "betrag" (Angebote einholen) and "budget_ok" (Budget pruefen) are both
    # read by "Bestellung freigeben".
    ss, ins, orgs, log = _fresh_stores()
    demo.load_demo(schema_store=ss, instance_store=ins, org_store=orgs, audit_log=log)
    besch = ss.get(demo.SCHEMA_BESCHAFFUNG)
    assert besch is not None

    assert besch.data_elements["betrag"].data_type is DataType.FLOAT
    assert besch.data_elements["budget_ok"].data_type is DataType.BOOLEAN
    angebote = _node_id(besch, "Angebote einholen")
    budget = _node_id(besch, "Budget pr\u00fcfen")
    freigeben = _node_id(besch, "Bestellung freigeben")
    assert angebote in _accessors(besch, "betrag", AccessMode.WRITE)
    assert budget in _accessors(besch, "budget_ok", AccessMode.WRITE)
    assert freigeben in _accessors(besch, "betrag", AccessMode.READ)
    assert freigeben in _accessors(besch, "budget_ok", AccessMode.READ)


def test_demo_urlaub_showcases_mask_valueclass_priority_and_time() -> None:
    # The released schema demonstrates the presentation/analytical features so
    # every view (mask designer, value breakdown, worklist priority, time) has
    # something to show.
    ss, ins, orgs, log = _fresh_stores()
    demo.load_demo(schema_store=ss, instance_store=ins, org_store=orgs, audit_log=log)
    urlaub = ss.get(demo.SCHEMA_URLAUB)
    assert urlaub is not None

    erfassen = _node_id(urlaub, "Antrag erfassen")
    # Input mask (form designer) with a number field and an optional text area.
    mask = urlaub.forms.get(erfassen)
    assert mask is not None
    assert {f.element_id for f in mask.fields} == {"tage", "grund"}

    # All three value classes appear, a priority is set and the temporal
    # perspective is populated (per-step durations + a process deadline).
    classes = {n.value_class for n in urlaub.nodes.values() if n.value_class is not None}
    assert classes == {
        ValueClass.VALUE_ADDING,
        ValueClass.BUSINESS_NECESSARY,
        ValueClass.NON_VALUE_ADDING,
    }
    assert urlaub.node_priorities  # at least one prioritised step
    assert urlaub.time_constraints  # per-step target durations
    assert urlaub.deadline_seconds is not None


def test_demo_beschaffung_showcases_connector_and_staff_rules() -> None:
    # The draft schema demonstrates the advanced/integration features: a
    # connector with a CbC-safe scalar SQL binding and structured staff rules
    # (role, org-unit + OR combinator). Every step is interactive, so the flow is
    # completable in the GUI without an external worker.
    ss, ins, orgs, log = _fresh_stores()
    demo.load_demo(schema_store=ss, instance_store=ins, org_store=orgs, audit_log=log)
    besch = ss.get(demo.SCHEMA_BESCHAFFUNG)
    assert besch is not None

    # Connector + scalar SQL-bound EXTERNAL element (C1/C4-C6).
    assert "erp" in besch.connectors
    kreditlimit = besch.data_elements["kreditlimit"]
    assert kreditlimit.source is DataSourceKind.EXTERNAL
    assert kreditlimit.select is not None
    assert kreditlimit.select.connector_id == "erp"

    # The offer step is interactive (staff rule + input mask that supplies
    # betrag/lieferant_nr), so a person can complete it -- no external worker.
    angebote = _node_id(besch, "Angebote einholen")
    assert angebote not in besch.service_bindings
    assert besch.staff_rules[angebote].kind is StaffRuleKind.ROLE
    assert {f.element_id for f in besch.forms[angebote].fields} == {"betrag", "lieferant_nr"}

    # Structured staff rules: an org-unit leaf and an OR combinator.
    budget = _node_id(besch, "Budget pr\u00fcfen")
    freigeben = _node_id(besch, "Bestellung freigeben")
    assert besch.staff_rules[budget].kind is StaffRuleKind.ORG_UNIT
    assert besch.staff_rules[freigeben].kind is StaffRuleKind.OR


def test_demo_beschaffung_flow_completes_without_worker() -> None:
    # Regression: the procurement demo must be playable end-to-end by a person in
    # the GUI -- no external worker. Every step is interactive; filling the input
    # masks supplies betrag/lieferant_nr/budget_ok and the instance reaches
    # COMPLETED (previously "Angebote einholen" was an automatic external task and
    # the flow got stuck with the values never set).
    ss, ins, orgs, log = _fresh_stores()
    demo.load_demo(schema_store=ss, instance_store=ins, org_store=orgs, audit_log=log)
    besch = hydrate_org(ss.get(demo.SCHEMA_BESCHAFFUNG), make_org_resolver(orgs))  # type: ignore[arg-type]

    ctx = ExecutionContext(make_resolver(InMemorySchemaStore()), ins)
    inst = instantiate(
        besch, instance_id="besch-test-1", context=ctx, allow_unreleased=True, is_test=True
    )

    angebote = _node_id(besch, "Angebote einholen")
    budget = _node_id(besch, "Budget pr\u00fcfen")
    freigeben = _node_id(besch, "Bestellung freigeben")

    # A person fills the mask on the offer step -> the two values are set here.
    inst = complete_activity(
        inst, besch, angebote, {"betrag": 1200.0, "lieferant_nr": 42}, context=ctx
    )
    inst = complete_activity(inst, besch, budget, {"budget_ok": True}, context=ctx)
    inst = complete_activity(inst, besch, freigeben, None, context=ctx)

    assert inst.state is InstanceState.COMPLETED
    assert inst.data_values["betrag"] == 1200.0
    assert inst.data_values["lieferant_nr"] == 42
    assert inst.data_values["budget_ok"] is True


def test_demo_org_has_two_level_hierarchy() -> None:
    # The shared org forms a real tree (management over sales and purchasing) so
    # the org chart shows more than a flat list.
    ss, ins, orgs, log = _fresh_stores()
    demo.load_demo(schema_store=ss, instance_store=ins, org_store=orgs, audit_log=log)
    org = orgs.get(demo.ORG_ID)
    assert org is not None

    assert org.org_units["vertrieb"].parent_id == "leitung"
    assert org.org_units["einkauf-abt"].parent_id == "leitung"
    assert org.org_units["leitung"].manager_id == "a-sabine"


# --- store clear ----------------------------------------------------------


def test_in_memory_stores_clear() -> None:
    ss, ins, orgs, log = _fresh_stores()
    demo.load_demo(schema_store=ss, instance_store=ins, org_store=orgs, audit_log=log)
    ss.clear()
    ins.clear()
    orgs.clear()
    log.clear()
    assert ss.list_ids() == []
    assert ins.list_ids() == []
    assert orgs.list_ids() == []
    assert log.list_all() == []


# --- admin reset endpoint -------------------------------------------------


@pytest.fixture
def clean_api() -> Iterator[None]:
    """Isolate the module-global stores so a reset never touches other tests."""

    saved = (
        api_module._store,
        api_module._instances,
        api_module._org_store,
        api_module._audit,
        api_module._resolver,
        api_module._org_resolver,
        api_module._context,
    )
    api_module._store = InMemorySchemaStore()
    api_module._instances = InMemoryInstanceStore()
    api_module._org_store = InMemoryOrgStore()
    api_module._audit = InMemoryAuditLog()
    api_module._resolver = make_resolver(api_module._store)
    api_module._org_resolver = make_org_resolver(api_module._org_store)
    api_module._context = ExecutionContext(api_module._resolver, api_module._instances)
    try:
        yield
    finally:
        (
            api_module._store,
            api_module._instances,
            api_module._org_store,
            api_module._audit,
            api_module._resolver,
            api_module._org_resolver,
            api_module._context,
        ) = saved


@pytest.fixture
def clean_password_api(clean_api: None) -> Iterator[PasswordAuthBackend]:
    """As ``clean_api`` but also swap in a fresh password backend with an admin."""

    original = api_module._auth_backend
    backend = PasswordAuthBackend(InMemoryCredentialStore())
    backend.store.put_user(
        User(
            login="admin",
            password_hash=hash_password("admin-pw1"),
            subject="admin",
            roles=frozenset({"admin"}),
            display_name="Ada Admin",
            must_change=False,
        )
    )
    api_module._auth_backend = backend
    try:
        yield backend
    finally:
        api_module._auth_backend = original


def test_admin_reset_loads_and_clears_demo(clean_api: None) -> None:
    # Open dev mode grants admin -> load the demo, then wipe to zero.
    loaded = client.post("/admin/reset", json={"load_demo": True})
    assert loaded.status_code == 200
    body = loaded.json()
    assert body["demo_loaded"] is True
    assert body["schemas"] == 2
    assert body["instances"] == 3
    assert body["org_models"] == 1
    assert set(client.get("/schemas").json()) == {
        demo.SCHEMA_URLAUB,
        demo.SCHEMA_BESCHAFFUNG,
    }
    assert len(client.get("/instances").json()) == 3

    emptied = client.post("/admin/reset", json={"load_demo": False})
    assert emptied.status_code == 200
    empty_body = emptied.json()
    assert empty_body["schemas"] == 0
    assert empty_body["instances"] == 0
    assert empty_body["org_models"] == 0
    assert client.get("/schemas").json() == []


def _login(backend: PasswordAuthBackend, login: str) -> str:
    return backend.login(login, "admin-pw1").token


def test_admin_reset_requires_admin(clean_password_api: PasswordAuthBackend) -> None:
    backend = clean_password_api
    backend.store.put_user(
        User(
            login="vera.viewer",
            password_hash=hash_password("admin-pw1"),
            subject="vera.viewer",
            roles=frozenset({"viewer"}),
            must_change=False,
        )
    )
    token = backend.login("vera.viewer", "admin-pw1").token
    resp = client.post(
        "/admin/reset",
        json={"load_demo": False},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


def test_admin_reset_keeps_acting_admin_login(
    clean_password_api: PasswordAuthBackend,
) -> None:
    backend = clean_password_api
    backend.store.put_user(
        User(
            login="leftover.user",
            password_hash=hash_password("admin-pw1"),
            subject="leftover.user",
            roles=frozenset({"operator"}),
            must_change=False,
        )
    )
    token = _login(backend, "admin")
    resp = client.post(
        "/admin/reset",
        json={"load_demo": False},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    # The acting admin survives; the unrelated login is wiped.
    assert backend.store.get_user("admin") is not None
    assert backend.store.get_user("leftover.user") is None
    # The admin's session is still valid afterwards.
    assert client.get("/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 200


def test_admin_reset_demo_seeds_usable_logins(
    clean_password_api: PasswordAuthBackend,
) -> None:
    backend = clean_password_api
    token = _login(backend, "admin")
    resp = client.post(
        "/admin/reset",
        json={"load_demo": True},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    # The demo operator login works out of the box (no forced change).
    erika = backend.login("erika.sander", demo.DEMO_PASSWORD)
    assert "operator" in erika.principal.roles
    assert erika.principal.agent_id == "a-erika"
    # ... and she has an open task from the freshly loaded instances.
    tasks = client.get(
        "/me/tasks", headers={"Authorization": f"Bearer {erika.token}"}
    )
    assert tasks.status_code == 200
    assert len(tasks.json()) >= 1
