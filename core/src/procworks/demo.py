# SPDX-License-Identifier: BUSL-1.1
"""Built-in demo data set and the one-shot loader behind the admin reset.

A fresh kernel is empty, which makes it hard to grasp what the tool can do. The
:func:`load_demo` loader populates the stores with a small but complete world so
every view has something to show: one shared organisation, two example
processes (one *released*, one *draft*), three running/finished instances at
different points, process variables and -- in password mode -- a handful of
ready-to-use logins.

The data is built exclusively through the public operations (the same
validate-before-commit path every client uses), so the demo can never create an
incorrect schema. Loading is wired to ``POST /admin/reset`` (admin only); the
same endpoint also wipes everything back to an empty system.
"""

from __future__ import annotations

from procworks import execution as exe
from procworks import operations as ops
from procworks import org as org_ops
from procworks.audit import AuditLog, EventType
from procworks.auth_password import (
    PasswordAuthBackend,
    User,
    hash_password,
)
from procworks.model import (
    AccessMode,
    Cardinality,
    ConnectorKind,
    DataType,
    FilterOperator,
    ImpactUrgency,
    InstanceState,
    NodeType,
    OrgModel,
    ProcessInstance,
    ProcessSchema,
    QueryFilter,
    StaffRule,
    StaffRuleKind,
    TimeConstraint,
    ValueClass,
    WidgetKind,
    WorkItemPriority,
)
from procworks.store import (
    InstanceStore,
    OrgStore,
    SchemaStore,
    dehydrate_org,
    make_resolver,
)

#: Stable ids so the demo is recognisable and reset-idempotent.
ORG_ID = "org-acme"
SCHEMA_URLAUB = "urlaubsantrag"
SCHEMA_BESCHAFFUNG = "beschaffung"

#: Shared password for every seeded demo login (documented in the README).
#: The demo users skip the forced first-change so they work out of the box.
DEMO_PASSWORD = "demo-procworks"

#: The demo logins seeded in password mode: (login, name, roles, agent id).
DEMO_USERS: list[tuple[str, str, frozenset[str], str | None]] = [
    ("mara.modell", "Mara Modell", frozenset({"modeler"}), None),
    ("erika.sander", "Erika Sander", frozenset({"operator"}), "a-erika"),
    ("tom.berger", "Tom Berger", frozenset({"operator"}), "a-tom"),
    ("vera.viewer", "Vera Viewer", frozenset({"viewer"}), None),
]


def _nid(schema: ProcessSchema, label: str) -> str:
    """Return the id of the (unique) node carrying ``label``."""

    return next(n.id for n in schema.nodes.values() if n.label == label)


def _gateway_id(schema: ProcessSchema, node_type: NodeType) -> str:
    """Return the id of the (unique) gateway node of ``node_type``."""

    return next(n.id for n in schema.nodes.values() if n.type is node_type)


def _label(schema: ProcessSchema, node_id: str) -> str | None:
    node = schema.nodes.get(node_id)
    return node.label if node is not None else None


def _role(role_id: str) -> StaffRule:
    return StaffRule(kind=StaffRuleKind.ROLE, ref=role_id)


def _build_org() -> OrgModel:
    """The shared organisation reused by both example processes."""

    org = org_ops.create_org_model("ACME Mittelstand GmbH", org_id=ORG_ID)
    org = org_ops.org_add_role(org, "Sachbearbeiter", role_id="sachbearbeiter")
    org = org_ops.org_add_role(org, "Teamleitung", role_id="teamleitung")
    org = org_ops.org_add_role(org, "Einkauf", role_id="einkauf")
    org = org_ops.org_add_unit(org, "Gesch\u00e4ftsleitung", org_unit_id="leitung")
    org = org_ops.org_add_unit(org, "Vertrieb", org_unit_id="vertrieb")
    org = org_ops.org_add_unit(org, "Einkauf", org_unit_id="einkauf-abt")
    org = org_ops.org_add_agent(
        org, "Sabine Chef", role_ids=["teamleitung"], org_unit_id="leitung", agent_id="a-sabine"
    )
    org = org_ops.org_add_agent(
        org, "Erika Sander", role_ids=["sachbearbeiter"], org_unit_id="vertrieb", agent_id="a-erika"
    )
    org = org_ops.org_add_agent(
        org, "Tom Berger", role_ids=["teamleitung"], org_unit_id="vertrieb", agent_id="a-tom"
    )
    org = org_ops.org_add_agent(
        org,
        "Nina Wolf",
        role_ids=["sachbearbeiter"],
        org_unit_id="vertrieb",
        agent_id="a-nina",
        deputy_id="a-erika",
    )
    org = org_ops.org_add_agent(
        org, "Paul Klein", role_ids=["einkauf"], org_unit_id="einkauf-abt", agent_id="a-paul"
    )
    # A two-level hierarchy so the org chart shows a real tree: sales and
    # purchasing both report to the management unit, each with its own manager.
    org = org_ops.org_set_parent(org, "vertrieb", "leitung")
    org = org_ops.org_set_parent(org, "einkauf-abt", "leitung")
    org = org_ops.org_set_manager(org, "leitung", "a-sabine")
    org = org_ops.org_set_manager(org, "vertrieb", "a-tom")
    org = org_ops.org_set_manager(org, "einkauf-abt", "a-paul")
    return org


def _build_urlaubsantrag(org: OrgModel) -> ProcessSchema:
    """Released process: a leave request with an approval/rejection decision."""

    s = ops.create_empty_schema("Urlaubsantrag", schema_id=SCHEMA_URLAUB)
    s = ops.serial_insert(s, "Antrag erfassen", after_node_id="start")
    erfassen = _nid(s, "Antrag erfassen")
    s = ops.serial_insert(s, "Antrag pr\u00fcfen", after_node_id=erfassen)
    pruefen = _nid(s, "Antrag pr\u00fcfen")

    # The branch discriminator must exist and be guaranteed written before the
    # split, so the data element and its mandatory write are added first (K7):
    # "Antrag erfassen" writes the number of days, "Antrag pruefen" reads it.
    s = ops.add_data_element(s, "Urlaubstage", DataType.INTEGER, element_id="tage")
    s = ops.connect_data(s, erfassen, "tage", AccessMode.WRITE)
    s = ops.connect_data(s, pruefen, "tage", AccessMode.READ)

    # Structured XOR partition over "tage" (INTEGER -> THRESHOLD): up to 10 days
    # is approved by the team lead, 11 or more days is rejected. The two cells
    # tile the whole number line (< 11 and >= 11), so exactly one branch is ever
    # enabled -- the engine resolves it automatically from the instance data.
    s = ops.conditional_insert(
        s,
        after_node_id=pruefen,
        discriminator="tage",
        branches=[
            ops.BranchSpec(label="Genehmigung durch Leitung", upper=11),
            ops.BranchSpec(label="Ablehnung dokumentieren"),
        ],
    )
    join = _gateway_id(s, NodeType.XOR_JOIN)
    s = ops.serial_insert(s, "Mitarbeiter benachrichtigen", after_node_id=join)

    # A second data object that *travels and is enriched along the flow*: the
    # decision is filled in by whichever XOR branch runs (approval or rejection)
    # and then consumed by the notification at the end. Because both branches
    # write it, the value is guaranteed present on every path after the join
    # (D1 holds via the XOR-join intersection).
    s = ops.add_data_element(s, "Entscheidung", DataType.STRING, element_id="entscheidung")
    s = ops.connect_data(s, _nid(s, "Genehmigung durch Leitung"), "entscheidung", AccessMode.WRITE)
    s = ops.connect_data(s, _nid(s, "Ablehnung dokumentieren"), "entscheidung", AccessMode.WRITE)
    s = ops.connect_data(s, _nid(s, "Mitarbeiter benachrichtigen"), "entscheidung", AccessMode.READ)

    s = ops.link_org_model(s, ORG_ID, org)
    s = ops.assign_staff_rule(s, erfassen, _role("sachbearbeiter"))
    s = ops.assign_staff_rule(s, pruefen, _role("sachbearbeiter"))
    s = ops.assign_staff_rule(s, _nid(s, "Genehmigung durch Leitung"), _role("teamleitung"))
    s = ops.assign_staff_rule(s, _nid(s, "Ablehnung dokumentieren"), _role("sachbearbeiter"))
    s = ops.assign_staff_rule(s, _nid(s, "Mitarbeiter benachrichtigen"), _role("sachbearbeiter"))

    # Input mask (form designer, U1-U3): the first step is entered through a
    # designed mask -- a number field for the days plus an optional free-text
    # reason. The mask *is* the data flow (a WRITE field yields a write access),
    # so "tage" stays guaranteed-written before the split (K7 still holds).
    s = ops.add_data_element(s, "Begr\u00fcndung", DataType.STRING, element_id="grund")
    s = ops.set_form(
        s,
        erfassen,
        title="Urlaubsantrag erfassen",
        fields=[
            ops.FormFieldSpec(
                element_id="tage",
                widget=WidgetKind.NUMBER,
                label="Urlaubstage",
                help_text="Anzahl der beantragten Arbeitstage.",
            ),
            ops.FormFieldSpec(
                element_id="grund",
                widget=WidgetKind.TEXTAREA,
                label="Begr\u00fcndung (optional)",
                required=False,
            ),
        ],
    )

    # Value-adding classification (E3) -- all three classes appear so the
    # monitoring value breakdown has something to show.
    s = ops.set_value_class(s, erfassen, ValueClass.BUSINESS_NECESSARY)
    s = ops.set_value_class(s, pruefen, ValueClass.BUSINESS_NECESSARY)
    s = ops.set_value_class(s, _nid(s, "Genehmigung durch Leitung"), ValueClass.VALUE_ADDING)
    s = ops.set_value_class(s, _nid(s, "Ablehnung dokumentieren"), ValueClass.NON_VALUE_ADDING)
    s = ops.set_value_class(s, _nid(s, "Mitarbeiter benachrichtigen"), ValueClass.VALUE_ADDING)

    # Work-item priority (E8): the approval by the team lead is the most urgent
    # step, so it sorts to the top of the worklist.
    s = ops.set_node_priority(
        s, pruefen, WorkItemPriority(impact=ImpactUrgency.MEDIUM, urgency=ImpactUrgency.HIGH)
    )
    s = ops.set_node_priority(
        s,
        _nid(s, "Genehmigung durch Leitung"),
        WorkItemPriority(impact=ImpactUrgency.HIGH, urgency=ImpactUrgency.HIGH),
    )

    # Temporal perspective (E5, T1/T2 static): per-step target durations and a
    # process deadline. The critical path (erfassen + pruefen + longest branch +
    # benachrichtigen) must fit the deadline, which the validator checks (T2).
    s = ops.set_time_constraint(s, erfassen, TimeConstraint(max_duration_seconds=3600))
    s = ops.set_time_constraint(s, pruefen, TimeConstraint(max_duration_seconds=7200))
    s = ops.set_time_constraint(
        s, _nid(s, "Genehmigung durch Leitung"), TimeConstraint(max_duration_seconds=86400)
    )
    s = ops.set_time_constraint(
        s, _nid(s, "Ablehnung dokumentieren"), TimeConstraint(max_duration_seconds=3600)
    )
    s = ops.set_time_constraint(
        s, _nid(s, "Mitarbeiter benachrichtigen"), TimeConstraint(max_duration_seconds=1800)
    )
    s = ops.set_deadline(s, 3 * 86400)  # three working days
    return ops.release(s)


def _build_beschaffung(org: OrgModel) -> ProcessSchema:
    """Draft process: a procurement request with a parallel block (still ENTWURF).

    This second, unreleased schema deliberately exercises the *advanced* feature
    set so every view has something to show even before release: an external
    SQL-bound data element (connector for the supplier credit limit), input masks,
    structured staff rules (role, org-unit and OR combinator) and the analytical
    annotations (value class, priority, time). Every step is interactive, so the
    whole flow can be played through in the GUI without an external worker; the
    External-Task/automation feature is shown separately in the integration guide
    (it needs a worker to complete an automatic step).
    """

    s = ops.create_empty_schema("Beschaffungsantrag", schema_id=SCHEMA_BESCHAFFUNG)
    s = ops.parallel_insert(s, ["Angebote einholen", "Budget pr\u00fcfen"], after_node_id="start")
    join = _gateway_id(s, NodeType.AND_JOIN)
    s = ops.serial_insert(s, "Bestellung freigeben", after_node_id=join)
    angebote = _nid(s, "Angebote einholen")
    budget = _nid(s, "Budget pr\u00fcfen")
    freigeben = _nid(s, "Bestellung freigeben")

    # Two data objects filled on the *parallel* branches and merged downstream:
    # "Angebote einholen" writes the order value, "Budget pr\u00fcfen" writes the
    # budget verdict; the final activity reads both (union at the AND-join -> D1
    # holds, and the writers target different elements -> no D2 conflict).
    s = ops.add_data_element(s, "Bestellwert", DataType.FLOAT, element_id="betrag")
    s = ops.add_data_element(s, "Budget genehmigt", DataType.BOOLEAN, element_id="budget_ok")
    # A lookup key (written on the offer branch) and an EXTERNAL element whose
    # value is fetched from the ERP via a structured scalar select (see below).
    s = ops.add_data_element(s, "Lieferantennummer", DataType.INTEGER, element_id="lieferant_nr")
    s = ops.add_data_element(s, "Kreditlimit", DataType.FLOAT, element_id="kreditlimit")
    s = ops.connect_data(s, angebote, "betrag", AccessMode.WRITE)
    s = ops.connect_data(s, angebote, "lieferant_nr", AccessMode.WRITE)
    s = ops.connect_data(s, budget, "budget_ok", AccessMode.WRITE)
    s = ops.connect_data(s, freigeben, "betrag", AccessMode.READ)
    s = ops.connect_data(s, freigeben, "budget_ok", AccessMode.READ)
    # EXTERNAL reads are non-mandatory (resolved by the connector at runtime),
    # otherwise D1 would demand a prior WRITE that an external element never has.
    s = ops.connect_data(s, freigeben, "kreditlimit", AccessMode.READ, mandatory=False)

    # Data connector + CbC-safe scalar SQL binding (C1/C4-C6): the supplier's
    # credit limit is read from the ERP by supplier number. The select is a
    # structured skizze (never free-form SQL): one typed column, an equality
    # filter on the (INSTANCE) key written beforehand, and a KEY_UNIQUE
    # cardinality guarantee -- so exactly one typed scalar comes back.
    s = ops.register_connector(s, "ERP-System", ConnectorKind.MS_SQL, connector_id="erp")
    s = ops.bind_sql_select(
        s,
        "kreditlimit",
        connector_id="erp",
        entity="lieferanten",
        column="kreditlimit",
        column_type=DataType.FLOAT,
        filters=[
            QueryFilter(
                column="nr",
                column_type=DataType.INTEGER,
                operator=FilterOperator.EQ,
                key_element_id="lieferant_nr",
            )
        ],
        cardinality=Cardinality.KEY_UNIQUE,
        unique_column="nr",
    )

    s = ops.link_org_model(s, ORG_ID, org)

    # Input masks (form designer): "Angebote einholen" captures the order value
    # and the supplier number, "Budget pruefen" the budget verdict. This is where
    # betrag/lieferant_nr/budget_ok get their values at runtime -- a person fills
    # the WRITE fields, so the whole procurement flow is completable in the GUI
    # end-to-end without any external worker.
    s = ops.set_form(
        s,
        angebote,
        title="Angebote einholen",
        fields=[
            ops.FormFieldSpec(
                element_id="betrag",
                widget=WidgetKind.NUMBER,
                label="Bestellwert (EUR)",
            ),
            ops.FormFieldSpec(
                element_id="lieferant_nr",
                widget=WidgetKind.NUMBER,
                label="Lieferantennummer",
            ),
        ],
    )
    s = ops.set_form(
        s,
        budget,
        title="Budgetpr\u00fcfung",
        fields=[
            ops.FormFieldSpec(
                element_id="budget_ok",
                widget=WidgetKind.CHECKBOX,
                label="Budget genehmigt",
            )
        ],
    )

    # Structured staff rules (BZR): a plain role leaf, an org-unit leaf and an OR
    # combinator, so the resource view shows the full range.
    s = ops.assign_staff_rule(s, angebote, _role("einkauf"))
    s = ops.assign_staff_rule(
        s, budget, StaffRule(kind=StaffRuleKind.ORG_UNIT, ref="vertrieb")
    )
    s = ops.assign_staff_rule(
        s,
        freigeben,
        StaffRule(
            kind=StaffRuleKind.OR,
            operands=[_role("teamleitung"), _role("einkauf")],
        ),
    )

    # Analytical annotations (E3/E8/E5) on the draft as well.
    s = ops.set_value_class(s, angebote, ValueClass.VALUE_ADDING)
    s = ops.set_value_class(s, budget, ValueClass.BUSINESS_NECESSARY)
    s = ops.set_value_class(s, freigeben, ValueClass.VALUE_ADDING)
    s = ops.set_node_priority(
        s, freigeben, WorkItemPriority(impact=ImpactUrgency.HIGH, urgency=ImpactUrgency.MEDIUM)
    )
    s = ops.set_time_constraint(s, angebote, TimeConstraint(max_duration_seconds=7200))
    s = ops.set_time_constraint(s, budget, TimeConstraint(max_duration_seconds=3600))
    s = ops.set_time_constraint(s, freigeben, TimeConstraint(max_duration_seconds=1800))
    s = ops.set_deadline(s, 86400)  # one working day
    return s  # left in ENTWURF on purpose: shows a draft / test-instance state


def _emit(
    audit: AuditLog,
    event_type: EventType,
    instance: ProcessInstance,
    *,
    node_id: str | None = None,
    label: str | None = None,
    agent_id: str | None = None,
    detail: dict[str, str] | None = None,
) -> None:
    audit.append(
        event_type,
        instance.id,
        instance.schema_id,
        schema_version=instance.schema_version,
        node_id=node_id,
        label=label,
        agent_id=agent_id,
        detail=detail,
    )


def _start(
    schema: ProcessSchema, ctx: exe.ExecutionContext, audit: AuditLog, instance_id: str
) -> ProcessInstance:
    inst = exe.instantiate(schema, instance_id=instance_id, context=ctx)
    _emit(audit, EventType.INSTANCE_CREATED, inst)
    return inst


def _complete(
    schema: ProcessSchema,
    inst: ProcessInstance,
    node_id: str,
    ctx: exe.ExecutionContext,
    audit: AuditLog,
    *,
    agent_id: str | None = None,
    data: dict[str, object] | None = None,
) -> ProcessInstance:
    after = exe.complete_activity(inst, schema, node_id, data, agent_id=agent_id, context=ctx)
    _emit(
        audit,
        EventType.ACTIVITY_COMPLETED,
        after,
        node_id=node_id,
        label=_label(schema, node_id),
        agent_id=agent_id,
    )
    if after.state is InstanceState.COMPLETED:
        _emit(audit, EventType.INSTANCE_COMPLETED, after)
    ctx.instances.put(after)
    return after


def _seed_instances(
    schema: ProcessSchema, instance_store: InstanceStore, audit: AuditLog
) -> None:
    """Create three leave-request instances at different points in the flow."""

    ctx = exe.ExecutionContext(make_resolver(_NoopSchemaStore()), instance_store)
    erfassen = _nid(schema, "Antrag erfassen")
    pruefen = _nid(schema, "Antrag pr\u00fcfen")
    ablehnung = _nid(schema, "Ablehnung dokumentieren")
    benachrichtigen = _nid(schema, "Mitarbeiter benachrichtigen")

    # 1) Freshly started -- waiting at the very first activity.
    _start(schema, ctx, audit, "urlaub-2026-001")

    # 2) In progress -- captured and checked. With 8 days recorded the XOR split
    # resolves itself (8 < 11) to the approval branch, so the instance now waits
    # at "Genehmigung durch Leitung" without any manual decision.
    i2 = _start(schema, ctx, audit, "urlaub-2026-002")
    i2 = _complete(schema, i2, erfassen, ctx, audit, agent_id="a-erika", data={"tage": 8})
    _complete(schema, i2, pruefen, ctx, audit, agent_id="a-erika")

    # 3) Finished -- a rejected request that ran all the way to the end. With 20
    # days recorded the split resolves to the rejection branch (20 >= 11). The
    # "entscheidung" object is written by the rejection step and then read by
    # the notification, so the finished instance carries the enriched value.
    i3 = _start(schema, ctx, audit, "urlaub-2026-003")
    i3 = _complete(schema, i3, erfassen, ctx, audit, agent_id="a-erika", data={"tage": 20})
    i3 = _complete(schema, i3, pruefen, ctx, audit, agent_id="a-erika")
    i3 = _complete(
        schema,
        i3,
        ablehnung,
        ctx,
        audit,
        agent_id="a-erika",
        data={"entscheidung": "Abgelehnt: 20 Tage \u00fcberschreiten das Kontingent (max. 10)."},
    )
    _complete(schema, i3, benachrichtigen, ctx, audit, agent_id="a-erika")


class _NoopSchemaStore:
    """A throwaway empty schema store for the instance execution context.

    The demo drives execution against the in-memory released schema directly;
    the resolver is only consulted for sub-processes, of which the demo has
    none, so an empty store is sufficient (and keeps the loader self-contained).
    """

    def put(self, schema: ProcessSchema) -> ProcessSchema:
        return schema

    def get(self, schema_id: str) -> ProcessSchema | None:
        return None

    def list_ids(self) -> list[str]:
        return []

    def clear(self) -> None:
        return None


def _seed_users(backend: PasswordAuthBackend) -> int:
    """Seed the ready-to-use demo logins (idempotent); returns how many added."""

    store = backend.store
    seeded = 0
    for login, name, roles, agent_id in DEMO_USERS:
        if store.get_user(login) is not None:
            continue
        store.put_user(
            User(
                login=login,
                password_hash=hash_password(DEMO_PASSWORD),
                subject=login,
                agent_id=agent_id,
                roles=roles,
                display_name=name,
                must_change=False,
            )
        )
        seeded += 1
    return seeded


def load_demo(
    *,
    schema_store: SchemaStore,
    instance_store: InstanceStore,
    org_store: OrgStore,
    audit_log: AuditLog,
    password_backend: PasswordAuthBackend | None = None,
) -> int:
    """Populate the stores with the demo world; returns the seeded-user count.

    Call this on an already-empty system (the admin reset clears first). The
    shared org, both schemas and the three instances are always created; demo
    logins are only seeded when password login is active (otherwise the open
    dev mode already grants every role and needs no users).
    """

    org = _build_org()
    org_store.put(org)

    urlaub = _build_urlaubsantrag(org)
    beschaffung = _build_beschaffung(org)
    schema_store.put(dehydrate_org(urlaub))
    schema_store.put(dehydrate_org(beschaffung))

    _seed_instances(urlaub, instance_store, audit_log)

    if password_backend is not None:
        return _seed_users(password_backend)
    return 0
