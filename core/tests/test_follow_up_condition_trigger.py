# SPDX-License-Identifier: BUSL-1.1
"""A follow-up started on completion carries no condition (F4).

A follow-up with trigger ``ON_COMPLETE`` always starts; the runtime never
reads its ``condition``. A condition stored on such a link was therefore never
checked (``betrag >`` was accepted) and never evaluated -- the model carried a
meaningless expression that a reader takes for a rule. Such a link is now
refused for every input (``validate()``), with its own code that names the
cause. A model stored before keeps the finding as an old one: other edits and
removing the link stay possible, a release does not, and migration and test
instances tolerate it because the runtime ignores the value.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from staffing import staff_via_api, staffed
from web_vm import needs_node, run_app_js

from procworks import (
    ExecutionContext,
    complete_activity,
    create_empty_schema,
    instantiate,
    link_follow_up,
    release,
    serial_insert,
    unlink_follow_up,
    validate,
)
from procworks.api import app
from procworks.migration import RUNTIME_SAFE_OLD_FINDINGS, draft_test_findings
from procworks.model import (
    FollowUpLink,
    FollowUpTrigger,
    InstanceState,
    NodeType,
    ProcessSchema,
)
from procworks.operations import CorrectnessError
from procworks.store import InMemoryInstanceStore

_CODE = "F4.condition-ignored"


def _resolver_for(*schemas: ProcessSchema):
    by_id = {s.id: s for s in schemas}

    def resolve(schema_id: str, version: int | None) -> ProcessSchema | None:
        schema = by_id.get(schema_id)
        if schema is None or (version is not None and schema.version != version):
            return None
        return schema

    return resolve


def _target() -> ProcessSchema:
    target = create_empty_schema("Folge", schema_id="cond_trigger_target")
    target = serial_insert(target, "Nacharbeit", after_node_id="start")
    return release(staffed(target))


def _source() -> ProcessSchema:
    source = create_empty_schema("Quelle", schema_id="cond_trigger_source")
    return serial_insert(source, "Bearbeiten", after_node_id="start")


def _with_old_link(schema: ProcessSchema, condition: str) -> ProcessSchema:
    """``schema`` plus an ON_COMPLETE link with ``condition``, as a model
    stored before the rule (no operation can build it any more)."""

    old = schema.model_copy(deep=True)
    old.follow_up_links.append(FollowUpLink(
        id="followup_old", target_schema_id="cond_trigger_target", target_version=1,
        trigger=FollowUpTrigger.ON_COMPLETE, condition=condition))
    return old


@pytest.mark.parametrize("condition", ["betrag >", "betrag > 100", "True"])
def test_a_condition_on_complete_is_refused_with_its_own_code(condition: str) -> None:
    """Negativ (der Grund): refused for the trigger, not for the syntax --
    also a well-formed condition over no element at all."""

    target = _target()
    with pytest.raises(CorrectnessError) as exc:
        link_follow_up(_source(), target.id, target_version=1,
                       trigger=FollowUpTrigger.ON_COMPLETE, condition=condition,
                       resolver=_resolver_for(target))
    codes = [f.code for f in exc.value.findings]
    assert codes == [_CODE]
    assert exc.value.findings[0].rule == "F4"
    params = exc.value.findings[0].params
    assert params == {"link": params["link"], "target": "cond_trigger_target"}


def test_validate_reports_it_for_every_input() -> None:
    """Negativ: the rule lives in ``validate()`` -- a model from any other
    route (store, template, a library caller) shows it as well, also without
    a resolver."""

    findings = validate(_with_old_link(_source(), "betrag >"))
    assert [(f.rule, f.code, f.params) for f in findings] == [
        ("F4", _CODE, {"link": "followup_old", "target": "cond_trigger_target"})]


@pytest.mark.parametrize("condition", [None, "", "   "])
def test_on_complete_without_a_condition_stays_allowed(condition: str | None) -> None:
    """Positiv/Grenzfall: no condition, or an empty one (a client that always
    sends the field), is no expression -- nothing to refuse."""

    target = _target()
    linked = link_follow_up(_source(), target.id, target_version=1,
                            trigger=FollowUpTrigger.ON_COMPLETE, condition=condition,
                            resolver=_resolver_for(target))
    assert len(linked.follow_up_links) == 1
    assert validate(linked, _resolver_for(target)) == []


def test_an_old_model_stays_editable_and_the_link_removable() -> None:
    """Grenzfall (Reparaturweg): the stored finding is old -- an unrelated edit
    and the removal of the link go through; a release does not."""

    old = _with_old_link(_source(), "betrag >")
    edited = serial_insert(old, "Danach", after_node_id=next(
        n.id for n in old.nodes.values() if n.label == "Bearbeiten"))
    assert [f.code for f in validate(edited)] == [_CODE]
    cleaned = unlink_follow_up(edited, "followup_old")
    assert validate(cleaned) == []
    with pytest.raises(CorrectnessError) as exc:
        release(staffed(old), _resolver_for(_target()))
    assert _CODE in {f.code for f in exc.value.findings}


def test_the_runtime_ignores_it_so_it_is_runtime_safe() -> None:
    """Grenzfall: the follow-up of an old released model still starts on
    completion -- the condition is never read. Hence the code is a runtime-safe
    old finding: test instances and migrations are not blocked by it."""

    assert _CODE in RUNTIME_SAFE_OLD_FINDINGS
    target = _target()
    old = _with_old_link(staffed(_source()), "betrag >")
    assert draft_test_findings(old, _resolver_for(target)) == []
    old.lifecycle_state = release(staffed(_source())).lifecycle_state
    context = ExecutionContext(_resolver_for(old, target), InMemoryInstanceStore())
    inst = instantiate(old, context=context)
    act = next(n.id for n in old.nodes.values() if n.type is NodeType.ACTIVITY)
    inst = complete_activity(inst, old, act, context=context)
    assert inst.state is InstanceState.COMPLETED
    assert len(inst.follow_up_instances) == 1


def test_the_api_answers_422_with_the_code() -> None:
    """Negativ (der Grund) über die Schnittstelle: 422, the code names the
    cause; the model is unchanged."""

    client = TestClient(app)
    follow = client.post("/schemas", json={"name": "Folge-Bedingung-Ziel"}).json()["id"]
    client.post(f"/schemas/{follow}/serial-insert",
                json={"label": "Folgeschritt", "after_node_id": "start"})
    staff_via_api(client, follow)
    assert client.post(f"/schemas/{follow}/release").status_code == 200
    pid = client.post("/schemas", json={"name": "Folge-Bedingung-Quelle"}).json()["id"]
    resp = client.post(f"/schemas/{pid}/follow-up", json={
        "target_schema_id": follow, "target_version": 1,
        "trigger": "ON_COMPLETE", "condition": "betrag >"})
    assert resp.status_code == 422
    assert [f["code"] for f in resp.json()["detail"]["findings"]] == [_CODE]
    assert client.get(f"/schemas/{pid}").json()["follow_up_links"] == []
    ok = client.post(f"/schemas/{pid}/follow-up", json={
        "target_schema_id": follow, "target_version": 1, "trigger": "ON_COMPLETE"})
    assert ok.status_code == 200, ok.text


def test_each_link_is_named_once_and_by_its_target() -> None:
    """Grenzfall (zwei Befunde in der Oberfläche): two links carry a condition
    -- two findings, one per link, each naming its own link and target; one
    link gives exactly one finding, also with a resolver (the local check is
    not run a second time behind the resolver gate)."""

    target = _target()
    one = _with_old_link(_source(), "betrag >")
    assert [f.code for f in validate(one, _resolver_for(target))] == [_CODE]
    two = one.model_copy(deep=True)
    two.follow_up_links.append(FollowUpLink(
        id="followup_other", target_schema_id="cond_trigger_other",
        trigger=FollowUpTrigger.ON_COMPLETE, condition="x > 1"))
    found = [f.params for f in validate(two) if f.code == _CODE]
    assert found == [
        {"link": "followup_old", "target": "cond_trigger_target"},
        {"link": "followup_other", "target": "cond_trigger_other"},
    ]


@needs_node
def test_the_client_names_the_follow_up_and_only_a_way_that_exists(
        tmp_path: Path) -> None:
    """Positiv/Negativ (Text): the finding names the target process (its name
    from the loaded list, else the id) and the link id; the hint names the
    interface, the only place a follow-up can be changed -- it never promises
    a control the client does not offer."""

    result = run_app_js(r"""
state.schemaNames = { schema_9: "Rechnung prüfen" };
const f = (target) => findingText({ rule: "F4", code: "F4.condition-ignored",
  params: { link: "followup_3", target } }, { withHint: true });
return { named: f("schema_9"), unknown: f("schema_unbekannt") };
""", tmp_path)
    assert isinstance(result, dict)
    named = result["named"]
    assert "„Rechnung prüfen“" in named and "followup_3" in named
    assert "Schnittstelle" in named
    assert "/follow-up/followup_3" in named
    assert "{id}" not in named
    assert "lässt sich dafür entfernen und neu anlegen" not in named
    assert "„schema_unbekannt“" in result["unknown"]


@needs_node
def test_the_hint_names_the_model_and_the_ids_the_condition_needs(
        tmp_path: Path) -> None:
    """Positiv (Text): the hint spells the request with the id of the shown
    model (the interface needs it, the client shows it nowhere) and says
    that a condition reads data elements by id -- for every element whose
    *name* the old condition uses, the id it must use instead."""

    result = run_app_js(r"""
state.schemaNames = { schema_9: "Rechnung prüfen" };
state.schema = { id: "schema_704146", nodes: {},
  data_elements: { data_704148: { id: "data_704148", name: "betrag" },
                   data_704149: { id: "data_704149", name: "Freigabe Chef" } },
  follow_up_links: [{ id: "followup_3", target_schema_id: "schema_9",
                      trigger: "ON_COMPLETE", condition: "betrag > 5 and data_704149" }] };
return findingText({ rule: "F4", code: "F4.condition-ignored",
  params: { link: "followup_3", target: "schema_9" } }, { withHint: true });
""", tmp_path)
    assert isinstance(result, str)
    assert "DELETE /schemas/schema_704146/follow-up/followup_3" in result
    assert "{id}" not in result
    assert "Kennung" in result and "„betrag“ als data_704148" in result
    # An element the condition already names by id needs no translation.
    assert "Freigabe Chef" not in result


@needs_node
@pytest.mark.parametrize("schema_js", [
    "null",
    # A loaded model that does not own the link (e.g. after switching models).
    '{ id: "schema_other", nodes: {}, data_elements: {}, follow_up_links: [] }',
])
def test_without_the_owning_model_the_hint_names_no_wrong_id(
        tmp_path: Path, schema_js: str) -> None:
    """Grenzfall (Text): when the shown model does not own the link, the hint
    names no model id at all -- never a placeholder, never another model's
    id -- and still says how a condition names data elements."""

    result = run_app_js(rf"""
state.schema = {schema_js};
return findingText({{ rule: "F4", code: "F4.condition-ignored",
  params: {{ link: "followup_3", target: "schema_9" }} }}, {{ withHint: true }});
""", tmp_path)
    assert isinstance(result, str)
    assert "{id}" not in result and "schema_other" not in result
    assert "/follow-up/followup_3" in result
    assert "Kennung" in result
