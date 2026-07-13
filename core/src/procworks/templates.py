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
    ProcessSchema,
    ProcessTemplate,
    StaffRule,
    StaffRuleKind,
    TemplateOrigin,
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


#: Builder functions for the built-in library, invoked lazily by
#: :func:`builtin_templates` so a broken builder is caught by the test suite.
_BUILDERS = (
    _build_vacation_request,
    _build_invoice_approval,
    _build_onboarding,
)


def builtin_templates() -> list[ProcessTemplate]:
    """Return the built-in template library (freshly built, correct by construction).

    Rebuilt on each call rather than cached so the returned templates are never
    shared mutable state; every blueprint is validated as it is built.
    """

    return [build() for build in _BUILDERS]
