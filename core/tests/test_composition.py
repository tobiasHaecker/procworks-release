# SPDX-License-Identifier: BUSL-1.1
"""Tests for the composition rules H1-H4 (sub-processes) and F1-F3 (follow-ups).

A sub-process or follow-up may only reference a RELEASED target with a
type-conformant mapping, and the sub-process hierarchy must stay acyclic.
These checks need a resolver that looks other schemas up.
"""

from __future__ import annotations

import pytest
from staffing import staffed

from procworks import (
    add_data_element,
    connect_data,
    convert_activity_to_subprocess,
    create_empty_schema,
    insert_subprocess,
    link_follow_up,
    release,
    serial_insert,
    set_library_subprocess,
    set_subprocess_binding,
    validate,
)
from procworks.model import (
    AccessMode,
    DataType,
    FollowUpTrigger,
    NodeType,
    ProcessSchema,
)
from procworks.operations import CorrectnessError


def _resolver_for(*schemas: ProcessSchema):
    by_id = {s.id: s for s in schemas}

    def resolve(schema_id: str, version: int | None) -> ProcessSchema | None:
        schema = by_id.get(schema_id)
        if schema is None:
            return None
        if version is not None and schema.version != version:
            return None
        return schema

    return resolve


def _released_target() -> ProcessSchema:
    sub = create_empty_schema("Sub", schema_id="sub_target")
    sub = serial_insert(sub, "Pruefen", after_node_id="start")
    sub = add_data_element(sub, "betrag", DataType.FLOAT, element_id="betrag")
    return release(staffed(sub))


def test_insert_subprocess_against_released_target() -> None:
    target = _released_target()
    parent = create_empty_schema("Haupt", schema_id="parent")
    resolver = _resolver_for(target, parent)
    parent = insert_subprocess(
        parent, "start", "sub_target", 1, label="Teilprozess", resolver=resolver
    )
    assert validate(parent, resolver) == []
    assert sum(1 for n in parent.nodes.values() if n.type is NodeType.SUBPROCESS) == 1


def test_subprocess_target_must_be_released() -> None:
    draft = create_empty_schema("Entwurf", schema_id="draft_target")
    parent = create_empty_schema("Haupt", schema_id="parent")
    resolver = _resolver_for(draft, parent)
    with pytest.raises(CorrectnessError) as exc:
        insert_subprocess(parent, "start", "draft_target", 1, resolver=resolver)
    assert any(f.rule == "H1" for f in exc.value.findings)


def test_subprocess_unknown_target_is_h1() -> None:
    parent = create_empty_schema("Haupt", schema_id="parent")
    resolver = _resolver_for(parent)
    with pytest.raises(CorrectnessError) as exc:
        insert_subprocess(parent, "start", "ghost", 1, resolver=resolver)
    assert any(f.rule == "H1" for f in exc.value.findings)


def test_subprocess_type_mismatch_is_h2() -> None:
    target = _released_target()  # betrag: FLOAT
    parent = create_empty_schema("Haupt", schema_id="parent")
    parent = add_data_element(parent, "menge", DataType.INTEGER, element_id="menge")
    resolver = _resolver_for(target, parent)
    with pytest.raises(CorrectnessError) as exc:
        insert_subprocess(
            parent,
            "start",
            "sub_target",
            1,
            input_mapping={"betrag": "menge"},  # FLOAT <- INTEGER
            resolver=resolver,
        )
    assert any(f.rule == "H2" for f in exc.value.findings)


def test_subprocess_type_conformant_mapping_ok() -> None:
    target = _released_target()  # betrag: FLOAT
    parent = create_empty_schema("Haupt", schema_id="parent")
    parent = add_data_element(parent, "summe", DataType.FLOAT, element_id="summe")
    # A mapped input must already hold a value when the child starts, so a step
    # ahead of the sub-process supplies it (H2, parent side).
    parent = serial_insert(parent, "Vorbereiten", after_node_id="start")
    pre = next(n.id for n in parent.nodes.values() if n.label == "Vorbereiten")
    parent = connect_data(parent, pre, "summe", AccessMode.WRITE)
    resolver = _resolver_for(target, parent)
    parent = insert_subprocess(
        parent,
        pre,
        "sub_target",
        1,
        input_mapping={"betrag": "summe"},  # FLOAT <- FLOAT
        resolver=resolver,
    )
    assert validate(parent, resolver) == []


def test_self_reference_is_cyclic_h3() -> None:
    # A released schema that we pretend references itself via the resolver.
    parent = create_empty_schema("Selbst", schema_id="self")
    resolver = _resolver_for(parent)
    with pytest.raises(CorrectnessError) as exc:
        insert_subprocess(parent, "start", "self", 1, resolver=resolver)
    # self target is ENTWURF (H1) and cyclic (H3)
    assert any(f.rule == "H3" for f in exc.value.findings)


def test_follow_up_against_released_target() -> None:
    target = _released_target()
    parent = create_empty_schema("Haupt", schema_id="parent")
    resolver = _resolver_for(target, parent)
    parent = link_follow_up(parent, "sub_target", target_version=1, resolver=resolver)
    assert validate(parent, resolver) == []
    assert len(parent.follow_up_links) == 1


def test_follow_up_unknown_target_is_f1() -> None:
    parent = create_empty_schema("Haupt", schema_id="parent")
    resolver = _resolver_for(parent)
    with pytest.raises(CorrectnessError) as exc:
        link_follow_up(parent, "ghost", resolver=resolver)
    assert any(f.rule == "F1" for f in exc.value.findings)


def test_follow_up_handover_type_mismatch_is_f2() -> None:
    target = _released_target()  # betrag: FLOAT
    parent = create_empty_schema("Haupt", schema_id="parent")
    parent = add_data_element(parent, "anzahl", DataType.INTEGER, element_id="anzahl")
    resolver = _resolver_for(target, parent)
    with pytest.raises(CorrectnessError) as exc:
        link_follow_up(
            parent,
            "sub_target",
            target_version=1,
            handover_mapping={"betrag": "anzahl"},  # FLOAT <- INTEGER
            resolver=resolver,
        )
    assert any(f.rule == "F2" for f in exc.value.findings)


def test_conditional_follow_up_unknown_element_is_f4() -> None:
    target = _released_target()
    parent = create_empty_schema("Haupt", schema_id="parent")
    resolver = _resolver_for(target, parent)
    with pytest.raises(CorrectnessError) as exc:
        link_follow_up(
            parent,
            "sub_target",
            target_version=1,
            trigger=FollowUpTrigger.CONDITIONAL,
            condition="unbekannt > 0",
            resolver=resolver,
        )
    assert any(f.rule == "F4" for f in exc.value.findings)


def test_conditional_follow_up_without_condition_is_f4() -> None:
    target = _released_target()
    parent = create_empty_schema("Haupt", schema_id="parent")
    resolver = _resolver_for(target, parent)
    with pytest.raises(CorrectnessError) as exc:
        link_follow_up(
            parent,
            "sub_target",
            target_version=1,
            trigger=FollowUpTrigger.CONDITIONAL,
            resolver=resolver,
        )
    assert any(f.rule == "F4" for f in exc.value.findings)


def test_conditional_follow_up_with_valid_condition_ok() -> None:
    target = _released_target()
    parent = create_empty_schema("Haupt", schema_id="parent")
    parent = add_data_element(parent, "anzahl", DataType.INTEGER, element_id="anzahl")
    # The predicate runs at instance completion, so the element it reads must be
    # written on every path to END (F4).
    parent = serial_insert(parent, "Erfassen", after_node_id="start")
    erfassen = next(n.id for n in parent.nodes.values() if n.label == "Erfassen")
    parent = connect_data(parent, erfassen, "anzahl", AccessMode.WRITE)
    resolver = _resolver_for(target, parent)
    parent = link_follow_up(
        parent,
        "sub_target",
        target_version=1,
        trigger=FollowUpTrigger.CONDITIONAL,
        condition="anzahl > 0",
        resolver=resolver,
    )
    assert validate(parent, resolver) == []


def test_release_parent_with_released_subprocess() -> None:
    target = _released_target()
    parent = create_empty_schema("Haupt", schema_id="parent")
    resolver = _resolver_for(target, parent)
    parent = insert_subprocess(parent, "start", "sub_target", 1, resolver=resolver)
    released = release(staffed(parent), resolver)
    assert released.lifecycle_state.value == "RELEASED"


def _released_producer() -> ProcessSchema:
    """A RELEASED sub-process that guarantees to produce ``ergebnis``."""

    sub = create_empty_schema("Rechner", schema_id="calc")
    sub = serial_insert(sub, "Rechnen", after_node_id="start")
    sub = add_data_element(sub, "ergebnis", DataType.FLOAT, element_id="ergebnis")
    act = next(n.id for n in sub.nodes.values() if n.type is NodeType.ACTIVITY)
    sub = connect_data(sub, act, "ergebnis", AccessMode.WRITE)
    return release(staffed(sub))


def _activity(schema: ProcessSchema) -> str:
    return next(n.id for n in schema.nodes.values() if n.type is NodeType.ACTIVITY)


def test_convert_activity_to_subprocess_binds_and_stays_valid() -> None:
    target = _released_producer()
    parent = create_empty_schema("Haupt", schema_id="parent")
    parent = serial_insert(parent, "Schritt", after_node_id="start")
    parent = add_data_element(parent, "summe", DataType.FLOAT, element_id="summe")
    node_id = _activity(parent)
    resolver = _resolver_for(target, parent)
    parent = convert_activity_to_subprocess(
        parent,
        node_id,
        "calc",
        1,
        output_mapping={"ergebnis": "summe"},
        resolver=resolver,
    )
    assert parent.nodes[node_id].type is NodeType.SUBPROCESS
    assert node_id in parent.sub_process_bindings
    assert validate(parent, resolver) == []


def test_convert_non_activity_is_rejected() -> None:
    target = _released_producer()
    parent = create_empty_schema("Haupt", schema_id="parent")
    resolver = _resolver_for(target, parent)
    with pytest.raises(CorrectnessError) as exc:
        convert_activity_to_subprocess(parent, "start", "calc", 1, resolver=resolver)
    assert any(f.rule == "OP" for f in exc.value.findings)


def test_convert_drops_activity_data_access() -> None:
    target = _released_producer()
    parent = create_empty_schema("Haupt", schema_id="parent")
    parent = serial_insert(parent, "Schritt", after_node_id="start")
    parent = add_data_element(parent, "wert", DataType.FLOAT, element_id="wert")
    node_id = _activity(parent)
    parent = connect_data(parent, node_id, "wert", AccessMode.WRITE)
    resolver = _resolver_for(target, parent)
    parent = convert_activity_to_subprocess(parent, node_id, "calc", 1, resolver=resolver)
    assert all(a.node_id != node_id for a in parent.data_accesses)


def test_convert_with_unproduced_output_is_h2() -> None:
    # target that declares but never guarantees to write ``ergebnis``
    loose = create_empty_schema("Lose", schema_id="loose")
    loose = serial_insert(loose, "Tun", after_node_id="start")
    loose = add_data_element(loose, "ergebnis", DataType.FLOAT, element_id="ergebnis")
    loose = release(staffed(loose))
    parent = create_empty_schema("Haupt", schema_id="parent")
    parent = serial_insert(parent, "Schritt", after_node_id="start")
    parent = add_data_element(parent, "summe", DataType.FLOAT, element_id="summe")
    node_id = _activity(parent)
    resolver = _resolver_for(loose, parent)
    with pytest.raises(CorrectnessError) as exc:
        convert_activity_to_subprocess(
            parent,
            node_id,
            "loose",
            1,
            output_mapping={"ergebnis": "summe"},
            resolver=resolver,
        )
    assert any(f.rule == "H2" for f in exc.value.findings)


def test_set_subprocess_binding_rebinds_target() -> None:
    first = _released_producer()
    second = create_empty_schema("Rechner2", schema_id="calc2")
    second = serial_insert(second, "Rechnen", after_node_id="start")
    second = add_data_element(second, "ergebnis", DataType.FLOAT, element_id="ergebnis")
    act2 = _activity(second)
    second = connect_data(second, act2, "ergebnis", AccessMode.WRITE)
    second = release(staffed(second))
    parent = create_empty_schema("Haupt", schema_id="parent")
    resolver = _resolver_for(first, second, parent)
    parent = insert_subprocess(parent, "start", "calc", 1, resolver=resolver)
    sub_node = next(n.id for n in parent.nodes.values() if n.type is NodeType.SUBPROCESS)
    parent = set_subprocess_binding(parent, sub_node, "calc2", 1, resolver=resolver)
    assert parent.sub_process_bindings[sub_node].target_schema_id == "calc2"
    assert validate(parent, resolver) == []


def test_set_subprocess_binding_on_non_subprocess_is_rejected() -> None:
    target = _released_producer()
    parent = create_empty_schema("Haupt", schema_id="parent")
    resolver = _resolver_for(target, parent)
    with pytest.raises(CorrectnessError) as exc:
        set_subprocess_binding(parent, "start", "calc", 1, resolver=resolver)
    assert any(f.rule == "OP" for f in exc.value.findings)


def test_set_library_subprocess_toggles_flag_on_released() -> None:
    sub = _released_producer()
    assert sub.is_library_subprocess is False
    flagged = set_library_subprocess(sub, True)
    assert flagged.is_library_subprocess is True
    assert flagged.lifecycle_state.value == "RELEASED"
    cleared = set_library_subprocess(flagged, False)
    assert cleared.is_library_subprocess is False


def test_subprocess_output_satisfies_downstream_read_d1() -> None:
    # A downstream activity may read a subprocess output on all paths: the
    # output mapping counts as a guaranteed write of the SUBPROCESS node, so
    # D1 ("no read without a prior set") is satisfied by the data passing.
    target = _released_producer()  # guarantees to write ``ergebnis``
    parent = create_empty_schema("Haupt", schema_id="parent")
    parent = serial_insert(parent, "Nutzen", after_node_id="start")
    parent = add_data_element(parent, "summe", DataType.FLOAT, element_id="summe")
    reader = _activity(parent)
    resolver = _resolver_for(target, parent)
    # start -> SUBPROCESS -> Nutzen -> end, subprocess writes ``summe``
    parent = insert_subprocess(
        parent,
        "start",
        "calc",
        1,
        output_mapping={"ergebnis": "summe"},
        resolver=resolver,
    )
    parent = connect_data(parent, reader, "summe", AccessMode.READ)
    assert validate(parent, resolver) == []




def test_subprocess_input_must_be_written_before_the_call() -> None:
    """H2 (parent side): a mapped input must hold a value when the child starts.

    Type conformance alone is not enough. Without this the child begins with a
    missing input and the failure surfaces at runtime inside a *different*
    schema -- the mirror image of the output guarantee, which was checked all
    along. The Code-Wegweiser recorded this as a known silent gap ("H2 prueft
    Eingaenge nur auf Existenz und Typ, der Fehler bliebe still").
    """

    target = _released_target()  # betrag: FLOAT
    parent = create_empty_schema("Haupt", schema_id="parent")
    parent = add_data_element(parent, "summe", DataType.FLOAT, element_id="summe")
    resolver = _resolver_for(target, parent)
    with pytest.raises(CorrectnessError, match=r"\[H2\]") as exc:
        insert_subprocess(
            parent,
            "start",
            "sub_target",
            1,
            input_mapping={"betrag": "summe"},  # never written by the parent
            resolver=resolver,
        )
    assert any(
        "not guaranteed to be written" in f.message for f in exc.value.findings
    ), exc.value.findings


def test_conditional_follow_up_needs_its_element_written_on_every_path() -> None:
    """F4 (evaluability): an unwritten element makes the instance uncompletable.

    The predicate is evaluated while the *final* activity is completed. If it
    reads an element that no path guarantees, the evaluator raises there -- so
    the instance can never be completed at all, the same dead end K1 produced,
    reached by a different route. Verified end-to-end before this check existed.
    """

    target = _released_target()
    parent = create_empty_schema("Haupt", schema_id="parent")
    parent = add_data_element(parent, "flag", DataType.BOOLEAN, element_id="flag")
    parent = serial_insert(parent, "A1", after_node_id="start")
    resolver = _resolver_for(target, parent)
    with pytest.raises(CorrectnessError, match=r"\[F4\]") as exc:
        link_follow_up(
            parent,
            "sub_target",
            target_version=1,
            trigger=FollowUpTrigger.CONDITIONAL,
            condition="flag",  # never written
            resolver=resolver,
        )
    assert any(
        "not written on every path" in f.message for f in exc.value.findings
    ), exc.value.findings


def test_conditional_follow_up_rejects_element_written_in_only_one_branch() -> None:
    """The XOR case: written on *a* path is not written on *every* path."""

    from procworks import BranchSpec, conditional_insert

    target = _released_target()
    parent = create_empty_schema("Haupt", schema_id="parent")
    parent = add_data_element(parent, "betrag", DataType.INTEGER, element_id="betrag")
    parent = add_data_element(parent, "notiz", DataType.STRING, element_id="notiz")
    parent = serial_insert(parent, "Erfassen", after_node_id="start")
    erfassen = next(n.id for n in parent.nodes.values() if n.label == "Erfassen")
    parent = connect_data(parent, erfassen, "betrag", AccessMode.WRITE)
    parent = conditional_insert(
        parent,
        erfassen,
        discriminator="betrag",
        branches=[BranchSpec(label="Klein", upper=100), BranchSpec(label="Gross")],
    )
    klein = next(n.id for n in parent.nodes.values() if n.label == "Klein")
    parent = connect_data(parent, klein, "notiz", AccessMode.WRITE)  # one branch only
    resolver = _resolver_for(target, parent)
    with pytest.raises(CorrectnessError, match=r"\[F4\]"):
        link_follow_up(
            parent,
            "sub_target",
            target_version=1,
            trigger=FollowUpTrigger.CONDITIONAL,
            condition='notiz == "x"',
            resolver=resolver,
        )


def test_deleting_the_writer_of_a_mapped_input_is_rejected() -> None:
    """The H2 input rule must hold for resolver-free operations too.

    ``delete_node`` validates without a resolver, so a composition rule placed
    behind the resolver gate would not see it: removing the step that writes a
    mapped input left a model that failed validation afterwards. Found by
    fuzzing the sub-process operations, which is why the check lives in the
    resolver-free part of the composition rules.
    """

    target = _released_target()  # betrag: FLOAT
    parent = create_empty_schema("Haupt", schema_id="parent")
    parent = add_data_element(parent, "summe", DataType.FLOAT, element_id="summe")
    parent = serial_insert(parent, "Vorbereiten", after_node_id="start")
    pre = next(n.id for n in parent.nodes.values() if n.label == "Vorbereiten")
    parent = connect_data(parent, pre, "summe", AccessMode.WRITE)
    resolver = _resolver_for(target, parent)
    parent = insert_subprocess(
        parent, pre, "sub_target", 1, input_mapping={"betrag": "summe"}, resolver=resolver
    )
    assert validate(parent, resolver) == []

    # Removing the only writer would strand the sub-process input -- and the
    # operation must catch that itself, without being handed a resolver.
    from procworks import delete_node

    with pytest.raises(CorrectnessError, match=r"\[H2\]"):
        delete_node(parent, pre)
