# SPDX-License-Identifier: BUSL-1.1
"""A model that a stricter rule made invalid after it was stored stays repairable.

Rules get stricter over time: a time value above 100 years, an input pattern
the complexity check now refuses. A schema stored before such a rule carries
a finding it cannot avoid. When every operation counted all findings of its
result, such a schema was frozen for good -- the revision copy was refused,
in a draft every single correction failed on the *other* old value, an
ad-hoc change on one of its cases was refused for the old finding on another
step, a case of the previous version could not move onto it, and a template
captured from it could not even be instantiated.

The repair path: an edit (on a draft or revision, ad hoc on a case) is
refused only for findings it **adds**; old findings stay visible and are
removed step by step. Release and saving a template still demand a model
without any finding, so an invalid model is never released anew. A
migration tolerates an old finding of the target only where the runtime
protects itself against it.
"""

from __future__ import annotations

import itertools
from typing import Any

import pytest
from fastapi.testclient import TestClient
from planted import plant
from staffing import staff_via_api, staffed

import procworks.api as api_module
from procworks import (
    CorrectnessError,
    ExecutionContext,
    FormFieldSpec,
    ProcessSchema,
    ProcessTemplate,
    StaffRule,
    StaffRuleKind,
    TemplateOrigin,
    TimeConstraint,
    ValidationFinding,
    WidgetKind,
    add_data_element,
    adhoc_insert_activity,
    adhoc_rename_activity,
    check_migration,
    connect_data,
    create_empty_schema,
    instantiate,
    instantiate_template,
    new_revision,
    raise_if_findings_added,
    release,
    rename_node,
    save_as_template,
    serial_insert,
    set_deadline,
    set_form,
    set_time_constraint,
    validate,
)
from procworks.model import AccessMode, DataType
from procworks.store import InMemoryInstanceStore

client = TestClient(api_module.app)
_ids = (f"legacy-repair-{n}" for n in itertools.count(1))

#: An address pattern customers commonly used; the complexity check of input
#: patterns refuses it today, a model stored earlier may still carry it.
_OLD_PATTERN = r"^.+@.+\..+$"


def _codes(findings: list[ValidationFinding]) -> list[str]:
    return sorted(f.code or "" for f in findings)


def _mail_field(pattern: str) -> FormFieldSpec:
    return FormFieldSpec(element_id="mail", widget=WidgetKind.TEXT, pattern=pattern)


def _two_steps(name: str) -> tuple[ProcessSchema, str, str]:
    """A staffed draft ``A -> B`` with a mail field on A's mask."""

    schema = create_empty_schema(name, schema_id=next(_ids))
    schema = serial_insert(schema, "A", after_node_id="start")
    a = next(n.id for n in schema.nodes.values() if n.label == "A")
    schema = serial_insert(schema, "B", after_node_id=a)
    b = next(n.id for n in schema.nodes.values() if n.label == "B")
    schema = add_data_element(schema, "Mail", DataType.STRING, element_id="mail")
    schema = connect_data(schema, a, "mail", AccessMode.WRITE)
    schema = set_form(schema, a, fields=[_mail_field("^[a-z]+$")])
    return staffed(schema), a, b


def _aged(schema: ProcessSchema, node: str, *, deadline: bool = True) -> ProcessSchema:
    """``schema`` as an update finds it: a reaction time of 1e12 s on ``node``,
    optionally a deadline of 1e12 s, and the old pattern on the mask of
    ``node`` -- set behind the operations, as stored before the bounds."""

    aged = schema.model_copy(deep=True)
    aged.time_constraints[node] = TimeConstraint(target_lead_seconds=1e12)
    if deadline:
        aged.deadline_seconds = 1e12
    field = aged.forms[node].fields[0]
    aged.forms[node].fields[0] = field.model_copy(update={"pattern": _OLD_PATTERN})
    assert {"T1.lead-too-large", "U2.pattern-too-complex"} <= set(_codes(validate(aged)))
    return aged


# --- the comparison itself ------------------------------------------------------


def test_raise_if_findings_added_names_only_the_added_ones() -> None:
    schema, a, b = _two_steps("Vergleich")
    old = _aged(schema, a, deadline=False)
    worse = old.model_copy(deep=True)
    worse.time_constraints[b] = TimeConstraint(max_duration_seconds=1e13)

    assert raise_if_findings_added(old, old) is old
    with pytest.raises(CorrectnessError) as exc:
        raise_if_findings_added(old, worse)
    assert [(f.code, f.node_id) for f in exc.value.findings] == [("T1.duration-too-large", b)]


# --- drafts: correct one old value after the other ------------------------------


def test_a_draft_with_two_old_values_can_be_corrected_value_by_value() -> None:
    schema, a, _ = _two_steps("Zwei Altwerte")
    draft = _aged(schema, a)

    draft = set_time_constraint(draft, a, TimeConstraint(target_lead_seconds=60))
    assert "T1.lead-too-large" not in _codes(validate(draft))
    assert "T1.deadline-too-large" in _codes(validate(draft))
    draft = set_deadline(draft, None)
    draft = set_form(draft, a, fields=[_mail_field("^[a-z]+@[a-z.]+$")])

    assert validate(draft) == []
    assert release(draft).lifecycle_state.value == "RELEASED"


def test_an_unrelated_edit_of_a_draft_with_old_findings_is_accepted() -> None:
    schema, a, b = _two_steps("Unabhängig")
    draft = _aged(schema, a)

    edited = rename_node(serial_insert(draft, "C", after_node_id=b), b, "B neu")

    assert set(_codes(validate(edited))) == set(_codes(validate(draft)))


def test_an_edit_that_adds_a_finding_is_refused_for_that_finding_only() -> None:
    schema, a, b = _two_steps("Neuer Befund")
    draft = _aged(schema, a)

    with pytest.raises(CorrectnessError) as exc:
        set_time_constraint(draft, b, TimeConstraint(max_duration_seconds=1e12))

    assert [(f.code, f.node_id) for f in exc.value.findings] == [("T1.duration-too-large", b)]


def test_worsening_an_old_value_counts_as_a_new_finding() -> None:
    """A changed value is a changed finding (its parameter differs): the
    comparison errs on the side of refusing."""

    schema, a, _ = _two_steps("Verschlechtert")
    draft = _aged(schema, a, deadline=False)

    with pytest.raises(CorrectnessError) as exc:
        set_time_constraint(draft, a, TimeConstraint(target_lead_seconds=2e12))

    assert _codes(exc.value.findings) == ["T1.lead-too-large"]


def test_release_still_demands_a_model_without_findings() -> None:
    schema, a, _ = _two_steps("Freigabe")
    draft = _aged(schema, a)

    with pytest.raises(CorrectnessError) as exc:
        release(draft)

    assert {"T1.lead-too-large", "T1.deadline-too-large", "U2.pattern-too-complex"} <= set(
        _codes(exc.value.findings)
    )


# --- revision of a released old schema ------------------------------------------


def test_a_released_old_schema_can_be_revised_and_repaired() -> None:
    schema, a, _ = _two_steps("Revision")
    released = _aged(release(schema), a)

    revision = new_revision(released)
    assert revision.lifecycle_state.value == "ENTWURF"
    assert set(_codes(validate(revision))) == set(_codes(validate(released)))
    with pytest.raises(CorrectnessError, match=r"\[T1\]"):
        release(revision)

    revision = set_time_constraint(revision, a, TimeConstraint(target_lead_seconds=60))
    revision = set_deadline(revision, 86400)
    revision = set_form(revision, a, fields=[_mail_field("^[a-z@.]+$")])

    assert release(revision).version == released.version + 1


# --- templates ------------------------------------------------------------------


def test_saving_a_template_still_demands_a_model_without_findings() -> None:
    schema, a, _ = _two_steps("Vorlage speichern")

    with pytest.raises(CorrectnessError) as exc:
        save_as_template(_aged(schema, a), name="Alt")

    assert "T1.lead-too-large" in _codes(exc.value.findings)


def test_an_old_template_instantiates_as_a_draft_that_cannot_be_released_yet() -> None:
    schema, a, _ = _two_steps("Altvorlage")
    template = save_as_template(schema, name="Altvorlage", origin=TemplateOrigin.USER)
    old = ProcessTemplate.model_validate(
        {**template.model_dump(), "blueprint": _aged(template.blueprint, a).model_dump()}
    )

    draft = instantiate_template(old, name="Aus Altvorlage")

    assert draft.lifecycle_state.value == "ENTWURF"
    assert "T1.lead-too-large" in _codes(validate(draft))
    with pytest.raises(CorrectnessError, match=r"\[T1\]"):
        release(draft)


# --- ad hoc on a case of an old schema ------------------------------------------


def _running_case(released: ProcessSchema) -> Any:
    context = ExecutionContext(lambda sid, v: released, InMemoryInstanceStore())
    return instantiate(released, context=context)


def test_an_ad_hoc_change_on_a_case_of_an_old_schema_is_accepted() -> None:
    schema, a, b = _two_steps("Ad hoc")
    released = _aged(release(schema), a)
    case = _running_case(released)
    rule = released.staff_rules[a]

    changed = adhoc_insert_activity(case, released, a, "Zusatz", staff_rule=rule)
    changed = adhoc_rename_activity(changed, released, b, "B umbenannt")

    assert changed.ad_hoc_schema is not None
    assert "Zusatz" in {n.label for n in changed.ad_hoc_schema.nodes.values()}


def test_an_ad_hoc_change_that_adds_a_finding_stays_refused() -> None:
    schema, a, _ = _two_steps("Ad hoc negativ")
    released = _aged(release(schema), a)
    case = _running_case(released)

    with pytest.raises(CorrectnessError) as exc:
        adhoc_insert_activity(
            case, released, a, "Falsch besetzt",
            staff_rule=StaffRule(kind=StaffRuleKind.ROLE, ref="ghost"),
        )

    assert {f.rule for f in exc.value.findings} == {"Z1"}


# --- migration onto a target with old findings ----------------------------------


def _versions(name: str) -> tuple[ProcessSchema, ProcessSchema, str]:
    """Released v1 and released v2 (an extra step at the end) of one lineage."""

    schema, a, b = _two_steps(name)
    v1 = release(schema)
    v2 = new_revision(v1)
    v2 = staffed(serial_insert(v2, "C", after_node_id=b))
    return v1, release(v2), a


def test_a_case_can_move_onto_a_target_whose_old_findings_the_runtime_survives() -> None:
    """Time values beyond the bound are treated as "no due date" at runtime,
    an unsafe old pattern is not run -- neither endangers the case."""

    v1, v2, a = _versions("Migration sicher")
    target = _aged(v2, a)
    case = _running_case(v1)

    assert check_migration(case, v1, target) == []


def test_an_old_finding_the_runtime_does_not_survive_still_blocks_the_migration() -> None:
    """An old staffing rule naming a role that does not exist (Z1) leaves the
    step in nobody's worklist: the target is not tolerated."""

    v1, v2, a = _versions("Migration unsicher")
    target = _aged(v2, a)
    c = next(n.id for n in target.nodes.values() if n.label == "C")
    target.staff_rules[c] = StaffRule(kind=StaffRuleKind.ROLE, ref="ghost")
    case = _running_case(v1)

    findings = check_migration(case, v1, target)

    assert {(f.code, f.params.get("rule")) for f in findings} == {("M1.incorrect", "Z1")}


# --- over the API ---------------------------------------------------------------


def _api_released(name: str) -> tuple[str, str]:
    sid = client.post("/schemas", json={"name": name}).json()["id"]
    schema = client.post(f"/schemas/{sid}/serial-insert",
                         json={"label": "A", "after_node_id": "start"}).json()
    node = next(n["id"] for n in schema["nodes"].values() if n["label"] == "A")
    staff_via_api(client, sid)
    assert client.post(f"/schemas/{sid}/release").status_code == 200
    return sid, node


def _api_age(sid: str, node: str) -> None:
    stored = api_module._store.get(sid)
    assert stored is not None
    aged = stored.model_copy(deep=True)
    aged.time_constraints[node] = TimeConstraint(target_lead_seconds=1e12)
    aged.deadline_seconds = 1e12
    plant(api_module._store, aged)


def test_api_revision_of_an_old_schema_is_created_and_repaired() -> None:
    sid, node = _api_released("API Altschema")
    _api_age(sid, node)

    revision = client.post(f"/schemas/{sid}/revision", json={})
    assert revision.status_code == 200, revision.text
    rid = revision.json()["id"]

    refused = client.post(f"/schemas/{rid}/release")
    assert refused.status_code == 422
    assert {"T1.lead-too-large", "T1.deadline-too-large"} <= {
        f["code"] for f in refused.json()["detail"]["findings"]
    }
    fixed = client.post(f"/schemas/{rid}/time-constraint",
                        json={"node_id": node, "constraint": {"target_lead_seconds": 60}})
    assert fixed.status_code == 200, fixed.text
    assert client.post(f"/schemas/{rid}/deadline",
                       json={"deadline_seconds": None}).status_code == 200
    released = client.post(f"/schemas/{rid}/release")
    assert released.status_code == 200, released.text


def test_api_a_new_too_large_value_in_a_revision_is_still_refused() -> None:
    sid, node = _api_released("API neuer Wert")
    _api_age(sid, node)
    rid = client.post(f"/schemas/{sid}/revision", json={}).json()["id"]

    resp = client.post(f"/schemas/{rid}/time-constraint",
                       json={"node_id": node,
                             "constraint": {"target_lead_seconds": 1e12,
                                            "max_duration_seconds": 1e12}})

    assert resp.status_code == 422
    assert [f["code"] for f in resp.json()["detail"]["findings"]] == ["T1.duration-too-large"]


# --- what counts as the same old finding ----------------------------------------
#
# The identity of a finding is rule, code, step and parameters. An old pattern
# finding named only the field, an old robustness finding only the bound: one
# unsafe pattern exchanged for another, or one too large value for another,
# looked like the old finding and slipped into the draft. Now these findings
# carry the offending value. The field itself is identified by the element it
# binds, so renaming a field that has an old finding is no new finding.


def test_exchanging_an_old_pattern_for_another_unsafe_one_is_refused() -> None:
    schema, a, _ = _two_steps("Muster getauscht")
    draft = _aged(schema, a, deadline=False)

    with pytest.raises(CorrectnessError) as exc:
        set_form(draft, a, fields=[_mail_field("(a+)+b")])

    [finding] = exc.value.findings
    assert (finding.code, finding.node_id) == ("U2.pattern-too-complex", a)
    assert finding.params["pattern"] == "(a+)+b"


def test_the_unchanged_old_pattern_still_passes_other_edits() -> None:
    schema, a, b = _two_steps("Muster unverändert")
    draft = _aged(schema, a, deadline=False)

    edited = set_form(draft, a, title="Neu", fields=[_mail_field(_OLD_PATTERN)])
    edited = rename_node(edited, b, "B neu")

    [old] = [f for f in validate(edited) if f.code == "U2.pattern-too-complex"]
    assert old.params["pattern"] == _OLD_PATTERN


def test_renaming_a_field_with_an_old_pattern_is_no_new_finding() -> None:
    """Grenzfall: the label is shown, the bound element identifies."""

    schema, a, _ = _two_steps("Feld umbenannt")
    draft = _aged(schema, a, deadline=False)
    field = FormFieldSpec(
        element_id="mail", widget=WidgetKind.TEXT, pattern=_OLD_PATTERN, label="E-Mail neu"
    )

    renamed = set_form(draft, a, fields=[field])

    [old] = [f for f in validate(renamed) if f.code == "U2.pattern-too-complex"]
    # Shown by its label, identified by the element it binds (a mask binds an
    # element once; field ids are reissued whenever the mask is saved).
    assert (old.params["field"], old.params["field_key"]) == ("E-Mail neu", "mail")


def test_the_same_field_name_does_not_hide_a_new_field() -> None:
    """Negativ: a second field with the same label but another element is a new finding."""

    schema, a, _ = _two_steps("Zweites Feld")
    draft = _aged(schema, a, deadline=False)
    draft = add_data_element(draft, "Mail 2", DataType.STRING, element_id="mail2")
    draft = connect_data(draft, a, "mail2", AccessMode.WRITE)
    label = draft.forms[a].fields[0].label

    with pytest.raises(CorrectnessError) as exc:
        set_form(draft, a, fields=[
            FormFieldSpec(element_id="mail", widget=WidgetKind.TEXT,
                          pattern=_OLD_PATTERN, label=label),
            FormFieldSpec(element_id="mail2", widget=WidgetKind.TEXT,
                          pattern=_OLD_PATTERN, label=label),
        ])

    assert _codes(exc.value.findings) == ["U2.pattern-too-complex"]


def _automatic(name: str, **values: int) -> tuple[ProcessSchema, str]:
    """A draft with one automatic external-task step whose binding is aged
    with ``values`` (set behind the operations, as stored before the bounds)."""

    from procworks import assign_service, set_automation
    from procworks.model import AutomationKind

    schema = create_empty_schema(name, schema_id=next(_ids))
    schema = serial_insert(schema, "Lauf", after_node_id="start")
    step = next(n.id for n in schema.nodes.values() if n.label == "Lauf")
    schema = assign_service(schema, step, "Lauf", automatic=True)
    schema = set_automation(schema, step, AutomationKind.EXTERNAL_TASK, topic="t")
    aged = schema.model_copy(deep=True)
    aged.service_bindings[step] = aged.service_bindings[step].model_copy(update=values)
    return aged, step


@pytest.mark.parametrize(
    ("field", "code", "old", "other"),
    [
        ("retry_backoff_ms", "I1.backoff-too-large", 10**12, 10**13),
        ("request_timeout_ms", "I1.timeout-too-large", 10**6, 10**7),
        ("retry_max", "I1.retries-too-large", 10**6, 10**7),
    ],
)
def test_exchanging_an_old_robustness_value_for_another_too_large_one_is_refused(
    field: str, code: str, old: int, other: int
) -> None:
    from procworks import set_automation
    from procworks.model import AutomationKind

    draft, step = _automatic(f"Robust {field}", **{field: old})
    [finding] = validate(draft)
    assert (finding.code, finding.params["value"]) == (code, str(old))

    # Unchanged old value: another edit passes.
    renamed = rename_node(draft, step, "Lauf neu")
    assert [f.code for f in validate(renamed)] == [code]

    with pytest.raises(CorrectnessError) as exc:
        set_automation(draft, step, AutomationKind.EXTERNAL_TASK, topic="t", **{field: other})
    [added] = exc.value.findings
    assert (added.code, added.params["value"]) == (code, str(other))
