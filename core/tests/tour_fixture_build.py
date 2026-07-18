# SPDX-License-Identifier: BUSL-1.1
"""Aufbau des Tutorial-Beispielprozesses in seinen vier Stufen.

Der Web-Client fuehrt seine geführte Tour (``docs/Tutorial-Konzept.md``) in
einem **schreibfreien Sandkasten**: Waehrend der Tour verlaesst kein
schreibender Aufruf den Browser, stattdessen spielt der Client vorbereitete
Schnappschuesse ab (``web/tour/fixtures.js``). Damit entstehen durch
Tutorial-Eingaben **keine dauerhaften Daten**.

Dieses Modul ist die **einzige Quelle** dieser Schnappschuesse. Es baut den
Beispielprozess ausschliesslich ueber die oeffentlichen Change-Operationen
(``operations.py``) auf -- genau wie ``demo.py`` und die eingebauten Vorlagen --
und ist damit *correct by construction*: Jede Stufe ist ein Modell, das der Kern
in dieser Situation wirklich liefern wuerde.

Verwendet von:

* ``test_tour_web.py`` -- vergleicht die abgelegten Schnappschuesse gegen die
  hier frisch gebauten (Drift-Waechter), und
* ``python -m tests.tour_fixture_build`` -- schreibt ``web/tour/fixtures.js``
  neu, wenn der Beispielprozess absichtlich geaendert wurde.

Die Stufen erzaehlen den Bogen der Modellierer-Tour:

===== ================================================= ======================
Stufe Zustand                                           Ergebnis
===== ================================================= ======================
0     zwei fertig verdrahtete Schritte                  korrekt
1     nach dem Einfuegen von "Antragsteller informieren" korrekt
--    Versuch, dort "Urlaubstage" **lesend** zu binden  **abgelehnt (D1)**
2     nach dem Binden der Rolle                         korrekt
===== ================================================= ======================

Der **abgelehnte** Schritt dazwischen ist die Pointe der Tour und wird deshalb
mit erzeugt (:func:`build_rejection`): Der neue Schritt liegt **vor** dem
Schreiber der Urlaubstage, also koennte er sie auf einem Ausfuehrungspfad lesen,
bevor sie existieren. Der Kern weist das zurueck -- das Modell bleibt gueltig,
statt kaputtzugehen. Genau das ist Correctness by Construction, und es laesst
sich nicht erzaehlen, nur vorfuehren.

Weil jede Change-Operation vor dem Commit validiert, ist ein erreichbarer
Entwurf praktisch **immer** korrekt; die Befunde-Liste zeigt daher im Normalfall
dauerhaft "✓". Die Tour sagt das so und macht den Ablehnungsfall zum Beleg --
statt einen Befund zu inszenieren, den es in echt nicht gaebe.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from procworks import operations as ops
from procworks import validate
from procworks.model import (
    AccessMode,
    DataType,
    ProcessSchema,
    StaffRule,
    StaffRuleKind,
)
from procworks.validator import CorrectnessError

#: Ids des Tutorial-Kosmos. Bewusst mit ``tour-`` praefixiert, damit ein
#: Schnappschuss nie mit echten Stammdaten eines Kunden verwechselt werden kann.
SCHEMA_ID = "tour-schema"
ORG_ID = "tour-org"

#: Beschriftungen der Knoten, auf die sich die Tour-Schritte beziehen.
LABEL_ERFASSEN = "Antrag erfassen"
LABEL_PRUEFEN = "Antrag prüfen"
LABEL_INFORMIEREN = "Antragsteller informieren"

#: Zielpfad der abgelegten Schnappschuesse (relativ zum Repo-Stamm).
FIXTURE_PATH = Path(__file__).resolve().parents[2] / "web" / "tour" / "fixtures.js"


def _nid(schema: ProcessSchema, label: str) -> str:
    """Liefert die Id des (eindeutigen) Knotens mit der Beschriftung ``label``."""

    return next(n.id for n in schema.nodes.values() if n.label == label)


def _role(ref: str) -> StaffRule:
    """Kurzform fuer eine rollenbasierte Bearbeiterzuordnungsregel (Z1)."""

    return StaffRule(kind=StaffRuleKind.ROLE, ref=ref)


def build_stages() -> list[ProcessSchema]:
    """Baut die vier Stufen des Tutorial-Prozesses in ihrer Reihenfolge.

    :return: Liste ``[stufe0, stufe1, stufe2]``. Jede Stufe entsteht aus der
        vorigen durch **genau eine** Change-Operation -- dieselbe, die der
        Nutzer im zugehoerigen Tour-Schritt ausloest.
    """

    s = ops.create_empty_schema("Urlaubsantrag (Tutorial)", schema_id=SCHEMA_ID)

    # Organisation direkt im Schema aufbauen (eingebettet, kein geteiltes
    # Org-Modell): zwei Rollen, eine Abteilung, zwei Agenten.
    s = ops.add_role(s, "Sachbearbeiter", role_id="sachbearbeiter")
    s = ops.add_role(s, "Teamleitung", role_id="teamleitung")
    s = ops.add_org_unit(s, "Personal", org_unit_id="personal")
    s = ops.add_agent(
        s, "Erika Sander", role_ids=["sachbearbeiter"], org_unit_id="personal",
        agent_id="tour-a-erika",
    )
    s = ops.add_agent(
        s, "Tom Berger", role_ids=["teamleitung"], org_unit_id="personal",
        agent_id="tour-a-tom",
    )
    s = ops.set_org_unit_manager(s, "personal", "tour-a-tom")

    # Zwei fertig verdrahtete Schritte: erfassen schreibt die Urlaubstage,
    # pruefen liest sie. Beide tragen eine Bearbeiterzuordnung -> Stufe 0 ist
    # vollstaendig korrekt.
    s = ops.serial_insert(s, LABEL_ERFASSEN, after_node_id="start")
    erfassen = _nid(s, LABEL_ERFASSEN)
    s = ops.serial_insert(s, LABEL_PRUEFEN, after_node_id=erfassen)
    pruefen = _nid(s, LABEL_PRUEFEN)
    s = ops.add_data_element(s, "Urlaubstage", DataType.INTEGER, element_id="tage")
    s = ops.connect_data(s, erfassen, "tage", AccessMode.WRITE)
    s = ops.connect_data(s, pruefen, "tage", AccessMode.READ)
    s = ops.assign_staff_rule(s, erfassen, _role("sachbearbeiter"))
    s = ops.assign_staff_rule(s, pruefen, _role("teamleitung"))
    stage0 = s

    # Stufe 1: der Nutzer fuegt einen Schritt ein -- bewusst gleich hinter dem
    # Start und damit VOR dem Schreiber der Urlaubstage. Das legt die Buehne fuer
    # die Ablehnung in build_rejection().
    stage1 = ops.serial_insert(stage0, LABEL_INFORMIEREN, after_node_id="start")
    informieren = _nid(stage1, LABEL_INFORMIEREN)

    # Stufe 2: Bearbeiter binden (Z-Regeln) -- das ist zulaessig und geht durch.
    stage2 = ops.assign_staff_rule(stage1, informieren, _role("sachbearbeiter"))

    return [stage0, stage1, stage2]


def build_rejection() -> list[dict[str, Any]]:
    """Faehrt die **abgelehnte** Operation der Tour nach und liefert ihre Befunde.

    Der Nutzer versucht, an "Antragsteller informieren" (Stufe 1, gleich hinter
    dem Start) das Datenelement *Urlaubstage* **lesend** zu binden. Auf dem Pfad
    dorthin hat es noch niemand geschrieben, also weist der Kern die Operation
    zurueck (D1) -- das Modell bleibt unveraendert gueltig.

    :return: Die Befundliste als JSON, in der Form, die die API im
        ``detail``-Feld einer HTTP-422-Antwort liefert.
    :raises AssertionError: wenn der Kern die Operation wider Erwarten
        **annimmt**. Dann stimmt die Tour-Erzaehlung nicht mehr, und der Wachtest
        soll fehlschlagen, statt dem Nutzer eine erfundene Ablehnung zu zeigen.
    """

    stage1 = build_stages()[1]
    informieren = _nid(stage1, LABEL_INFORMIEREN)
    try:
        ops.connect_data(stage1, informieren, "tage", AccessMode.READ)
    except CorrectnessError as exc:
        return [json.loads(f.model_dump_json()) for f in exc.findings]
    raise AssertionError(
        "Der Kern akzeptiert die Lesebindung vor dem Schreiber -- die "
        "Tour-Erzaehlung (Ablehnung nach D1) trifft nicht mehr zu."
    )


#: Erkennt die vom Kern vergebenen laufenden Ids (``act_17``, ``xor_18`` …).
_GENERATED_ID = re.compile(r"\b(act|xor|and|schema|elem)_(\d+)\b")


def _canonicalise(payload: dict[str, Any]) -> dict[str, Any]:
    """Ersetzt die laufenden Knoten-Ids durch stabile Tutorial-Ids.

    ``operations._new_id`` zaehlt einen **prozessweiten** Zaehler hoch. Welche
    Zahl der Beispielprozess bekommt, haengt also davon ab, was im selben
    Python-Prozess vorher gebaut wurde -- in der vollen Testsuite etwas anderes
    als beim einzelnen Erzeugen. Ohne diese Normalisierung wuerde der
    Drift-Waechter je nach Testreihenfolge fehlschlagen.

    Ersetzt wird auf der serialisierten Form, damit Vorkommen in Schluesseln,
    Werten und zusammengesetzten Kennungen (``"a->b"``) gleichermassen erfasst
    werden. Die Reihenfolge des ersten Auftretens ist dank ``sort_keys``
    deterministisch.

    :param payload: Aufzeichnung mit laufenden Ids.
    :return: Dieselbe Aufzeichnung mit stabilen Ids.
    """

    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    mapping: dict[str, str] = {}
    for match in _GENERATED_ID.finditer(raw):
        original = match.group(0)
        if original not in mapping:
            mapping[original] = f"{match.group(1)}_{len(mapping) + 1}"
    # In EINEM Durchlauf ersetzen. Nacheinander waere falsch: Eine erzeugte
    # stabile Id (act_1) kann selbst wie eine laufende Id aussehen und von einer
    # spaeteren Ersetzung nochmals getroffen werden -- dann fallen mehrere
    # Knoten auf dieselbe Id zusammen.
    result: dict[str, Any] = json.loads(
        _GENERATED_ID.sub(lambda m: mapping[m.group(0)], raw)
    )
    return result


def build_payload() -> dict[str, Any]:
    """Baut die vollstaendige Aufzeichnung fuer den Sandkasten.

    Je Stufe Schema **und** Validierungsbefund -- exakt das Paar, das
    ``refreshSchema()`` im Web-Client sonst ueber zwei GET-Aufrufe holt --, dazu
    die Befunde der abgelehnten Operation.

    :return: ``{"stages": [{"schema": …, "validation": …}, …],
        "rejection": [finding, …]}`` (reines JSON).
    """

    payload: list[dict[str, Any]] = []
    for stage in build_stages():
        # Exakt die Form, die ``GET /schemas/{id}/validation`` liefert
        # (``api.ValidationReport``) -- der Client soll den Unterschied nicht
        # bemerken.
        findings = validate(stage, stage.org_model)
        payload.append(
            {
                "schema": json.loads(stage.model_dump_json()),
                "validation": {
                    "correct": not findings,
                    "findings": [json.loads(f.model_dump_json()) for f in findings],
                },
            }
        )
    return _canonicalise({"stages": payload, "rejection": build_rejection()})


_HEADER = """// SPDX-License-Identifier: BUSL-1.1
// ---------------------------------------------------------------------------
// Schnappschuesse des Tutorial-Beispielprozesses ("geführte Tour").
//
// ERZEUGT -- NICHT VON HAND BEARBEITEN.
//   Neu erzeugen mit:  cd core && python -m tests.tour_fixture_build
//   Quelle:            core/tests/tour_fixture_build.py
//
// Waehrend der Tour laeuft der Web-Client im schreibfreien Sandkasten: Kein
// POST/PUT/DELETE verlaesst den Browser, und jeder /schemas-Aufruf wird aus
// diesen Stufen bedient. Dadurch entstehen durch Tutorial-Eingaben keine
// dauerhaften Daten (docs/Tutorial-Konzept.md, §4).
//
// Es ist eine AUFZEICHNUNG, keine Logik: Der Client wertet hier nichts aus, er
// spielt ab, was der Kern in dieser Situation geliefert haette. Der Wachtest
// core/tests/test_tour_web.py faehrt die Stufen gegen den echten Kern nach und
// schlaegt fehl, sobald die Aufzeichnung von ihm abweicht.
//
// Stufe 0 = Ausgangslage · 1 = nach dem Einfuegen · 2 = nach der Datenbindung
// Stufe 3 = nach der Bearbeiterbindung (Befund aus Stufe 1 wieder aufgeloest).
// ---------------------------------------------------------------------------

const TOUR_FIXTURES = """

_FOOTER = ";\n"


def write_fixture_file() -> Path:
    """Schreibt ``web/tour/fixtures.js`` neu und liefert den geschriebenen Pfad."""

    body = json.dumps(build_payload(), ensure_ascii=False, indent=2, sort_keys=True)
    FIXTURE_PATH.write_text(_HEADER + body + _FOOTER, encoding="utf-8")
    return FIXTURE_PATH


if __name__ == "__main__":  # pragma: no cover - Entwickler-Werkzeug
    print(f"geschrieben: {write_fixture_file()}")
