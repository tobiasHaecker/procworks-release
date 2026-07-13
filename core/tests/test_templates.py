# SPDX-License-Identifier: BUSL-1.1
"""Tests for reusable process templates (built-in library + user templates).

Covers the two operations (``save_as_template``/``instantiate_template``), the
built-in library (self-contained + correct), and the HTTP surface
(list/get/save/instantiate/delete) including the guard that built-in templates
cannot be deleted.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

import procworks
from procworks import operations as ops
from procworks.api import app
from procworks.model import LifecycleState, TemplateOrigin
from procworks.templates import builtin_templates

client = TestClient(app)


# --- operations ----------------------------------------------------------


def _draft_with_step() -> procworks.ProcessSchema:
    """A minimal correct draft: START -> one activity -> END."""

    s = ops.create_empty_schema("Vorlagenquelle")
    s = ops.serial_insert(s, "Aufgabe", s.start_node().id)
    return s


def test_save_and_instantiate_roundtrip() -> None:
    source = _draft_with_step()
    template = ops.save_as_template(source, name="Meine Vorlage", category="Test")
    assert template.origin is TemplateOrigin.USER
    # The blueprint is a clean, self-contained draft.
    assert template.blueprint.lifecycle_state is LifecycleState.ENTWURF
    assert template.blueprint.version == 1
    assert template.blueprint.org_model_id is None

    instance = ops.instantiate_template(template, name="Konkret")
    assert instance.id != template.blueprint.id
    assert instance.name == "Konkret"
    assert instance.lifecycle_state is LifecycleState.ENTWURF
    # Same structure carried over.
    assert len(instance.nodes) == len(source.nodes)


def test_instantiate_keeps_template_name_when_unnamed() -> None:
    template = ops.save_as_template(_draft_with_step(), name="Namensvorlage")
    instance = ops.instantiate_template(template)
    assert instance.name == "Namensvorlage"


def test_snapshot_embeds_shared_org() -> None:
    """A template built from a schema is independent of any shared org model."""

    template = ops.save_as_template(_draft_with_step(), name="X")
    # snapshot_for_template clears the shared-org link and keeps embedded data.
    assert template.blueprint.org_model_id is None


# --- built-in library ----------------------------------------------------


def test_builtin_templates_are_correct_and_self_contained() -> None:
    library = builtin_templates()
    assert len(library) >= 3
    for template in library:
        assert template.origin is TemplateOrigin.BUILTIN
        assert template.blueprint.org_model_id is None
        # Built as correct by construction: no validation findings.
        assert procworks.validate(template.blueprint) == []
        # And instantiable into a fresh draft.
        instance = ops.instantiate_template(template)
        assert instance.lifecycle_state is LifecycleState.ENTWURF


# --- HTTP surface --------------------------------------------------------


def test_list_templates_includes_builtins() -> None:
    resp = client.get("/templates")
    assert resp.status_code == 200
    ids = {t["id"] for t in resp.json()}
    assert "tpl-urlaubsantrag" in ids
    # Built-ins are listed before user templates.
    origins = [t["origin"] for t in resp.json()]
    assert origins[0] == "BUILTIN"


def test_get_template_returns_blueprint() -> None:
    resp = client.get("/templates/tpl-urlaubsantrag")
    assert resp.status_code == 200
    body = resp.json()
    assert body["origin"] == "BUILTIN"
    assert "blueprint" in body and body["blueprint"]["nodes"]


def test_get_unknown_template_404() -> None:
    assert client.get("/templates/does-not-exist").status_code == 404


def test_save_instantiate_and_delete_user_template() -> None:
    sid = client.post("/schemas", json={"name": "Quelle"}).json()["id"]
    client.post(
        f"/schemas/{sid}/serial-insert",
        json={"label": "Schritt", "after_node_id": "start"},
    )

    # Save as a user template.
    resp = client.post(
        "/templates",
        json={"schema_id": sid, "name": "Team-Vorlage", "category": "Eigene"},
    )
    assert resp.status_code == 201, resp.text
    tid = resp.json()["id"]
    assert resp.json()["origin"] == "USER"

    # It appears in the listing.
    ids = {t["id"] for t in client.get("/templates").json()}
    assert tid in ids

    # Instantiate it into a new schema.
    resp = client.post(f"/templates/{tid}/instantiate", json={"name": "Aus Vorlage"})
    assert resp.status_code == 201, resp.text
    new_schema = resp.json()
    assert new_schema["name"] == "Aus Vorlage"
    assert new_schema["id"] != sid
    assert new_schema["lifecycle_state"] == "ENTWURF"

    # Delete the user template.
    assert client.delete(f"/templates/{tid}").status_code == 204
    assert tid not in {t["id"] for t in client.get("/templates").json()}


def test_save_template_from_unknown_schema_404() -> None:
    resp = client.post("/templates", json={"schema_id": "nope", "name": "X"})
    assert resp.status_code == 404


def test_builtin_template_cannot_be_deleted() -> None:
    resp = client.delete("/templates/tpl-urlaubsantrag")
    assert resp.status_code == 422
    # Still present afterwards.
    assert client.get("/templates/tpl-urlaubsantrag").status_code == 200


def test_instantiate_builtin_via_http() -> None:
    resp = client.post(
        "/templates/tpl-rechnungsfreigabe/instantiate", json={"name": "Q3-Rechnungen"}
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "Q3-Rechnungen"
    # Instantiated schema validates.
    vid = body["id"]
    assert client.get(f"/schemas/{vid}/validation").json()["correct"] is True
