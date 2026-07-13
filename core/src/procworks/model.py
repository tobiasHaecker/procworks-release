# SPDX-License-Identifier: BUSL-1.1
"""Meta-model (Pydantic) for the block-structured process schema.

This mirrors Section 4 of the architecture concept: Node, ControlEdge and the
versioned ProcessSchema with a lifecycle state, the data-flow layer
(DataElement, DataAccess) used by the data-flow rules D1-D4, and the resource
layer (OrgModel, StaffRule, ServiceBinding) used by the resource rules Z1-Z4.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class NodeType(StrEnum):
    """Node types of the block-structured control graph."""

    START = "START"
    END = "END"
    ACTIVITY = "ACTIVITY"
    AND_SPLIT = "AND_SPLIT"
    AND_JOIN = "AND_JOIN"
    XOR_SPLIT = "XOR_SPLIT"
    XOR_JOIN = "XOR_JOIN"
    SUBPROCESS = "SUBPROCESS"


#: Gateway node types that open a block.
SPLIT_TYPES = frozenset({NodeType.AND_SPLIT, NodeType.XOR_SPLIT})
#: Gateway node types that close a block.
JOIN_TYPES = frozenset({NodeType.AND_JOIN, NodeType.XOR_JOIN})
#: Matching split -> join pairs (K1).
SPLIT_JOIN_PAIR = {
    NodeType.AND_SPLIT: NodeType.AND_JOIN,
    NodeType.XOR_SPLIT: NodeType.XOR_JOIN,
}


class EdgeType(StrEnum):
    """Control edge types (SYNC/LOOP reserved for later roadmap steps)."""

    CONTROL = "CONTROL"


class ValueClass(StrEnum):
    """Value-adding classification of an activity (Section 8.4.1, roadmap E3).

    A purely analytical, optional annotation used by the monitoring/leistungs
    sicht to highlight the share of non-value-adding steps. It carries no
    correctness weight (Stufe A/B unaffected).
    """

    VALUE_ADDING = "VALUE_ADDING"
    BUSINESS_NECESSARY = "BUSINESS_NECESSARY"
    NON_VALUE_ADDING = "NON_VALUE_ADDING"


class LifecycleState(StrEnum):
    """Schema lifecycle states (Section 4.1)."""

    ENTWURF = "ENTWURF"
    REVIEW = "REVIEW"
    RELEASED = "RELEASED"
    DEPRECATED = "DEPRECATED"
    ARCHIVED = "ARCHIVED"


class DataType(StrEnum):
    """Data element value types (Section 3.2)."""

    INTEGER = "INTEGER"
    FLOAT = "FLOAT"
    STRING = "STRING"
    DATE = "DATE"
    BOOLEAN = "BOOLEAN"
    URI = "URI"


class AccessMode(StrEnum):
    """Direction of a data access between an activity and a data element."""

    READ = "READ"
    WRITE = "WRITE"
    READ_WRITE = "READ_WRITE"


class DataSourceKind(StrEnum):
    """Where a data element is stored (Section 9.1).

    ``INSTANCE`` values live in the process instance; ``EXTERNAL`` values are
    resolved through a connector against a central database/application.
    """

    INSTANCE = "INSTANCE"
    EXTERNAL = "EXTERNAL"


class ConnectorKind(StrEnum):
    """The external system a connector talks to (Section 9.2).

    ``CUSTOM`` is the open connector SPI for further systems (REST/files/...).
    """

    MS_SQL = "MS_SQL"
    MYSQL = "MYSQL"
    DYNAMICS_365 = "DYNAMICS_365"
    SAP = "SAP"
    CUSTOM = "CUSTOM"


class ExternalBinding(BaseModel):
    """Binds an EXTERNAL data element to a connector entity (Section 9.1).

    ``connector_id`` selects a registered connector, ``entity`` names the
    business object (table/entity/BAPI), and ``key_element_id`` references the
    INSTANCE data element whose value is the lookup key. The key is passed as a
    parameter at access time -- never concatenated into a query (no injection).
    """

    connector_id: str
    entity: str
    key_element_id: str


class FilterOperator(StrEnum):
    """Comparison operator of a structured SQL-select filter (§6, rule C5).

    A closed whitelist so a filter never carries free-form SQL. Ordering
    operators only apply to ordered types (checked by C5); ``IN`` matches set
    membership; ``LIKE`` matches string patterns.
    """

    EQ = "EQ"
    NE = "NE"
    LT = "LT"
    LE = "LE"
    GT = "GT"
    GE = "GE"
    LIKE = "LIKE"
    IN = "IN"


class AggregateKind(StrEnum):
    """Aggregate applied to the projected column of a scalar select (rule C4).

    ``NONE`` projects the column itself; the others always yield exactly one row
    (which is what makes ``AGGREGATE`` a cardinality guarantee, C6). The result
    type is derived by :func:`aggregate_result_type`.
    """

    NONE = "NONE"
    COUNT = "COUNT"
    SUM = "SUM"
    MIN = "MIN"
    MAX = "MAX"
    AVG = "AVG"


class Cardinality(StrEnum):
    """How a scalar select guarantees at most one result row (rule C6).

    ``KEY_UNIQUE``: an equality filter on the declared unique column.
    ``AGGREGATE``: the projection is an aggregate (always one row).
    ``FIRST_ORDERED``: ``ORDER BY ... LIMIT 1`` over a non-empty ordering.
    """

    KEY_UNIQUE = "KEY_UNIQUE"
    AGGREGATE = "AGGREGATE"
    FIRST_ORDERED = "FIRST_ORDERED"


class QueryFilter(BaseModel):
    """A structured ``WHERE`` condition of a scalar select (rule C5).

    ``column`` is the (whitelisted) filter column, ``column_type`` its declared
    type, ``operator`` a member of the closed :class:`FilterOperator` whitelist
    and ``key_element_id`` the INSTANCE data element whose value is bound as a
    parameter at access time (never concatenated -- no injection surface).
    """

    column: str
    column_type: DataType
    operator: FilterOperator
    key_element_id: str


class OrderBy(BaseModel):
    """One ``ORDER BY`` term of a ``FIRST_ORDERED`` scalar select (rule C6)."""

    column: str
    descending: bool = False


class SqlSelectBinding(BaseModel):
    """A structured, type- and cardinality-safe scalar SQL binding (§4).

    A ``select``-bound EXTERNAL data element resolves to a single, typed scalar
    compiled from this specification (never free-form SQL). ``column`` is the one
    projected column (or the aggregate's argument), ``column_type`` its declared
    type; the effective result type is :func:`aggregate_result_type` and must
    equal the element's type (C4). ``filters`` supply parameterized ``WHERE``
    conditions (C5); ``cardinality`` picks the guarantee that at most one row is
    returned (C6): ``KEY_UNIQUE`` requires an equality filter on ``unique_column``,
    ``AGGREGATE`` an aggregate, ``FIRST_ORDERED`` a non-empty ``order_by``.
    """

    connector_id: str
    entity: str
    column: str
    column_type: DataType
    aggregate: AggregateKind = AggregateKind.NONE
    filters: list[QueryFilter] = Field(default_factory=list)
    cardinality: Cardinality = Cardinality.KEY_UNIQUE
    order_by: list[OrderBy] = Field(default_factory=list)
    unique_column: str = ""


def aggregate_result_type(aggregate: AggregateKind, column_type: DataType) -> DataType:
    """Return the result type a projection yields under ``aggregate`` (C4).

    ``COUNT`` always yields an INTEGER, ``AVG`` always a FLOAT; ``SUM``/``MIN``/
    ``MAX`` and the non-aggregated projection keep the column's own type.
    """

    if aggregate is AggregateKind.COUNT:
        return DataType.INTEGER
    if aggregate is AggregateKind.AVG:
        return DataType.FLOAT
    return column_type


class SqlWriteBinding(BaseModel):
    """A structured, type-safe scalar SQL write-back binding (§7, Q4).

    Symmetric to :class:`SqlSelectBinding`: when the bound EXTERNAL element is
    written by an activity, the produced scalar is post-flushed as a single,
    parameterized ``UPDATE <entity> SET <column> = :val WHERE ...``. ``column``
    is the target column, ``column_type`` its declared type (must equal the
    element's type, C7); ``filters`` locate the row (C8). To make the write
    address **exactly one** row (C9), a write always uses the KEY_UNIQUE
    guarantee: an equality filter on the declared ``unique_column`` (no
    aggregate/ordering -- an UPDATE targets the keyed row, never many).
    """

    connector_id: str
    entity: str
    column: str
    column_type: DataType
    filters: list[QueryFilter] = Field(default_factory=list)
    unique_column: str = ""


#: Access modes that read a data element.
READ_MODES = frozenset({AccessMode.READ, AccessMode.READ_WRITE})
#: Access modes that write a data element.
WRITE_MODES = frozenset({AccessMode.WRITE, AccessMode.READ_WRITE})


def value_matches_type(data_type: DataType, value: object) -> bool:
    """Return whether ``value`` is a valid runtime value for ``data_type``.

    A boundary type check (runtime D3) shared by the inbound data-write API and
    the external-task runtime: JSON carries no schema types, so values handed in
    from the outside are validated against the declared :class:`DataType` before
    they reach the pure engine. ``bool`` is excluded from the numeric types
    because in Python ``bool`` is a subclass of ``int``.
    """

    if data_type is DataType.INTEGER:
        return isinstance(value, int) and not isinstance(value, bool)
    if data_type is DataType.FLOAT:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if data_type is DataType.BOOLEAN:
        return isinstance(value, bool)
    # STRING, DATE and URI are all carried as strings over the wire.
    return isinstance(value, str)


class NodeState(StrEnum):
    """Runtime node marking (NS) of a process instance (Section 4 / step 8)."""

    NOT_ACTIVATED = "NOT_ACTIVATED"
    ACTIVATED = "ACTIVATED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    SKIPPED = "SKIPPED"


class EdgeState(StrEnum):
    """Runtime edge marking (ES) of a process instance."""

    NOT_SIGNALED = "NOT_SIGNALED"
    TRUE_SIGNALED = "TRUE_SIGNALED"
    FALSE_SIGNALED = "FALSE_SIGNALED"


class InstanceState(StrEnum):
    """Lifecycle state of a running process instance."""

    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"


class Node(BaseModel):
    """A node of the control graph."""

    id: str
    type: NodeType
    label: str = ""
    #: Optional value-adding classification (Section 8.4.1, roadmap E3). Only
    #: meaningful for ACTIVITY/SUBPROCESS nodes; ``None`` means unclassified.
    value_class: ValueClass | None = None


class DataElement(BaseModel):
    """A process instance variable (Section 3.2).

    ``source`` selects the storage scope (Section 9.1): ``INSTANCE`` values
    live in the process instance, ``EXTERNAL`` values are resolved through a
    connector via ``external`` (the connector rules C1-C3 keep that binding
    consistent).
    """

    id: str
    name: str
    data_type: DataType
    source: DataSourceKind = DataSourceKind.INSTANCE
    external: ExternalBinding | None = None
    #: Alternative EXTERNAL binding: a structured, type- and cardinality-safe
    #: scalar SQL select (rules C4-C6). Mutually exclusive with ``external`` --
    #: an EXTERNAL element is either record-bound (``external``) or scalar-select-
    #: bound (``select``). ``None`` on INSTANCE elements and record-bound ones.
    select: SqlSelectBinding | None = None
    #: Alternative EXTERNAL binding: a structured scalar SQL write-back (rules
    #: C7-C9). Mutually exclusive with ``external`` and ``select``.
    write: SqlWriteBinding | None = None


class DataAccess(BaseModel):
    """A typed read/write link between an ACTIVITY and a data element.

    ``mandatory`` marks a non-optional parameter: a mandatory read must be
    supplied on every path (D1); a mandatory write guarantees a supply.
    ``param_type`` is the activity parameter type; if set it must match the
    element type (D3).
    """

    node_id: str
    element_id: str
    mode: AccessMode
    mandatory: bool = True
    param_type: DataType | None = None


class WidgetKind(StrEnum):
    """Presentation widget of an input-mask field (form designer).

    The widget only controls how a value is *entered/shown*; the value itself is
    always a typed :class:`DataElement`. ``widget_matches_type`` keeps the two
    consistent so a mask can never present a value with an incompatible control.
    """

    TEXT = "TEXT"
    TEXTAREA = "TEXTAREA"
    NUMBER = "NUMBER"
    DROPDOWN = "DROPDOWN"
    CHECKBOX = "CHECKBOX"
    DATE = "DATE"


#: Which widgets may present each data type in an input mask. A field whose
#: widget is not listed for its element's type is rejected by rule U2.
_WIDGETS_FOR_TYPE: dict[DataType, frozenset[WidgetKind]] = {
    DataType.STRING: frozenset({WidgetKind.TEXT, WidgetKind.TEXTAREA, WidgetKind.DROPDOWN}),
    DataType.URI: frozenset({WidgetKind.TEXT}),
    DataType.INTEGER: frozenset({WidgetKind.NUMBER}),
    DataType.FLOAT: frozenset({WidgetKind.NUMBER}),
    DataType.BOOLEAN: frozenset({WidgetKind.CHECKBOX}),
    DataType.DATE: frozenset({WidgetKind.DATE}),
}


def widget_matches_type(widget: WidgetKind, data_type: DataType) -> bool:
    """Return whether ``widget`` can present a value of ``data_type`` (rule U2)."""

    return widget in _WIDGETS_FOR_TYPE.get(data_type, frozenset())


class FormField(BaseModel):
    """One control of an input mask, bound to a data element.

    ``mode`` ties the field to the data flow: a ``WRITE`` field is an input that
    *sets* the element, a ``READ`` field displays a value that must have been
    *set before* on every path (D1). ``options`` carries the choices of a
    ``DROPDOWN``. There is no position -- the mask is laid out automatically.
    """

    id: str
    element_id: str
    widget: WidgetKind
    label: str
    mode: AccessMode = AccessMode.WRITE
    required: bool = True
    options: list[str] = Field(default_factory=list)
    help_text: str | None = None


class Form(BaseModel):
    """An input mask attached to an ACTIVITY (form designer).

    The mask is a plain, *ordered* list of fields; the concrete arrangement is
    derived automatically at render time. Every field is a presentation layer
    over a :class:`DataAccess`, so using a mask stays Correct by Construction
    (rules U1-U3 keep mask and data flow consistent).
    """

    node_id: str
    title: str = ""
    fields: list[FormField] = Field(default_factory=list)


class ConnectorDescriptor(BaseModel):
    """A registered data connector (Section 9.2).

    The descriptor only carries modelling metadata; credentials and endpoints
    live server-side in a secret store, never in the schema.
    """

    id: str
    name: str
    kind: ConnectorKind


# --- resource / organisation model (Z1-Z4) -------------------------------


class Role(BaseModel):
    """An organisational role (e.g. ``Sachbearbeiter``).

    ``mailbox`` is an optional shared group/distribution address for the role
    (e.g. ``sachbearbeitung@firma.de``). It is the target of a modelled e-mail
    notification in ``TO_GROUP_MAILBOX`` mode (rule group N); ``None`` means the
    role has no group mailbox. Purely master data -- addresses are never inlined
    in a process schema.
    """

    id: str
    name: str
    mailbox: str | None = None


class OrgUnit(BaseModel):
    """An organisational unit; ``parent_id`` builds the unit hierarchy.

    ``manager_id`` names the supervisor (an agent) responsible for the unit.
    ``mailbox`` is an optional shared department address (e.g. ``einkauf@firma.de``),
    the target of a ``TO_GROUP_MAILBOX`` e-mail notification (rule group N).
    """

    id: str
    name: str
    parent_id: str | None = None
    manager_id: str | None = None
    mailbox: str | None = None


class Agent(BaseModel):
    """A concrete actor that can perform interactive steps.

    ``deputy_id`` names another agent that stands in for this one: whenever
    this agent is eligible for a task, the deputy is eligible too (the
    substitution chain is followed transitively at runtime).

    ``email`` is the agent's personal mailbox, the target of a modelled e-mail
    notification in ``TO_ELIGIBLE_AGENTS`` mode (rule group N). ``None`` means no
    address is on file; the correctness rule N3 forbids modelling a per-agent
    notification for any activity that could be assigned to an address-less
    agent, so a notification can never be sent to a missing address.
    """

    id: str
    name: str
    role_ids: list[str] = Field(default_factory=list)
    org_unit_id: str | None = None
    deputy_id: str | None = None
    email: str | None = None


class OrgModel(BaseModel):
    """The organisational model a staff rule is resolved against.

    An org model can be **embedded** in a single schema (the default; ``id`` is
    ``None``) or a **shared**, standalone master-data entity reused across many
    schemas (``id``/``name`` set, stored in its own registry). A schema that
    references a shared org model via ``ProcessSchema.org_model_id`` resolves
    staff rules against that shared model, so one organisation can be modelled
    once and used in several process models.
    """

    id: str | None = None
    name: str = ""
    roles: dict[str, Role] = Field(default_factory=dict)
    org_units: dict[str, OrgUnit] = Field(default_factory=dict)
    agents: dict[str, Agent] = Field(default_factory=dict)


class StaffRuleKind(StrEnum):
    """Node kinds of the structured staff-assignment rule (BZR) tree."""

    ROLE = "ROLE"
    ORG_UNIT = "ORG_UNIT"
    #: A single, explicitly named agent (``ref`` is an agent id). Lets a model
    #: pin a step to a concrete person instead of a role/unit -- the eligible
    #: set is exactly that agent (plus deputies, added at resolution time).
    AGENT = "AGENT"
    #: The agent who performed a prior node (``ref`` is that node's id). The
    #: performer is only known at runtime, so Z3 requires the node to run first.
    NODE_PERFORMING_AGENT = "NODE_PERFORMING_AGENT"
    #: A resource chosen *relative to* the performer of a prior node: the
    #: supervisor (the manager of the performer's org unit). ``ref`` is the
    #: prior node's id (same back-reference discipline as
    #: ``NODE_PERFORMING_AGENT``, enforced by Z3). Example: the supervisor of
    #: the creator of a vacation request approves it.
    NODE_PERFORMING_AGENT_SUPERVISOR = "NODE_PERFORMING_AGENT_SUPERVISOR"
    AND = "AND"
    OR = "OR"
    EXCEPT = "EXCEPT"


#: Leaf staff-rule kinds that reference a prior *node* (their performer is only
#: known at runtime, so Z3 requires the node to be guaranteed-executed before).
STAFF_NODE_REF_KINDS = frozenset(
    {
        StaffRuleKind.NODE_PERFORMING_AGENT,
        StaffRuleKind.NODE_PERFORMING_AGENT_SUPERVISOR,
    }
)
#: Leaf staff-rule kinds (reference an org element or a prior node).
STAFF_LEAF_KINDS = frozenset(
    {
        StaffRuleKind.ROLE,
        StaffRuleKind.ORG_UNIT,
        StaffRuleKind.AGENT,
        StaffRuleKind.NODE_PERFORMING_AGENT,
        StaffRuleKind.NODE_PERFORMING_AGENT_SUPERVISOR,
    }
)
#: Combinator staff-rule kinds (operate on operands).
STAFF_COMBINATOR_KINDS = frozenset(
    {StaffRuleKind.AND, StaffRuleKind.OR, StaffRuleKind.EXCEPT}
)


class StaffRule(BaseModel):
    """A structured staff-assignment rule (BZR) as an expression tree.

    Leaf kinds carry ``ref``: a role id (``ROLE``), an org-unit id (``ORG_UNIT``),
    an agent id (``AGENT``), or a prior node's id (``NODE_PERFORMING_AGENT`` and
    ``NODE_PERFORMING_AGENT_SUPERVISOR``). ``recursive`` applies to ``ORG_UNIT``
    to include sub-units (the ADEPT ``*``/``+`` modifiers). Combinator kinds
    (AND/OR/EXCEPT) carry ``operands``.
    """

    kind: StaffRuleKind
    ref: str | None = None
    recursive: bool = False
    operands: list[StaffRule] = Field(default_factory=list)


# --- modelled e-mail notification (rule group N) -------------------------


class MailRecipientMode(StrEnum):
    """Who a modelled e-mail notification of an activity is addressed to."""

    #: To each concrete agent currently eligible for the task (personal mailbox,
    #: ``Agent.email``), resolved at runtime from the activity's staff rule.
    TO_ELIGIBLE_AGENTS = "TO_ELIGIBLE_AGENTS"
    #: To the shared group mailbox(es) of the role(s)/unit(s) the activity's
    #: staff rule addresses (``Role.mailbox`` / ``OrgUnit.mailbox``).
    TO_GROUP_MAILBOX = "TO_GROUP_MAILBOX"


class MailBinding(BaseModel):
    """A modelled e-mail notification attached to a single ACTIVITY node.

    Optional and opt-in: only activities that carry a binding in
    ``ProcessSchema.mail_bindings`` send a mail, and they send it once when the
    task becomes ready (the node is activated). There is deliberately no global
    "mail on every task" switch.

    ``subject``/``body`` are plain-text templates that may contain
    ``{element_id}`` placeholders; rule N4 guarantees every placeholder refers to
    an INSTANCE data element that is written before the node, so it always
    resolves at send time. ``include_deputies`` (per-agent mode only) mirrors the
    runtime staff resolution, which also lists deputies as eligible.
    """

    mode: MailRecipientMode = MailRecipientMode.TO_ELIGIBLE_AGENTS
    include_deputies: bool = True
    subject: str
    body: str = ""


#: Matches a ``{element_id}`` placeholder in a mail template. The captured name
#: is a bare data-element id (letters, digits, underscore, hyphen, dot); a literal
#: ``{{`` / ``}}`` is *not* matched, so it can escape a literal brace if ever
#: needed. Kept here so the validator (rule N4) and the runtime renderer share a
#: single definition of what a placeholder is.
_PLACEHOLDER_RE = re.compile(r"(?<!\{)\{([A-Za-z0-9_.\-]+)\}(?!\})")


def template_placeholders(template: str) -> list[str]:
    """Return the data-element ids referenced by ``{...}`` placeholders.

    Order-preserving with duplicates removed. Used by rule N4 to check every
    placeholder resolves and by the runtime renderer to substitute values.
    """

    seen: dict[str, None] = {}
    for match in _PLACEHOLDER_RE.finditer(template):
        seen.setdefault(match.group(1), None)
    return list(seen)


#: A deliberately strict, pragmatic e-mail syntax check for master-data
#: addresses (rule N1). Not a full RFC 5322 parser -- it rejects the mistakes
#: that matter (empty, embedded whitespace/newline, missing local or domain
#: part, no dot in the domain). The no-whitespace guarantee also means a stored
#: address can never smuggle a header-injecting newline into a sent mail.
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def is_valid_email(address: str) -> bool:
    """True if ``address`` is a syntactically acceptable e-mail address (N1).

    Shared by the validator (rule N1 on embedded org models) and
    :func:`procworks.org.validate_org` (the same rule on shared org models), so
    both paths reject the same malformed addresses.
    """

    return bool(_EMAIL_RE.match(address))


# --- work-item priority (Section 3.8 / 6.2.1, roadmap E8) -----------------


class PriorityLevel(StrEnum):
    """A ranked priority level of a work item (ascending severity)."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


#: Numeric rank of each priority level (higher = more urgent), used to sort a
#: worklist. Kept separate from the enum so the ordering is explicit.
PRIORITY_RANK: dict[PriorityLevel, int] = {
    PriorityLevel.LOW: 0,
    PriorityLevel.MEDIUM: 1,
    PriorityLevel.HIGH: 2,
    PriorityLevel.CRITICAL: 3,
}


class ImpactUrgency(StrEnum):
    """A three-step impact or urgency scale (ITIL-style, Section 3.8)."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


#: Maps (impact, urgency) to a derived priority level, the classic
#: ``Priorit\u00e4t = Auswirkung + Dringlichkeit`` matrix (Section 3.8). The score is
#: the sum of the two 0..2 scales (0..4) bucketed into the four levels.
_IMPACT_URGENCY_SCORE: dict[ImpactUrgency, int] = {
    ImpactUrgency.LOW: 0,
    ImpactUrgency.MEDIUM: 1,
    ImpactUrgency.HIGH: 2,
}


class WorkItemPriority(BaseModel):
    """The modelled priority of an interactive step (roadmap E8).

    The effective :class:`PriorityLevel` is *derived* from impact and urgency
    (``Priorit\u00e4t = Auswirkung + Dringlichkeit``) so the two business inputs stay
    the single source of truth; the level is never stored independently.
    """

    impact: ImpactUrgency = ImpactUrgency.MEDIUM
    urgency: ImpactUrgency = ImpactUrgency.MEDIUM

    @property
    def level(self) -> PriorityLevel:
        """Derive the ranked priority from impact and urgency."""

        score = _IMPACT_URGENCY_SCORE[self.impact] + _IMPACT_URGENCY_SCORE[self.urgency]
        if score >= 4:
            return PriorityLevel.CRITICAL
        if score == 3:
            return PriorityLevel.HIGH
        if score >= 1:
            return PriorityLevel.MEDIUM
        return PriorityLevel.LOW

    @property
    def rank(self) -> int:
        """Numeric rank of the derived level (higher = more urgent)."""

        return PRIORITY_RANK[self.level]


# --- temporal perspective (Section 3.8, T1-T3, roadmap E5) ----------------


class TimeConstraint(BaseModel):
    """An optional temporal annotation of a node (roadmap E5).

    ``max_duration_seconds`` is the maximum expected *processing* duration of the
    step (measured from the moment an agent starts working it); the schema-level
    ``deadline_seconds`` (see :class:`ProcessSchema`) bounds the whole process.
    Both feed the static time-consistency rules T1 (well-formed) and T2 (the
    critical path must fit the deadline).

    ``target_lead_seconds`` is the optional *reaction* time (SLA) measured from
    the moment the node becomes ready (ACTIVATED): the window within which the
    task should be picked up and finished. It is the natural steering value for
    the time-based worklist prioritisation (see ``worklist_priority``), because a
    task "rots" while nobody touches it. When it is not set, the prioritisation
    falls back to ``max_duration_seconds`` as the reaction time (fallback rule S
    of the prioritisation concept), so a single annotated duration is enough to
    drive the automatic ordering.
    """

    max_duration_seconds: float | None = None
    target_lead_seconds: float | None = None


# --- time-based worklist criticality (Zeitbasierte-Priorisierung-Konzept) --


class TimeCriticality(StrEnum):
    """Explainable criticality band of an open work item, derived at read time
    from how much of its target time it has already consumed (never stored).

    The band mirrors :class:`PriorityLevel` in spirit: an ordered, deterministic
    label that makes the automatic worklist ordering explainable to end users
    ("this is *overdue*"). ``NONE`` is the backward-compatible band for a task
    without a target time -- it takes no part in the time ordering and is placed
    purely by its static business priority.
    """

    ON_TRACK = "ON_TRACK"
    WARNING = "WARNING"
    AT_RISK = "AT_RISK"
    OVERDUE = "OVERDUE"
    NONE = "NONE"


#: Numeric rank of each criticality band (higher = more urgent), used to sort a
#: worklist. Kept separate from the enum so the ordering is explicit; ``NONE``
#: ranks below ``ON_TRACK`` so timed tasks always outrank untimed ones.
CRITICALITY_RANK: dict[TimeCriticality, int] = {
    TimeCriticality.NONE: 0,
    TimeCriticality.ON_TRACK: 1,
    TimeCriticality.WARNING: 2,
    TimeCriticality.AT_RISK: 3,
    TimeCriticality.OVERDUE: 4,
}


# --- activity repository (templates, A1-A3) -------------------------------


class ExecutorKind(StrEnum):
    """How an activity template is executed (Section 6).

    ``MANUAL`` steps are interactive (need a staff rule); the others run
    automatically (script, internal service call, or remote web service).
    """

    MANUAL = "MANUAL"
    SCRIPT = "SCRIPT"
    SERVICE = "SERVICE"
    WEB_SERVICE = "WEB_SERVICE"


class TemplateParameter(BaseModel):
    """A typed input/output parameter of an activity template."""

    name: str
    data_type: DataType
    mandatory: bool = True


class ActivityTemplate(BaseModel):
    """A reusable activity component with a typed I/O interface (Section 6).

    Templates homogenise services "upwards" (logical procedures with typed
    parameters) and carry the executor that runs them "downwards". Binding a
    template to a node enables Plug-&-Play modelling and a data-flow check
    against the declared interface (A1-A3).
    """

    id: str
    name: str
    executor: ExecutorKind
    inputs: list[TemplateParameter] = Field(default_factory=list)
    outputs: list[TemplateParameter] = Field(default_factory=list)

    @property
    def is_automatic(self) -> bool:
        """Whether the executor runs without an interactive performer."""

        return self.executor is not ExecutorKind.MANUAL


class AutomationKind(StrEnum):
    """How an automatic ACTIVITY is driven by an external tool (roadmap E11).

    ``MANUAL_NONE`` (default) keeps the historical behaviour: the step is either
    interactive or completed through the regular API. ``EXTERNAL_TASK`` exposes
    the activated step as an external task an outside *worker* pulls (fetch-and-
    lock); ``HTTP_PUSH`` makes ProcWorks call a server-side endpoint reference.
    The integration rules I1-I4 keep the binding well-formed and secret-free.
    """

    MANUAL_NONE = "MANUAL_NONE"
    EXTERNAL_TASK = "EXTERNAL_TASK"
    HTTP_PUSH = "HTTP_PUSH"


class ServiceBinding(BaseModel):
    """The executing service (ActivityTemplate) bound to an ACTIVITY node.

    ``automatic`` distinguishes automated steps (no staff rule needed) from
    interactive steps (which require a staff rule for release). When
    ``template_id`` references a repository template, ``parameter_mapping``
    maps each template parameter name to a schema data-element id, and the
    binding is checked against the template interface (A1-A3).

    The integration fields (roadmap E11) drive an automatic step from the
    integration boundary. ``automation`` defaults to ``MANUAL_NONE`` so existing
    schemas are unchanged; ``EXTERNAL_TASK`` uses ``topic`` (pull),
    ``HTTP_PUSH`` uses ``endpoint_ref`` (push). They are validated by I1-I4 and
    never carry secrets (only references).
    """

    node_id: str
    name: str
    automatic: bool = False
    template_id: str | None = None
    parameter_mapping: dict[str, str] = Field(default_factory=dict)
    automation: AutomationKind = AutomationKind.MANUAL_NONE
    topic: str | None = None
    endpoint_ref: str | None = None
    retry_max: int = 5
    retry_backoff_ms: int = 2000
    request_timeout_ms: int = 30000


# --- composition: sub-processes and follow-up links (H1-H4, F1-F3) --------


class FollowUpMode(StrEnum):
    """Coupling mode of a follow-up process (Section 4.2)."""

    ASYNC = "ASYNC"
    SYNC = "SYNC"


class FollowUpTrigger(StrEnum):
    """When a follow-up process is started."""

    ON_COMPLETE = "ON_COMPLETE"
    CONDITIONAL = "CONDITIONAL"


class SubProcessBinding(BaseModel):
    """Binds a SUBPROCESS node to a pinned, RELEASED target schema (H1-H4).

    ``input_mapping`` maps a target input data-element id to a parent
    data-element id (the parent supplies the sub-process input);
    ``output_mapping`` maps a target output data-element id to a parent
    data-element id (the sub-process writes back into the parent).
    """

    node_id: str
    target_schema_id: str
    target_version: int
    input_mapping: dict[str, str] = Field(default_factory=dict)
    output_mapping: dict[str, str] = Field(default_factory=dict)


class FollowUpLink(BaseModel):
    """A lateral link to a follow-up process started after this one (F1-F3).

    ``handover_mapping`` maps a target start data-element id to a source
    data-element id of this schema. ``target_version`` ``None`` means "latest
    RELEASED".
    """

    id: str
    target_schema_id: str
    target_version: int | None = None
    trigger: FollowUpTrigger = FollowUpTrigger.ON_COMPLETE
    condition: str | None = None
    handover_mapping: dict[str, str] = Field(default_factory=dict)
    mode: FollowUpMode = FollowUpMode.ASYNC


class XorDecisionKind(StrEnum):
    """How an XOR split partitions its discriminator's value domain (K7).

    The kind is derived from the discriminator data element's type so the
    partition is always decidable: ``THRESHOLD`` for ordered numbers
    (INTEGER/FLOAT), ``BOOLEAN`` for booleans, ``ENUM`` for categorical strings.
    Each kind admits a *total* and *disjoint* partition, so exactly one branch
    is ever enabled (no deadlock, no multiple activation).
    """

    THRESHOLD = "THRESHOLD"
    BOOLEAN = "BOOLEAN"
    ENUM = "ENUM"


class XorBranch(BaseModel):
    """One cell of an XOR partition, bound to a branch body via ``target``.

    The cell shape depends on the owning :class:`XorDecision`'s ``kind``:

    - ``THRESHOLD``: the half-open interval ``[lower, upper)`` over the reals;
      ``upper`` ``None`` means ``+inf``. The lower bound is implied by the
      previous branch's ``upper`` (``-inf`` for the first) and is not stored, so
      consecutive branches tile the whole number line without gap or overlap.
    - ``BOOLEAN``: ``bool_value`` selects the ``true`` or ``false`` cell.
    - ``ENUM``: ``values`` lists the matched strings; exactly one branch sets
      ``is_else`` as the catch-all complement so every string is covered.
    """

    target: str
    upper: float | None = None
    bool_value: bool | None = None
    values: list[str] = Field(default_factory=list)
    is_else: bool = False


class XorDecision(BaseModel):
    """A total, disjoint partition of a discriminator element's domain (K7).

    The ``branches`` are ordered and, by construction, partition the
    discriminator's value domain completely and without overlap. The runtime
    therefore always finds *exactly one* enabled branch from the instance data
    (see :func:`resolve_xor_target`) -- the structural guarantee that no XOR
    split can deadlock or activate several paths at once.
    """

    discriminator: str
    kind: XorDecisionKind
    branches: list[XorBranch] = Field(default_factory=list)


def _fmt_bound(value: float) -> str:
    """Render a numeric threshold without a trailing ``.0`` for whole numbers."""

    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def discriminator_kind(data_type: DataType) -> XorDecisionKind | None:
    """Map a discriminator element's type to its XOR partition kind (K7).

    Returns ``None`` for types that cannot be partitioned decidably (DATE/URI),
    which the validator turns into a finding. The mapping is the single source
    of truth shared by the validator and the modelling operations.
    """

    return {
        DataType.INTEGER: XorDecisionKind.THRESHOLD,
        DataType.FLOAT: XorDecisionKind.THRESHOLD,
        DataType.BOOLEAN: XorDecisionKind.BOOLEAN,
        DataType.STRING: XorDecisionKind.ENUM,
    }.get(data_type)


def resolve_xor_target(decision: XorDecision, value: object) -> str | None:
    """Return the branch target whose cell contains ``value`` (or ``None``).

    A well-formed (K7-valid) decision always returns a target; ``None`` only
    occurs for malformed data (e.g. a non-numeric value on a THRESHOLD split),
    which the caller turns into a runtime error.
    """

    if decision.kind is XorDecisionKind.THRESHOLD:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return None
        for branch in decision.branches:
            if branch.upper is None or value < branch.upper:
                return branch.target
        return None
    if decision.kind is XorDecisionKind.BOOLEAN:
        truth = bool(value)
        for branch in decision.branches:
            if branch.bool_value is truth:
                return branch.target
        return None
    # ENUM
    text = str(value)
    else_target: str | None = None
    for branch in decision.branches:
        if branch.is_else:
            else_target = branch.target
            continue
        if text in branch.values:
            return branch.target
    return else_target


def xor_condition_text(
    discriminator_name: str, decision: XorDecision, index: int
) -> str:
    """Render a human-readable predicate for branch ``index`` (display only).

    The structured :class:`XorDecision` is the source of truth; this string is a
    derived caption used by the editor and the BPMN ``conditionExpression``.
    """

    branch = decision.branches[index]
    disc = discriminator_name
    if decision.kind is XorDecisionKind.THRESHOLD:
        lower = decision.branches[index - 1].upper if index > 0 else None
        upper = branch.upper
        if lower is None:
            return f"{disc} < {_fmt_bound(upper)}" if upper is not None else disc
        if upper is None:
            return f"{disc} >= {_fmt_bound(lower)}"
        return f"{_fmt_bound(lower)} <= {disc} < {_fmt_bound(upper)}"
    if decision.kind is XorDecisionKind.BOOLEAN:
        return f"{disc} == true" if branch.bool_value else f"{disc} == false"
    # ENUM
    if branch.is_else:
        return f"{disc}: otherwise"
    return f"{disc} in [{', '.join(branch.values)}]"


class ControlEdge(BaseModel):
    """A directed control edge between two nodes."""

    source: str
    target: str
    type: EdgeType = EdgeType.CONTROL
    #: Derived, human-readable branch predicate for edges leaving an XOR_SPLIT.
    #: The structured ``XorDecision`` on the schema is the source of truth; this
    #: caption is regenerated from it and is for display / BPMN export only.
    condition: str | None = None


class ProcessSchema(BaseModel):
    """A versioned, block-structured process schema."""

    id: str
    name: str
    version: int = 1
    lifecycle_state: LifecycleState = LifecycleState.ENTWURF
    nodes: dict[str, Node] = Field(default_factory=dict)
    edges: list[ControlEdge] = Field(default_factory=list)
    #: Structured branch partition per XOR_SPLIT node id (K7). Every XOR_SPLIT
    #: carries exactly one decision; the partition is total and disjoint so the
    #: runtime always enables exactly one branch from the instance data.
    xor_decisions: dict[str, XorDecision] = Field(default_factory=dict)
    data_elements: dict[str, DataElement] = Field(default_factory=dict)
    data_accesses: list[DataAccess] = Field(default_factory=list)
    #: Optional input mask per ACTIVITY node id (form designer). A mask is a
    #: presentation layer over ``data_accesses``; rules U1-U3 keep the two
    #: consistent so masks stay Correct by Construction (kein Read ohne Set).
    forms: dict[str, Form] = Field(default_factory=dict)
    connectors: dict[str, ConnectorDescriptor] = Field(default_factory=dict)
    org_model: OrgModel = Field(default_factory=OrgModel)
    #: When set, the schema uses a shared, standalone org model (resolved from
    #: the org registry by this id) instead of its embedded ``org_model``. The
    #: embedded field is then a hydrated, in-memory cache only -- the shared
    #: model in the registry is the single source of truth.
    org_model_id: str | None = None
    staff_rules: dict[str, StaffRule] = Field(default_factory=dict)
    service_bindings: dict[str, ServiceBinding] = Field(default_factory=dict)
    activity_templates: dict[str, ActivityTemplate] = Field(default_factory=dict)
    sub_process_bindings: dict[str, SubProcessBinding] = Field(default_factory=dict)
    follow_up_links: list[FollowUpLink] = Field(default_factory=list)
    #: Optional work-item priorities per interactive node (roadmap E8). Absent
    #: entries default to ``MEDIUM/MEDIUM`` when a worklist is rendered.
    node_priorities: dict[str, WorkItemPriority] = Field(default_factory=dict)
    #: Optional modelled e-mail notifications per ACTIVITY node id (rule group
    #: N). Empty by default so the mail rules N1-N4 stay silent for models
    #: without notifications (fully additive). An entry means "send a mail when
    #: this task becomes ready".
    mail_bindings: dict[str, MailBinding] = Field(default_factory=dict)
    #: Optional per-node temporal annotations (roadmap E5). Empty by default so
    #: the temporal rules T1/T2 stay silent for models without time data.
    time_constraints: dict[str, TimeConstraint] = Field(default_factory=dict)
    #: Optional hard deadline of the whole process in seconds (roadmap E5).
    deadline_seconds: float | None = None
    #: Marks this schema as a reusable sub-process ("sub-model"): once RELEASED
    #: it is offered in the sub-process library for binding into other schemas'
    #: SUBPROCESS nodes. Purely a catalogue flag -- it never affects validation.
    is_library_subprocess: bool = False

    # --- read helpers -----------------------------------------------------

    def start_node(self) -> Node:
        return next(n for n in self.nodes.values() if n.type is NodeType.START)

    def end_node(self) -> Node:
        return next(n for n in self.nodes.values() if n.type is NodeType.END)

    def outgoing(self, node_id: str) -> list[ControlEdge]:
        return [e for e in self.edges if e.source == node_id]

    def incoming(self, node_id: str) -> list[ControlEdge]:
        return [e for e in self.edges if e.target == node_id]

    def accesses_of(self, node_id: str) -> list[DataAccess]:
        return [a for a in self.data_accesses if a.node_id == node_id]

    def writers_of(self, element_id: str) -> list[DataAccess]:
        return [
            a
            for a in self.data_accesses
            if a.element_id == element_id and a.mode in WRITE_MODES
        ]

    def readers_of(self, element_id: str) -> list[DataAccess]:
        return [
            a
            for a in self.data_accesses
            if a.element_id == element_id and a.mode in READ_MODES
        ]


class TemplateOrigin(StrEnum):
    """Where a process template came from.

    ``BUILTIN`` templates ship with the product (blueprints for processes that
    exist in most companies); ``USER`` templates are saved by a modeller from an
    existing schema. The distinction gates deletion: built-in templates are
    provided by code and can never be removed, only user templates can.
    """

    BUILTIN = "BUILTIN"
    USER = "USER"


class ProcessTemplate(BaseModel):
    """A reusable process blueprint: a self-contained schema snapshot + metadata.

    A template is *not* a runnable schema; it is a stored, correct
    :class:`ProcessSchema` snapshot that a modeller can *instantiate* into a
    fresh, editable draft schema. Two invariants keep a template portable and
    safe to instantiate anywhere:

    * the snapshot is always ``ENTWURF`` and ``version == 1`` (a clean draft),
    * its organisation master data is **embedded** (``org_model`` populated,
      ``org_model_id is None``) so the template never depends on a shared org
      model that may or may not exist in the target installation.

    Built-in templates are provided by :mod:`procworks.templates` and are always
    available; user templates are persisted in the template store. Both are
    validated before they are ever stored (No-Bypass), so a template can never
    carry an incorrect blueprint.
    """

    id: str
    name: str
    #: Short human-readable summary shown in the template gallery.
    description: str = ""
    #: Optional grouping label (e.g. "Personal", "Einkauf", "IT") for the
    #: gallery; purely presentational, never validated.
    category: str = ""
    origin: TemplateOrigin = TemplateOrigin.USER
    #: The self-contained schema blueprint (see the class docstring for the
    #: invariants that ``operations.save_as_template`` enforces on it). Named
    #: ``blueprint`` rather than ``schema`` to avoid shadowing Pydantic's
    #: ``BaseModel.schema`` helper.
    blueprint: ProcessSchema
    #: Wall-clock creation time (user templates); ``None`` for built-ins.
    created_at: datetime | None = None


class ProcessInstance(BaseModel):
    """A running instance of a RELEASED schema (Execution Engine, step 8).

    The instance carries the ADEPT-style markings: a node marking (NS) per node
    and an edge marking (ES) per control edge. ``decisions`` records the chosen
    branch of each XOR_SPLIT; ``data_values`` holds the process variables.
    """

    id: str
    schema_id: str
    schema_version: int = 1
    state: InstanceState = InstanceState.RUNNING
    node_states: dict[str, NodeState] = Field(default_factory=dict)
    edge_states: dict[str, EdgeState] = Field(default_factory=dict)
    decisions: dict[str, str] = Field(default_factory=dict)
    data_values: dict[str, object] = Field(default_factory=dict)
    #: Records which agent performed a (completed) node, keyed by node id.
    #: Drives runtime resolution of NodePerformingAgent staff rules and the
    #: per-agent task list.
    performed_by: dict[str, str] = Field(default_factory=dict)
    #: Composition wiring for sub-process execution (step 9 runtime). A child
    #: instance points back to the spawning parent; the parent records, per
    #: SUBPROCESS node, the id of the child instance it started.
    parent_instance_id: str | None = None
    parent_node_id: str | None = None
    child_instances: dict[str, str] = Field(default_factory=dict)
    #: Ids of the decoupled follow-up instances this instance started on
    #: completion (F3, ASYNC). Kept for traceability only.
    follow_up_instances: list[str] = Field(default_factory=list)
    #: Instance-specific ad-hoc schema variant (step 10). When set the engine
    #: runs this instance against this schema instead of the released base; the
    #: ids of executed nodes/edges stay stable so the markings remain valid.
    ad_hoc_schema: ProcessSchema | None = None
    #: Human-readable log of the applied ad-hoc deltas (R1/R2), used for
    #: traceability and the migration ad-hoc compatibility check (M5).
    ad_hoc_deltas: list[str] = Field(default_factory=list)
    #: Marks a throw-away test run started from a non-RELEASED (draft) schema by
    #: a modeller/admin. Test instances are excluded from monitoring KPIs (no
    #: audit events are recorded for them) and flagged as such in the UI.
    is_test: bool = False
    #: Wall-clock time this instance was created, used as the origin for the
    #: process-deadline slack of the time-based worklist prioritisation
    #: (Zeitbasierte-Priorisierung-Konzept, Section 5.2). Additive and optional:
    #: absent on instances created before the feature; the prioritisation then
    #: simply omits the process-slack factor.
    started_at: datetime | None = None
    #: Wall-clock time each node last became ready (ACTIVATED), keyed by node id.
    #: This is the runtime clock of the time-based worklist prioritisation: the
    #: reaction time of an open task is measured from here. Stamped at the API
    #: boundary (mirroring the mail-notification before/after diff) rather than
    #: inside the engine, so ``execution.py`` stays untouched. A re-activated
    #: node (loop) overwrites its stamp, so the clock restarts. Additive: absent
    #: entries fall back to the ``NONE`` criticality band (backward compatible).
    node_activated_at: dict[str, datetime] = Field(default_factory=dict)


# --- integration runtime entities (roadmap E10-E13) ----------------------
# These carry no schema/correctness weight; they are persisted server-side by
# the integration boundary (the external-task runtime and the webhook outbox
# added in the later phases). They live here so the data model stays in one
# place; the validator never inspects them.


class ExternalTaskState(StrEnum):
    """Lifecycle state of an external task (roadmap E11)."""

    CREATED = "CREATED"
    LOCKED = "LOCKED"
    COMPLETED = "COMPLETED"
    INCIDENT = "INCIDENT"
    BPMN_ERROR = "BPMN_ERROR"


class ExternalTask(BaseModel):
    """An automatic activity exposed to an external worker (roadmap E11).

    Created when an automatic ``EXTERNAL_TASK`` step is activated; an outside
    worker fetches and locks it, then reports completion/failure. The
    ``instance_revision_guard`` makes the completion idempotent -- it is applied
    at most once even under at-least-once delivery (rule I5).
    """

    id: str
    instance_id: str
    node_id: str
    topic: str
    state: ExternalTaskState = ExternalTaskState.CREATED
    worker_id: str | None = None
    lock_expires_at: float | None = None
    #: Earliest wall-clock time (epoch seconds) at which a CREATED task may be
    #: fetched again. Set by a failure with retries remaining (backoff); ``None``
    #: means immediately available.
    available_at: float | None = None
    retries_left: int = 5
    input_variables: dict[str, object] = Field(default_factory=dict)
    priority: PriorityLevel = PriorityLevel.MEDIUM
    instance_revision_guard: int = 0
    #: BPMN error code reported by the worker (state ``BPMN_ERROR``); ``None``
    #: otherwise.
    error_code: str | None = None


class Incident(BaseModel):
    """A captured failure of an external task after its retries are exhausted.

    Surfaced in the monitoring view and resolvable by an admin/operator; never
    blocks the pure engine, which keeps the step activated until resolved.
    """

    id: str
    external_task_id: str
    instance_id: str
    node_id: str
    message: str
    created_at: float
    resolved: bool = False


class WebhookSubscription(BaseModel):
    """A server-side subscription delivering domain events to a tool (E13).

    ``secret_ref`` references the HMAC signing secret in the server-side secret
    store; the secret itself never lives in the model (rule I4).
    """

    id: str
    url: str
    events: list[str] = Field(default_factory=list)
    secret_ref: str
    active: bool = True


class OutboxState(StrEnum):
    """Delivery lifecycle of a queued outbound webhook event (E13)."""

    PENDING = "PENDING"      # awaiting (first or retried) delivery
    DELIVERED = "DELIVERED"  # accepted by the receiver (2xx)
    FAILED = "FAILED"        # transient failure, will be retried after back-off
    DEAD = "DEAD"            # retries exhausted -> dead-letter


class OutboxEntry(BaseModel):
    """One queued webhook delivery (transactional outbox row, E13).

    Persisted in the same step as the triggering domain event, so an event is
    never lost on a crash. A dispatcher later delivers it with a back-off retry,
    an HMAC signature and a per-target circuit breaker. ``delivery_id`` is unique
    per attempt-set so the receiver can de-duplicate (idempotent delivery).
    """

    id: str
    subscription_id: str
    event_type: str
    delivery_id: str
    url: str
    payload: dict[str, object] = Field(default_factory=dict)
    state: OutboxState = OutboxState.PENDING
    attempts: int = 0
    max_attempts: int = 5
    next_attempt_at: float = 0.0
    created_at: float = 0.0
    last_status: int | None = None
    last_error: str | None = None
    #: Secret reference for HMAC signing of a subscription-less push delivery
    #: (``HTTP_PUSH`` activity push). Subscription deliveries resolve the secret
    #: from the subscription instead; empty means "do not sign". Never the
    #: secret itself (rule I4).
    secret_ref: str = ""


class WebhookDelivery(BaseModel):
    """A single delivery attempt of an outbox entry (delivery log, E13)."""

    id: str
    outbox_id: str
    subscription_id: str
    event_type: str
    attempt: int
    at: float
    ok: bool
    status_code: int | None = None
    error: str | None = None


class MailOutboxState(StrEnum):
    """Delivery lifecycle of a queued e-mail notification (rule group N).

    Mirrors :class:`OutboxState` for the SMTP channel: an entry is ``PENDING``
    until a send attempt succeeds (``SENT``); a transient error puts it back to
    ``FAILED`` for a back-off retry; once the attempt budget is spent it becomes
    a ``DEAD`` dead-letter (never silently dropped).
    """

    PENDING = "PENDING"    # awaiting (first or retried) send
    SENT = "SENT"          # accepted by the SMTP server
    FAILED = "FAILED"      # transient failure, will be retried after back-off
    DEAD = "DEAD"          # retries exhausted -> dead-letter


class MailOutboxEntry(BaseModel):
    """One durably-queued modelled e-mail notification (transactional outbox, N).

    The durable counterpart of the best-effort :class:`~procworks.mail_runtime.
    MailMessage`: written to the mail outbox in the same boundary step that
    observes a task becoming ready, so a notification is never lost on a crash;
    a dispatcher later delivers it with a back-off retry and a dead-letter.

    ``dedup_key`` is the *idempotency key per activation* (instance + node +
    activation instant, see :func:`~procworks.mail_runtime.activation_dedup_key`):
    a given task activation is enqueued at most once, while a loop re-activation
    (a new activation instant) yields a fresh key and thus a fresh notification.
    The rendered ``recipients``/``subject``/``body`` are snapshotted at enqueue
    time so a later org edit cannot change an already-queued message.
    """

    id: str
    dedup_key: str
    instance_id: str
    node_id: str
    schema_id: str
    recipients: list[str] = Field(default_factory=list)
    subject: str = ""
    body: str = ""
    state: MailOutboxState = MailOutboxState.PENDING
    attempts: int = 0
    max_attempts: int = 5
    next_attempt_at: float = 0.0
    created_at: float = 0.0
    last_error: str | None = None


# --- absence / deputy substitution (operational runtime state) -----------


class AbsenceEntry(BaseModel):
    """A recorded absence (vacation / out-of-office) of an agent for a window.

    Purely **operational runtime state** -- like the activation clock stamps and
    the mail outbox it lives *outside* the correctness model: it is never part of
    a :class:`ProcessSchema`, carries no validator rule, and never changes how a
    model is validated. Its only effect is at runtime, on the *concrete* eligible
    set (:func:`procworks.assignment.eligible_agents`): while ``now`` lies within
    ``[start_at, end_at]`` the agent's deputy (``Agent.deputy_id``) is added to
    the worklist **in parallel** to the agent.

    Crucially the agent itself is **never removed** from the eligible set by an
    absence -- absence only *adds* the deputy. An absence therefore can never
    leave a task without an assignee, so an unregistered deputy cannot stall an
    instance (the safety invariant): worst case the task simply stays with the
    (absent) agent.

    ``start_at``/``end_at`` are timezone-aware instants (inclusive window);
    ``end_at >= start_at`` is checked at the API boundary. ``note`` is an optional
    free-text reason shown back to the agent.
    """

    id: str
    agent_id: str
    start_at: datetime
    end_at: datetime
    note: str = ""

