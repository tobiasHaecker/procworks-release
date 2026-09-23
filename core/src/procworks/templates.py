# SPDX-License-Identifier: BUSL-1.1
"""Built-in process templates (blueprints for common company processes).

A fresh installation ships with a small library of ready-to-use process
templates -- blueprints for processes that exist in most companies (a vacation
request, an invoice approval, an onboarding). A modeller instantiates one into a
fresh draft schema and adapts it, instead of starting from an empty START -> END.

The templates are built **exclusively through the public change operations** (the
same validate-before-commit path every client uses), so a built-in template can
never carry an incorrect blueprint -- exactly like :mod:`procworks.demo`. Each is
**self-contained**: it embeds its own small organisation (roles + one agent per
role, so the ROLE staff rules are satisfiable, Z2) and does not reference a
shared org model, so it instantiates cleanly on any installation.

Built-in templates are provided here as code (never persisted); the template
store holds only modeller-created (``USER``) templates. Both are surfaced
together by the API's template gallery.
"""

from __future__ import annotations

from procworks import operations as ops
from procworks.model import (
    AccessMode,
    DataType,
    Node,
    ProcessSchema,
    ProcessTemplate,
    StaffRule,
    StaffRuleKind,
    TemplateOrigin,
    staff_rule_text,
)


def _role_rule(role_id: str) -> StaffRule:
    """A staff rule (BZR) binding an activity to a single organisational role."""

    return StaffRule(kind=StaffRuleKind.ROLE, ref=role_id)


def _nid(schema: ProcessSchema, label: str) -> str:
    """Return the id of the (unique) node carrying ``label`` (build-time helper)."""

    return next(n.id for n in schema.nodes.values() if n.label == label)


def _with_role(schema: ProcessSchema, role_id: str, role_name: str) -> ProcessSchema:
    """Add a role plus one bearer agent, so ROLE staff rules stay satisfiable (Z2).

    A ROLE staff rule is only satisfiable if at least one agent carries the role
    at design time; the built-in templates therefore seed one agent per role.
    """

    schema = ops.add_role(schema, role_name, role_id=role_id)
    schema = ops.add_agent(
        schema, f"{role_name} (Beispiel)", role_ids=[role_id], agent_id=f"a-{role_id}"
    )
    return schema


def _build_vacation_request() -> ProcessTemplate:
    """Urlaubsantrag: request -> supervisor review -> approve/reject (XOR).

    Exercises a decision: the reviewer sets a BOOLEAN discriminator, and the XOR
    splits into an "eintragen" (approved) and an "ablehnen" (rejected) branch.
    """

    s = ops.create_empty_schema("Urlaubsantrag")
    s = _with_role(s, "mitarbeiter", "Mitarbeiter")
    s = _with_role(s, "vorgesetzte", "Vorgesetzte")

    start = s.start_node().id
    s = ops.serial_insert(s, "Urlaubsantrag stellen", start)
    stellen = _nid(s, "Urlaubsantrag stellen")
    s = ops.serial_insert(s, "Antrag prüfen", stellen)
    pruefen = _nid(s, "Antrag prüfen")

    # The reviewer decides; the BOOLEAN result drives the XOR (written before the
    # split on all paths, as D1/K7 require of a discriminator).
    s = ops.add_data_element(s, "Antrag genehmigt", DataType.BOOLEAN, element_id="genehmigt")
    s = ops.connect_data(s, pruefen, "genehmigt", AccessMode.WRITE)

    s = ops.conditional_insert(
        s,
        pruefen,
        discriminator="genehmigt",
        branches=[
            ops.BranchSpec(label="Urlaub eintragen", bool_value=True),
            ops.BranchSpec(label="Ablehnung mitteilen", bool_value=False),
        ],
    )

    s = ops.assign_staff_rule(s, stellen, _role_rule("mitarbeiter"))
    s = ops.assign_staff_rule(s, pruefen, _role_rule("vorgesetzte"))
    s = ops.assign_staff_rule(s, _nid(s, "Urlaub eintragen"), _role_rule("vorgesetzte"))
    s = ops.assign_staff_rule(s, _nid(s, "Ablehnung mitteilen"), _role_rule("vorgesetzte"))

    return ops.save_as_template(
        s,
        template_id="tpl-urlaubsantrag",
        name="Urlaubsantrag",
        description=(
            "Mitarbeiter stellt einen Urlaubsantrag, die vorgesetzte Person prüft "
            "ihn und genehmigt oder lehnt ihn ab."
        ),
        category="Personal",
        origin=TemplateOrigin.BUILTIN,
    )


def _build_invoice_approval() -> ProcessTemplate:
    """Rechnungsfreigabe: capture -> factual check -> approve/reject (XOR)."""

    s = ops.create_empty_schema("Rechnungsfreigabe")
    s = _with_role(s, "sachbearbeiter", "Sachbearbeiter")
    s = _with_role(s, "fachabteilung", "Fachabteilung")
    s = _with_role(s, "buchhaltung", "Buchhaltung")

    start = s.start_node().id
    s = ops.serial_insert(s, "Rechnung erfassen", start)
    erfassen = _nid(s, "Rechnung erfassen")
    s = ops.serial_insert(s, "Sachlich prüfen", erfassen)
    pruefen = _nid(s, "Sachlich prüfen")

    s = ops.add_data_element(s, "Rechnung freigegeben", DataType.BOOLEAN, element_id="freigegeben")
    s = ops.connect_data(s, pruefen, "freigegeben", AccessMode.WRITE)

    s = ops.conditional_insert(
        s,
        pruefen,
        discriminator="freigegeben",
        branches=[
            ops.BranchSpec(label="Zahlung anweisen", bool_value=True),
            ops.BranchSpec(label="Rückfrage klären", bool_value=False),
        ],
    )

    s = ops.assign_staff_rule(s, erfassen, _role_rule("sachbearbeiter"))
    s = ops.assign_staff_rule(s, pruefen, _role_rule("fachabteilung"))
    s = ops.assign_staff_rule(s, _nid(s, "Zahlung anweisen"), _role_rule("buchhaltung"))
    s = ops.assign_staff_rule(s, _nid(s, "Rückfrage klären"), _role_rule("sachbearbeiter"))

    return ops.save_as_template(
        s,
        template_id="tpl-rechnungsfreigabe",
        name="Rechnungsfreigabe",
        description=(
            "Eingehende Rechnung erfassen, sachlich prüfen und je nach Ergebnis "
            "zur Zahlung anweisen oder eine Rückfrage klären."
        ),
        category="Finanzen",
        origin=TemplateOrigin.BUILTIN,
    )


def _build_onboarding() -> ProcessTemplate:
    """Onboarding: prepare -> (IT + workplace in parallel) -> welcome talk.

    Exercises an AND block: IT provisioning and workplace setup run in parallel
    before the introductory meeting.
    """

    s = ops.create_empty_schema("Onboarding neuer Mitarbeiter")
    s = _with_role(s, "personal", "Personal")
    s = _with_role(s, "it", "IT")
    s = _with_role(s, "facility", "Facility Management")
    s = _with_role(s, "fuehrung", "Führungskraft")

    start = s.start_node().id
    s = ops.serial_insert(s, "Eintritt vorbereiten", start)
    vorbereiten = _nid(s, "Eintritt vorbereiten")
    s = ops.parallel_insert(
        s, ["IT-Ausstattung bereitstellen", "Arbeitsplatz einrichten"], vorbereiten
    )
    # The AND join is END's unique predecessor; insert the welcome talk after it.
    and_join = next(e.source for e in s.incoming(s.end_node().id))
    s = ops.serial_insert(s, "Einführungsgespräch", and_join)

    s = ops.assign_staff_rule(s, vorbereiten, _role_rule("personal"))
    s = ops.assign_staff_rule(s, _nid(s, "IT-Ausstattung bereitstellen"), _role_rule("it"))
    s = ops.assign_staff_rule(s, _nid(s, "Arbeitsplatz einrichten"), _role_rule("facility"))
    s = ops.assign_staff_rule(s, _nid(s, "Einführungsgespräch"), _role_rule("fuehrung"))

    return ops.save_as_template(
        s,
        template_id="tpl-onboarding",
        name="Onboarding neuer Mitarbeiter",
        description=(
            "Eintritt vorbereiten, IT-Ausstattung und Arbeitsplatz parallel einrichten "
            "und mit einem Einführungsgespräch abschließen."
        ),
        category="Personal",
        origin=TemplateOrigin.BUILTIN,
    )


def _join_before_end(schema: ProcessSchema) -> str:
    """The node right before END (e.g. the join closing the last block)."""

    return next(e.source for e in schema.incoming(schema.end_node().id))


def _build_purchase_approval() -> ProcessTemplate:
    """Bestellfreigabe: request -> approval level by amount (THRESHOLD) -> order.

    The amount does not *decide* anything -- it only routes to the right
    approval level (below 1 000 the team lead, from 1 000 the management), which
    is what a threshold partition is for.
    """

    s = ops.create_empty_schema("Bestellfreigabe")
    s = _with_role(s, "anforderer", "Anforderer")
    s = _with_role(s, "teamleitung", "Teamleitung")
    s = _with_role(s, "geschaeftsfuehrung", "Geschäftsführung")
    s = _with_role(s, "einkauf", "Einkauf")

    s = ops.serial_insert(s, "Bestellung anfordern", s.start_node().id)
    anfordern = _nid(s, "Bestellung anfordern")
    s = ops.add_data_element(s, "Bestellwert", DataType.FLOAT, element_id="bestellwert")
    s = ops.connect_data(s, anfordern, "bestellwert", AccessMode.WRITE)
    s = ops.conditional_insert(
        s,
        anfordern,
        discriminator="bestellwert",
        branches=[
            ops.BranchSpec(label="Freigabe Teamleitung", upper=1000.0),
            ops.BranchSpec(label="Freigabe Geschäftsführung"),
        ],
    )
    s = ops.serial_insert(s, "Bestellung auslösen", _join_before_end(s))

    s = ops.assign_staff_rule(s, anfordern, _role_rule("anforderer"))
    s = ops.assign_staff_rule(s, _nid(s, "Freigabe Teamleitung"), _role_rule("teamleitung"))
    s = ops.assign_staff_rule(
        s, _nid(s, "Freigabe Geschäftsführung"), _role_rule("geschaeftsfuehrung")
    )
    s = ops.assign_staff_rule(s, _nid(s, "Bestellung auslösen"), _role_rule("einkauf"))

    return ops.save_as_template(
        s,
        template_id="tpl-bestellfreigabe",
        name="Bestellfreigabe",
        description=(
            "Bestellung anfordern; unter 1 000 € gibt die Teamleitung frei, darüber "
            "die Geschäftsführung. Danach löst der Einkauf die Bestellung aus."
        ),
        category="Einkauf",
        origin=TemplateOrigin.BUILTIN,
    )


def _build_travel_expenses() -> ProcessTemplate:
    """Reisekostenabrechnung: capture -> review (decision) -> pay out / correct."""

    s = ops.create_empty_schema("Reisekostenabrechnung")
    s = _with_role(s, "mitarbeiter", "Mitarbeiter")
    s = _with_role(s, "vorgesetzte", "Vorgesetzte")
    s = _with_role(s, "buchhaltung", "Buchhaltung")

    s = ops.serial_insert(s, "Reisekosten erfassen", s.start_node().id)
    erfassen = _nid(s, "Reisekosten erfassen")
    s = ops.serial_insert(s, "Abrechnung prüfen", erfassen)
    pruefen = _nid(s, "Abrechnung prüfen")
    s = ops.add_data_element(s, "Abrechnung genehmigt", DataType.BOOLEAN, element_id="genehmigt")
    s = ops.connect_data(s, pruefen, "genehmigt", AccessMode.WRITE)
    s = ops.conditional_insert(
        s,
        pruefen,
        discriminator="genehmigt",
        branches=[
            ops.BranchSpec(label="Erstattung anweisen", bool_value=True),
            ops.BranchSpec(label="Korrektur anfordern", bool_value=False),
        ],
    )

    s = ops.assign_staff_rule(s, erfassen, _role_rule("mitarbeiter"))
    s = ops.assign_staff_rule(s, pruefen, _role_rule("vorgesetzte"))
    s = ops.assign_staff_rule(s, _nid(s, "Erstattung anweisen"), _role_rule("buchhaltung"))
    s = ops.assign_staff_rule(s, _nid(s, "Korrektur anfordern"), _role_rule("vorgesetzte"))

    return ops.save_as_template(
        s,
        template_id="tpl-reisekosten",
        name="Reisekostenabrechnung",
        description=(
            "Reisekosten erfassen, von der vorgesetzten Person prüfen lassen und "
            "erstatten oder zur Korrektur zurückgeben."
        ),
        category="Finanzen",
        origin=TemplateOrigin.BUILTIN,
    )


def _build_complaint() -> ProcessTemplate:
    """Reklamation: capture -> assess (three-way ENUM decision) -> inform customer."""

    s = ops.create_empty_schema("Reklamation")
    s = _with_role(s, "kundenservice", "Kundenservice")
    s = _with_role(s, "qualitaet", "Qualitätssicherung")
    s = _with_role(s, "buchhaltung", "Buchhaltung")

    s = ops.serial_insert(s, "Reklamation erfassen", s.start_node().id)
    erfassen = _nid(s, "Reklamation erfassen")
    s = ops.serial_insert(s, "Reklamation bewerten", erfassen)
    bewerten = _nid(s, "Reklamation bewerten")
    s = ops.add_data_element(s, "Maßnahme", DataType.STRING, element_id="massnahme")
    s = ops.connect_data(s, bewerten, "massnahme", AccessMode.WRITE)
    s = ops.conditional_insert(
        s,
        bewerten,
        discriminator="massnahme",
        branches=[
            ops.BranchSpec(label="Ersatz liefern", values=("ersatz",)),
            ops.BranchSpec(label="Gutschrift erstellen", values=("gutschrift",)),
            ops.BranchSpec(label="Ablehnung begründen", is_else=True),
        ],
    )
    s = ops.serial_insert(s, "Kunde informieren", _join_before_end(s))

    s = ops.assign_staff_rule(s, erfassen, _role_rule("kundenservice"))
    s = ops.assign_staff_rule(s, bewerten, _role_rule("qualitaet"))
    s = ops.assign_staff_rule(s, _nid(s, "Ersatz liefern"), _role_rule("kundenservice"))
    s = ops.assign_staff_rule(s, _nid(s, "Gutschrift erstellen"), _role_rule("buchhaltung"))
    s = ops.assign_staff_rule(s, _nid(s, "Ablehnung begründen"), _role_rule("qualitaet"))
    s = ops.assign_staff_rule(s, _nid(s, "Kunde informieren"), _role_rule("kundenservice"))

    return ops.save_as_template(
        s,
        template_id="tpl-reklamation",
        name="Reklamation",
        description=(
            "Reklamation erfassen und bewerten; je nach Maßnahme Ersatz liefern, "
            "Gutschrift erstellen oder die Ablehnung begründen, dann den Kunden informieren."
        ),
        category="Vertrieb",
        origin=TemplateOrigin.BUILTIN,
    )


def _build_four_eyes() -> ProcessTemplate:
    """Vier-Augen-Freigabe: the approver is the supervisor of whoever captured it.

    Uses the relative staff rule ``NODE_PERFORMING_AGENT_SUPERVISOR``: the
    manager of the org unit in which "Vorgang erfassen" was performed approves.
    The sample org therefore places the capturing role in a unit with a manager
    (Z2 needs at least one possible supervisor at design time).
    """

    s = ops.create_empty_schema("Vier-Augen-Freigabe")
    s = ops.add_role(s, "Sachbearbeitung", role_id="sachbearbeitung")
    s = ops.add_org_unit(s, "Fachbereich", org_unit_id="fachbereich")
    s = ops.add_agent(s, "Leitung Fachbereich (Beispiel)", org_unit_id="fachbereich",
                      agent_id="a-leitung")
    s = ops.set_org_unit_manager(s, "fachbereich", "a-leitung")
    s = ops.add_agent(s, "Sachbearbeitung (Beispiel)", role_ids=["sachbearbeitung"],
                      org_unit_id="fachbereich", agent_id="a-sachbearbeitung")

    s = ops.serial_insert(s, "Vorgang erfassen", s.start_node().id)
    erfassen = _nid(s, "Vorgang erfassen")
    s = ops.serial_insert(s, "Freigabe durch Vorgesetzte", erfassen)
    freigabe = _nid(s, "Freigabe durch Vorgesetzte")
    s = ops.serial_insert(s, "Vorgang abschließen", freigabe)

    s = ops.assign_staff_rule(s, erfassen, _role_rule("sachbearbeitung"))
    s = ops.assign_staff_rule(
        s,
        freigabe,
        StaffRule(kind=StaffRuleKind.NODE_PERFORMING_AGENT_SUPERVISOR, ref=erfassen),
    )
    s = ops.assign_staff_rule(s, _nid(s, "Vorgang abschließen"), _role_rule("sachbearbeitung"))

    return ops.save_as_template(
        s,
        template_id="tpl-vier-augen",
        name="Vier-Augen-Freigabe",
        description=(
            "Ein Vorgang wird erfasst und von der vorgesetzten Person der erfassenden "
            "Person freigegeben – nie von ihr selbst."
        ),
        category="Allgemein",
        origin=TemplateOrigin.BUILTIN,
    )


def _build_incident() -> ProcessTemplate:
    """Störungsmeldung: report -> analyse -> fix-and-test loop (bounded) -> inform."""

    s = ops.create_empty_schema("Störungsmeldung")
    s = _with_role(s, "mitarbeiter", "Mitarbeiter")
    s = _with_role(s, "it_support", "IT-Support")

    s = ops.serial_insert(s, "Störung melden", s.start_node().id)
    melden = _nid(s, "Störung melden")
    s = ops.serial_insert(s, "Störung analysieren", melden)
    analysieren = _nid(s, "Störung analysieren")
    s = ops.add_data_element(s, "Nacharbeit nötig", DataType.BOOLEAN, element_id="nacharbeit")
    # Repeat "fix and test" while the test says rework is needed -- at most five
    # rounds (K6 hard brake), after which the loop is left and escalated manually.
    s = ops.insert_loop(
        s,
        analysieren,
        "Lösung umsetzen und testen",
        discriminator="nacharbeit",
        repeat_value=True,
        max_iterations=5,
    )
    s = ops.serial_insert(s, "Meldende Person informieren", _join_before_end(s))

    s = ops.assign_staff_rule(s, melden, _role_rule("mitarbeiter"))
    s = ops.assign_staff_rule(s, analysieren, _role_rule("it_support"))
    s = ops.assign_staff_rule(s, _nid(s, "Lösung umsetzen und testen"), _role_rule("it_support"))
    s = ops.assign_staff_rule(s, _nid(s, "Meldende Person informieren"), _role_rule("it_support"))

    return ops.save_as_template(
        s,
        template_id="tpl-stoerung",
        name="Störungsmeldung",
        description=(
            "Störung melden und analysieren; die Lösung wird umgesetzt und getestet, "
            "bis keine Nacharbeit mehr nötig ist (höchstens fünf Runden)."
        ),
        category="IT",
        origin=TemplateOrigin.BUILTIN,
    )


def _build_supplier_approval() -> ProcessTemplate:
    """Lieferantenfreigabe: capture -> (credit + compliance in parallel) -> approve."""

    s = ops.create_empty_schema("Lieferantenfreigabe")
    s = _with_role(s, "einkauf", "Einkauf")
    s = _with_role(s, "finanzen", "Finanzen")
    s = _with_role(s, "compliance", "Compliance")

    s = ops.serial_insert(s, "Lieferant erfassen", s.start_node().id)
    erfassen = _nid(s, "Lieferant erfassen")
    s = ops.parallel_insert(s, ["Bonität prüfen", "Compliance prüfen"], erfassen)
    s = ops.serial_insert(s, "Lieferant freigeben", _join_before_end(s))

    s = ops.assign_staff_rule(s, erfassen, _role_rule("einkauf"))
    s = ops.assign_staff_rule(s, _nid(s, "Bonität prüfen"), _role_rule("finanzen"))
    s = ops.assign_staff_rule(s, _nid(s, "Compliance prüfen"), _role_rule("compliance"))
    s = ops.assign_staff_rule(s, _nid(s, "Lieferant freigeben"), _role_rule("einkauf"))

    return ops.save_as_template(
        s,
        template_id="tpl-lieferantenfreigabe",
        name="Lieferantenfreigabe",
        description=(
            "Neuen Lieferanten erfassen, Bonität und Compliance parallel prüfen "
            "und anschließend freigeben."
        ),
        category="Einkauf",
        origin=TemplateOrigin.BUILTIN,
    )


def _build_access_request() -> ProcessTemplate:
    """Zugriffsantrag: request -> supervisor decision -> set up / reject."""

    s = ops.create_empty_schema("Zugriffsantrag")
    s = _with_role(s, "mitarbeiter", "Mitarbeiter")
    s = _with_role(s, "vorgesetzte", "Vorgesetzte")
    s = _with_role(s, "it", "IT-Administration")

    s = ops.serial_insert(s, "Zugriff beantragen", s.start_node().id)
    beantragen = _nid(s, "Zugriff beantragen")
    s = ops.serial_insert(s, "Antrag genehmigen", beantragen)
    genehmigen = _nid(s, "Antrag genehmigen")
    s = ops.add_data_element(s, "Zugriff genehmigt", DataType.BOOLEAN, element_id="genehmigt")
    s = ops.connect_data(s, genehmigen, "genehmigt", AccessMode.WRITE)
    s = ops.conditional_insert(
        s,
        genehmigen,
        discriminator="genehmigt",
        branches=[
            ops.BranchSpec(label="Berechtigung einrichten", bool_value=True),
            ops.BranchSpec(label="Ablehnung mitteilen", bool_value=False),
        ],
    )

    s = ops.assign_staff_rule(s, beantragen, _role_rule("mitarbeiter"))
    s = ops.assign_staff_rule(s, genehmigen, _role_rule("vorgesetzte"))
    s = ops.assign_staff_rule(s, _nid(s, "Berechtigung einrichten"), _role_rule("it"))
    s = ops.assign_staff_rule(s, _nid(s, "Ablehnung mitteilen"), _role_rule("vorgesetzte"))

    return ops.save_as_template(
        s,
        template_id="tpl-zugriffsantrag",
        name="Zugriffsantrag",
        description=(
            "Zugriff auf ein System beantragen; die vorgesetzte Person entscheidet, "
            "die IT richtet die Berechtigung ein."
        ),
        category="IT",
        origin=TemplateOrigin.BUILTIN,
    )


def template_roles(schema: ProcessSchema) -> list[tuple[str, list[str]]]:
    """Who does what in a blueprint: ``(role description, step labels)`` pairs.

    Derived from the staff rules, so it always matches the template's content
    (no separately maintained text that could drift). A ROLE rule names the
    role, an ORG_UNIT rule the unit, an AGENT rule the person, and the relative
    rules the reference step ("Vorgesetzte:r von „Vorgang erfassen“"). Ordered
    by the first step each performer appears in.
    """

    org = schema.org_model
    order = {n.id: i for i, n in enumerate(_step_order(schema))}
    grouped: dict[str, list[str]] = {}
    for node_id in sorted(schema.staff_rules, key=lambda n: order.get(n, 10**6)):
        rule = schema.staff_rules[node_id]
        node = schema.nodes.get(node_id)
        if node is None:
            continue
        grouped.setdefault(staff_rule_text(rule, schema, org), []).append(node.label or node_id)
    return list(grouped.items())


def _step_order(schema: ProcessSchema) -> list[Node]:
    """Nodes in a stable breadth-first order from START (for readable grouping)."""

    seen: list[str] = []
    queue = [schema.start_node().id]
    while queue:
        current = queue.pop(0)
        if current in seen:
            continue
        seen.append(current)
        queue.extend(e.target for e in schema.outgoing(current))
    return [schema.nodes[n] for n in seen if n in schema.nodes]


#: Builder functions for the built-in library, invoked lazily by
#: :func:`builtin_templates` so a broken builder is caught by the test suite.
_BUILDERS = (
    _build_vacation_request,
    _build_invoice_approval,
    _build_onboarding,
    _build_purchase_approval,
    _build_travel_expenses,
    _build_complaint,
    _build_four_eyes,
    _build_incident,
    _build_supplier_approval,
    _build_access_request,
)


def builtin_templates() -> list[ProcessTemplate]:
    """Return the built-in template library (freshly built, correct by construction).

    Rebuilt on each call rather than cached so the returned templates are never
    shared mutable state; every blueprint is validated as it is built.
    """

    return [build() for build in _BUILDERS]
