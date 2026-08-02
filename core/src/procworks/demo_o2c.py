# SPDX-License-Identifier: BUSL-1.1
"""Order-to-Cash: der grosse, zusaetzliche Demo-Datensatz.

Neben dem schlanken Demo-Kosmos (:mod:`procworks.demo`, Urlaubsantrag und
Beschaffung) laedt dieses Modul den **kompletten Order-to-Cash-Wertstrom**:
Kundenanfrage -> Angebot -> Auftrag -> Lieferung -> Rechnung -> Zahlungseingang
-> Forderungsmanagement. Konzept und Begruendung der Modellierung stehen in
``docs/Order-to-Cash-Demoprozess-Konzept.md``.

Warum eine **Prozessfamilie** statt eines einzigen Schemas: Der Kern ist
block-strukturiert und zyklenfrei, und ein Schema mit ~100 Knoten waere auf der
Arbeitsflaeche unbedienbar. Die Zerlegung folgt der Verantwortung (Vertrieb /
Logistik / Finanzen) und fuehrt damit genau die Komposition vor, die den Kern
auszeichnet:

* ``o2c-auftragsabwicklung`` -- der Hauptprozess (RELEASED),
* ``o2c-bonitaet`` / ``o2c-versand`` / ``o2c-faktura`` -- Teilprozesse
  (SUBPROCESS, Regeln H1-H4), zugleich als Bibliotheks-Teilprozesse markiert,
* ``o2c-forderung`` / ``o2c-retoure`` -- Folgeprozesse (F1-F4), bedingt beim
  Abschluss einer Instanz ausgeloest.

Drei Muster, die man sonst reflexhaft als Schleife zeichnet, sind hier
zyklenfrei geloest -- das ist der didaktische Kern des Datensatzes:

* **frueher Abbruch** (Angebot abgelehnt): der komplette Rest der Abwicklung
  liegt *im* XOR-Zweig "Angenommen", statt an ihm vorbeizuspringen;
* **Mahn-Eskalation**: geschachtelte XOR-Bloecke, jede Stufe enthaelt die
  naechste (:func:`_build_forderung`);
* **Reklamation nach Lieferung**: eigener, bedingt ausgeloester Folgeprozess.

**Ohne externe Systeme.** Jede Stelle, an der in der Praxis ein ERP, ein
Lagersystem oder ein Kontoauszug stuende, ist eine normale interaktive
Aktivitaet mit dem Namenspraefix ``System (simuliert):`` (siehe
:data:`SYSTEM_PREFIX`). Alle Datenelemente sind ``INSTANCE`` -- kein Connector,
kein Worker, keine Datenbank noetig, der Datensatz ist am Bildschirm vollstaendig
durchspielbar.

Wie :mod:`procworks.demo` und :mod:`procworks.templates` wird **ausschliesslich
ueber die oeffentlichen Change-Operationen** gebaut (derselbe
Validate-before-Commit-Pfad wie jeder Client), also correct by construction.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from procworks import execution as exe
from procworks import operations as ops
from procworks import org as org_ops
from procworks.audit import AuditLog, EventType
from procworks.auth_password import PasswordAuthBackend, User, hash_password
from procworks.demo import DEMO_PASSWORD
from procworks.model import (
    AbsenceEntry,
    AccessMode,
    DataType,
    FollowUpMode,
    FollowUpTrigger,
    ImpactUrgency,
    InstanceState,
    MailBinding,
    MailRecipientMode,
    OrgModel,
    ProcessInstance,
    ProcessSchema,
    StaffRule,
    StaffRuleKind,
    TimeConstraint,
    ValueClass,
    WidgetKind,
    WorkItemPriority,
)
from procworks.store import (
    AbsenceStore,
    InstanceStore,
    OrgStore,
    SchemaStore,
    dehydrate_org,
)
from procworks.validator import SchemaResolver

#: Stabile Ids, damit der Datensatz wiedererkennbar und reset-idempotent ist.
ORG_ID = "org-nordwind"
SCHEMA_MAIN = "o2c-auftragsabwicklung"
SCHEMA_BONITAET = "o2c-bonitaet"
SCHEMA_VERSAND = "o2c-versand"
SCHEMA_FAKTURA = "o2c-faktura"
SCHEMA_FORDERUNG = "o2c-forderung"
SCHEMA_RETOURE = "o2c-retoure"

#: Namenspraefix der simulierten Systemschritte. Wo in der Praxis ein ERP, ein
#: Lagersystem oder ein Kontoauszug stuende, steht hier eine ganz normale
#: interaktive Aktivitaet -- so bleibt der Datensatz ohne Connector und ohne
#: External-Task-Worker vollstaendig durchklickbar. Der Hilfetext der Maske sagt
#: jeweils, welche echte Anbindung dort spaeter ansetzen wuerde.
SYSTEM_PREFIX = "System (simuliert): "

#: Hinweis in jeder Maske eines simulierten Systemschritts.
SYSTEM_HINT = (
    "In der Produktion liefert das Vorsystem diesen Wert über einen "
    "Daten-Connector (C1-C9) bzw. einen External Task (I1-I4). Im Demo wird er "
    "von Hand erfasst."
)

#: Die Demo-Logins dieses Datensatzes: (login, name, rollen, agent-id).
#: Das Passwort ist dasselbe wie im Basis-Demo (``demo.DEMO_PASSWORD``), damit
#: sich niemand zwei merken muss.
O2C_USERS: list[tuple[str, str, frozenset[str], str]] = [
    ("sina.springer", "Sina Springer", frozenset({"operator"}), "a-sina"),
    ("nadja.neumann", "Nadja Neumann", frozenset({"operator"}), "a-nadja"),
    ("viktor.vogel", "Viktor Vogel", frozenset({"operator"}), "a-viktor"),
    ("karin.kredel", "Karin Kredel", frozenset({"operator"}), "a-karin"),
    ("lars.lange", "Lars Lange", frozenset({"operator"}), "a-lars"),
    ("bianca.buch", "Bianca Buch", frozenset({"operator"}), "a-bianca"),
    ("gustav.gross", "Gustav Groß", frozenset({"operator"}), "a-gustav"),
    ("automat.nordwind", "Automat Nordwind", frozenset({"operator"}), "a-automat"),
]


# --- kleine Helfer --------------------------------------------------------


def _nid(schema: ProcessSchema, label: str) -> str:
    """Id des (eindeutigen) Knotens mit dieser Bezeichnung.

    Alle Bezeichnungen innerhalb eines Schemas sind bewusst eindeutig gehalten
    (auch die drei Verbuchungsschritte des Mahnwesens tragen ihre Stufe im
    Namen), damit dieser Zugriff eindeutig bleibt.
    """

    return next(n.id for n in schema.nodes.values() if n.label == label)


def _succ(schema: ProcessSchema, node_id: str) -> str:
    """Id des einzigen Nachfolgers -- nur fuer Knoten mit genau einer Kante.

    Damit finden wir nach einem Einfuegen die frisch entstandenen Gateways:
    hinter dem Anker liegt der Split, hinter einem Zweigrumpf der Join. Ein
    ``_gateway_id``-Ansatz ueber den Knotentyp (wie im Basis-Demo) traegt hier
    nicht, weil dieser Datensatz mehrere Splits desselben Typs je Schema hat.
    """

    return schema.outgoing(node_id)[0].target


def _role(role_id: str) -> StaffRule:
    """Bearbeiterzuordnungsregel (BZR) auf genau eine Rolle."""

    return StaffRule(kind=StaffRuleKind.ROLE, ref=role_id)


def _system_form(
    schema: ProcessSchema,
    node_id: str,
    title: str,
    fields: list[ops.FormFieldSpec],
    confirm_element: str,
) -> ProcessSchema:
    """Maske eines simulierten Systemschritts: Nutzdaten + Bestaetigungshaken.

    Der Haken (``confirm_element``, ein BOOLEAN) steht fuer den Lauf des
    Vorsystems und macht den Schritt auch ohne Nutzdaten quittierbar. Jedes Feld
    traegt den Hinweis, welche echte Anbindung hier spaeter greifen wuerde.
    """

    marked = [
        ops.FormFieldSpec(
            element_id=spec.element_id,
            widget=spec.widget,
            label=spec.label,
            mode=spec.mode,
            required=spec.required,
            options=spec.options,
            help_text=spec.help_text or SYSTEM_HINT,
        )
        for spec in fields
    ]
    marked.append(
        ops.FormFieldSpec(
            element_id=confirm_element,
            widget=WidgetKind.CHECKBOX,
            label="Systemlauf bestätigt",
            help_text="Ersetzt die Rückmeldung des Vorsystems.",
        )
    )
    return ops.set_form(schema, node_id, title=title, fields=marked)


# --- Organisation ---------------------------------------------------------


def _build_org() -> OrgModel:
    """Die Organisation des Datensatzes: Nordwind Handels GmbH.

    Vier Fachbereiche unter der Geschaeftsleitung, acht Personen. Drei Details
    tragen fachliche Last:

    * **Sina Springer** haelt *alle* operativen Rollen. Wer den Prozess allein
      durchklicken will, meldet sich als Sina an und sieht jede Aufgabe in einer
      Liste -- ohne dass die Rollentrennung im Modell aufgeweicht wird.
    * **Jede** Person hat ein Postfach, und die vier Sammelrollen zusaetzlich
      ein Gruppenpostfach: ohne Adresse fuer *jeden moeglichen* Empfaenger
      (Vertretungen eingeschlossen) weist N3 jede Benachrichtigung ab.
    * Die Leitungen der Einheiten sind gepflegt, weil die Auftragsfreigabe die
      **Vorgesetzten-BZR** relativ zum Erfasser nutzt (Z1-Z3).
    """

    org = org_ops.create_org_model("Nordwind Handels GmbH", org_id=ORG_ID)
    for role_id, name in [
        ("innendienst", "Vertriebsinnendienst"),
        ("vertriebsleitung", "Vertriebsleitung"),
        ("kreditmanagement", "Kreditmanagement"),
        ("lager", "Lager"),
        ("versand", "Versand"),
        ("buchhaltung", "Debitorenbuchhaltung"),
        ("geschaeftsfuehrung", "Geschäftsführung"),
        ("systemdienst", "Systemdienst (simuliert)"),
    ]:
        org = org_ops.org_add_role(org, name, role_id=role_id)

    for unit_id, name in [
        ("leitung", "Geschäftsleitung"),
        ("vertrieb", "Vertrieb"),
        ("logistik", "Logistik"),
        ("finanzen", "Finanzen"),
        ("it", "IT-Betrieb"),
    ]:
        org = org_ops.org_add_unit(org, name, org_unit_id=unit_id)

    org = org_ops.org_add_agent(
        org,
        "Gustav Groß",
        role_ids=["geschaeftsfuehrung"],
        org_unit_id="leitung",
        agent_id="a-gustav",
        email="gustav.gross@nordwind.example",
    )
    org = org_ops.org_add_agent(
        org,
        "Viktor Vogel",
        role_ids=["vertriebsleitung"],
        org_unit_id="vertrieb",
        agent_id="a-viktor",
        email="viktor.vogel@nordwind.example",
    )
    org = org_ops.org_add_agent(
        org,
        "Nadja Neumann",
        role_ids=["innendienst"],
        org_unit_id="vertrieb",
        agent_id="a-nadja",
        email="nadja.neumann@nordwind.example",
    )
    org = org_ops.org_add_agent(
        org,
        "Karin Kredel",
        role_ids=["kreditmanagement"],
        org_unit_id="finanzen",
        agent_id="a-karin",
        email="karin.kredel@nordwind.example",
    )
    org = org_ops.org_add_agent(
        org,
        "Lars Lange",
        role_ids=["lager", "versand"],
        org_unit_id="logistik",
        agent_id="a-lars",
        email="lars.lange@nordwind.example",
    )
    org = org_ops.org_add_agent(
        org,
        "Bianca Buch",
        role_ids=["buchhaltung"],
        org_unit_id="finanzen",
        agent_id="a-bianca",
        email="bianca.buch@nordwind.example",
    )
    # Die Springerin traegt alle operativen Rollen -- der Ein-Login-Durchlauf.
    # Sie sitzt im Vertrieb, damit auch bei ihr als Erfasserin eine vorgesetzte
    # Person gepflegt ist (Z2 fuer die Vorgesetzten-BZR der Freigabe).
    org = org_ops.org_add_agent(
        org,
        "Sina Springer",
        role_ids=[
            "innendienst",
            "kreditmanagement",
            "lager",
            "versand",
            "buchhaltung",
            "systemdienst",
        ],
        org_unit_id="vertrieb",
        agent_id="a-sina",
        email="sina.springer@nordwind.example",
    )
    org = org_ops.org_add_agent(
        org,
        "Automat Nordwind",
        role_ids=["systemdienst"],
        org_unit_id="it",
        agent_id="a-automat",
        email="automat@nordwind.example",
    )

    for unit_id in ("vertrieb", "logistik", "finanzen", "it"):
        org = org_ops.org_set_parent(org, unit_id, "leitung")
    org = org_ops.org_set_manager(org, "leitung", "a-gustav")
    org = org_ops.org_set_manager(org, "vertrieb", "a-viktor")
    org = org_ops.org_set_manager(org, "logistik", "a-lars")
    org = org_ops.org_set_manager(org, "finanzen", "a-bianca")
    org = org_ops.org_set_manager(org, "it", "a-gustav")

    # Gruppenpostfaecher fuer die Benachrichtigungen im Modus TO_GROUP_MAILBOX.
    org = org_ops.org_set_role_mailbox(org, "innendienst", "vertrieb@nordwind.example")
    org = org_ops.org_set_role_mailbox(org, "kreditmanagement", "kredit@nordwind.example")
    org = org_ops.org_set_role_mailbox(org, "versand", "versand@nordwind.example")
    org = org_ops.org_set_role_mailbox(org, "buchhaltung", "debitoren@nordwind.example")

    # Vertretung: Karin (Kreditmanagement) wird von Bianca (Buchhaltung)
    # vertreten -- bewusst ueber die Rollengrenze hinweg. Nur so ist die
    # Substitution ueberhaupt *sichtbar*: haette die Vertretung dieselbe Rolle,
    # saehe sie die Aufgaben ohnehin, und die Abwesenheit aenderte nichts.
    # Sina bleibt aussen vor, sie traegt alle Rollen ohnehin.
    org = org_ops.org_set_deputy(org, "a-karin", "a-bianca")
    return org


# --- Teilprozess: Bonitaets- und Kreditpruefung ---------------------------


def _build_bonitaet(org: OrgModel) -> ProcessSchema:
    """Teilprozess (SUBPROCESS-Ziel): Bonitaet pruefen und Kreditlimit setzen.

    Eingang (``input_mapping``): Kundennummer und Auftragswert des Elternvorgangs.
    Ausgang (``output_mapping``): Kreditlimit, Kreditfreigabe, Zahlungsart.

    Zwei Konstruktionsdetails, die H2 erzwingt bzw. D1 verlangt:

    * Die **Ausgaenge werden hinter dem Join geschrieben**, nicht in den Zweigen.
      H2 verlangt, dass ein zugeordneter Ausgang auf *jedem* Pfad des Kindes
      entsteht -- sonst waere das Elternelement undefiniert.
    * Der **Uebernahmeschritt** "Kundenstammdaten pruefen" schreibt die von aussen
      gereichten Werte verbindlich fort (die Maske ist mit ihnen vorbelegt).
      Dadurch gelten sie fuer D1 als gesetzt und duerfen spaeter verbindlich
      gelesen und in Mail-Vorlagen verwendet werden (N4).
    """

    s = ops.create_empty_schema("Bonitäts- und Kreditprüfung", schema_id=SCHEMA_BONITAET)
    s = ops.add_data_element(s, "Kundennummer", DataType.INTEGER, element_id="kunden_nr")
    s = ops.add_data_element(s, "Auftragswert (EUR)", DataType.FLOAT, element_id="auftragswert")
    s = ops.add_data_element(s, "Stammdaten geprüft", DataType.BOOLEAN, element_id="stammdaten_ok")
    s = ops.add_data_element(s, "Bonitätsindex", DataType.INTEGER, element_id="bonitaet_score")
    s = ops.add_data_element(s, "Auskunft eingeholt", DataType.BOOLEAN, element_id="auskunft_ok")
    s = ops.add_data_element(
        s, "Limitvorschlag (EUR)", DataType.FLOAT, element_id="kredit_vorschlag"
    )
    s = ops.add_data_element(s, "Kreditlimit (EUR)", DataType.FLOAT, element_id="kreditlimit")
    s = ops.add_data_element(
        s, "Kreditfreigabe erteilt", DataType.BOOLEAN, element_id="kreditfreigabe"
    )
    s = ops.add_data_element(s, "Zahlungsart", DataType.STRING, element_id="zahlungsart")

    s = ops.serial_insert(s, "Kundenstammdaten prüfen", after_node_id="start")
    stamm = _nid(s, "Kundenstammdaten prüfen")
    s = ops.set_form(
        s,
        stamm,
        title="Kundenstammdaten prüfen",
        fields=[
            ops.FormFieldSpec(
                element_id="kunden_nr",
                widget=WidgetKind.NUMBER,
                label="Kundennummer",
                help_text="Aus dem Auftragsvorgang übernommen -- bitte bestätigen.",
            ),
            ops.FormFieldSpec(
                element_id="auftragswert",
                widget=WidgetKind.NUMBER,
                label="Auftragswert (EUR)",
                help_text="Aus dem Auftragsvorgang übernommen -- Prüfgrundlage.",
            ),
            ops.FormFieldSpec(
                element_id="stammdaten_ok",
                widget=WidgetKind.CHECKBOX,
                label="Stammdaten vollständig und aktuell",
            ),
        ],
    )

    s = ops.serial_insert(s, SYSTEM_PREFIX + "Wirtschaftsauskunft einholen", after_node_id=stamm)
    auskunft = _nid(s, SYSTEM_PREFIX + "Wirtschaftsauskunft einholen")
    s = _system_form(
        s,
        auskunft,
        "Wirtschaftsauskunft",
        [
            ops.FormFieldSpec(
                element_id="bonitaet_score",
                widget=WidgetKind.NUMBER,
                label="Bonitätsindex (0-100)",
            )
        ],
        confirm_element="auskunft_ok",
    )

    # Schwellwert-Verzweigung ueber den Bonitaetsindex: hier entscheidet die
    # Kennzahl tatsaechlich ueber die *Zustaendigkeit* -- schlechte Bonitaet geht
    # an die Geschaeftsfuehrung. Der letzte Zweig ist unbeschraenkt (K7).
    s = ops.conditional_insert(
        s,
        after_node_id=auskunft,
        discriminator="bonitaet_score",
        branches=[
            ops.BranchSpec(label="Kreditentscheidung eskalieren", upper=40),
            ops.BranchSpec(label="Kreditlimit manuell festlegen", upper=80),
            ops.BranchSpec(label="Kreditlimit freigeben"),
        ],
    )
    eskalieren = _nid(s, "Kreditentscheidung eskalieren")
    manuell = _nid(s, "Kreditlimit manuell festlegen")
    freigeben = _nid(s, "Kreditlimit freigeben")
    for node_id, title in [
        (eskalieren, "Kreditentscheidung der Geschäftsführung"),
        (manuell, "Kreditlimit manuell festlegen"),
        (freigeben, "Kreditlimit freigeben"),
    ]:
        s = ops.set_form(
            s,
            node_id,
            title=title,
            fields=[
                ops.FormFieldSpec(
                    element_id="bonitaet_score",
                    widget=WidgetKind.NUMBER,
                    label="Bonitätsindex",
                    mode=AccessMode.READ,
                    help_text="Entscheidungsgrundlage -- hier nur zur Ansicht.",
                ),
                ops.FormFieldSpec(
                    element_id="kredit_vorschlag",
                    widget=WidgetKind.NUMBER,
                    label="Vorgeschlagenes Limit (EUR)",
                ),
            ],
        )

    join = _succ(s, eskalieren)
    s = ops.serial_insert(s, "Kreditentscheidung dokumentieren", after_node_id=join)
    doku = _nid(s, "Kreditentscheidung dokumentieren")
    s = ops.set_form(
        s,
        doku,
        title="Kreditentscheidung dokumentieren",
        fields=[
            ops.FormFieldSpec(
                element_id="kredit_vorschlag",
                widget=WidgetKind.NUMBER,
                label="Vorgeschlagenes Limit (EUR)",
                mode=AccessMode.READ,
            ),
            ops.FormFieldSpec(
                element_id="kreditlimit",
                widget=WidgetKind.NUMBER,
                label="Kreditlimit (EUR)",
            ),
            ops.FormFieldSpec(
                element_id="kreditfreigabe",
                widget=WidgetKind.CHECKBOX,
                label="Kreditfreigabe erteilt",
            ),
            ops.FormFieldSpec(
                element_id="zahlungsart",
                widget=WidgetKind.DROPDOWN,
                label="Zahlungsart",
                options=("Rechnung", "Lastschrift", "Vorkasse"),
                help_text="Ergebnis der Kreditprüfung -- geht in den Vorgang zurück.",
            ),
        ],
    )

    s = ops.link_org_model(s, ORG_ID, org)
    s = ops.assign_staff_rule(s, stamm, _role("kreditmanagement"))
    s = ops.assign_staff_rule(s, auskunft, _role("systemdienst"))
    s = ops.assign_staff_rule(s, eskalieren, _role("geschaeftsfuehrung"))
    s = ops.assign_staff_rule(s, manuell, _role("kreditmanagement"))
    s = ops.assign_staff_rule(s, freigeben, _role("kreditmanagement"))
    s = ops.assign_staff_rule(s, doku, _role("kreditmanagement"))

    s = ops.set_mail_binding(
        s,
        eskalieren,
        MailBinding(
            mode=MailRecipientMode.TO_ELIGIBLE_AGENTS,
            subject="Kreditentscheidung nötig (Bonitätsindex {bonitaet_score})",
            body=(
                "Für Kunde {kunden_nr} liegt ein Bonitätsindex von "
                "{bonitaet_score} vor. Bitte über das Kreditlimit entscheiden."
            ),
        ),
    )

    s = ops.set_value_class(s, stamm, ValueClass.BUSINESS_NECESSARY)
    s = ops.set_value_class(s, auskunft, ValueClass.BUSINESS_NECESSARY)
    s = ops.set_value_class(s, eskalieren, ValueClass.BUSINESS_NECESSARY)
    s = ops.set_value_class(s, manuell, ValueClass.BUSINESS_NECESSARY)
    s = ops.set_value_class(s, freigeben, ValueClass.VALUE_ADDING)
    s = ops.set_value_class(s, doku, ValueClass.BUSINESS_NECESSARY)
    s = ops.set_node_priority(
        s, eskalieren, WorkItemPriority(impact=ImpactUrgency.HIGH, urgency=ImpactUrgency.HIGH)
    )

    s = ops.set_time_constraint(s, stamm, TimeConstraint(max_duration_seconds=3600))
    s = ops.set_time_constraint(s, auskunft, TimeConstraint(max_duration_seconds=1800))
    s = ops.set_time_constraint(
        s, eskalieren, TimeConstraint(max_duration_seconds=14400, target_lead_seconds=7200)
    )
    s = ops.set_time_constraint(s, manuell, TimeConstraint(max_duration_seconds=7200))
    s = ops.set_time_constraint(s, freigeben, TimeConstraint(max_duration_seconds=1800))
    s = ops.set_time_constraint(s, doku, TimeConstraint(max_duration_seconds=3600))
    s = ops.set_deadline(s, 86400)  # ein Arbeitstag
    return ops.set_library_subprocess(ops.release(s), True)


# --- Teilprozess: Versand und Zustellung ----------------------------------


def _build_versand(org: OrgModel) -> ProcessSchema:
    """Teilprozess: verpacken, uebergeben, zustellen.

    Eingang: Auftragsnummer und Lieferadresse. Ausgang: Lieferscheinnummer,
    Sendungsverfolgung (Datentyp ``URI``) und Zustelldatum. Kein XOR, also sind
    alle Ausgaenge trivial auf jedem Pfad gesetzt (H2).
    """

    s = ops.create_empty_schema("Versand und Zustellung", schema_id=SCHEMA_VERSAND)
    s = ops.add_data_element(s, "Auftragsnummer", DataType.STRING, element_id="auftrags_nr")
    s = ops.add_data_element(s, "Lieferadresse", DataType.STRING, element_id="lieferadresse")
    s = ops.add_data_element(s, "Packstücke", DataType.INTEGER, element_id="packstuecke")
    s = ops.add_data_element(s, "Gewicht (kg)", DataType.FLOAT, element_id="gewicht")
    s = ops.add_data_element(s, "Lieferscheinnummer", DataType.STRING, element_id="lieferschein_nr")
    s = ops.add_data_element(s, "Sendungsverfolgung", DataType.URI, element_id="tracking_url")
    s = ops.add_data_element(
        s, "Frachtauftrag übergeben", DataType.BOOLEAN, element_id="fracht_ok"
    )
    s = ops.add_data_element(s, "Sendung übergeben", DataType.BOOLEAN, element_id="uebergabe_ok")
    s = ops.add_data_element(s, "Kunde informiert", DataType.BOOLEAN, element_id="kunde_informiert")
    s = ops.add_data_element(s, "Zustelldatum", DataType.DATE, element_id="zustellung_datum")
    s = ops.add_data_element(
        s, "Zustellung bestätigt", DataType.BOOLEAN, element_id="zustellung_ok"
    )

    s = ops.serial_insert(s, "Sendung übernehmen und verpacken", after_node_id="start")
    packen = _nid(s, "Sendung übernehmen und verpacken")
    s = ops.set_form(
        s,
        packen,
        title="Sendung verpacken",
        fields=[
            ops.FormFieldSpec(
                element_id="auftrags_nr",
                widget=WidgetKind.TEXT,
                label="Auftragsnummer",
                help_text="Aus dem Auftragsvorgang übernommen -- bitte bestätigen.",
            ),
            ops.FormFieldSpec(
                element_id="lieferadresse",
                widget=WidgetKind.TEXTAREA,
                label="Lieferadresse",
            ),
            ops.FormFieldSpec(
                element_id="packstuecke", widget=WidgetKind.NUMBER, label="Packstücke"
            ),
            ops.FormFieldSpec(element_id="gewicht", widget=WidgetKind.NUMBER, label="Gewicht (kg)"),
        ],
    )

    s = ops.serial_insert(s, SYSTEM_PREFIX + "Frachtauftrag übergeben", after_node_id=packen)
    fracht = _nid(s, SYSTEM_PREFIX + "Frachtauftrag übergeben")
    s = _system_form(
        s,
        fracht,
        "Frachtauftrag",
        [
            ops.FormFieldSpec(
                element_id="lieferschein_nr", widget=WidgetKind.TEXT, label="Lieferscheinnummer"
            ),
            ops.FormFieldSpec(
                element_id="tracking_url",
                widget=WidgetKind.TEXT,
                label="Sendungsverfolgung (URL)",
            ),
        ],
        confirm_element="fracht_ok",
    )

    s = ops.parallel_insert(
        s,
        ["Sendung an Spediteur übergeben", "Kunde über Versand informieren"],
        after_node_id=fracht,
    )
    uebergabe = _nid(s, "Sendung an Spediteur übergeben")
    infomail = _nid(s, "Kunde über Versand informieren")
    s = ops.set_form(
        s,
        uebergabe,
        title="Übergabe an den Spediteur",
        fields=[
            ops.FormFieldSpec(
                element_id="uebergabe_ok", widget=WidgetKind.CHECKBOX, label="Sendung übergeben"
            )
        ],
    )
    s = ops.set_form(
        s,
        infomail,
        title="Versandinformation an den Kunden",
        fields=[
            ops.FormFieldSpec(
                element_id="tracking_url",
                widget=WidgetKind.TEXT,
                label="Sendungsverfolgung",
                mode=AccessMode.READ,
            ),
            ops.FormFieldSpec(
                element_id="kunde_informiert",
                widget=WidgetKind.CHECKBOX,
                label="Kunde informiert",
            ),
        ],
    )

    join = _succ(s, uebergabe)
    s = ops.serial_insert(s, "Zustellung bestätigen", after_node_id=join)
    zustellung = _nid(s, "Zustellung bestätigen")
    s = ops.set_form(
        s,
        zustellung,
        title="Zustellung bestätigen",
        fields=[
            ops.FormFieldSpec(
                element_id="zustellung_datum", widget=WidgetKind.DATE, label="Zustelldatum"
            ),
            ops.FormFieldSpec(
                element_id="zustellung_ok",
                widget=WidgetKind.CHECKBOX,
                label="Zustellung bestätigt",
            ),
        ],
    )

    s = ops.link_org_model(s, ORG_ID, org)
    s = ops.assign_staff_rule(s, packen, _role("versand"))
    s = ops.assign_staff_rule(s, fracht, _role("systemdienst"))
    s = ops.assign_staff_rule(s, uebergabe, _role("versand"))
    s = ops.assign_staff_rule(s, infomail, _role("innendienst"))
    s = ops.assign_staff_rule(s, zustellung, _role("versand"))

    s = ops.set_mail_binding(
        s,
        infomail,
        MailBinding(
            mode=MailRecipientMode.TO_GROUP_MAILBOX,
            subject="Sendung {lieferschein_nr} ist unterwegs",
            body=(
                "Die Sendung zum Auftrag {auftrags_nr} wurde übergeben.\n"
                "Sendungsverfolgung: {tracking_url}"
            ),
        ),
    )

    s = ops.set_value_class(s, packen, ValueClass.VALUE_ADDING)
    s = ops.set_value_class(s, fracht, ValueClass.BUSINESS_NECESSARY)
    s = ops.set_value_class(s, uebergabe, ValueClass.VALUE_ADDING)
    s = ops.set_value_class(s, infomail, ValueClass.VALUE_ADDING)
    s = ops.set_value_class(s, zustellung, ValueClass.BUSINESS_NECESSARY)

    s = ops.set_time_constraint(
        s, packen, TimeConstraint(max_duration_seconds=7200, target_lead_seconds=3600)
    )
    s = ops.set_time_constraint(s, fracht, TimeConstraint(max_duration_seconds=3600))
    s = ops.set_time_constraint(s, uebergabe, TimeConstraint(max_duration_seconds=3600))
    s = ops.set_time_constraint(s, infomail, TimeConstraint(max_duration_seconds=1800))
    s = ops.set_time_constraint(s, zustellung, TimeConstraint(max_duration_seconds=172800))
    s = ops.set_deadline(s, 259200)  # drei Tage -- deckt sich mit der Frist am Elternknoten
    return ops.set_library_subprocess(ops.release(s), True)


# --- Teilprozess: Fakturierung --------------------------------------------


def _build_faktura(org: OrgModel) -> ProcessSchema:
    """Teilprozess: Rechnung erzeugen, pruefen, versenden.

    Eingang: Auftragsnummer und Auftragswert. Ausgang: Rechnungsnummer,
    Rechnungsbetrag und Faelligkeitsdatum.
    """

    s = ops.create_empty_schema("Fakturierung", schema_id=SCHEMA_FAKTURA)
    s = ops.add_data_element(s, "Auftragsnummer", DataType.STRING, element_id="auftrags_nr")
    s = ops.add_data_element(s, "Auftragswert (EUR)", DataType.FLOAT, element_id="auftragswert")
    s = ops.add_data_element(s, "Rechnungsnummer", DataType.STRING, element_id="rechnungs_nr")
    s = ops.add_data_element(
        s, "Rechnungsbetrag (EUR)", DataType.FLOAT, element_id="rechnungsbetrag"
    )
    s = ops.add_data_element(s, "Rechnung erzeugt", DataType.BOOLEAN, element_id="rechnung_ok")
    s = ops.add_data_element(
        s, "Rechnung geprüft", DataType.BOOLEAN, element_id="rechnung_geprueft"
    )
    s = ops.add_data_element(s, "Fällig am", DataType.DATE, element_id="faellig_am")
    s = ops.add_data_element(s, "Versandweg", DataType.STRING, element_id="versandweg")

    s = ops.serial_insert(s, SYSTEM_PREFIX + "Rechnung erzeugen", after_node_id="start")
    erzeugen = _nid(s, SYSTEM_PREFIX + "Rechnung erzeugen")
    s = _system_form(
        s,
        erzeugen,
        "Rechnung erzeugen",
        [
            ops.FormFieldSpec(
                element_id="auftrags_nr",
                widget=WidgetKind.TEXT,
                label="Auftragsnummer",
                help_text="Aus dem Auftragsvorgang übernommen.",
            ),
            ops.FormFieldSpec(
                element_id="auftragswert",
                widget=WidgetKind.NUMBER,
                label="Auftragswert (EUR)",
                help_text="Aus dem Auftragsvorgang übernommen.",
            ),
            ops.FormFieldSpec(
                element_id="rechnungs_nr", widget=WidgetKind.TEXT, label="Rechnungsnummer"
            ),
            ops.FormFieldSpec(
                element_id="rechnungsbetrag",
                widget=WidgetKind.NUMBER,
                label="Rechnungsbetrag (EUR)",
            ),
        ],
        confirm_element="rechnung_ok",
    )

    s = ops.serial_insert(s, "Rechnung fachlich prüfen", after_node_id=erzeugen)
    pruefen = _nid(s, "Rechnung fachlich prüfen")
    s = ops.set_form(
        s,
        pruefen,
        title="Rechnung prüfen",
        fields=[
            ops.FormFieldSpec(
                element_id="rechnungsbetrag",
                widget=WidgetKind.NUMBER,
                label="Rechnungsbetrag (EUR)",
                mode=AccessMode.READ,
            ),
            ops.FormFieldSpec(
                element_id="auftragswert",
                widget=WidgetKind.NUMBER,
                label="Auftragswert (EUR)",
                mode=AccessMode.READ,
                help_text="Abgleich: Rechnung gegen Auftrag.",
            ),
            ops.FormFieldSpec(
                element_id="rechnung_geprueft",
                widget=WidgetKind.CHECKBOX,
                label="Rechnung geprüft",
            ),
        ],
    )

    s = ops.serial_insert(s, "Rechnung versenden", after_node_id=pruefen)
    versenden = _nid(s, "Rechnung versenden")
    s = ops.set_form(
        s,
        versenden,
        title="Rechnung versenden",
        fields=[
            ops.FormFieldSpec(
                element_id="rechnungs_nr",
                widget=WidgetKind.TEXT,
                label="Rechnungsnummer",
                mode=AccessMode.READ,
            ),
            ops.FormFieldSpec(
                element_id="versandweg",
                widget=WidgetKind.DROPDOWN,
                label="Versandweg",
                options=("E-Mail", "Post", "Kundenportal"),
            ),
            ops.FormFieldSpec(element_id="faellig_am", widget=WidgetKind.DATE, label="Fällig am"),
        ],
    )

    s = ops.link_org_model(s, ORG_ID, org)
    s = ops.assign_staff_rule(s, erzeugen, _role("systemdienst"))
    s = ops.assign_staff_rule(s, pruefen, _role("buchhaltung"))
    s = ops.assign_staff_rule(s, versenden, _role("buchhaltung"))

    s = ops.set_value_class(s, erzeugen, ValueClass.BUSINESS_NECESSARY)
    s = ops.set_value_class(s, pruefen, ValueClass.BUSINESS_NECESSARY)
    s = ops.set_value_class(s, versenden, ValueClass.VALUE_ADDING)

    s = ops.set_time_constraint(s, erzeugen, TimeConstraint(max_duration_seconds=1800))
    s = ops.set_time_constraint(
        s, pruefen, TimeConstraint(max_duration_seconds=3600, target_lead_seconds=3600)
    )
    s = ops.set_time_constraint(s, versenden, TimeConstraint(max_duration_seconds=3600))
    s = ops.set_deadline(s, 86400)
    return ops.set_library_subprocess(ops.release(s), True)


# --- Folgeprozess: Forderungsmanagement -----------------------------------


def _build_forderung(org: OrgModel) -> ProcessSchema:
    """Folgeprozess: Mahnwesen bis zum Inkasso -- eine Eskalation *ohne* Zyklus.

    Das Herzstueck des Datensatzes. Fachlich ist das eine Schleife ("mahnen, bis
    bezahlt ist"), im block-strukturierten Modell wird daraus eine **Kaskade
    geschachtelter XOR-Bloecke**: der "noch nicht bezahlt"-Zweig jeder Stufe
    *enthaelt* die naechste Stufe. Jede Stufe ist damit total und disjunkt
    partitioniert (K7) -- die Eskalation kann weder verklemmen noch zwei Wege
    gleichzeitig oeffnen, und ihre Tiefe ist am Modell ablesbar.

    ``ausgang`` wird auf **jedem** Pfad geschrieben (von den drei
    Verbuchungsschritten und vom Inkasso), darum darf der Abschlussschritt ihn
    verbindlich lesen (D1 ueber die geschachtelten XOR-Joins hinweg).
    """

    s = ops.create_empty_schema("Forderungsmanagement", schema_id=SCHEMA_FORDERUNG)
    s = ops.add_data_element(s, "Kunde", DataType.STRING, element_id="kunde")
    s = ops.add_data_element(s, "Rechnungsnummer", DataType.STRING, element_id="rechnungs_nr")
    s = ops.add_data_element(s, "Offener Betrag (EUR)", DataType.FLOAT, element_id="offener_betrag")
    s = ops.add_data_element(s, "Fällig am", DataType.DATE, element_id="faellig_am")
    s = ops.add_data_element(s, "Erinnerung am", DataType.DATE, element_id="erinnerung_am")
    s = ops.add_data_element(s, "Zahlung nach Erinnerung", DataType.BOOLEAN, element_id="zahlung_1")
    s = ops.add_data_element(s, "1. Mahnung am", DataType.DATE, element_id="mahnung_1_am")
    s = ops.add_data_element(s, "Zahlung nach 1. Mahnung", DataType.BOOLEAN, element_id="zahlung_2")
    s = ops.add_data_element(s, "2. Mahnung am", DataType.DATE, element_id="mahnung_2_am")
    s = ops.add_data_element(s, "Zahlung nach 2. Mahnung", DataType.BOOLEAN, element_id="zahlung_3")
    s = ops.add_data_element(s, "Ausgang", DataType.STRING, element_id="ausgang")
    s = ops.add_data_element(s, "Abschlussvermerk", DataType.STRING, element_id="abschluss_vermerk")

    # Uebernahmeschritt: schreibt die aus dem Vorprozess gereichten Werte
    # verbindlich fort (die Maske ist damit vorbelegt), damit sie fuer D1 als
    # gesetzt gelten und in den Mail-Vorlagen verwendet werden duerfen (N4).
    s = ops.serial_insert(s, "Forderungsakte anlegen", after_node_id="start")
    akte = _nid(s, "Forderungsakte anlegen")
    s = ops.set_form(
        s,
        akte,
        title="Forderungsakte anlegen",
        fields=[
            ops.FormFieldSpec(element_id="kunde", widget=WidgetKind.TEXT, label="Kunde"),
            ops.FormFieldSpec(
                element_id="rechnungs_nr", widget=WidgetKind.TEXT, label="Rechnungsnummer"
            ),
            ops.FormFieldSpec(
                element_id="offener_betrag",
                widget=WidgetKind.NUMBER,
                label="Offener Betrag (EUR)",
            ),
            ops.FormFieldSpec(element_id="faellig_am", widget=WidgetKind.DATE, label="Fällig am"),
        ],
    )

    s = ops.serial_insert(s, "Zahlungserinnerung versenden", after_node_id=akte)
    erinnerung = _nid(s, "Zahlungserinnerung versenden")
    s = ops.set_form(
        s,
        erinnerung,
        title="Zahlungserinnerung",
        fields=[
            ops.FormFieldSpec(
                element_id="offener_betrag",
                widget=WidgetKind.NUMBER,
                label="Offener Betrag (EUR)",
                mode=AccessMode.READ,
            ),
            ops.FormFieldSpec(
                element_id="erinnerung_am", widget=WidgetKind.DATE, label="Erinnerung versendet am"
            ),
        ],
    )

    # --- Stufe 1 ----------------------------------------------------------
    s = ops.serial_insert(
        s, SYSTEM_PREFIX + "Zahlungseingang prüfen (Stufe 1)", after_node_id=erinnerung
    )
    pruef_1 = _nid(s, SYSTEM_PREFIX + "Zahlungseingang prüfen (Stufe 1)")
    s = _system_form(s, pruef_1, "Zahlungseingang (Stufe 1)", [], confirm_element="zahlung_1")
    s = ops.conditional_insert(
        s,
        after_node_id=pruef_1,
        discriminator="zahlung_1",
        branches=[
            ops.BranchSpec(label="Zahlung verbuchen (Stufe 1)", bool_value=True),
            ops.BranchSpec(label="1. Mahnung versenden", bool_value=False),
        ],
    )
    verbuchen_1 = _nid(s, "Zahlung verbuchen (Stufe 1)")
    mahnung_1 = _nid(s, "1. Mahnung versenden")
    s = ops.set_form(
        s,
        mahnung_1,
        title="1. Mahnung",
        fields=[
            ops.FormFieldSpec(
                element_id="mahnung_1_am", widget=WidgetKind.DATE, label="1. Mahnung versendet am"
            )
        ],
    )

    # --- Stufe 2 (im "nicht bezahlt"-Zweig der Stufe 1) -------------------
    s = ops.serial_insert(
        s, SYSTEM_PREFIX + "Zahlungseingang prüfen (Stufe 2)", after_node_id=mahnung_1
    )
    pruef_2 = _nid(s, SYSTEM_PREFIX + "Zahlungseingang prüfen (Stufe 2)")
    s = _system_form(s, pruef_2, "Zahlungseingang (Stufe 2)", [], confirm_element="zahlung_2")
    s = ops.conditional_insert(
        s,
        after_node_id=pruef_2,
        discriminator="zahlung_2",
        branches=[
            ops.BranchSpec(label="Zahlung verbuchen (Stufe 2)", bool_value=True),
            ops.BranchSpec(label="2. Mahnung mit Fristsetzung", bool_value=False),
        ],
    )
    verbuchen_2 = _nid(s, "Zahlung verbuchen (Stufe 2)")
    mahnung_2 = _nid(s, "2. Mahnung mit Fristsetzung")
    s = ops.set_form(
        s,
        mahnung_2,
        title="2. Mahnung mit Fristsetzung",
        fields=[
            ops.FormFieldSpec(
                element_id="mahnung_2_am", widget=WidgetKind.DATE, label="2. Mahnung versendet am"
            )
        ],
    )

    # --- Stufe 3 (im "nicht bezahlt"-Zweig der Stufe 2) -------------------
    s = ops.serial_insert(
        s, SYSTEM_PREFIX + "Zahlungseingang prüfen (Stufe 3)", after_node_id=mahnung_2
    )
    pruef_3 = _nid(s, SYSTEM_PREFIX + "Zahlungseingang prüfen (Stufe 3)")
    s = _system_form(s, pruef_3, "Zahlungseingang (Stufe 3)", [], confirm_element="zahlung_3")
    s = ops.conditional_insert(
        s,
        after_node_id=pruef_3,
        discriminator="zahlung_3",
        branches=[
            ops.BranchSpec(label="Zahlung verbuchen (Stufe 3)", bool_value=True),
            ops.BranchSpec(label="Inkasso beauftragen", bool_value=False),
        ],
    )
    verbuchen_3 = _nid(s, "Zahlung verbuchen (Stufe 3)")
    inkasso = _nid(s, "Inkasso beauftragen")

    # Jeder Ausstiegspunkt schreibt denselben Ausgang -- damit ist er nach allen
    # drei geschachtelten Joins garantiert gesetzt (D1).
    for node_id, title, options in [
        (
            verbuchen_1,
            "Zahlung verbuchen (nach Erinnerung)",
            ("Bezahlt nach Erinnerung", "Teilzahlung nach Erinnerung"),
        ),
        (
            verbuchen_2,
            "Zahlung verbuchen (nach 1. Mahnung)",
            ("Bezahlt nach 1. Mahnung", "Teilzahlung nach 1. Mahnung"),
        ),
        (
            verbuchen_3,
            "Zahlung verbuchen (nach 2. Mahnung)",
            ("Bezahlt nach 2. Mahnung", "Teilzahlung nach 2. Mahnung"),
        ),
        (inkasso, "Inkasso beauftragen", ("An Inkasso übergeben", "Ausgebucht")),
    ]:
        s = ops.set_form(
            s,
            node_id,
            title=title,
            fields=[
                ops.FormFieldSpec(
                    element_id="offener_betrag",
                    widget=WidgetKind.NUMBER,
                    label="Offener Betrag (EUR)",
                    mode=AccessMode.READ,
                ),
                ops.FormFieldSpec(
                    element_id="ausgang",
                    widget=WidgetKind.DROPDOWN,
                    label="Ausgang",
                    options=options,
                ),
            ],
        )

    join_3 = _succ(s, verbuchen_3)
    join_2 = _succ(s, join_3)
    join_1 = _succ(s, join_2)
    s = ops.serial_insert(s, "Forderungsvorgang abschließen", after_node_id=join_1)
    abschluss = _nid(s, "Forderungsvorgang abschließen")
    s = ops.set_form(
        s,
        abschluss,
        title="Forderungsvorgang abschließen",
        fields=[
            ops.FormFieldSpec(
                element_id="ausgang", widget=WidgetKind.TEXT, label="Ausgang", mode=AccessMode.READ
            ),
            ops.FormFieldSpec(
                element_id="abschluss_vermerk",
                widget=WidgetKind.TEXTAREA,
                label="Abschlussvermerk",
            ),
        ],
    )

    s = ops.link_org_model(s, ORG_ID, org)
    for node_id in (
        akte,
        erinnerung,
        pruef_1,
        pruef_2,
        pruef_3,
        mahnung_1,
        mahnung_2,
        verbuchen_1,
        verbuchen_2,
        verbuchen_3,
        abschluss,
    ):
        s = ops.assign_staff_rule(s, node_id, _role("buchhaltung"))
    s = ops.assign_staff_rule(s, pruef_1, _role("systemdienst"))
    s = ops.assign_staff_rule(s, pruef_2, _role("systemdienst"))
    s = ops.assign_staff_rule(s, pruef_3, _role("systemdienst"))
    s = ops.assign_staff_rule(s, inkasso, _role("geschaeftsfuehrung"))

    for node_id, subject in [
        (erinnerung, "Zahlungserinnerung zu Rechnung {rechnungs_nr}"),
        (mahnung_1, "1. Mahnung zu Rechnung {rechnungs_nr}"),
        (mahnung_2, "2. Mahnung zu Rechnung {rechnungs_nr}"),
    ]:
        s = ops.set_mail_binding(
            s,
            node_id,
            MailBinding(
                mode=MailRecipientMode.TO_GROUP_MAILBOX,
                subject=subject,
                body=(
                    "Zu Rechnung {rechnungs_nr} des Kunden {kunde} sind "
                    "{offener_betrag} EUR offen (fällig war der {faellig_am})."
                ),
            ),
        )
    s = ops.set_mail_binding(
        s,
        inkasso,
        MailBinding(
            mode=MailRecipientMode.TO_ELIGIBLE_AGENTS,
            subject="Inkasso-Freigabe nötig: {rechnungs_nr}",
            body=(
                "Nach drei Stufen sind {offener_betrag} EUR des Kunden {kunde} "
                "weiterhin offen. Bitte über die Übergabe an das Inkasso "
                "entscheiden."
            ),
        ),
    )

    # Das gesamte Mahnwesen ist Blindleistung -- genau das soll die
    # Wertklassen-Auswertung zeigen.
    for node_id in (akte, erinnerung, pruef_1, pruef_2, pruef_3, mahnung_1, mahnung_2, inkasso):
        s = ops.set_value_class(s, node_id, ValueClass.NON_VALUE_ADDING)
    for node_id in (verbuchen_1, verbuchen_2, verbuchen_3, abschluss):
        s = ops.set_value_class(s, node_id, ValueClass.BUSINESS_NECESSARY)
    for node_id in (mahnung_1, mahnung_2, inkasso):
        s = ops.set_node_priority(
            s, node_id, WorkItemPriority(impact=ImpactUrgency.HIGH, urgency=ImpactUrgency.HIGH)
        )

    s = ops.set_time_constraint(s, akte, TimeConstraint(max_duration_seconds=3600))
    s = ops.set_time_constraint(
        s, erinnerung, TimeConstraint(max_duration_seconds=3600, target_lead_seconds=3600)
    )
    # Die Wartezeit auf den Zahlungseingang steckt in den Pruefschritten.
    s = ops.set_time_constraint(s, pruef_1, TimeConstraint(max_duration_seconds=864000))
    s = ops.set_time_constraint(s, pruef_2, TimeConstraint(max_duration_seconds=864000))
    s = ops.set_time_constraint(s, pruef_3, TimeConstraint(max_duration_seconds=1209600))
    s = ops.set_time_constraint(s, mahnung_1, TimeConstraint(max_duration_seconds=3600))
    s = ops.set_time_constraint(s, mahnung_2, TimeConstraint(max_duration_seconds=3600))
    for node_id in (verbuchen_1, verbuchen_2, verbuchen_3):
        s = ops.set_time_constraint(s, node_id, TimeConstraint(max_duration_seconds=1800))
    s = ops.set_time_constraint(s, inkasso, TimeConstraint(max_duration_seconds=7200))
    s = ops.set_time_constraint(s, abschluss, TimeConstraint(max_duration_seconds=3600))
    s = ops.set_deadline(s, 45 * 86400)
    return ops.release(s)


# --- Folgeprozess: Retoure und Gutschrift ---------------------------------


def _build_retoure(org: OrgModel) -> ProcessSchema:
    """Folgeprozess: Reklamation aufnehmen, Ware pruefen, Gutschrift oder Absage."""

    s = ops.create_empty_schema("Retoure und Gutschrift", schema_id=SCHEMA_RETOURE)
    s = ops.add_data_element(s, "Kunde", DataType.STRING, element_id="kunde")
    s = ops.add_data_element(s, "Auftragsnummer", DataType.STRING, element_id="auftrags_nr")
    s = ops.add_data_element(s, "Rechnungsnummer", DataType.STRING, element_id="rechnungs_nr")
    s = ops.add_data_element(
        s, "Reklamationsgrund", DataType.STRING, element_id="reklamationsgrund"
    )
    s = ops.add_data_element(s, "Retourenmenge", DataType.INTEGER, element_id="retoure_menge")
    s = ops.add_data_element(s, "Ware in Ordnung", DataType.BOOLEAN, element_id="ware_ok")
    s = ops.add_data_element(s, "Prüfvermerk", DataType.STRING, element_id="pruef_vermerk")
    s = ops.add_data_element(
        s, "Gutschriftbetrag (EUR)", DataType.FLOAT, element_id="gutschrift_betrag"
    )
    s = ops.add_data_element(s, "Gutschriftnummer", DataType.STRING, element_id="gutschrift_nr")
    s = ops.add_data_element(s, "Ergebnis", DataType.STRING, element_id="retoure_ergebnis")
    s = ops.add_data_element(s, "Abschlussnotiz", DataType.STRING, element_id="retoure_notiz")

    s = ops.serial_insert(s, "Reklamation aufnehmen", after_node_id="start")
    aufnehmen = _nid(s, "Reklamation aufnehmen")
    s = ops.set_form(
        s,
        aufnehmen,
        title="Reklamation aufnehmen",
        fields=[
            ops.FormFieldSpec(element_id="kunde", widget=WidgetKind.TEXT, label="Kunde"),
            ops.FormFieldSpec(
                element_id="auftrags_nr", widget=WidgetKind.TEXT, label="Auftragsnummer"
            ),
            ops.FormFieldSpec(
                element_id="rechnungs_nr", widget=WidgetKind.TEXT, label="Rechnungsnummer"
            ),
            ops.FormFieldSpec(
                element_id="reklamationsgrund",
                widget=WidgetKind.TEXTAREA,
                label="Reklamationsgrund",
            ),
            ops.FormFieldSpec(
                element_id="retoure_menge", widget=WidgetKind.NUMBER, label="Retourenmenge"
            ),
        ],
    )

    s = ops.parallel_insert(
        s, ["Ware zurücknehmen und prüfen", "Kaufmännische Bewertung"], after_node_id=aufnehmen
    )
    pruefen = _nid(s, "Ware zurücknehmen und prüfen")
    bewerten = _nid(s, "Kaufmännische Bewertung")
    s = ops.set_form(
        s,
        pruefen,
        title="Warenprüfung",
        fields=[
            ops.FormFieldSpec(
                element_id="retoure_menge",
                widget=WidgetKind.NUMBER,
                label="Retourenmenge",
                mode=AccessMode.READ,
            ),
            ops.FormFieldSpec(
                element_id="ware_ok",
                widget=WidgetKind.CHECKBOX,
                label="Ware in Ordnung (Gutschrift möglich)",
                help_text="Steuert die Verzweigung: nur eine einwandfreie Rücknahme "
                "führt zur Gutschrift.",
            ),
            ops.FormFieldSpec(
                element_id="pruef_vermerk", widget=WidgetKind.TEXTAREA, label="Prüfvermerk"
            ),
        ],
    )
    s = ops.set_form(
        s,
        bewerten,
        title="Kaufmännische Bewertung",
        fields=[
            ops.FormFieldSpec(
                element_id="gutschrift_betrag",
                widget=WidgetKind.NUMBER,
                label="Gutschriftbetrag (EUR)",
            )
        ],
    )

    join = _succ(s, pruefen)
    s = ops.conditional_insert(
        s,
        after_node_id=join,
        discriminator="ware_ok",
        branches=[
            ops.BranchSpec(label="Gutschrift erstellen und versenden", bool_value=True),
            ops.BranchSpec(label="Reklamation zurückweisen", bool_value=False),
        ],
    )
    gutschrift = _nid(s, "Gutschrift erstellen und versenden")
    zurueckweisen = _nid(s, "Reklamation zurückweisen")
    s = ops.set_form(
        s,
        gutschrift,
        title="Gutschrift erstellen",
        fields=[
            ops.FormFieldSpec(
                element_id="gutschrift_betrag",
                widget=WidgetKind.NUMBER,
                label="Gutschriftbetrag (EUR)",
                mode=AccessMode.READ,
            ),
            ops.FormFieldSpec(
                element_id="gutschrift_nr", widget=WidgetKind.TEXT, label="Gutschriftnummer"
            ),
            ops.FormFieldSpec(
                element_id="retoure_ergebnis",
                widget=WidgetKind.DROPDOWN,
                label="Ergebnis",
                options=("Gutschrift erteilt", "Teilgutschrift erteilt"),
            ),
        ],
    )
    s = ops.set_form(
        s,
        zurueckweisen,
        title="Reklamation zurückweisen",
        fields=[
            ops.FormFieldSpec(
                element_id="pruef_vermerk",
                widget=WidgetKind.TEXTAREA,
                label="Prüfvermerk",
                mode=AccessMode.READ,
            ),
            ops.FormFieldSpec(
                element_id="retoure_ergebnis",
                widget=WidgetKind.DROPDOWN,
                label="Ergebnis",
                options=("Reklamation zurückgewiesen", "Kulanz ohne Gutschrift"),
            ),
        ],
    )

    xor_join = _succ(s, gutschrift)
    s = ops.serial_insert(s, "Retourenvorgang abschließen", after_node_id=xor_join)
    abschluss = _nid(s, "Retourenvorgang abschließen")
    s = ops.set_form(
        s,
        abschluss,
        title="Retourenvorgang abschließen",
        fields=[
            ops.FormFieldSpec(
                element_id="retoure_ergebnis",
                widget=WidgetKind.TEXT,
                label="Ergebnis",
                mode=AccessMode.READ,
            ),
            ops.FormFieldSpec(
                element_id="retoure_notiz", widget=WidgetKind.TEXTAREA, label="Abschlussnotiz"
            ),
        ],
    )

    s = ops.link_org_model(s, ORG_ID, org)
    s = ops.assign_staff_rule(s, aufnehmen, _role("innendienst"))
    s = ops.assign_staff_rule(s, pruefen, _role("lager"))
    s = ops.assign_staff_rule(s, bewerten, _role("buchhaltung"))
    s = ops.assign_staff_rule(s, gutschrift, _role("buchhaltung"))
    s = ops.assign_staff_rule(s, zurueckweisen, _role("innendienst"))
    s = ops.assign_staff_rule(s, abschluss, _role("innendienst"))

    s = ops.set_mail_binding(
        s,
        gutschrift,
        MailBinding(
            mode=MailRecipientMode.TO_GROUP_MAILBOX,
            subject="Gutschrift zu Auftrag {auftrags_nr}",
            body="Für {kunde} wurde eine Gutschrift über {gutschrift_betrag} EUR erstellt.",
        ),
    )

    s = ops.set_value_class(s, aufnehmen, ValueClass.NON_VALUE_ADDING)
    s = ops.set_value_class(s, pruefen, ValueClass.NON_VALUE_ADDING)
    s = ops.set_value_class(s, bewerten, ValueClass.NON_VALUE_ADDING)
    s = ops.set_value_class(s, gutschrift, ValueClass.BUSINESS_NECESSARY)
    s = ops.set_value_class(s, zurueckweisen, ValueClass.NON_VALUE_ADDING)
    s = ops.set_value_class(s, abschluss, ValueClass.BUSINESS_NECESSARY)

    s = ops.set_time_constraint(
        s, aufnehmen, TimeConstraint(max_duration_seconds=3600, target_lead_seconds=3600)
    )
    s = ops.set_time_constraint(s, pruefen, TimeConstraint(max_duration_seconds=7200))
    s = ops.set_time_constraint(s, bewerten, TimeConstraint(max_duration_seconds=3600))
    s = ops.set_time_constraint(s, gutschrift, TimeConstraint(max_duration_seconds=7200))
    s = ops.set_time_constraint(s, zurueckweisen, TimeConstraint(max_duration_seconds=3600))
    s = ops.set_time_constraint(s, abschluss, TimeConstraint(max_duration_seconds=1800))
    s = ops.set_deadline(s, 7 * 86400)
    return ops.release(s)


# --- Hauptprozess: Auftragsabwicklung (Order-to-Cash) ---------------------


def _build_main(org: OrgModel, resolver: SchemaResolver) -> ProcessSchema:
    """Der Hauptprozess: von der Kundenanfrage bis zum Zahlungseingang.

    Aufbau (Verzweigungen jeweils mit ihrem Diskriminator)::

        Kundenanfrage erfassen
        AND: Preis kalkulieren | Verfuegbarkeit vorab klaeren
        SUB: Bonitaets- und Kreditpruefung
        Angebot erstellen und versenden
        Kundenrückmeldung erfassen                  -> angebot_status
        XOR angebot_status (ENUM)
          "Angenommen"      -> Auftrag anlegen       -> auftrags_nr, verfuegbarkeit
                               XOR auftragswert (THRESHOLD)  Wertgrenzen-Freigabe
                               XOR verfuegbarkeit (ENUM)     Beschaffungsweg
                               AND: Kommissionieren | Versandpapiere
                               SUB: Versand und Zustellung
                               SUB: Fakturierung
                               Zahlungseingang pruefen -> zahlungseingang
                               XOR zahlungseingang (BOOLEAN)
          "Nachverhandlung" -> Angebot nachverhandeln
          sonst             -> Absage dokumentieren
        Vorgang abschliessen und archivieren

    Drei Modellierungsentscheidungen, die bewusst so und nicht anders fallen:

    * **Frueher Abbruch durch Verschachtelung.** Wird das Angebot abgelehnt, ist
      der Vorgang beendet. Ein block-strukturiertes Modell kennt keinen Sprung
      zum Ende, also liegt die gesamte weitere Abwicklung *im* Zweig
      "Angenommen". Das ist keine Notloesung, sondern die ehrliche Aussage: es
      gibt keinen Auftrag, dessen Rest man ueberspringen muesste.
    * **Der Schwellwert-Split routet die Zustaendigkeit, nicht das Ergebnis.**
      ``auftragswert`` entscheidet, *wer* freigibt (Wertgrenzen der
      Unterschriftenregelung) -- alle drei Zweige tun dasselbe, nur mit anderer
      Bearbeiterzuordnung. Kein Auftrag wird abgelehnt, *weil* er gross ist; die
      Kundenentscheidung haengt an ``angebot_status``, dem Ergebnis eines
      eigenen Schrittes davor.
    * **``vorgangsstatus`` wird auf jedem Zweig geschrieben** und ist deshalb
      nach dem aeusseren Join garantiert gesetzt (D1). Alles, was nur im Zweig
      "Angenommen" entsteht (Rechnungsnummer, offener Betrag), liest der
      Abschlussschritt dagegen unverbindlich -- auf dem Absage-Pfad gibt es
      schlicht keine Rechnung.
    """

    s = ops.create_empty_schema("Auftragsabwicklung (Order-to-Cash)", schema_id=SCHEMA_MAIN)

    # --- Datenelemente ----------------------------------------------------
    for name, dtype, eid in [
        ("Kunde", DataType.STRING, "kunde"),
        ("Kundennummer", DataType.INTEGER, "kunden_nr"),
        ("Anfragedatum", DataType.DATE, "anfrage_datum"),
        ("Artikel / Leistung", DataType.STRING, "artikel"),
        ("Menge", DataType.INTEGER, "menge"),
        ("Wunschtermin", DataType.DATE, "wunschtermin"),
        ("Lieferadresse", DataType.STRING, "lieferadresse"),
        ("Einzelpreis (EUR)", DataType.FLOAT, "einzelpreis"),
        ("Auftragswert (EUR)", DataType.FLOAT, "auftragswert"),
        ("Rabatt (%)", DataType.FLOAT, "rabatt"),
        ("Zahlungsziel (Tage)", DataType.INTEGER, "zahlungsziel"),
        ("Lagerbestand", DataType.INTEGER, "lager_bestand"),
        ("Kreditlimit (EUR)", DataType.FLOAT, "kreditlimit"),
        ("Kreditfreigabe erteilt", DataType.BOOLEAN, "kreditfreigabe"),
        ("Zahlungsart", DataType.STRING, "zahlungsart"),
        ("Angebotsnummer", DataType.STRING, "angebots_nr"),
        ("Kundenentscheidung", DataType.STRING, "angebot_status"),
        ("Auftragsnummer", DataType.STRING, "auftrags_nr"),
        ("Beschaffungsweg", DataType.STRING, "verfuegbarkeit"),
        ("Freigabevermerk", DataType.STRING, "freigabe_vermerk"),
        ("Bestätigter Liefertermin", DataType.DATE, "liefertermin"),
        ("Kommissionierung vollständig", DataType.BOOLEAN, "kommission_ok"),
        ("Versandpapiere erstellt", DataType.BOOLEAN, "versandpapiere_ok"),
        ("Lieferscheinnummer", DataType.STRING, "lieferschein_nr"),
        ("Sendungsverfolgung", DataType.URI, "tracking_url"),
        ("Zustelldatum", DataType.DATE, "zustellung_datum"),
        ("Rechnungsnummer", DataType.STRING, "rechnungs_nr"),
        ("Rechnungsbetrag (EUR)", DataType.FLOAT, "rechnungsbetrag"),
        ("Fällig am", DataType.DATE, "faellig_am"),
        ("Zahlung eingegangen", DataType.BOOLEAN, "zahlungseingang"),
        ("Gezahlter Betrag (EUR)", DataType.FLOAT, "zahlbetrag"),
        ("Offener Betrag (EUR)", DataType.FLOAT, "offener_betrag"),
        ("Vorgangsstatus", DataType.STRING, "vorgangsstatus"),
        ("Offene Forderung", DataType.BOOLEAN, "forderung_offen"),
        ("Reklamation gemeldet", DataType.BOOLEAN, "reklamation"),
        ("Abschlussnotiz", DataType.STRING, "abschluss_notiz"),
    ]:
        s = ops.add_data_element(s, name, dtype, element_id=eid)

    # --- Anfrage ----------------------------------------------------------
    s = ops.serial_insert(s, "Kundenanfrage erfassen", after_node_id="start")
    anfrage = _nid(s, "Kundenanfrage erfassen")
    s = ops.set_form(
        s,
        anfrage,
        title="Kundenanfrage erfassen",
        fields=[
            ops.FormFieldSpec(element_id="kunde", widget=WidgetKind.TEXT, label="Kunde"),
            ops.FormFieldSpec(
                element_id="kunden_nr", widget=WidgetKind.NUMBER, label="Kundennummer"
            ),
            ops.FormFieldSpec(
                element_id="anfrage_datum", widget=WidgetKind.DATE, label="Anfragedatum"
            ),
            ops.FormFieldSpec(
                element_id="artikel", widget=WidgetKind.TEXT, label="Artikel / Leistung"
            ),
            ops.FormFieldSpec(element_id="menge", widget=WidgetKind.NUMBER, label="Menge"),
            ops.FormFieldSpec(
                element_id="wunschtermin", widget=WidgetKind.DATE, label="Wunschtermin"
            ),
            ops.FormFieldSpec(
                element_id="lieferadresse", widget=WidgetKind.TEXTAREA, label="Lieferadresse"
            ),
        ],
    )

    # --- Kalkulation und Bestandsklaerung (parallel) ----------------------
    s = ops.parallel_insert(
        s, ["Preis und Konditionen kalkulieren", "Verfügbarkeit vorab klären"], anfrage
    )
    kalkulation = _nid(s, "Preis und Konditionen kalkulieren")
    bestand = _nid(s, "Verfügbarkeit vorab klären")
    s = ops.set_form(
        s,
        kalkulation,
        title="Preis und Konditionen",
        fields=[
            ops.FormFieldSpec(
                element_id="menge", widget=WidgetKind.NUMBER, label="Menge", mode=AccessMode.READ
            ),
            ops.FormFieldSpec(
                element_id="einzelpreis", widget=WidgetKind.NUMBER, label="Einzelpreis (EUR)"
            ),
            ops.FormFieldSpec(
                element_id="auftragswert", widget=WidgetKind.NUMBER, label="Auftragswert (EUR)"
            ),
            ops.FormFieldSpec(
                element_id="rabatt", widget=WidgetKind.NUMBER, label="Rabatt (%)", required=False
            ),
            ops.FormFieldSpec(
                element_id="zahlungsziel", widget=WidgetKind.NUMBER, label="Zahlungsziel (Tage)"
            ),
        ],
    )
    s = ops.set_form(
        s,
        bestand,
        title="Verfügbarkeit vorab klären",
        fields=[
            ops.FormFieldSpec(
                element_id="artikel",
                widget=WidgetKind.TEXT,
                label="Artikel",
                mode=AccessMode.READ,
            ),
            ops.FormFieldSpec(
                element_id="lager_bestand",
                widget=WidgetKind.NUMBER,
                label="Verfügbarer Lagerbestand",
                help_text="Entscheidungsgrundlage für den späteren Beschaffungsweg.",
            ),
        ],
    )
    and_join = _succ(s, kalkulation)

    # --- Teilprozess Bonitaet --------------------------------------------
    # Sequenziell *nach* der Kalkulation, nicht parallel dazu: die Kreditpruefung
    # braucht den Auftragswert, und ein Eingang, der beim Start des Kindes noch
    # nicht geschrieben ist, kaeme leer an.
    s = ops.insert_subprocess(
        s,
        after_node_id=and_join,
        target_schema_id=SCHEMA_BONITAET,
        target_version=1,
        label="Bonitäts- und Kreditprüfung",
        input_mapping={"kunden_nr": "kunden_nr", "auftragswert": "auftragswert"},
        output_mapping={
            "kreditlimit": "kreditlimit",
            "kreditfreigabe": "kreditfreigabe",
            "zahlungsart": "zahlungsart",
        },
        resolver=resolver,
    )
    bonitaet = _nid(s, "Bonitäts- und Kreditprüfung")

    # --- Angebot ----------------------------------------------------------
    s = ops.serial_insert(s, "Angebot erstellen und versenden", after_node_id=bonitaet)
    angebot = _nid(s, "Angebot erstellen und versenden")
    s = ops.set_form(
        s,
        angebot,
        title="Angebot erstellen",
        fields=[
            ops.FormFieldSpec(
                element_id="auftragswert",
                widget=WidgetKind.NUMBER,
                label="Auftragswert (EUR)",
                mode=AccessMode.READ,
            ),
            ops.FormFieldSpec(
                element_id="kreditfreigabe",
                widget=WidgetKind.CHECKBOX,
                label="Kreditfreigabe erteilt",
                mode=AccessMode.READ,
                help_text="Ergebnis der Kreditprüfung -- hier nur zur Ansicht.",
            ),
            ops.FormFieldSpec(
                element_id="zahlungsart",
                widget=WidgetKind.TEXT,
                label="Zahlungsart",
                mode=AccessMode.READ,
            ),
            ops.FormFieldSpec(
                element_id="angebots_nr", widget=WidgetKind.TEXT, label="Angebotsnummer"
            ),
        ],
    )

    s = ops.serial_insert(s, "Kundenrückmeldung erfassen", after_node_id=angebot)
    rueckmeldung = _nid(s, "Kundenrückmeldung erfassen")
    s = ops.set_form(
        s,
        rueckmeldung,
        title="Kundenrückmeldung erfassen",
        fields=[
            ops.FormFieldSpec(
                element_id="angebots_nr",
                widget=WidgetKind.TEXT,
                label="Angebotsnummer",
                mode=AccessMode.READ,
            ),
            ops.FormFieldSpec(
                element_id="angebot_status",
                widget=WidgetKind.DROPDOWN,
                label="Entscheidung des Kunden",
                options=("Angenommen", "Nachverhandlung", "Abgelehnt"),
                help_text="Steuert die Verzweigung: nur „Angenommen“ führt "
                "in die Auftragsabwicklung.",
            ),
        ],
    )

    # --- Kundenentscheidung (aeusserer XOR) -------------------------------
    s = ops.conditional_insert(
        s,
        after_node_id=rueckmeldung,
        discriminator="angebot_status",
        branches=[
            ops.BranchSpec(label="Auftrag anlegen und bestätigen", values=("Angenommen",)),
            ops.BranchSpec(label="Angebot nachverhandeln", values=("Nachverhandlung",)),
            ops.BranchSpec(label="Absage dokumentieren", is_else=True),
        ],
    )
    auftrag = _nid(s, "Auftrag anlegen und bestätigen")
    nachverhandeln = _nid(s, "Angebot nachverhandeln")
    absage = _nid(s, "Absage dokumentieren")
    s = ops.set_form(
        s,
        auftrag,
        title="Auftrag anlegen und bestätigen",
        fields=[
            ops.FormFieldSpec(
                element_id="lager_bestand",
                widget=WidgetKind.NUMBER,
                label="Verfügbarer Lagerbestand",
                mode=AccessMode.READ,
                help_text="Entscheidungsgrundlage für den Beschaffungsweg.",
            ),
            ops.FormFieldSpec(
                element_id="menge", widget=WidgetKind.NUMBER, label="Menge", mode=AccessMode.READ
            ),
            ops.FormFieldSpec(
                element_id="auftrags_nr", widget=WidgetKind.TEXT, label="Auftragsnummer"
            ),
            ops.FormFieldSpec(
                element_id="verfuegbarkeit",
                widget=WidgetKind.DROPDOWN,
                label="Beschaffungsweg",
                options=("Ab Lager", "Fertigung", "Zukauf"),
                help_text="Entscheidung des Innendienstes -- steuert die Beschaffung.",
            ),
        ],
    )

    # --- Wertgrenzen-Freigabe (THRESHOLD) ---------------------------------
    s = ops.conditional_insert(
        s,
        after_node_id=auftrag,
        discriminator="auftragswert",
        branches=[
            ops.BranchSpec(label="Freigabe durch Innendienst", upper=5000),
            ops.BranchSpec(label="Freigabe durch Vertriebsleitung", upper=25000),
            ops.BranchSpec(label="Freigabe durch Geschäftsführung"),
        ],
    )
    frei_id = _nid(s, "Freigabe durch Innendienst")
    frei_vl = _nid(s, "Freigabe durch Vertriebsleitung")
    frei_gf = _nid(s, "Freigabe durch Geschäftsführung")
    for node_id, title in [
        (frei_id, "Freigabe (bis 5.000 EUR)"),
        (frei_vl, "Freigabe (bis 25.000 EUR)"),
        (frei_gf, "Freigabe (über 25.000 EUR)"),
    ]:
        s = ops.set_form(
            s,
            node_id,
            title=title,
            fields=[
                ops.FormFieldSpec(
                    element_id="auftragswert",
                    widget=WidgetKind.NUMBER,
                    label="Auftragswert (EUR)",
                    mode=AccessMode.READ,
                ),
                ops.FormFieldSpec(
                    element_id="kreditlimit",
                    widget=WidgetKind.NUMBER,
                    label="Kreditlimit (EUR)",
                    mode=AccessMode.READ,
                ),
                ops.FormFieldSpec(
                    element_id="freigabe_vermerk",
                    widget=WidgetKind.TEXTAREA,
                    label="Freigabevermerk",
                ),
            ],
        )
    frei_join = _succ(s, frei_id)

    # --- Beschaffungsweg (ENUM) -------------------------------------------
    s = ops.conditional_insert(
        s,
        after_node_id=frei_join,
        discriminator="verfuegbarkeit",
        branches=[
            ops.BranchSpec(label="Ware reservieren", values=("Ab Lager",)),
            ops.BranchSpec(label="Fertigungsauftrag anlegen", values=("Fertigung",)),
            ops.BranchSpec(label="Zukauf beauftragen", is_else=True),
        ],
    )
    reservieren = _nid(s, "Ware reservieren")
    fertigung = _nid(s, "Fertigungsauftrag anlegen")
    zukauf = _nid(s, "Zukauf beauftragen")
    for node_id, title in [
        (reservieren, "Ware ab Lager reservieren"),
        (fertigung, "Fertigungsauftrag anlegen"),
        (zukauf, "Zukauf beauftragen"),
    ]:
        s = ops.set_form(
            s,
            node_id,
            title=title,
            fields=[
                ops.FormFieldSpec(
                    element_id="menge",
                    widget=WidgetKind.NUMBER,
                    label="Menge",
                    mode=AccessMode.READ,
                ),
                ops.FormFieldSpec(
                    element_id="wunschtermin",
                    widget=WidgetKind.DATE,
                    label="Wunschtermin",
                    mode=AccessMode.READ,
                ),
                ops.FormFieldSpec(
                    element_id="liefertermin",
                    widget=WidgetKind.DATE,
                    label="Bestätigter Liefertermin",
                ),
            ],
        )
    verf_join = _succ(s, reservieren)

    # --- Kommissionierung und Versandpapiere (parallel) -------------------
    s = ops.parallel_insert(s, ["Kommissionieren", "Versandpapiere erstellen"], verf_join)
    kommission = _nid(s, "Kommissionieren")
    papiere = _nid(s, "Versandpapiere erstellen")
    s = ops.set_form(
        s,
        kommission,
        title="Kommissionierung",
        fields=[
            ops.FormFieldSpec(
                element_id="menge", widget=WidgetKind.NUMBER, label="Menge", mode=AccessMode.READ
            ),
            ops.FormFieldSpec(
                element_id="kommission_ok",
                widget=WidgetKind.CHECKBOX,
                label="Kommissionierung vollständig",
            ),
        ],
    )
    s = ops.set_form(
        s,
        papiere,
        title="Versandpapiere",
        fields=[
            ops.FormFieldSpec(
                element_id="lieferadresse",
                widget=WidgetKind.TEXTAREA,
                label="Lieferadresse",
                mode=AccessMode.READ,
            ),
            ops.FormFieldSpec(
                element_id="versandpapiere_ok",
                widget=WidgetKind.CHECKBOX,
                label="Versandpapiere erstellt",
            ),
        ],
    )
    pack_join = _succ(s, kommission)

    # --- Teilprozesse Versand und Fakturierung ----------------------------
    s = ops.insert_subprocess(
        s,
        after_node_id=pack_join,
        target_schema_id=SCHEMA_VERSAND,
        target_version=1,
        label="Versand und Zustellung",
        input_mapping={"auftrags_nr": "auftrags_nr", "lieferadresse": "lieferadresse"},
        output_mapping={
            "lieferschein_nr": "lieferschein_nr",
            "tracking_url": "tracking_url",
            "zustellung_datum": "zustellung_datum",
        },
        resolver=resolver,
    )
    versand = _nid(s, "Versand und Zustellung")
    s = ops.insert_subprocess(
        s,
        after_node_id=versand,
        target_schema_id=SCHEMA_FAKTURA,
        target_version=1,
        label="Fakturierung",
        input_mapping={"auftrags_nr": "auftrags_nr", "auftragswert": "auftragswert"},
        output_mapping={
            "rechnungs_nr": "rechnungs_nr",
            "rechnungsbetrag": "rechnungsbetrag",
            "faellig_am": "faellig_am",
        },
        resolver=resolver,
    )
    faktura = _nid(s, "Fakturierung")

    # --- Zahlungseingang --------------------------------------------------
    s = ops.serial_insert(s, SYSTEM_PREFIX + "Zahlungseingang prüfen", after_node_id=faktura)
    zahlpruef = _nid(s, SYSTEM_PREFIX + "Zahlungseingang prüfen")
    s = ops.set_form(
        s,
        zahlpruef,
        title="Zahlungseingang prüfen",
        fields=[
            ops.FormFieldSpec(
                element_id="rechnungsbetrag",
                widget=WidgetKind.NUMBER,
                label="Rechnungsbetrag (EUR)",
                mode=AccessMode.READ,
            ),
            ops.FormFieldSpec(
                element_id="faellig_am",
                widget=WidgetKind.DATE,
                label="Fällig am",
                mode=AccessMode.READ,
            ),
            ops.FormFieldSpec(
                element_id="zahlungseingang",
                widget=WidgetKind.CHECKBOX,
                label="Zahlungseingang festgestellt",
                help_text=SYSTEM_HINT,
            ),
        ],
    )
    s = ops.conditional_insert(
        s,
        after_node_id=zahlpruef,
        discriminator="zahlungseingang",
        branches=[
            ops.BranchSpec(label="Zahlung verbuchen", bool_value=True),
            ops.BranchSpec(label="Offene Forderung dokumentieren", bool_value=False),
        ],
    )
    verbuchen = _nid(s, "Zahlung verbuchen")
    offen = _nid(s, "Offene Forderung dokumentieren")
    s = ops.set_form(
        s,
        verbuchen,
        title="Zahlung verbuchen",
        fields=[
            ops.FormFieldSpec(
                element_id="rechnungsbetrag",
                widget=WidgetKind.NUMBER,
                label="Rechnungsbetrag (EUR)",
                mode=AccessMode.READ,
            ),
            ops.FormFieldSpec(
                element_id="zahlbetrag", widget=WidgetKind.NUMBER, label="Gezahlter Betrag (EUR)"
            ),
            ops.FormFieldSpec(
                element_id="offener_betrag",
                widget=WidgetKind.NUMBER,
                label="Offener Betrag (EUR)",
                help_text="Bei vollständiger Zahlung 0.",
            ),
            ops.FormFieldSpec(
                element_id="vorgangsstatus",
                widget=WidgetKind.DROPDOWN,
                label="Vorgangsstatus",
                options=("Abgeschlossen - bezahlt", "Abgeschlossen - teilbezahlt"),
            ),
        ],
    )
    s = ops.set_form(
        s,
        offen,
        title="Offene Forderung dokumentieren",
        fields=[
            ops.FormFieldSpec(
                element_id="rechnungsbetrag",
                widget=WidgetKind.NUMBER,
                label="Rechnungsbetrag (EUR)",
                mode=AccessMode.READ,
            ),
            ops.FormFieldSpec(
                element_id="zahlbetrag",
                widget=WidgetKind.NUMBER,
                label="Gezahlter Betrag (EUR)",
                help_text="Ohne Zahlungseingang 0.",
            ),
            ops.FormFieldSpec(
                element_id="offener_betrag",
                widget=WidgetKind.NUMBER,
                label="Offener Betrag (EUR)",
            ),
            ops.FormFieldSpec(
                element_id="vorgangsstatus",
                widget=WidgetKind.DROPDOWN,
                label="Vorgangsstatus",
                options=("Offene Forderung", "Zahlungsziel verlängert"),
            ),
        ],
    )

    # --- die beiden uebrigen Zweige der Kundenentscheidung ----------------
    s = ops.set_form(
        s,
        nachverhandeln,
        title="Angebot nachverhandeln",
        fields=[
            ops.FormFieldSpec(
                element_id="auftragswert",
                widget=WidgetKind.NUMBER,
                label="Bisheriger Auftragswert (EUR)",
                mode=AccessMode.READ,
            ),
            ops.FormFieldSpec(
                element_id="vorgangsstatus",
                widget=WidgetKind.DROPDOWN,
                label="Vorgangsstatus",
                options=("Nachverhandlung läuft", "Angebot überarbeitet"),
            ),
        ],
    )
    s = ops.set_form(
        s,
        absage,
        title="Absage dokumentieren",
        fields=[
            ops.FormFieldSpec(
                element_id="angebot_status",
                widget=WidgetKind.TEXT,
                label="Kundenentscheidung",
                mode=AccessMode.READ,
            ),
            ops.FormFieldSpec(
                element_id="vorgangsstatus",
                widget=WidgetKind.DROPDOWN,
                label="Vorgangsstatus",
                options=("Angebot abgelehnt", "Kunde abgesprungen"),
            ),
        ],
    )

    # --- Abschluss --------------------------------------------------------
    outer_join = _succ(s, nachverhandeln)
    s = ops.serial_insert(s, "Vorgang abschließen und archivieren", after_node_id=outer_join)
    abschluss = _nid(s, "Vorgang abschließen und archivieren")
    s = ops.set_form(
        s,
        abschluss,
        title="Vorgang abschließen",
        fields=[
            ops.FormFieldSpec(
                element_id="vorgangsstatus",
                widget=WidgetKind.TEXT,
                label="Vorgangsstatus",
                mode=AccessMode.READ,
                help_text="Auf jedem Pfad gesetzt -- deshalb hier verbindlich lesbar.",
            ),
            ops.FormFieldSpec(
                element_id="offener_betrag",
                widget=WidgetKind.NUMBER,
                label="Offener Betrag (EUR)",
                mode=AccessMode.READ,
                required=False,
                help_text="Nur auf dem Auftragspfad vorhanden -- deshalb unverbindlich.",
            ),
            ops.FormFieldSpec(
                element_id="forderung_offen",
                widget=WidgetKind.CHECKBOX,
                label="Offene Forderung an das Forderungsmanagement übergeben",
                help_text="Startet den Folgeprozess Forderungsmanagement.",
            ),
            ops.FormFieldSpec(
                element_id="reklamation",
                widget=WidgetKind.CHECKBOX,
                label="Reklamation gemeldet",
                help_text="Startet den Folgeprozess Retoure und Gutschrift.",
            ),
            ops.FormFieldSpec(
                element_id="abschluss_notiz",
                widget=WidgetKind.TEXTAREA,
                label="Abschlussnotiz",
                required=False,
            ),
        ],
    )

    # --- Organisation und Bearbeiterzuordnung -----------------------------
    s = ops.link_org_model(s, ORG_ID, org)
    s = ops.assign_staff_rule(s, anfrage, _role("innendienst"))
    s = ops.assign_staff_rule(s, kalkulation, _role("innendienst"))
    s = ops.assign_staff_rule(s, bestand, _role("lager"))
    s = ops.assign_staff_rule(s, angebot, _role("innendienst"))
    s = ops.assign_staff_rule(s, rueckmeldung, _role("innendienst"))
    s = ops.assign_staff_rule(s, auftrag, _role("innendienst"))
    s = ops.assign_staff_rule(s, frei_id, _role("innendienst"))
    # Wertgrenze 2: die vorgesetzte Person der erfassenden Kraft gibt frei
    # (Z1-Z3, relativ zum Ausfuehrer von "Auftrag anlegen und bestaetigen").
    s = ops.assign_staff_rule(
        s,
        frei_vl,
        StaffRule(kind=StaffRuleKind.NODE_PERFORMING_AGENT_SUPERVISOR, ref=auftrag),
    )
    s = ops.assign_staff_rule(s, frei_gf, _role("geschaeftsfuehrung"))
    s = ops.assign_staff_rule(s, reservieren, _role("lager"))
    s = ops.assign_staff_rule(s, fertigung, _role("lager"))
    s = ops.assign_staff_rule(s, zukauf, _role("innendienst"))
    s = ops.assign_staff_rule(s, kommission, _role("lager"))
    s = ops.assign_staff_rule(s, papiere, _role("versand"))
    # Der Zahlungsabgleich darf von der Buchhaltung ODER vom (simulierten)
    # Systemdienst erledigt werden -- ein zusammengesetzter BZR-Baum.
    s = ops.assign_staff_rule(
        s,
        zahlpruef,
        StaffRule(
            kind=StaffRuleKind.OR,
            operands=[_role("buchhaltung"), _role("systemdienst")],
        ),
    )
    s = ops.assign_staff_rule(s, verbuchen, _role("buchhaltung"))
    s = ops.assign_staff_rule(s, offen, _role("buchhaltung"))
    s = ops.assign_staff_rule(s, nachverhandeln, _role("vertriebsleitung"))
    s = ops.assign_staff_rule(s, absage, _role("innendienst"))
    s = ops.assign_staff_rule(s, abschluss, _role("innendienst"))

    # --- Benachrichtigungen (N1-N4) ---------------------------------------
    s = ops.set_mail_binding(
        s,
        angebot,
        MailBinding(
            mode=MailRecipientMode.TO_GROUP_MAILBOX,
            subject="Angebot für {kunde} erstellen",
            body=(
                "Für {kunde} (Kundennummer {kunden_nr}) ist ein Angebot über "
                "{auftragswert} EUR zu erstellen."
            ),
        ),
    )
    s = ops.set_mail_binding(
        s,
        frei_gf,
        MailBinding(
            mode=MailRecipientMode.TO_ELIGIBLE_AGENTS,
            subject="Freigabe erforderlich: Auftrag {auftrags_nr} über {auftragswert} EUR",
            body=(
                "Der Auftrag {auftrags_nr} für {kunde} liegt mit {auftragswert} EUR "
                "über der Wertgrenze und braucht Ihre Freigabe."
            ),
        ),
    )

    # --- Wertklassen, Prioritaeten, Zeiten --------------------------------
    for node_id in (
        kalkulation,
        angebot,
        auftrag,
        reservieren,
        fertigung,
        zukauf,
        kommission,
        verbuchen,
    ):
        s = ops.set_value_class(s, node_id, ValueClass.VALUE_ADDING)
    for node_id in (
        anfrage,
        bestand,
        rueckmeldung,
        frei_id,
        frei_vl,
        frei_gf,
        papiere,
        zahlpruef,
        abschluss,
    ):
        s = ops.set_value_class(s, node_id, ValueClass.BUSINESS_NECESSARY)
    for node_id in (nachverhandeln, absage, offen):
        s = ops.set_value_class(s, node_id, ValueClass.NON_VALUE_ADDING)

    s = ops.set_node_priority(
        s, frei_gf, WorkItemPriority(impact=ImpactUrgency.HIGH, urgency=ImpactUrgency.HIGH)
    )
    s = ops.set_node_priority(
        s, frei_vl, WorkItemPriority(impact=ImpactUrgency.MEDIUM, urgency=ImpactUrgency.HIGH)
    )
    s = ops.set_node_priority(
        s, rueckmeldung, WorkItemPriority(impact=ImpactUrgency.HIGH, urgency=ImpactUrgency.MEDIUM)
    )
    s = ops.set_node_priority(
        s, zahlpruef, WorkItemPriority(impact=ImpactUrgency.MEDIUM, urgency=ImpactUrgency.MEDIUM)
    )

    # Die Fristen der drei Teilprozess-Knoten decken sich mit den Terminen der
    # Kind-Schemata: T2 rechnet einen SUBPROCESS flach mit seiner eigenen
    # Annotation und steigt nicht in das Kind ab.
    for node_id, seconds, lead in [
        (anfrage, 7200, 1800),
        (kalkulation, 14400, 7200),
        (bestand, 7200, 3600),
        (bonitaet, 86400, None),
        (angebot, 14400, 7200),
        (rueckmeldung, 864000, 86400),
        (auftrag, 14400, 7200),
        (frei_id, 3600, 3600),
        (frei_vl, 14400, 7200),
        (frei_gf, 172800, 28800),
        (reservieren, 3600, 3600),
        (fertigung, 864000, 86400),
        (zukauf, 259200, 28800),
        (kommission, 86400, 14400),
        (papiere, 7200, 3600),
        (versand, 259200, None),
        (faktura, 86400, None),
        (zahlpruef, 2592000, 172800),
        (verbuchen, 1800, 1800),
        (offen, 3600, 3600),
        (nachverhandeln, 172800, 28800),
        (absage, 3600, 3600),
        (abschluss, 14400, 7200),
    ]:
        s = ops.set_time_constraint(
            s, node_id, TimeConstraint(max_duration_seconds=seconds, target_lead_seconds=lead)
        )
    s = ops.set_deadline(s, 60 * 86400)

    # --- Folgeprozesse (F1-F4) --------------------------------------------
    # Beide Bedingungen lesen ein BOOLEAN, das der Abschlussschritt auf *jedem*
    # Pfad schreibt. Das ist kein Zufall: eine Bedingung ueber einem Element, das
    # nur auf einem Zweig entsteht, waere zur Laufzeit nicht auswertbar und
    # wuerde den Abschluss der Instanz blockieren.
    s = ops.link_follow_up(
        s,
        SCHEMA_FORDERUNG,
        target_version=1,
        trigger=FollowUpTrigger.CONDITIONAL,
        condition="forderung_offen == True",
        handover_mapping={
            "kunde": "kunde",
            "rechnungs_nr": "rechnungs_nr",
            "offener_betrag": "offener_betrag",
            "faellig_am": "faellig_am",
        },
        mode=FollowUpMode.ASYNC,
        resolver=resolver,
        link_id="followup-forderung",
    )
    s = ops.link_follow_up(
        s,
        SCHEMA_RETOURE,
        target_version=1,
        trigger=FollowUpTrigger.CONDITIONAL,
        condition="reklamation == True",
        handover_mapping={
            "kunde": "kunde",
            "auftrags_nr": "auftrags_nr",
            "rechnungs_nr": "rechnungs_nr",
        },
        mode=FollowUpMode.SYNC,
        resolver=resolver,
        link_id="followup-retoure",
    )
    return ops.release(s, resolver)


# --- Laufzeit: geseedete Instanzen ----------------------------------------


def _emit(
    audit: AuditLog,
    event_type: EventType,
    instance: ProcessInstance,
    *,
    node_id: str | None = None,
    label: str | None = None,
    agent_id: str | None = None,
) -> None:
    audit.append(
        event_type,
        instance.id,
        instance.schema_id,
        schema_version=instance.schema_version,
        node_id=node_id,
        label=label,
        agent_id=agent_id,
    )


class _Seeder:
    """Kleiner Fahrer fuer die geseedete Startlage.

    Buendelt Schema-Nachschlag, Ausfuehrungskontext und Audit-Log, damit ein
    Seed-Schritt so kurz ist wie der fachliche Satz dahinter::

        seeder.do("o2c-2026-004", "Auftrag anlegen und bestaetigen", "a-nadja",
                  {"auftrags_nr": "AB-2026-0104", "verfuegbarkeit": "Fertigung"})

    Alle Zustandsuebergaenge laufen ueber die echte Engine (``exe``), es wird
    nichts von Hand in eine Instanz geschrieben. Kind- und Folgeprozess-Instanzen
    entstehen dadurch von selbst; ihre ``INSTANCE_CREATED``-Ereignisse traegt der
    Seeder nach, weil sonst nur die von aussen gestarteten Instanzen im
    Monitoring auftauchen wuerden (die API tut an dieser Stelle dasselbe).
    """

    def __init__(
        self,
        schemas: dict[str, ProcessSchema],
        context: exe.ExecutionContext,
        audit: AuditLog,
    ) -> None:
        self.schemas = schemas
        self.ctx = context
        self.audit = audit
        self._known: set[str] = set()

    def schema_of(self, instance: ProcessInstance) -> ProcessSchema:
        return self.schemas[instance.schema_id]

    def get(self, instance_id: str) -> ProcessInstance:
        instance = self.ctx.instances.get(instance_id)
        if instance is None:  # pragma: no cover - Seed-Fehler faellt sofort auf
            raise RuntimeError(f"Instanz '{instance_id}' fehlt im Store")
        return instance

    def start(self, schema_id: str, instance_id: str) -> ProcessInstance:
        instance = exe.instantiate(
            self.schemas[schema_id], instance_id=instance_id, context=self.ctx
        )
        self._known.add(instance.id)
        _emit(self.audit, EventType.INSTANCE_CREATED, instance)
        self._track_new()
        return instance

    def child(self, instance_id: str, node_label: str) -> str:
        """Id der Kind-Instanz, die am Teilprozess-Knoten ``node_label`` haengt."""

        parent = self.get(instance_id)
        node_id = _nid(self.schema_of(parent), node_label)
        child_id = parent.child_instances.get(node_id)
        if child_id is None:  # pragma: no cover - Seed-Fehler faellt sofort auf
            raise RuntimeError(f"Teilprozess '{node_label}' von '{instance_id}' laeuft nicht")
        return child_id

    def do(
        self,
        instance_id: str,
        node_label: str,
        agent_id: str,
        data: dict[str, object] | None = None,
    ) -> ProcessInstance:
        """Schliesst einen Schritt ueber seine Bezeichnung ab (wie ein Bearbeiter)."""

        before = self.get(instance_id)
        schema = self.schema_of(before)
        node_id = _nid(schema, node_label)
        after = exe.complete_activity(
            before, schema, node_id, data, agent_id=agent_id, context=self.ctx
        )
        self.ctx.instances.put(after)
        _emit(
            self.audit,
            EventType.ACTIVITY_COMPLETED,
            after,
            node_id=node_id,
            label=node_label,
            agent_id=agent_id,
        )
        if after.state is InstanceState.COMPLETED:
            _emit(self.audit, EventType.INSTANCE_COMPLETED, after)
        self._track_new()
        return after

    def _track_new(self) -> None:
        """Traegt ``INSTANCE_CREATED`` fuer neu entstandene Instanzen nach.

        Kind-Instanzen eines Teilprozesses und die Instanzen bedingt
        ausgeloester Folgeprozesse entstehen in der Engine, nicht durch einen
        Aufruf von aussen -- ohne diesen Nachtrag fehlten sie im Audit-Log und
        damit in KPIs und Prozesslandkarte.
        """

        for instance_id in self.ctx.instances.list_ids():
            if instance_id in self._known:
                continue
            self._known.add(instance_id)
            _emit(self.audit, EventType.INSTANCE_CREATED, self.get(instance_id))


def _run_bonitaet(seeder: _Seeder, instance_id: str, score: int, limit: float) -> None:
    """Spielt den Teilprozess Bonitaet des Vorgangs komplett durch.

    Der Zweig ergibt sich aus dem Bonitaetsindex -- genau wie zur Laufzeit, der
    Seeder waehlt ihn nicht selbst aus.
    """

    child = seeder.child(instance_id, "Bonitäts- und Kreditprüfung")
    parent = seeder.get(instance_id)
    seeder.do(
        child,
        "Kundenstammdaten prüfen",
        "a-karin",
        {
            "kunden_nr": parent.data_values.get("kunden_nr"),
            "auftragswert": parent.data_values.get("auftragswert"),
            "stammdaten_ok": True,
        },
    )
    seeder.do(
        child,
        SYSTEM_PREFIX + "Wirtschaftsauskunft einholen",
        "a-automat",
        {"bonitaet_score": score, "auskunft_ok": True},
    )
    if score < 40:
        seeder.do(child, "Kreditentscheidung eskalieren", "a-gustav", {"kredit_vorschlag": limit})
    elif score < 80:
        seeder.do(child, "Kreditlimit manuell festlegen", "a-karin", {"kredit_vorschlag": limit})
    else:
        seeder.do(child, "Kreditlimit freigeben", "a-karin", {"kredit_vorschlag": limit})
    seeder.do(
        child,
        "Kreditentscheidung dokumentieren",
        "a-karin",
        {"kreditlimit": limit, "kreditfreigabe": True, "zahlungsart": "Rechnung"},
    )


def _run_versand(seeder: _Seeder, instance_id: str, lieferschein: str, zugestellt: str) -> None:
    """Spielt den Teilprozess Versand des Vorgangs komplett durch."""

    child = seeder.child(instance_id, "Versand und Zustellung")
    parent = seeder.get(instance_id)
    seeder.do(
        child,
        "Sendung übernehmen und verpacken",
        "a-lars",
        {
            "auftrags_nr": parent.data_values.get("auftrags_nr"),
            "lieferadresse": parent.data_values.get("lieferadresse"),
            "packstuecke": 3,
            "gewicht": 42.5,
        },
    )
    seeder.do(
        child,
        SYSTEM_PREFIX + "Frachtauftrag übergeben",
        "a-automat",
        {
            "lieferschein_nr": lieferschein,
            "tracking_url": f"https://tracking.example/{lieferschein}",
            "fracht_ok": True,
        },
    )
    seeder.do(child, "Sendung an Spediteur übergeben", "a-lars", {"uebergabe_ok": True})
    seeder.do(child, "Kunde über Versand informieren", "a-nadja", {"kunde_informiert": True})
    seeder.do(
        child,
        "Zustellung bestätigen",
        "a-lars",
        {"zustellung_datum": zugestellt, "zustellung_ok": True},
    )


def _run_faktura(seeder: _Seeder, instance_id: str, rechnung: str, faellig: str) -> None:
    """Spielt den Teilprozess Fakturierung des Vorgangs komplett durch."""

    child = seeder.child(instance_id, "Fakturierung")
    parent = seeder.get(instance_id)
    betrag = parent.data_values.get("auftragswert")
    seeder.do(
        child,
        SYSTEM_PREFIX + "Rechnung erzeugen",
        "a-automat",
        {
            "auftrags_nr": parent.data_values.get("auftrags_nr"),
            "auftragswert": betrag,
            "rechnungs_nr": rechnung,
            "rechnungsbetrag": betrag,
            "rechnung_ok": True,
        },
    )
    seeder.do(child, "Rechnung fachlich prüfen", "a-bianca", {"rechnung_geprueft": True})
    seeder.do(
        child,
        "Rechnung versenden",
        "a-bianca",
        {"versandweg": "E-Mail", "faellig_am": faellig},
    )


def _anfrage_daten(
    kunde: str, kunden_nr: int, artikel: str, menge: int, tage_zurueck: int
) -> dict[str, object]:
    """Maskenwerte fuer "Kundenanfrage erfassen" mit gleitendem Datum."""

    heute = datetime.now(UTC).date()
    return {
        "kunde": kunde,
        "kunden_nr": kunden_nr,
        "anfrage_datum": (heute - timedelta(days=tage_zurueck)).isoformat(),
        "artikel": artikel,
        "menge": menge,
        "wunschtermin": (heute + timedelta(days=21)).isoformat(),
        "lieferadresse": f"{kunde}\nIndustriestrasse 7\n89150 Laichingen",
    }


def _kalkulation(einzelpreis: float, menge: int) -> dict[str, object]:
    return {
        "einzelpreis": einzelpreis,
        "auftragswert": round(einzelpreis * menge, 2),
        "rabatt": 3.0,
        "zahlungsziel": 30,
    }


def _seed_instances(
    schemas: dict[str, ProcessSchema], instance_store: InstanceStore, audit: AuditLog
) -> None:
    """Acht Vorgaenge an unterschiedlichen Stellen des Wertstroms.

    Damit ist jede Sicht von der ersten Sekunde an gefuellt: Arbeitslisten fuer
    sechs Rollen, laufende Teilprozess-Instanzen, ein ausgeloester Folgeprozess,
    abgeschlossene Vorgaenge fuer KPIs und Prozesslandkarte -- und der
    Abbruchpfad. Wer eine bestimmte Stelle vorfuehren will, muss nicht erst
    zwanzig Schritte klicken.
    """

    ctx = exe.ExecutionContext(
        lambda schema_id, version: (
            None
            if (s := schemas.get(schema_id)) is None
            or (version is not None and s.version != version)
            else s
        ),
        instance_store,
    )
    seeder = _Seeder(schemas, ctx, audit)
    heute = datetime.now(UTC).date()

    # 1) Frisch gestartet -- wartet auf die Erfassung der Anfrage.
    seeder.start(SCHEMA_MAIN, "o2c-2026-001")

    # 2) Offener AND-Block: der Bestand ist geklaert, die Kalkulation nicht.
    seeder.start(SCHEMA_MAIN, "o2c-2026-002")
    seeder.do(
        "o2c-2026-002",
        "Kundenanfrage erfassen",
        "a-nadja",
        _anfrage_daten("Weber Maschinenbau GmbH", 10021, "Kugellager 6204-2RS", 400, 2),
    )
    seeder.do("o2c-2026-002", "Verfügbarkeit vorab klären", "a-lars", {"lager_bestand": 950})

    # 3) Der Teilprozess Bonitaet laeuft -- eine echte Kind-Instanz wartet.
    seeder.start(SCHEMA_MAIN, "o2c-2026-003")
    seeder.do(
        "o2c-2026-003",
        "Kundenanfrage erfassen",
        "a-nadja",
        _anfrage_daten("Süd-West Anlagenbau AG", 10044, "Hydraulikzylinder HZ-90", 12, 4),
    )
    seeder.do("o2c-2026-003", "Verfügbarkeit vorab klären", "a-lars", {"lager_bestand": 4})
    seeder.do(
        "o2c-2026-003", "Preis und Konditionen kalkulieren", "a-nadja", _kalkulation(740.0, 12)
    )

    # 4) Grossauftrag -- wartet auf die Freigabe der Geschaeftsfuehrung.
    seeder.start(SCHEMA_MAIN, "o2c-2026-004")
    seeder.do(
        "o2c-2026-004",
        "Kundenanfrage erfassen",
        "a-nadja",
        _anfrage_daten("Nordlicht Energietechnik GmbH", 10077, "Schaltschrank NX-40", 25, 9),
    )
    seeder.do("o2c-2026-004", "Verfügbarkeit vorab klären", "a-lars", {"lager_bestand": 0})
    seeder.do(
        "o2c-2026-004", "Preis und Konditionen kalkulieren", "a-nadja", _kalkulation(1940.0, 25)
    )
    _run_bonitaet(seeder, "o2c-2026-004", score=55, limit=60000.0)
    seeder.do(
        "o2c-2026-004",
        "Angebot erstellen und versenden",
        "a-nadja",
        {"angebots_nr": "AN-2026-0231"},
    )
    seeder.do(
        "o2c-2026-004", "Kundenrückmeldung erfassen", "a-nadja", {"angebot_status": "Angenommen"}
    )
    seeder.do(
        "o2c-2026-004",
        "Auftrag anlegen und bestätigen",
        "a-nadja",
        {"auftrags_nr": "AB-2026-0104", "verfuegbarkeit": "Fertigung"},
    )

    # 5) In der Logistik: Fertigungsauftrag steht, Kommissionierung offen.
    seeder.start(SCHEMA_MAIN, "o2c-2026-005")
    seeder.do(
        "o2c-2026-005",
        "Kundenanfrage erfassen",
        "a-nadja",
        _anfrage_daten("Alpin Fenster GmbH", 10088, "Aluprofil AP-220", 300, 16),
    )
    seeder.do("o2c-2026-005", "Verfügbarkeit vorab klären", "a-lars", {"lager_bestand": 40})
    seeder.do(
        "o2c-2026-005", "Preis und Konditionen kalkulieren", "a-nadja", _kalkulation(38.0, 300)
    )
    _run_bonitaet(seeder, "o2c-2026-005", score=88, limit=25000.0)
    seeder.do(
        "o2c-2026-005",
        "Angebot erstellen und versenden",
        "a-nadja",
        {"angebots_nr": "AN-2026-0198"},
    )
    seeder.do(
        "o2c-2026-005", "Kundenrückmeldung erfassen", "a-nadja", {"angebot_status": "Angenommen"}
    )
    seeder.do(
        "o2c-2026-005",
        "Auftrag anlegen und bestätigen",
        "a-nadja",
        {"auftrags_nr": "AB-2026-0098", "verfuegbarkeit": "Fertigung"},
    )
    seeder.do(
        "o2c-2026-005",
        "Freigabe durch Vertriebsleitung",
        "a-viktor",
        {"freigabe_vermerk": "Konditionen geprüft, Freigabe erteilt."},
    )
    seeder.do(
        "o2c-2026-005",
        "Fertigungsauftrag anlegen",
        "a-lars",
        {"liefertermin": (heute + timedelta(days=18)).isoformat()},
    )

    # 6) Vollstaendig durchgelaufen und bezahlt -- fuellt die KPIs.
    _seed_completed_order(
        seeder,
        instance_id="o2c-2026-006",
        kunde="Hanse Logistik KG",
        kunden_nr=10102,
        artikel="Palettenregal PR-12",
        menge=60,
        einzelpreis=95.0,
        angebots_nr="AN-2026-0142",
        auftrags_nr="AB-2026-0071",
        lieferschein="LS-2026-0071",
        rechnung="RE-2026-0071",
        bezahlt=True,
    )

    # 7) Durchgelaufen, aber offen -- der Folgeprozess Forderungsmanagement
    #    wurde dadurch gestartet und wartet auf seinen ersten Schritt.
    _seed_completed_order(
        seeder,
        instance_id="o2c-2026-007",
        kunde="Kramer Fahrzeugtechnik e.K.",
        kunden_nr=10115,
        artikel="Achsschenkel AS-7",
        menge=80,
        einzelpreis=128.5,
        angebots_nr="AN-2026-0155",
        auftrags_nr="AB-2026-0080",
        lieferschein="LS-2026-0080",
        rechnung="RE-2026-0080",
        bezahlt=False,
    )

    # 8) Nachverhandlung -- der dritte Zweig der Kundenentscheidung, und die
    #    einzige offene Aufgabe der Vertriebsleitung.
    seeder.start(SCHEMA_MAIN, "o2c-2026-008")
    seeder.do(
        "o2c-2026-008",
        "Kundenanfrage erfassen",
        "a-nadja",
        _anfrage_daten("Bergmann Kunststofftechnik GmbH", 10126, "Spritzgussform SF-3", 2, 12),
    )
    seeder.do("o2c-2026-008", "Verfügbarkeit vorab klären", "a-lars", {"lager_bestand": 0})
    seeder.do(
        "o2c-2026-008", "Preis und Konditionen kalkulieren", "a-nadja", _kalkulation(9800.0, 2)
    )
    _run_bonitaet(seeder, "o2c-2026-008", score=64, limit=30000.0)
    seeder.do(
        "o2c-2026-008",
        "Angebot erstellen und versenden",
        "a-nadja",
        {"angebots_nr": "AN-2026-0260"},
    )
    seeder.do(
        "o2c-2026-008",
        "Kundenrückmeldung erfassen",
        "a-nadja",
        {"angebot_status": "Nachverhandlung"},
    )

    # 9) Abbruchpfad: der Kunde hat abgelehnt, der Vorgang endet frueh.
    seeder.start(SCHEMA_MAIN, "o2c-2026-009")
    seeder.do(
        "o2c-2026-009",
        "Kundenanfrage erfassen",
        "a-nadja",
        _anfrage_daten("Tiefbau Zeller GmbH", 10131, "Rohrschelle RS-160", 500, 25),
    )
    seeder.do("o2c-2026-009", "Verfügbarkeit vorab klären", "a-lars", {"lager_bestand": 1200})
    seeder.do(
        "o2c-2026-009", "Preis und Konditionen kalkulieren", "a-nadja", _kalkulation(4.2, 500)
    )
    _run_bonitaet(seeder, "o2c-2026-009", score=72, limit=8000.0)
    seeder.do(
        "o2c-2026-009",
        "Angebot erstellen und versenden",
        "a-nadja",
        {"angebots_nr": "AN-2026-0088"},
    )
    seeder.do(
        "o2c-2026-009", "Kundenrückmeldung erfassen", "a-nadja", {"angebot_status": "Abgelehnt"}
    )
    seeder.do(
        "o2c-2026-009",
        "Absage dokumentieren",
        "a-nadja",
        {"vorgangsstatus": "Angebot abgelehnt"},
    )
    seeder.do(
        "o2c-2026-009",
        "Vorgang abschließen und archivieren",
        "a-nadja",
        {
            "forderung_offen": False,
            "reklamation": False,
            "abschluss_notiz": "Kunde hat sich für einen Mitbewerber entschieden.",
        },
    )


def _seed_completed_order(
    seeder: _Seeder,
    *,
    instance_id: str,
    kunde: str,
    kunden_nr: int,
    artikel: str,
    menge: int,
    einzelpreis: float,
    angebots_nr: str,
    auftrags_nr: str,
    lieferschein: str,
    rechnung: str,
    bezahlt: bool,
) -> None:
    """Spielt einen Vorgang vollstaendig durch -- bezahlt oder offen geblieben.

    Der offene Fall setzt beim Abschluss ``forderung_offen``; dadurch startet die
    Engine den Folgeprozess Forderungsmanagement, der anschliessend auf seinen
    ersten Schritt wartet (F1-F4, sichtbar als Herkunft im Monitoring).
    """

    heute = datetime.now(UTC).date()
    wert = round(einzelpreis * menge, 2)
    seeder.start(SCHEMA_MAIN, instance_id)
    seeder.do(
        instance_id,
        "Kundenanfrage erfassen",
        "a-nadja",
        _anfrage_daten(kunde, kunden_nr, artikel, menge, 40),
    )
    seeder.do(instance_id, "Verfügbarkeit vorab klären", "a-lars", {"lager_bestand": menge * 3})
    seeder.do(
        instance_id,
        "Preis und Konditionen kalkulieren",
        "a-nadja",
        _kalkulation(einzelpreis, menge),
    )
    _run_bonitaet(seeder, instance_id, score=84, limit=max(wert * 2, 20000.0))
    seeder.do(
        instance_id,
        "Angebot erstellen und versenden",
        "a-nadja",
        {"angebots_nr": angebots_nr},
    )
    seeder.do(
        instance_id,
        "Kundenrückmeldung erfassen",
        "a-nadja",
        {"angebot_status": "Angenommen"},
    )
    seeder.do(
        instance_id,
        "Auftrag anlegen und bestätigen",
        "a-nadja",
        {"auftrags_nr": auftrags_nr, "verfuegbarkeit": "Ab Lager"},
    )
    if wert < 5000:
        seeder.do(
            instance_id,
            "Freigabe durch Innendienst",
            "a-nadja",
            {"freigabe_vermerk": "Innerhalb der Wertgrenze freigegeben."},
        )
    elif wert < 25000:
        seeder.do(
            instance_id,
            "Freigabe durch Vertriebsleitung",
            "a-viktor",
            {"freigabe_vermerk": "Konditionen geprüft, Freigabe erteilt."},
        )
    else:
        seeder.do(
            instance_id,
            "Freigabe durch Geschäftsführung",
            "a-gustav",
            {"freigabe_vermerk": "Großauftrag, Freigabe der Geschäftsführung."},
        )
    seeder.do(
        instance_id,
        "Ware reservieren",
        "a-lars",
        {"liefertermin": (heute - timedelta(days=20)).isoformat()},
    )
    seeder.do(instance_id, "Kommissionieren", "a-lars", {"kommission_ok": True})
    seeder.do(instance_id, "Versandpapiere erstellen", "a-lars", {"versandpapiere_ok": True})
    _run_versand(seeder, instance_id, lieferschein, (heute - timedelta(days=18)).isoformat())
    _run_faktura(seeder, instance_id, rechnung, (heute + timedelta(days=12)).isoformat())
    seeder.do(
        instance_id,
        SYSTEM_PREFIX + "Zahlungseingang prüfen",
        "a-bianca",
        {"zahlungseingang": bezahlt},
    )
    if bezahlt:
        seeder.do(
            instance_id,
            "Zahlung verbuchen",
            "a-bianca",
            {
                "zahlbetrag": wert,
                "offener_betrag": 0.0,
                "vorgangsstatus": "Abgeschlossen - bezahlt",
            },
        )
    else:
        seeder.do(
            instance_id,
            "Offene Forderung dokumentieren",
            "a-bianca",
            {"zahlbetrag": 0.0, "offener_betrag": wert, "vorgangsstatus": "Offene Forderung"},
        )
    seeder.do(
        instance_id,
        "Vorgang abschließen und archivieren",
        "a-nadja",
        {
            "forderung_offen": not bezahlt,
            "reklamation": False,
            "abschluss_notiz": (
                "Vorgang vollständig abgewickelt."
                if bezahlt
                else "Zahlungsziel verstrichen -- an das Forderungsmanagement übergeben."
            ),
        },
    )


def _seed_absences(absence_store: AbsenceStore) -> None:
    """Eine *aktive* Abwesenheit, damit die Vertretung sofort sichtbar ist.

    Karin Kredel (Kreditmanagement) ist im Urlaub; ihre Vertretung ist Bianca
    Buch aus der Buchhaltung. Waehrend des Fensters erscheint Karins offene
    Aufgabe "Kundenstammdaten pruefen" (Vorgang o2c-2026-003) **zusaetzlich** bei
    Bianca, ohne je aus Karins eigener Liste zu verschwinden -- und Bianca traegt
    die Rolle Kreditmanagement gerade *nicht*, die Substitution ist also wirklich
    der Grund. Das Fenster haengt an der Ladezeit, bleibt nach jedem Reset aktuell.
    """

    now = datetime.now(UTC)
    absence_store.put_entry(
        AbsenceEntry(
            id="abs-o2c-karin",
            agent_id="a-karin",
            start_at=now - timedelta(days=2),
            end_at=now + timedelta(days=5),
            note="Jahresurlaub",
        )
    )


def _seed_users(backend: PasswordAuthBackend) -> int:
    """Legt die Logins dieses Datensatzes an (idempotent); zaehlt die neuen."""

    seeded = 0
    for login, name, roles, agent_id in O2C_USERS:
        if backend.store.get_user(login) is not None:
            continue
        backend.store.put_user(
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


def load_o2c(
    *,
    schema_store: SchemaStore,
    instance_store: InstanceStore,
    org_store: OrgStore,
    audit_log: AuditLog,
    password_backend: PasswordAuthBackend | None = None,
    absence_store: AbsenceStore | None = None,
) -> int:
    """Laedt den Order-to-Cash-Datensatz in die Stores; liefert die Zahl neuer Logins.

    Additiv und unabhaengig vom Basis-Demo (:func:`procworks.demo.load_demo`):
    eigene Schema-Ids, eigenes Organisationsmodell, eigene Logins. Beide
    Datensaetze koennen nebeneinander im selben System liegen.

    Reihenfolge ist Pflicht, nicht Geschmack: Teil- und Folgeprozesse muessen
    **freigegeben** vorliegen, bevor der Hauptprozess sie binden darf (H1/F1) --
    deshalb werden sie zuerst gebaut und ueber einen lokalen Aufloeser gereicht.
    Die Instanzen werden anschliessend gegen die *hydrierten* Schemata gefahren
    (die Bearbeiterpruefung beim Abschluss braucht die Organisation); in den
    Store wandern sie danach dehydriert, wie ueberall im Kern.
    """

    org = _build_org()
    org_store.put(org)

    schemas: dict[str, ProcessSchema] = {}

    def resolver(schema_id: str, version: int | None) -> ProcessSchema | None:
        schema = schemas.get(schema_id)
        if schema is None or (version is not None and schema.version != version):
            return None
        return schema

    for build in (
        _build_bonitaet,
        _build_versand,
        _build_faktura,
        _build_forderung,
        _build_retoure,
    ):
        schema = build(org)
        schemas[schema.id] = schema
    main = _build_main(org, resolver)
    schemas[main.id] = main

    _seed_instances(schemas, instance_store, audit_log)

    for schema in schemas.values():
        schema_store.put(dehydrate_org(schema))

    if absence_store is not None:
        _seed_absences(absence_store)
    if password_backend is not None:
        return _seed_users(password_backend)
    return 0
