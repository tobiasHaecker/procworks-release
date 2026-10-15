# SPDX-License-Identifier: BUSL-1.1
"""Die Beispielvorgänge tragen sprechende Namen statt interner Kennungen.

Kein Beispielmodell setzte benennende Werte (``display_fields``); Monitoring,
Arbeitslisten und Ausführung zeigten deshalb ``instance_13``, und zwei gleiche
Aufgaben verschiedener Vorgänge waren nicht zu unterscheiden. Die
Beispieldaten sind zugleich das fachliche Schaufenster.
"""

from __future__ import annotations

from datetime import UTC, datetime

from procworks import demo, demo_o2c
from procworks.assignment import open_tasks
from procworks.audit import InMemoryAuditLog
from procworks.model import AccessMode, ProcessSchema
from procworks.store import InMemoryInstanceStore, InMemoryOrgStore, InMemorySchemaStore


def _all_demo_schemas() -> list[ProcessSchema]:
    ss, ins, orgs, log = (InMemorySchemaStore(), InMemoryInstanceStore(),
                          InMemoryOrgStore(), InMemoryAuditLog())
    demo.load_demo(schema_store=ss, instance_store=ins, org_store=orgs, audit_log=log)
    demo_o2c.load_o2c(schema_store=ss, instance_store=ins, org_store=orgs, audit_log=log)
    schemas = [ss.get(sid) for sid in ss.list_ids()]
    return [s for s in schemas if s is not None]


def display_field_problems(schema: ProcessSchema) -> list[str]:
    """Gründe, warum ``schema`` seine Vorgänge nicht benennt (leer = in Ordnung)."""
    if not schema.display_fields:
        return [f"{schema.name}: keine benennenden Werte"]
    written = {a.element_id for a in schema.data_accesses
               if a.mode in (AccessMode.WRITE, AccessMode.READ_WRITE)}
    return [f"{schema.name}: „{schema.data_elements[e].name}“ schreibt kein Schritt"
            for e in schema.display_fields if e not in written]


def test_every_demo_schema_names_its_instances_with_written_values() -> None:
    schemas = _all_demo_schemas()
    assert len(schemas) == 8
    problems = [p for s in schemas for p in display_field_problems(s)]
    assert not problems, "; ".join(problems)


def test_problem_finder_names_missing_and_unwritten_fields() -> None:
    """Gegenprobe des Prüfers: fehlende und nie geschriebene Werte mit Namen."""
    [urlaub] = [s for s in _all_demo_schemas() if s.id == demo.SCHEMA_URLAUB]
    bare = urlaub.model_copy(update={"display_fields": []})
    assert display_field_problems(bare) == ["Urlaubsantrag: keine benennenden Werte"]
    unwritten = urlaub.model_copy(deep=True)
    unwritten.data_accesses = [a for a in unwritten.data_accesses if a.element_id != "tage"]
    assert display_field_problems(unwritten) == [
        "Urlaubsantrag: „Urlaubstage“ schreibt kein Schritt"
    ]


def test_open_tasks_carry_the_instance_start() -> None:
    """Zwei gleiche erste Schritte lassen sich am Start des Vorgangs unterscheiden."""
    ss, ins, orgs, log = (InMemorySchemaStore(), InMemoryInstanceStore(),
                          InMemoryOrgStore(), InMemoryAuditLog())
    demo.load_demo(schema_store=ss, instance_store=ins, org_store=orgs, audit_log=log)
    schema = ss.get(demo.SCHEMA_URLAUB)
    assert schema is not None
    started = datetime(2026, 10, 1, 14, 3, tzinfo=UTC)
    instance = next(i for i in (ins.get(x) for x in ins.list_ids())
                    if i is not None and i.schema_id == demo.SCHEMA_URLAUB
                    and i.state.value == "RUNNING")
    instance = instance.model_copy(update={"started_at": started})

    tasks = open_tasks(schema, instance)

    assert tasks and all(t.instance_started_at == started for t in tasks)
    unstamped = instance.model_copy(update={"started_at": None})
    assert all(t.instance_started_at is None for t in open_tasks(schema, unstamped))


def test_every_seeded_instance_has_a_start_time() -> None:
    """Ohne Startzeit hiesse ein Beispielvorgang ohne Werte nur „Vorgang“ --
    auch die Kind-Vorgaenge der Teilprozesse, die der Seed nicht selbst startet."""
    ss, ins, orgs, log = (InMemorySchemaStore(), InMemoryInstanceStore(),
                          InMemoryOrgStore(), InMemoryAuditLog())
    demo.load_demo(schema_store=ss, instance_store=ins, org_store=orgs, audit_log=log)
    demo_o2c.load_o2c(schema_store=ss, instance_store=ins, org_store=orgs, audit_log=log)
    instances = [ins.get(i) for i in ins.list_ids()]
    missing = [i.id for i in instances if i is not None and i.started_at is None]
    assert missing == []


def test_display_values_carry_the_data_type() -> None:
    """Der Client formatiert nach Typ: eine Kundennummer anders als einen Betrag."""
    from procworks.assignment import display_values

    [bonitaet] = [s for s in _all_demo_schemas()
                  if s.display_fields == ["kunden_nr", "auftragswert"]]
    ss, ins, orgs, log = (InMemorySchemaStore(), InMemoryInstanceStore(),
                          InMemoryOrgStore(), InMemoryAuditLog())
    demo_o2c.load_o2c(schema_store=ss, instance_store=ins, org_store=orgs, audit_log=log)
    inst = next(i for i in (ins.get(x) for x in ins.list_ids())
                if i is not None and i.schema_id == bonitaet.id)
    types = {v.element_id: v.data_type for v in display_values(bonitaet, inst)}
    assert types == {"kunden_nr": "INTEGER", "auftragswert": "FLOAT"}
