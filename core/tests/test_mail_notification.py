# SPDX-License-Identifier: BUSL-1.1
"""Modelled e-mail notification tests (rule group N + mail_runtime + trigger).

Covers the correctness rules N1-N4 (only modellable when every recipient has an
address and every placeholder resolves), the runtime recipient resolution and
template rendering, and the API trigger that fires a notification when a task
becomes ready.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from procworks import (
    add_agent,
    add_data_element,
    add_org_unit,
    add_role,
    assign_staff_rule,
    connect_data,
    create_empty_schema,
    instantiate,
    release,
    serial_insert,
    set_agent_deputy,
    set_mail_binding,
    set_role_mailbox,
    set_unit_mailbox,
    update_agent,
    validate,
)
from procworks import api as api_module
from procworks.api import app
from procworks.mail_runtime import (
    MailMessage,
    build_message,
    newly_ready_mail_nodes,
    notify_ready_tasks,
    render_template,
)
from procworks.model import (
    AccessMode,
    DataType,
    MailBinding,
    MailRecipientMode,
    NodeState,
    StaffRule,
    StaffRuleKind,
)
from procworks.validator import CorrectnessError


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #
def _activity(schema, label):
    return next(n.id for n in schema.nodes.values() if n.label == label)


def _role_rule(role_id: str) -> StaffRule:
    return StaffRule(kind=StaffRuleKind.ROLE, ref=role_id)


def _base_schema(*, with_email: bool = True):
    """A two-activity schema: 'Erfassen' writes ``kundenname``, 'Prüfen' is the
    task that carries the notification and is assigned to role ``sb``."""

    schema = create_empty_schema("Mail", schema_id="mail")
    schema = serial_insert(schema, "Erfassen", after_node_id="start")
    schema = serial_insert(schema, "Prüfen", after_node_id=_activity(schema, "Erfassen"))
    schema = add_data_element(schema, "Kundenname", DataType.STRING, element_id="kundenname")
    schema = connect_data(
        schema, _activity(schema, "Erfassen"), "kundenname", AccessMode.WRITE
    )
    schema = add_role(schema, "Sachbearbeiter", role_id="sb")
    schema = add_agent(
        schema,
        "Erika",
        role_ids=["sb"],
        agent_id="a1",
        email="erika@firma.de" if with_email else None,
    )
    schema = assign_staff_rule(schema, _activity(schema, "Prüfen"), _role_rule("sb"))
    return schema


def _mail(**kw) -> MailBinding:
    kw.setdefault("subject", "Neue Aufgabe")
    return MailBinding(**kw)


# --------------------------------------------------------------------------- #
# N1 -- address well-formedness
# --------------------------------------------------------------------------- #
def test_n1_rejects_malformed_agent_email():
    schema = create_empty_schema("N1", schema_id="n1")
    schema = serial_insert(schema, "A", after_node_id="start")
    schema = add_role(schema, "R", role_id="r")
    with pytest.raises(CorrectnessError) as exc:
        add_agent(schema, "Bad", role_ids=["r"], agent_id="a1", email="not-an-email")
    assert any(f.rule == "N1" for f in exc.value.findings)


def test_n1_rejects_malformed_role_mailbox():
    schema = _base_schema()
    with pytest.raises(CorrectnessError) as exc:
        set_role_mailbox(schema, "sb", "missing-at-sign")
    assert any(f.rule == "N1" for f in exc.value.findings)


def test_n1_accepts_valid_addresses():
    schema = _base_schema()
    schema = set_role_mailbox(schema, "sb", "sachbearbeitung@firma.de")
    assert validate(schema) == []


# --------------------------------------------------------------------------- #
# N2 -- binding must sit on an ACTIVITY with a staff rule
# --------------------------------------------------------------------------- #
def test_n2_rejects_binding_on_activity_without_staff_rule():
    schema = _base_schema()
    # 'Erfassen' carries no staff rule.
    with pytest.raises(CorrectnessError) as exc:
        set_mail_binding(schema, _activity(schema, "Erfassen"), _mail())
    assert any(f.rule == "N2" for f in exc.value.findings)


def test_set_mail_binding_rejects_non_activity_node():
    schema = _base_schema()
    with pytest.raises(CorrectnessError) as exc:
        set_mail_binding(schema, "start", _mail())
    # The operation guard (OP) fires before validation for a wrong node type.
    assert any(f.rule in {"OP", "N2"} for f in exc.value.findings)


# --------------------------------------------------------------------------- #
# N3 -- every possible recipient is addressable
# --------------------------------------------------------------------------- #
def test_n3_per_agent_rejects_agent_without_email():
    schema = _base_schema(with_email=False)
    with pytest.raises(CorrectnessError) as exc:
        set_mail_binding(schema, _activity(schema, "Prüfen"), _mail())
    assert any(f.rule == "N3" for f in exc.value.findings)


def test_n3_per_agent_accepts_when_all_addressable():
    schema = _base_schema()
    schema = set_mail_binding(schema, _activity(schema, "Prüfen"), _mail())
    assert validate(schema) == []


def test_n3_per_agent_rejects_deputy_without_email():
    schema = _base_schema()
    # A deputy of Erika is also eligible at runtime; without an address N3 fails.
    schema = add_agent(schema, "Vertretung", role_ids=[], agent_id="a2", email=None)
    schema = set_agent_deputy(schema, "a1", "a2")
    with pytest.raises(CorrectnessError) as exc:
        set_mail_binding(
            schema, _activity(schema, "Prüfen"), _mail(include_deputies=True)
        )
    assert any(f.rule == "N3" for f in exc.value.findings)


def test_n3_per_agent_can_opt_out_of_deputies():
    schema = _base_schema()
    schema = add_agent(schema, "Vertretung", role_ids=[], agent_id="a2", email=None)
    schema = set_agent_deputy(schema, "a1", "a2")
    # With include_deputies=False the address-less deputy is not required.
    schema = set_mail_binding(
        schema, _activity(schema, "Prüfen"), _mail(include_deputies=False)
    )
    assert validate(schema) == []


def test_n3_per_agent_rejects_non_determinable_recipients():
    """A staff rule that depends on a prior node's performer cannot be enumerated
    statically, so per-agent notification is refused (use a group mailbox)."""

    schema = create_empty_schema("N3u", schema_id="n3u")
    schema = serial_insert(schema, "Erfassen", after_node_id="start")
    schema = serial_insert(schema, "Prüfen", after_node_id=_activity(schema, "Erfassen"))
    schema = add_role(schema, "R", role_id="r")
    schema = add_agent(schema, "Erika", role_ids=["r"], agent_id="a1", email="e@firma.de")
    schema = assign_staff_rule(schema, _activity(schema, "Erfassen"), _role_rule("r"))
    schema = assign_staff_rule(
        schema,
        _activity(schema, "Prüfen"),
        StaffRule(
            kind=StaffRuleKind.NODE_PERFORMING_AGENT, ref=_activity(schema, "Erfassen")
        ),
    )
    with pytest.raises(CorrectnessError) as exc:
        set_mail_binding(schema, _activity(schema, "Prüfen"), _mail())
    assert any(f.rule == "N3" for f in exc.value.findings)


def test_n3_group_rejects_role_without_mailbox():
    schema = _base_schema()
    with pytest.raises(CorrectnessError) as exc:
        set_mail_binding(
            schema,
            _activity(schema, "Prüfen"),
            _mail(mode=MailRecipientMode.TO_GROUP_MAILBOX),
        )
    assert any(f.rule == "N3" for f in exc.value.findings)


def test_n3_group_accepts_with_role_mailbox():
    schema = _base_schema()
    schema = set_role_mailbox(schema, "sb", "sachbearbeitung@firma.de")
    schema = set_mail_binding(
        schema,
        _activity(schema, "Prüfen"),
        _mail(mode=MailRecipientMode.TO_GROUP_MAILBOX),
    )
    assert validate(schema) == []


def test_n3_group_with_unit_mailbox():
    schema = _base_schema()
    schema = add_org_unit(schema, "Einkauf", org_unit_id="einkauf")
    schema = update_agent(schema, "a1", org_unit_id="einkauf")
    schema = assign_staff_rule(
        schema,
        _activity(schema, "Prüfen"),
        StaffRule(kind=StaffRuleKind.ORG_UNIT, ref="einkauf"),
    )
    # Without a unit mailbox N3 fails ...
    with pytest.raises(CorrectnessError):
        set_mail_binding(
            schema,
            _activity(schema, "Prüfen"),
            _mail(mode=MailRecipientMode.TO_GROUP_MAILBOX),
        )
    # ... with one it passes.
    schema = set_unit_mailbox(schema, "einkauf", "einkauf@firma.de")
    schema = set_mail_binding(
        schema,
        _activity(schema, "Prüfen"),
        _mail(mode=MailRecipientMode.TO_GROUP_MAILBOX),
    )
    assert validate(schema) == []


# --------------------------------------------------------------------------- #
# N4 -- template placeholders resolve
# --------------------------------------------------------------------------- #
def test_n4_accepts_placeholder_written_before():
    schema = _base_schema()
    schema = set_mail_binding(
        schema,
        _activity(schema, "Prüfen"),
        _mail(subject="Antrag von {kundenname}"),
    )
    assert validate(schema) == []


def test_n4_rejects_unknown_placeholder():
    schema = _base_schema()
    with pytest.raises(CorrectnessError) as exc:
        set_mail_binding(
            schema, _activity(schema, "Prüfen"), _mail(subject="Hallo {unbekannt}")
        )
    assert any(f.rule == "N4" for f in exc.value.findings)


def test_n4_rejects_placeholder_not_written_before():
    """An element only written *at* the notified node is not yet available."""

    schema = _base_schema()
    pruefen = _activity(schema, "Prüfen")
    schema = add_data_element(schema, "Ergebnis", DataType.STRING, element_id="ergebnis")
    schema = connect_data(schema, pruefen, "ergebnis", AccessMode.WRITE)
    with pytest.raises(CorrectnessError) as exc:
        set_mail_binding(schema, pruefen, _mail(body="Ergebnis: {ergebnis}"))
    assert any(f.rule == "N4" for f in exc.value.findings)


# --------------------------------------------------------------------------- #
# Additivity -- silent for models without notifications
# --------------------------------------------------------------------------- #
def test_group_n_is_silent_without_bindings_or_addresses():
    schema = _base_schema(with_email=False)  # no email, no binding
    assert validate(schema) == []


# --------------------------------------------------------------------------- #
# Runtime: recipient resolution, rendering, trigger detection
# --------------------------------------------------------------------------- #
def test_render_template_substitutes_instance_values():
    schema = _base_schema()
    schema = release(schema)
    instance = instantiate(schema)
    instance.data_values["kundenname"] = "Meier"
    assert render_template("Antrag von {kundenname}", instance) == "Antrag von Meier"


def test_build_message_resolves_agent_addresses():
    schema = _base_schema()
    binding = _mail(subject="Neue Aufgabe für {kundenname}")
    schema = set_mail_binding(schema, _activity(schema, "Prüfen"), binding)
    schema = release(schema)
    instance = instantiate(schema)
    instance.data_values["kundenname"] = "Meier"
    message = build_message(schema, _activity(schema, "Prüfen"), binding, instance)
    assert message is not None
    assert message.to == ["erika@firma.de"]
    assert message.subject == "Neue Aufgabe für Meier"


def test_build_message_group_mode_uses_role_mailbox():
    schema = _base_schema()
    schema = set_role_mailbox(schema, "sb", "sachbearbeitung@firma.de")
    binding = _mail(mode=MailRecipientMode.TO_GROUP_MAILBOX)
    schema = set_mail_binding(schema, _activity(schema, "Prüfen"), binding)
    schema = release(schema)
    instance = instantiate(schema)
    message = build_message(schema, _activity(schema, "Prüfen"), binding, instance)
    assert message is not None
    assert message.to == ["sachbearbeitung@firma.de"]


def test_newly_ready_only_reports_transition():
    schema = _base_schema()
    schema = set_mail_binding(schema, _activity(schema, "Prüfen"), _mail())
    schema = release(schema)
    instance = instantiate(schema)
    pruefen = _activity(schema, "Prüfen")
    # 'Prüfen' is not yet ready at instantiation (still NOT_ACTIVATED).
    assert newly_ready_mail_nodes(schema, None, instance) == []
    # Simulate the node becoming ready; with the correct 'before' it is a fresh
    # transition, but if it was already ACTIVATED before, it is not reported.
    before = dict(instance.node_states)
    instance.node_states[pruefen] = NodeState.ACTIVATED
    assert newly_ready_mail_nodes(schema, before, instance) == [pruefen]
    assert newly_ready_mail_nodes(schema, dict(instance.node_states), instance) == []


class _CollectingSender:
    def __init__(self) -> None:
        self.sent: list[MailMessage] = []

    def send(self, message: MailMessage) -> None:
        self.sent.append(message)


class _FailingSender:
    def send(self, message: MailMessage) -> None:
        raise RuntimeError("smtp down")


def test_notify_ready_tasks_sends_and_is_soft_on_failure():
    schema = _base_schema()
    pruefen = _activity(schema, "Prüfen")
    schema = set_mail_binding(schema, pruefen, _mail())
    schema = release(schema)
    instance = instantiate(schema)
    before = dict(instance.node_states)
    instance.node_states[pruefen] = NodeState.ACTIVATED

    collector = _CollectingSender()
    sent = notify_ready_tasks(schema, before, instance, collector)
    assert len(sent) == 1 and sent[0].to == ["erika@firma.de"]

    # A failing transport must not raise (stability: never break the step).
    assert notify_ready_tasks(schema, before, instance, _FailingSender()) == []


# --------------------------------------------------------------------------- #
# API trigger end-to-end (legacy instantiate path)
# --------------------------------------------------------------------------- #
def test_api_trigger_sends_mail_on_ready_task(monkeypatch):
    """A released schema whose first activity carries a notification sends a
    mail through the module sender when it is instantiated."""

    # First activity after start is the notified task, assigned + addressable.
    schema = create_empty_schema("ApiMail", schema_id="apimail")
    schema = serial_insert(schema, "Prüfen", after_node_id="start")
    schema = add_role(schema, "SB", role_id="sb")
    schema = add_agent(schema, "Erika", role_ids=["sb"], agent_id="a1", email="e@firma.de")
    schema = assign_staff_rule(schema, _activity(schema, "Prüfen"), _role_rule("sb"))
    schema = set_mail_binding(schema, _activity(schema, "Prüfen"), _mail())
    schema = release(schema)

    collector = _CollectingSender()
    monkeypatch.setattr(api_module, "_mail_sender", collector)
    api_module._store.put(schema)
    try:
        client = TestClient(app)
        resp = client.post(f"/schemas/{schema.id}/instances")
        assert resp.status_code == 201
        assert len(collector.sent) == 1
        assert collector.sent[0].to == ["e@firma.de"]
    finally:
        api_module._store.clear()


def test_api_trigger_on_downstream_task_after_complete(monkeypatch):
    """Completing an activity that activates a *downstream* notified task sends a
    mail exactly then -- not before (the earlier task carries no binding)."""

    schema = create_empty_schema("ApiMail2", schema_id="apimail2")
    schema = serial_insert(schema, "Erfassen", after_node_id="start")
    schema = serial_insert(schema, "Prüfen", after_node_id=_activity(schema, "Erfassen"))
    schema = add_role(schema, "SB", role_id="sb")
    schema = add_agent(schema, "Erika", role_ids=["sb"], agent_id="a1", email="e@firma.de")
    schema = assign_staff_rule(schema, _activity(schema, "Erfassen"), _role_rule("sb"))
    schema = assign_staff_rule(schema, _activity(schema, "Prüfen"), _role_rule("sb"))
    schema = set_mail_binding(schema, _activity(schema, "Prüfen"), _mail())
    schema = release(schema)

    collector = _CollectingSender()
    monkeypatch.setattr(api_module, "_mail_sender", collector)
    api_module._store.put(schema)
    try:
        client = TestClient(app)
        iid = client.post(f"/schemas/{schema.id}/instances").json()["id"]
        assert collector.sent == []  # 'Erfassen' carries no binding
        resp = client.post(
            f"/instances/{iid}/complete",
            json={"node_id": _activity(schema, "Erfassen"), "agent_id": "a1"},
        )
        assert resp.status_code == 200
        assert len(collector.sent) == 1 and collector.sent[0].to == ["e@firma.de"]
    finally:
        api_module._store.clear()
