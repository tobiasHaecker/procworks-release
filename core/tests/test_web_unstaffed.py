# SPDX-License-Identifier: BUSL-1.1
"""Monitoring „Niemand zuständig“ / „Nur Abwesende zuständig“ im Web-Client.

Der Kern meldet neben „keine Regel“ und „Regel findet niemanden“ auch Schritte,
deren Zuständige keinen Login haben oder alle ohne Vertretung abwesend sind.
Die Oberfläche trennt die schwächere Kategorie ab und nennt die Personen.
"""

from __future__ import annotations

from pathlib import Path

from web_vm import needs_node, run_app_js

_ROWS = r"""
state.agentDirectory = {
  "a-petra": { agent_id: "a-petra", name: "Petra Prüf" },
  "a-tom": { agent_id: "a-tom", name: "Tom Berger" },
};
const rows = [
  { instance_id: "i1", node_id: "n1", label: "Prüfen", reason: "no_login", agent_ids: ["a-petra"] },
  { instance_id: "i2", node_id: "n2", label: "Genehmigen", reason: "only_absent",
    agent_ids: ["a-tom"] },
  { instance_id: "i3", node_id: "n3", label: "Freigeben", reason: "nobody" },
];
"""


@needs_node
def test_reasons_name_the_people_and_categories_split(tmp_path: Path) -> None:
    res = run_app_js(
        _ROWS
        + r"""
const { stalled, absentOnly } = splitUnstaffed(rows);
return { stalled: stalled.map((u) => u.instance_id),
  absentOnly: absentOnly.map((u) => u.instance_id), texts: rows.map(unstaffedReasonText) };
""",
        tmp_path,
    )
    assert res["stalled"] == ["i1", "i3"]
    assert res["absentOnly"] == ["i2"]
    assert res["texts"] == [
        "kein Login: Petra Prüf",
        "abwesend ohne Vertretung: Tom Berger",
        "Regel findet aktuell niemanden",
    ]


@needs_node
def test_absence_hint_names_the_person_for_a_supervisor(tmp_path: Path) -> None:
    """Die Aufsicht liest den Namen der Person, nicht „deine Abwesenheit“."""
    res = run_app_js(
        r"""
return {
  other: textOf(absenceDeputyBanner("Tom Berger", null)),
  own: textOf(absenceDeputyBanner(null, null)),
  otherDeputy: textOf(absenceDeputyBanner("Tom Berger", "Erika Sander")),
};
""",
        tmp_path,
    )
    assert "Tom Berger" in res["other"] and "deine" not in res["other"]
    assert "Nur Abwesende zuständig" in res["other"]
    assert "deine Aufgaben" in res["own"]
    assert "Erika Sander erhält die Aufgaben von Tom Berger" in res["otherDeputy"]


@needs_node
def test_a_deputy_excluded_by_four_eyes_is_named_instead_of_no_deputy(tmp_path: Path) -> None:
    """Erika abwesend, ihre Vertretung Tom hat „Erfassen“ erledigt: Der Text
    sagt, dass die Vertretung nach dem Vier-Augen-Prinzip ausgeschlossen ist --
    nicht „ohne Vertretung“."""

    res = run_app_js(
        r"""
state.agentDirectory = {
  "erika": { agent_id: "erika", name: "Erika Sander" },
  "tom": { agent_id: "tom", name: "Tom Berger" },
};
return unstaffedReasonText({ instance_id: "i", node_id: "p", label: "Prüfen",
  reason: "only_absent", agent_ids: ["erika"],
  excluded_deputies: [{ agent_id: "tom", stands_in_for: "erika", step_id: "e",
                        step_label: "Erfassen" }] });
""",
        tmp_path,
    )
    assert "ohne Vertretung" not in res
    assert res == (
        "abwesend: Erika Sander – Vertretung Tom Berger ist nach dem Vier-Augen-Prinzip "
        "ausgeschlossen (hat „Erfassen“ erledigt)"
    )
