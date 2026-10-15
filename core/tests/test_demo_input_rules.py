# SPDX-License-Identifier: BUSL-1.1
"""The example masks refuse values no clerk would enter.

The example data set is the showcase of the product, yet none of its masks
carried an input check: -3 leave days, a credit score of 150 or a tracking
"URL" that is none were all accepted. Help texts spoke modelling jargon
("Daten-Connector (C1-C9)", "auf jedem Pfad gesetzt -- deshalb ...").
These tests pin the bounds, that the seeded instances respect them, and the
clerk-facing wording.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from procworks import api, demo, demo_o2c
from procworks.audit import InMemoryAuditLog
from procworks.model import AccessMode, FormField, ProcessSchema, WidgetKind
from procworks.store import InMemoryInstanceStore, InMemoryOrgStore, InMemorySchemaStore
from procworks.validator import form_value_findings


def _load() -> tuple[list[ProcessSchema], InMemoryInstanceStore]:
    ss, ins, orgs, log = (InMemorySchemaStore(), InMemoryInstanceStore(),
                          InMemoryOrgStore(), InMemoryAuditLog())
    demo.load_demo(schema_store=ss, instance_store=ins, org_store=orgs, audit_log=log)
    demo_o2c.load_o2c(schema_store=ss, instance_store=ins, org_store=orgs, audit_log=log)
    schemas = [ss.get(sid) for sid in ss.list_ids()]
    return [s for s in schemas if s is not None], ins


def _writable(schema: ProcessSchema) -> Iterator[tuple[str, FormField]]:
    for node_id, form in schema.forms.items():
        for field in form.fields:
            if field.mode is not AccessMode.READ:
                yield node_id, field


def test_every_writable_number_field_has_a_lower_bound() -> None:
    """Class guard: no example number field accepts a negative value."""

    schemas, _ = _load()
    missing = [
        f"{s.name} / {s.nodes[n].label} / {f.label}"
        for s in schemas for n, f in _writable(s)
        if f.widget is WidgetKind.NUMBER and f.min_value is None
    ]
    assert not missing, missing


def test_bounded_values_and_formats_where_the_domain_has_them() -> None:
    schemas, _ = _load()
    by_elem = {(s.id, f.element_id): f for s in schemas for _, f in _writable(s)}
    tage = by_elem[(demo.SCHEMA_URLAUB, "tage")]
    assert (tage.min_value, tage.max_value) == (1, 30)
    score = by_elem[("o2c-bonitaet", "bonitaet_score")]
    assert (score.min_value, score.max_value) == (0, 100)
    assert by_elem[("o2c-auftragsabwicklung", "rabatt")].max_value == 100
    url = by_elem[("o2c-versand", "tracking_url")]
    assert url.pattern and re.fullmatch(url.pattern, "https://tracking.example/LS-1")
    assert not re.fullmatch(url.pattern, "kein url")
    # Das erwartete Format steht im Hilfetext -- auch neben dem Systemhinweis
    # des simulierten Schritts, der ihn frueher verdraengte.
    assert (url.help_text or "").startswith("Web-Adresse des Spediteurs")
    # Jeder Regel-Hinweis ist ein abgeschlossener Satz -- er steht vor einem
    # weiteren Hilfetext und liefe sonst in ihn hinein.
    assert all((r.help_text or ".").endswith(".")
               for r in (*demo.DEMO_INPUT_RULES.values(), *demo_o2c.O2C_INPUT_RULES.values()))
    assert "angeschlossenes System" in (url.help_text or "")
    # Was die Maske nicht vergleichen kann, sagt der Hilfetext.
    assert "gelieferte Menge" in (by_elem[("o2c-retoure", "retoure_menge")].help_text or "")


def test_seeded_instances_respect_the_rules_of_their_masks() -> None:
    """The showcase data itself must pass the checks it now teaches."""

    schemas, ins = _load()
    by_id = {(s.id, s.version): s for s in schemas}
    problems = []
    checked = 0
    for iid in ins.list_ids():
        inst = ins.get(iid)
        schema = by_id.get((inst.schema_id, inst.schema_version))
        if schema is None:
            continue
        values = {k: v.value if hasattr(v, "value") else v for k, v in inst.data_values.items()}
        for node_id in schema.forms:
            checked += sum(1 for f in schema.forms[node_id].fields if f.element_id in values)
            for f in form_value_findings(schema, node_id, values):
                problems.append(f"{schema.name}/{iid}: {f.code} {f.params}")
    assert not problems, problems
    assert checked > 100  # der Test sieht die Werte wirklich


def test_problem_finder_sees_a_violation() -> None:
    """Counter-check of the seed test: a value out of bounds is found."""

    schemas, _ = _load()
    [urlaub] = [s for s in schemas if s.id == demo.SCHEMA_URLAUB]
    erfassen = next(n for n, f in urlaub.forms.items() if f.title == "Urlaubsantrag erfassen")
    [finding] = form_value_findings(urlaub, erfassen, {"tage": -3})
    assert finding.code == "U4.below-min" and finding.params["min"] == "1"


_JARGON = re.compile(r"--|\b[A-Z]\d-[A-Z]\d\b|Connector|External Task|Pfad|Diskriminator")


def test_mask_texts_speak_the_clerks_language() -> None:
    schemas, _ = _load()
    bad = []
    for s in schemas:
        for form in s.forms.values():
            texts = [form.title, *(f.help_text or "" for f in form.fields),
                     *(f.label for f in form.fields)]
            bad += [f"{s.name}: {t}" for t in texts if t and _JARGON.search(t)]
    assert not bad, bad


@pytest.fixture
def demo_api() -> Iterator[TestClient]:
    def clear() -> None:
        api._store.clear()
        api._instances.clear()
        api._org_store.clear()
        api._audit.clear()
        api._absence_store.clear()

    clear()
    demo.load_demo(schema_store=api._store, instance_store=api._instances,
                   org_store=api._org_store, audit_log=api._audit)
    try:
        yield TestClient(api.app)
    finally:
        clear()


def test_negative_leave_days_are_rejected_on_completion(demo_api: TestClient) -> None:
    client = demo_api
    iid = client.post(f"/schemas/{demo.SCHEMA_URLAUB}/instances").json()["id"]
    schema = client.get(f"/schemas/{demo.SCHEMA_URLAUB}").json()
    node = next(n for n, f in schema["forms"].items() if f["title"] == "Urlaubsantrag erfassen")
    resp = client.post(f"/instances/{iid}/complete",
                       json={"node_id": node, "data": {"tage": -3, "grund": "x"}})
    assert resp.status_code == 422
    [finding] = resp.json()["detail"]["findings"]
    assert finding["code"] == "U4.below-min"
    assert finding["params"] == {"field": "Urlaubstage", "min": "1"}
    ok = client.post(f"/instances/{iid}/complete",
                     json={"node_id": node, "data": {"tage": 3, "grund": "x"}})
    assert ok.status_code == 200, ok.text
