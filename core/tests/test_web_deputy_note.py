# SPDX-License-Identifier: BUSL-1.1
"""Deputies see for whom they work; operators are told who sets a deputy.

A deputy saw an absent colleague's task without "in Vertretung für …", and an
operator without a deputy was told to set one in the resource view -- a view
operators cannot see. The worklist now reads ``OpenTask.deputy_of`` and the
hint depends on the role.
"""

from __future__ import annotations

from pathlib import Path

from web_vm import needs_node, run_app_js


@needs_node
def test_deputy_task_names_the_absent_colleague(tmp_path: Path) -> None:
    res = run_app_js(
        r"""
state.agentDirectory = { "a-karin": { name: "Karin Kredel" } };
const own = deputyNote({ deputy_of: { "a-bianca": "a-karin" } }, "a-bianca");
const none = deputyNote({ deputy_of: {} }, "a-bianca");
const other = deputyNote({ deputy_of: { "a-x": "a-karin" } }, "a-bianca");
return { own: textOf(own), none: none === null, other: other === null };
""",
        tmp_path,
    )
    assert res["own"] == "in Vertretung für Karin Kredel"
    # Negativ: wer die Aufgabe selbst traegt, bekommt keinen Vermerk.
    assert res["none"] is True and res["other"] is True


@needs_node
def test_hint_without_deputy_depends_on_who_can_set_one(tmp_path: Path) -> None:
    res = run_app_js(
        r"""
state.principal = { roles: ["operator"] };
const op = textOf(absenceDeputyBanner(null, null));
state.principal = { roles: ["modeler"] };
const mod = textOf(absenceDeputyBanner(null, null));
return { op, mod };
""",
        tmp_path,
    )
    assert "bei der Administration hinterlegen lassen" in res["op"]
    assert "Ressourcensicht" not in res["op"]
    assert "Vertretung in der Ressourcensicht setzen" in res["mod"]
