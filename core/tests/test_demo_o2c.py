# SPDX-License-Identifier: BUSL-1.1
"""Waechter fuer den Order-to-Cash-Datensatz (:mod:`procworks.demo_o2c`).

Der Datensatz ist fachliches Schaufenster und Testflaeche zugleich, deshalb
pruefen diese Tests zweierlei:

* **Spielbarkeit** -- der Hauptprozess laeuft ohne Connector, ohne Worker und
  ohne Datenbank von START bis END durch, ebenso der Abbruchpfad und die
  Mahn-Eskalation. Ein Datensatz, den man nicht durchklicken kann, verfehlt
  seinen Zweck.
* **Fachliche Modellierung** -- die Kundenentscheidung haengt an der
  Entscheidung, nicht am Auftragswert; der einzige Schwellwert-Split routet nur
  die Zustaendigkeit. Diese Aussagen sind leicht wieder wegzurefaktorieren, ohne
  dass eine Regel des Kerns es merkt.

Ergaenzt um die Startlage (welche Instanz wartet wo), die Ausstattung
(Postfaecher, Vertretung, simulierte Systemschritte) und die API-Anbindung.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

import procworks.api as api_module
from procworks import assignment, demo, demo_o2c
from procworks import execution as exe
from procworks import operations as ops
from procworks.api import app
from procworks.audit import InMemoryAuditLog
from procworks.model import (
    InstanceState,
    LifecycleState,
    NodeType,
    ProcessSchema,
    StaffRuleKind,
    TimeConstraint,
    XorDecisionKind,
)
from procworks.store import (
    InMemoryAbsenceStore,
    InMemoryInstanceStore,
    InMemoryOrgStore,
    InMemorySchemaStore,
    hydrate_org,
    make_resolver,
)
from procworks.validator import CorrectnessError, validate

client = TestClient(app)

ALL_SCHEMAS = {
    demo_o2c.SCHEMA_MAIN,
    demo_o2c.SCHEMA_BONITAET,
    demo_o2c.SCHEMA_VERSAND,
    demo_o2c.SCHEMA_FAKTURA,
    demo_o2c.SCHEMA_FORDERUNG,
    demo_o2c.SCHEMA_RETOURE,
}


class _World:
    """Ein frisch geladener Datensatz samt hydrierten Schemata und Fahrer."""

    def __init__(self) -> None:
        self.schemas = InMemorySchemaStore()
        self.instances = InMemoryInstanceStore()
        self.orgs = InMemoryOrgStore()
        self.absences = InMemoryAbsenceStore()
        self.audit = InMemoryAuditLog()
        demo_o2c.load_o2c(
            schema_store=self.schemas,
            instance_store=self.instances,
            org_store=self.orgs,
            audit_log=self.audit,
            absence_store=self.absences,
        )
        org_resolver = self.orgs.get
        self.hydrated: dict[str, ProcessSchema] = {
            sid: hydrate_org(self.schemas.get(sid), org_resolver)  # type: ignore[arg-type]
            for sid in self.schemas.list_ids()
        }
        self.context = exe.ExecutionContext(
            lambda sid, version: (
                None
                if (s := self.hydrated.get(sid)) is None
                or (version is not None and s.version != version)
                else s
            ),
            self.instances,
        )
        self.seeder = demo_o2c._Seeder(self.hydrated, self.context, self.audit)

    def schema(self, schema_id: str) -> ProcessSchema:
        return self.hydrated[schema_id]

    def main(self) -> ProcessSchema:
        return self.hydrated[demo_o2c.SCHEMA_MAIN]

    def instance(self, instance_id: str):  # type: ignore[no-untyped-def]
        found = self.instances.get(instance_id)
        assert found is not None
        return found

    def open_labels(self, instance_id: str) -> set[str]:
        """Bezeichnungen der gerade offenen Aufgaben einer Instanz."""

        instance = self.instance(instance_id)
        schema = self.schema(instance.schema_id)
        return {t.label or t.node_id for t in assignment.open_tasks(schema, instance)}


@pytest.fixture
def world() -> _World:
    return _World()


# --- Aufbau ---------------------------------------------------------------


def test_load_o2c_builds_the_process_family(world: _World) -> None:
    """Sechs Schemata, ein Organisationsmodell, alle freigegeben."""

    assert set(world.schemas.list_ids()) == ALL_SCHEMAS
    assert world.orgs.list_ids() == [demo_o2c.ORG_ID]
    for schema_id in ALL_SCHEMAS:
        schema = world.schema(schema_id)
        assert schema.lifecycle_state is LifecycleState.RELEASED, schema_id

    # Die drei Teilprozesse stehen zusaetzlich im Bibliothekskatalog.
    for schema_id in (
        demo_o2c.SCHEMA_BONITAET,
        demo_o2c.SCHEMA_VERSAND,
        demo_o2c.SCHEMA_FAKTURA,
    ):
        assert world.schema(schema_id).is_library_subprocess is True, schema_id


def test_every_schema_stays_valid_with_the_resolver(world: _World) -> None:
    """Auch die uebergreifenden Regeln (H1-H4, F1-F4) halten."""

    resolver = make_resolver(world.schemas)
    for schema_id in ALL_SCHEMAS:
        assert validate(world.schema(schema_id), resolver) == [], schema_id


def test_main_process_composes_three_sub_processes(world: _World) -> None:
    main = world.main()
    bound = {b.target_schema_id for b in main.sub_process_bindings.values()}
    assert bound == {
        demo_o2c.SCHEMA_BONITAET,
        demo_o2c.SCHEMA_VERSAND,
        demo_o2c.SCHEMA_FAKTURA,
    }
    # Jeder gebundene Ausgang muss im Kind auf jedem Pfad entstehen (H2) -- das
    # prueft der Validator; hier sichern wir, dass ueberhaupt Ausgaenge fliessen.
    for binding in main.sub_process_bindings.values():
        assert binding.output_mapping, binding.target_schema_id


def test_no_external_data_and_no_automation(world: _World) -> None:
    """Der Datensatz kommt ohne Connector und ohne External-Task-Worker aus.

    Ein EXTERNAL-Datenelement braeuchte einen konfigurierten Connector und
    bliebe im Demo leer; ein automatischer Schritt braeuchte einen laufenden
    Worker und wuerde den Prozess stehen lassen. Beides ist hier bewusst
    vermieden -- genau deshalb ist der Datensatz ueberall durchklickbar.
    """

    for schema_id in ALL_SCHEMAS:
        schema = world.schema(schema_id)
        assert schema.connectors == {}, schema_id
        assert schema.service_bindings == {}, schema_id
        for element in schema.data_elements.values():
            assert element.source.value == "INSTANCE", (schema_id, element.id)


def test_system_steps_are_ordinary_interactive_activities(world: _World) -> None:
    """Jeder simulierte Systemschritt ist eine normale Aufgabe mit Maske und BZR."""

    found = 0
    for schema_id in ALL_SCHEMAS:
        schema = world.schema(schema_id)
        for node in schema.nodes.values():
            if not node.label.startswith(demo_o2c.SYSTEM_PREFIX):
                continue
            found += 1
            assert node.type is NodeType.ACTIVITY, node.label
            assert node.id in schema.forms, node.label
            assert node.id in schema.staff_rules, node.label
    assert found >= 5, "Die simulierten Systemschritte fehlen"


# --- fachliche Modellierung ----------------------------------------------


def test_offer_branches_on_the_decision_not_on_the_order_value(world: _World) -> None:
    """Die Kundenentscheidung verzweigt am Ergebnis, nicht an einer Kennzahl.

    Ein Angebot wird nicht abgelehnt, *weil* es gross ist -- es wird abgelehnt,
    weil der Kunde so entscheidet. Der Schritt davor erfasst die Entscheidung
    und schreibt den Diskriminator; die Zweige fuehren sie nur noch aus.
    (Dieselbe Regel wie beim Urlaubsantrag des Basis-Demos.)
    """

    main = world.main()
    entscheidung = next(
        d
        for d in main.xor_decisions.values()
        if {b.target for b in d.branches}
        & {
            n.id
            for n in main.nodes.values()
            if n.label in {"Angebot nachverhandeln", "Absage dokumentieren"}
        }
    )
    assert entscheidung.discriminator == "angebot_status"
    assert entscheidung.kind is XorDecisionKind.ENUM

    # Der Diskriminator wird vom Schritt unmittelbar davor geschrieben.
    rueckmeldung = next(
        n.id for n in main.nodes.values() if n.label == "Kundenrückmeldung erfassen"
    )
    assert any(
        a.node_id == rueckmeldung and a.element_id == "angebot_status" and a.mandatory
        for a in main.data_accesses
        if a.mode.value in {"WRITE", "READ_WRITE"}
    )


def test_the_only_threshold_split_routes_responsibility(world: _World) -> None:
    """Der Wertgrenzen-Split entscheidet, *wer* freigibt -- nicht *ob*.

    Alle drei Zweige tun dasselbe (Freigabevermerk schreiben) und unterscheiden
    sich nur in der Bearbeiterzuordnung. Waere einer davon eine automatische
    Ablehnung, waere es genau der Fehler, den der Urlaubsantrag frueher hatte.
    """

    main = world.main()
    thresholds = [
        d for d in main.xor_decisions.values() if d.kind is XorDecisionKind.THRESHOLD
    ]
    assert len(thresholds) == 1, "Es soll genau einen Schwellwert-Split geben"
    decision = thresholds[0]
    assert decision.discriminator == "auftragswert"

    writes = {
        (a.node_id, a.element_id)
        for a in main.data_accesses
        if a.mode.value in {"WRITE", "READ_WRITE"}
    }
    rules = set()
    for branch in decision.branches:
        assert (branch.target, "freigabe_vermerk") in writes, main.nodes[branch.target].label
        rules.add(main.staff_rules[branch.target].kind)
    # Die Zweige unterscheiden sich in der Zuordnung -- unter anderem ueber die
    # Vorgesetzten-Regel (Z1-Z3), die relativ zum Erfasser aufloest.
    assert StaffRuleKind.NODE_PERFORMING_AGENT_SUPERVISOR in rules


def test_dunning_escalates_without_a_cycle(world: _World) -> None:
    """Das Mahnwesen ist eine Kaskade geschachtelter XOR-Bloecke, kein Zyklus."""

    forderung = world.schema(demo_o2c.SCHEMA_FORDERUNG)
    booleans = [
        d for d in forderung.xor_decisions.values() if d.kind is XorDecisionKind.BOOLEAN
    ]
    assert len(booleans) == 3, "Drei Mahnstufen erwartet"
    assert {d.discriminator for d in booleans} == {"zahlung_1", "zahlung_2", "zahlung_3"}

    # Zyklenfrei: die topologische Sortierung erfasst jeden Knoten.
    order: list[str] = []
    indeg = {nid: len(forderung.incoming(nid)) for nid in forderung.nodes}
    queue = [nid for nid, deg in indeg.items() if deg == 0]
    while queue:
        current = queue.pop()
        order.append(current)
        for edge in forderung.outgoing(current):
            indeg[edge.target] -= 1
            if indeg[edge.target] == 0:
                queue.append(edge.target)
    assert len(order) == len(forderung.nodes), "Der Mahnprozess enthaelt einen Zyklus"


def test_every_agent_is_addressable_and_notifications_exist(world: _World) -> None:
    """N3 in der Praxis: jede Person hat ein Postfach, jede Gruppe auch."""

    org = world.orgs.get(demo_o2c.ORG_ID)
    assert org is not None
    for agent in org.agents.values():
        assert agent.email, agent.id

    notifications = sum(len(world.schema(sid).mail_bindings) for sid in ALL_SCHEMAS)
    assert notifications >= 6, "Der Datensatz soll Benachrichtigungen vorfuehren"

    # Beide Empfaengerarten kommen vor.
    modes = {
        binding.mode
        for sid in ALL_SCHEMAS
        for binding in world.schema(sid).mail_bindings.values()
    }
    assert len(modes) == 2, "Persoenliche *und* Gruppenpostfach-Benachrichtigung erwartet"


def test_critical_path_fits_the_deadline_and_a_longer_step_is_rejected(
    world: _World,
) -> None:
    """T2 ist erfuellt -- aber knapp genug, dass eine Verlaengerung auffliegt."""

    main = world.main()
    assert main.deadline_seconds == 60 * 86400

    draft = ops.new_revision(main)
    anfrage = next(
        n.id for n in draft.nodes.values() if n.label == "Kundenanfrage erfassen"
    )
    with pytest.raises(CorrectnessError) as excinfo:
        ops.set_time_constraint(
            draft, anfrage, TimeConstraint(max_duration_seconds=10 * 86400)
        )
    assert any(f.rule == "T2" for f in excinfo.value.findings)


# --- Spielbarkeit ---------------------------------------------------------


def _play_until_offer(world: _World, instance_id: str, *, wert: float, score: int = 84) -> None:
    """Vom Start bis zur erfassten Kundenrueckmeldung (ohne die Entscheidung)."""

    world.seeder.start(demo_o2c.SCHEMA_MAIN, instance_id)
    world.seeder.do(
        instance_id,
        "Kundenanfrage erfassen",
        "a-nadja",
        demo_o2c._anfrage_daten("Testkunde GmbH", 10999, "Testartikel", 10, 1),
    )
    world.seeder.do(instance_id, "Verfügbarkeit vorab klären", "a-lars", {"lager_bestand": 99})
    world.seeder.do(
        instance_id,
        "Preis und Konditionen kalkulieren",
        "a-nadja",
        demo_o2c._kalkulation(wert / 10, 10),
    )
    demo_o2c._run_bonitaet(world.seeder, instance_id, score=score, limit=wert * 2)
    world.seeder.do(
        instance_id, "Angebot erstellen und versenden", "a-nadja", {"angebots_nr": "AN-TEST"}
    )


def test_main_process_runs_end_to_end_without_worker_or_connector(world: _World) -> None:
    """Der Happy Path laeuft komplett durch -- inklusive aller drei Teilprozesse."""

    iid = "test-happy"
    _play_until_offer(world, iid, wert=3000.0)
    world.seeder.do(iid, "Kundenrückmeldung erfassen", "a-nadja", {"angebot_status": "Angenommen"})
    world.seeder.do(
        iid,
        "Auftrag anlegen und bestätigen",
        "a-nadja",
        {"auftrags_nr": "AB-TEST", "verfuegbarkeit": "Ab Lager"},
    )
    world.seeder.do(
        iid, "Freigabe durch Innendienst", "a-nadja", {"freigabe_vermerk": "ok"}
    )
    world.seeder.do(iid, "Ware reservieren", "a-lars", {"liefertermin": "2026-09-01"})
    world.seeder.do(iid, "Kommissionieren", "a-lars", {"kommission_ok": True})
    world.seeder.do(iid, "Versandpapiere erstellen", "a-lars", {"versandpapiere_ok": True})
    demo_o2c._run_versand(world.seeder, iid, "LS-TEST", "2026-09-02")
    demo_o2c._run_faktura(world.seeder, iid, "RE-TEST", "2026-10-02")
    world.seeder.do(
        iid,
        demo_o2c.SYSTEM_PREFIX + "Zahlungseingang prüfen",
        "a-bianca",
        {"zahlungseingang": True},
    )
    world.seeder.do(
        iid,
        "Zahlung verbuchen",
        "a-bianca",
        {"zahlbetrag": 3000.0, "offener_betrag": 0.0, "vorgangsstatus": "Abgeschlossen - bezahlt"},
    )
    final = world.seeder.do(
        iid,
        "Vorgang abschließen und archivieren",
        "a-nadja",
        {"forderung_offen": False, "reklamation": False},
    )

    assert final.state is InstanceState.COMPLETED
    assert final.follow_up_instances == [], "Ohne offene Posten faellt kein Folgeprozess an"
    # Die Ergebnisse der drei Teilprozesse sind im Elternvorgang angekommen.
    for element_id in ("kreditfreigabe", "lieferschein_nr", "rechnungs_nr"):
        assert element_id in final.data_values, element_id


def test_rejected_offer_ends_the_order_early(world: _World) -> None:
    """Der Abbruchpfad endet ohne Lieferung und ohne Rechnung."""

    iid = "test-absage"
    _play_until_offer(world, iid, wert=2000.0)
    world.seeder.do(iid, "Kundenrückmeldung erfassen", "a-nadja", {"angebot_status": "Abgelehnt"})
    world.seeder.do(
        iid, "Absage dokumentieren", "a-nadja", {"vorgangsstatus": "Angebot abgelehnt"}
    )
    final = world.seeder.do(
        iid,
        "Vorgang abschließen und archivieren",
        "a-nadja",
        {"forderung_offen": False, "reklamation": False},
    )

    assert final.state is InstanceState.COMPLETED
    assert "rechnungs_nr" not in final.data_values
    assert final.follow_up_instances == []


def _play_to_open_receivable(world: _World, iid: str, *, reklamation: bool) -> object:
    """Vollstaendiger Vorgang, der unbezahlt bleibt (offene Forderung)."""

    _play_until_offer(world, iid, wert=4000.0)
    world.seeder.do(iid, "Kundenrückmeldung erfassen", "a-nadja", {"angebot_status": "Angenommen"})
    world.seeder.do(
        iid,
        "Auftrag anlegen und bestätigen",
        "a-nadja",
        {"auftrags_nr": "AB-OFFEN", "verfuegbarkeit": "Ab Lager"},
    )
    world.seeder.do(iid, "Freigabe durch Innendienst", "a-nadja", {"freigabe_vermerk": "ok"})
    world.seeder.do(iid, "Ware reservieren", "a-lars", {"liefertermin": "2026-09-01"})
    world.seeder.do(iid, "Kommissionieren", "a-lars", {"kommission_ok": True})
    world.seeder.do(iid, "Versandpapiere erstellen", "a-lars", {"versandpapiere_ok": True})
    demo_o2c._run_versand(world.seeder, iid, "LS-OFFEN", "2026-09-02")
    demo_o2c._run_faktura(world.seeder, iid, "RE-OFFEN", "2026-10-02")
    world.seeder.do(
        iid,
        demo_o2c.SYSTEM_PREFIX + "Zahlungseingang prüfen",
        "a-bianca",
        {"zahlungseingang": False},
    )
    world.seeder.do(
        iid,
        "Offene Forderung dokumentieren",
        "a-bianca",
        {"zahlbetrag": 0.0, "offener_betrag": 4000.0, "vorgangsstatus": "Offene Forderung"},
    )
    return world.seeder.do(
        iid,
        "Vorgang abschließen und archivieren",
        "a-nadja",
        {"forderung_offen": True, "reklamation": reklamation},
    )


def test_open_receivable_starts_the_dunning_follow_up(world: _World) -> None:
    """F1-F4: die offene Forderung loest genau einen Folgeprozess aus."""

    final = _play_to_open_receivable(world, "test-offen", reklamation=False)
    assert final.state is InstanceState.COMPLETED  # type: ignore[attr-defined]
    follow_ups = final.follow_up_instances  # type: ignore[attr-defined]
    assert len(follow_ups) == 1

    child = world.instance(follow_ups[0])
    assert child.schema_id == demo_o2c.SCHEMA_FORDERUNG
    assert child.state is InstanceState.RUNNING
    # Die uebergebenen Werte sind angekommen (handover_mapping).
    assert child.data_values["offener_betrag"] == 4000.0
    assert child.data_values["rechnungs_nr"] == "RE-OFFEN"


def test_reported_complaint_starts_the_returns_follow_up(world: _World) -> None:
    """Beide Bedingungen greifen unabhaengig voneinander."""

    final = _play_to_open_receivable(world, "test-reklamation", reklamation=True)
    follow_ups = final.follow_up_instances  # type: ignore[attr-defined]
    schema_ids = {world.instance(fid).schema_id for fid in follow_ups}
    assert schema_ids == {demo_o2c.SCHEMA_FORDERUNG, demo_o2c.SCHEMA_RETOURE}


def test_dunning_runs_through_to_collection(world: _World) -> None:
    """Dreimal "nicht bezahlt" endet beim Inkasso -- und der Vorgang schliesst."""

    final = _play_to_open_receivable(world, "test-inkasso", reklamation=False)
    forderung_id = final.follow_up_instances[0]  # type: ignore[attr-defined]

    world.seeder.do(
        forderung_id,
        "Forderungsakte anlegen",
        "a-bianca",
        {
            "kunde": "Testkunde GmbH",
            "rechnungs_nr": "RE-OFFEN",
            "offener_betrag": 4000.0,
            "faellig_am": "2026-10-02",
        },
    )
    world.seeder.do(
        forderung_id, "Zahlungserinnerung versenden", "a-bianca", {"erinnerung_am": "2026-10-10"}
    )
    world.seeder.do(
        forderung_id,
        demo_o2c.SYSTEM_PREFIX + "Zahlungseingang prüfen (Stufe 1)",
        "a-automat",
        {"zahlung_1": False},
    )
    world.seeder.do(
        forderung_id, "1. Mahnung versenden", "a-bianca", {"mahnung_1_am": "2026-10-20"}
    )
    world.seeder.do(
        forderung_id,
        demo_o2c.SYSTEM_PREFIX + "Zahlungseingang prüfen (Stufe 2)",
        "a-automat",
        {"zahlung_2": False},
    )
    world.seeder.do(
        forderung_id, "2. Mahnung mit Fristsetzung", "a-bianca", {"mahnung_2_am": "2026-11-01"}
    )
    world.seeder.do(
        forderung_id,
        demo_o2c.SYSTEM_PREFIX + "Zahlungseingang prüfen (Stufe 3)",
        "a-automat",
        {"zahlung_3": False},
    )
    assert "Inkasso beauftragen" in world.open_labels(forderung_id)

    world.seeder.do(
        forderung_id, "Inkasso beauftragen", "a-gustav", {"ausgang": "An Inkasso übergeben"}
    )
    last = world.seeder.do(
        forderung_id,
        "Forderungsvorgang abschließen",
        "a-bianca",
        {"abschluss_vermerk": "uebergeben"},
    )
    assert last.state is InstanceState.COMPLETED


def test_paying_after_the_first_reminder_skips_the_later_stages(world: _World) -> None:
    """Die Kaskade steigt nur so tief, wie sie muss."""

    final = _play_to_open_receivable(world, "test-frueh-bezahlt", reklamation=False)
    forderung_id = final.follow_up_instances[0]  # type: ignore[attr-defined]
    world.seeder.do(
        forderung_id,
        "Forderungsakte anlegen",
        "a-bianca",
        {
            "kunde": "Testkunde GmbH",
            "rechnungs_nr": "RE-OFFEN",
            "offener_betrag": 4000.0,
            "faellig_am": "2026-10-02",
        },
    )
    world.seeder.do(
        forderung_id, "Zahlungserinnerung versenden", "a-bianca", {"erinnerung_am": "2026-10-10"}
    )
    world.seeder.do(
        forderung_id,
        demo_o2c.SYSTEM_PREFIX + "Zahlungseingang prüfen (Stufe 1)",
        "a-automat",
        {"zahlung_1": True},
    )
    assert world.open_labels(forderung_id) == {"Zahlung verbuchen (Stufe 1)"}


# --- Startlage ------------------------------------------------------------


def test_seeded_orders_cover_the_interesting_states(world: _World) -> None:
    """Neun Vorgaenge, jeder an einer anderen vorfuehrbaren Stelle."""

    main_ids = sorted(
        iid
        for iid in world.instances.list_ids()
        if world.instance(iid).schema_id == demo_o2c.SCHEMA_MAIN
    )
    assert len(main_ids) == 9

    assert world.open_labels("o2c-2026-001") == {"Kundenanfrage erfassen"}
    # Offener AND-Block: eine Seite erledigt, die andere wartet.
    assert world.open_labels("o2c-2026-002") == {"Preis und Konditionen kalkulieren"}
    # Der Teilprozess laeuft -- die offene Aufgabe haengt an der Kind-Instanz.
    assert world.open_labels("o2c-2026-003") == set()
    child = world.seeder.child("o2c-2026-003", "Bonitäts- und Kreditprüfung")
    assert world.instance(child).schema_id == demo_o2c.SCHEMA_BONITAET
    assert world.open_labels(child) == {"Kundenstammdaten prüfen"}

    assert world.open_labels("o2c-2026-004") == {"Freigabe durch Geschäftsführung"}
    assert world.open_labels("o2c-2026-005") == {"Kommissionieren", "Versandpapiere erstellen"}
    assert world.instance("o2c-2026-006").state is InstanceState.COMPLETED
    assert world.instance("o2c-2026-007").state is InstanceState.COMPLETED
    assert world.open_labels("o2c-2026-008") == {"Angebot nachverhandeln"}
    assert world.instance("o2c-2026-009").state is InstanceState.COMPLETED


def test_the_unpaid_order_left_a_running_dunning_case(world: _World) -> None:
    """Vorgang 007 hat den Folgeprozess bereits gestartet."""

    unbezahlt = world.instance("o2c-2026-007")
    assert len(unbezahlt.follow_up_instances) == 1
    forderung = world.instance(unbezahlt.follow_up_instances[0])
    assert forderung.schema_id == demo_o2c.SCHEMA_FORDERUNG
    assert forderung.state is InstanceState.RUNNING
    assert world.open_labels(forderung.id) == {"Forderungsakte anlegen"}


def test_seeded_absence_adds_the_deputy_across_the_role_boundary(world: _World) -> None:
    """Die Vertretung ist sichtbar, weil sie die Rolle gerade *nicht* traegt."""

    absent = assignment.absent_agent_ids(world.absences.list_entries(), datetime.now(UTC))
    assert absent == frozenset({"a-karin"})

    child = world.seeder.child("o2c-2026-003", "Bonitäts- und Kreditprüfung")
    instance = world.instance(child)
    schema = world.schema(instance.schema_id)
    node_id = next(n.id for n in schema.nodes.values() if n.label == "Kundenstammdaten prüfen")

    ohne = assignment.eligible_agents(schema, node_id, instance)
    mit = assignment.eligible_agents(schema, node_id, instance, absent_agents=absent)
    assert "a-bianca" not in ohne, "Ohne Abwesenheit ist die Vertretung nicht zustaendig"
    assert "a-bianca" in mit, "Die Vertretung kommt waehrend der Abwesenheit hinzu"
    assert "a-karin" in mit, "Die abwesende Person verschwindet nie aus der Liste"


def test_every_operational_role_has_an_open_task(world: _World) -> None:
    """Die Startlage fuellt die Arbeitslisten -- sonst waere sie kein Schaufenster."""

    open_for: dict[str, int] = {}
    for iid in world.instances.list_ids():
        instance = world.instance(iid)
        schema = world.schema(instance.schema_id)
        for task in assignment.open_tasks(schema, instance):
            for agent_id in task.eligible_agents:
                open_for[agent_id] = open_for.get(agent_id, 0) + 1

    for agent_id in ("a-nadja", "a-lars", "a-karin", "a-bianca", "a-gustav", "a-viktor"):
        assert open_for.get(agent_id, 0) >= 1, agent_id
    # Die Springerin sieht alles auf einmal -- der Ein-Login-Durchlauf.
    assert open_for.get("a-sina", 0) >= 5


# --- API-Anbindung --------------------------------------------------------


@pytest.fixture
def clean_api() -> Iterator[None]:
    """Kapselt die Modul-Singletons, damit ein Reset keine anderen Tests trifft."""

    saved = (
        api_module._store,
        api_module._instances,
        api_module._org_store,
        api_module._audit,
        api_module._absence_store,
        api_module._resolver,
        api_module._context,
    )
    api_module._store = InMemorySchemaStore()
    api_module._instances = InMemoryInstanceStore()
    api_module._org_store = InMemoryOrgStore()
    api_module._audit = InMemoryAuditLog()
    api_module._absence_store = InMemoryAbsenceStore()
    api_module._resolver = make_resolver(api_module._store)
    api_module._context = exe.ExecutionContext(api_module._resolver, api_module._instances)
    try:
        yield
    finally:
        (
            api_module._store,
            api_module._instances,
            api_module._org_store,
            api_module._audit,
            api_module._absence_store,
            api_module._resolver,
            api_module._context,
        ) = saved


def test_admin_reset_loads_the_o2c_data_set(clean_api: None) -> None:
    response = client.post("/admin/reset", json={"load_o2c": True})
    assert response.status_code == 200
    body = response.json()
    assert body["o2c_loaded"] is True
    assert body["demo_loaded"] is False
    assert body["schemas"] == 6
    assert set(client.get("/schemas").json()) == ALL_SCHEMAS


def test_admin_reset_without_the_flag_loads_nothing(clean_api: None) -> None:
    response = client.post("/admin/reset", json={})
    assert response.status_code == 200
    assert response.json()["o2c_loaded"] is False
    assert client.get("/schemas").json() == []


def test_both_data_sets_can_be_loaded_side_by_side(clean_api: None) -> None:
    """Die beiden Datensaetze stoeren einander nicht."""

    response = client.post("/admin/reset", json={"load_demo": True, "load_o2c": True})
    assert response.status_code == 200
    body = response.json()
    assert body["demo_loaded"] is True and body["o2c_loaded"] is True
    assert set(client.get("/schemas").json()) == ALL_SCHEMAS | {
        demo.SCHEMA_URLAUB,
        demo.SCHEMA_BESCHAFFUNG,
    }
    orgs = {entry["id"] for entry in client.get("/org-models").json()}
    assert orgs == {demo.ORG_ID, demo_o2c.ORG_ID}
