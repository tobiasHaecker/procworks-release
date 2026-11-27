# SPDX-License-Identifier: BUSL-1.1
"""Nach einem Konflikt und nach einem Dialog ist die Aufgabenliste sofort aktuell.

Hat jemand anderes eine Aufgabe schon erledigt, lehnt der Kern den eigenen
Abschluss mit 409 ab; die Meldung raet zum Aktualisieren. Die Liste zeigte den
erledigten Schritt trotzdem noch bis zu 40 s: ``pollLiveUpdates`` uebernahm die
neue Revision auch dann, wenn es wegen des offenen Dialogs nicht neu zeichnen
durfte -- die Aenderung galt danach als gesehen und wurde erst vom 30-s-Tick
nachgeholt. Jetzt bleibt eine verpasste Revision offen, bis wirklich neu
gezeichnet wurde, und ein 409 laedt die Laufzeit-Sicht sofort nach.

Geprueft wird das Verhalten in Node (``web_vm.py``) mit der echten
Aufgabenliste (``viewTasks``) und dem echten Abschlussdialog
(``completeTask`` -> ``promptComplete``).
"""

from __future__ import annotations

from pathlib import Path

from web_vm import needs_node, run_app_js

_SETUP = r"""
state.principal = { subject: "erika", roles: ["operator"], agent_id: "a1", display_name: "Erika" };
state.view = "tasks"; state.revision = 1;
byId("auth-overlay").style.display = "none";
document.body.appendChild(byId("content"));
document.body.appendChild(byId("modal-root"));
let rev = 1;
let completeStatus = 409;
let tasks = [
  { instance_id: "i1", node_id: "n1", schema_id: "s1", label: "Antrag erfassen",
    eligible_agents: ["a1"] },
  { instance_id: "i2", node_id: "n1", schema_id: "s1", label: "Antrag pruefen",
    eligible_agents: ["a1"] },
];
respond((p, m) => {
  if (p === "/monitoring/revision") return { body: { revision: rev } };
  if (p === "/directory/agents") return { body: [{ agent_id: "a1", name: "Erika" }] };
  if (p === "/me/tasks") return { body: tasks };
  if (p === "/schemas/s1") return { body: { name: "Urlaub", version: 1, data_elements: {},
    data_accesses: [], forms: {} } };
  if (p === "/instances/i2") return { body: { data_values: {} } };
  if (m === "POST" && p === "/instances/i2/complete") {
    return { status: completeStatus, body: { detail: { code: "EX.not-activated",
      params: { node: "Antrag pruefen" }, message: "x" } } };
  }
  return { status: 404 };
});
const settle = async () => {
  do { await new Promise((r) => setTimeout(r, 5)); } while (renderBusy);
};
const content = byId("content");
const listed = () => textOf(content).includes("Antrag pruefen");
const listLoads = () => calls.filter((c) => c === "GET /me/tasks").length;
const modalButton = (label) => byId("modal-root").querySelectorAll("button")
  .find((b) => textOf(b) === label);
render(); await settle();
"""


@needs_node
def test_conflict_on_completion_reloads_the_list_at_once(tmp_path: Path) -> None:
    """Positiv: 409 beim Abschliessen -> die Liste laedt sofort nach; der
    erledigte Schritt ist weg, sobald der Dialog geschlossen wird."""
    res = run_app_js(
        _SETUP
        + r"""
await completeTask(tasks[1], "a1");
// Parallel hat jemand anderes den Schritt erledigt.
tasks = tasks.filter((t) => t.instance_id !== "i2"); rev = 2;
const before = listLoads();
await modalButton("Abschließen").click(); await settle();
const dialogOpen = byId("modal-root").children.length > 0;
const reloads = listLoads() - before;
await modalButton("Abbrechen").click();
return { dialogOpen, reloads, listed: listed(),
         polled: calls.includes("GET /monitoring/revision") };
""",
        tmp_path,
    )
    assert res["dialogOpen"] is True, "der Dialog bleibt nach der Ablehnung offen"
    assert res["reloads"] == 1, "ein 409 muss die Liste sofort neu laden"
    assert res["listed"] is False, "der erledigte Schritt darf nicht mehr gelistet sein"
    assert res["polled"] is False, "das Nachladen haengt nicht am Revisions-Poll"


@needs_node
def test_other_rejections_do_not_reload(tmp_path: Path) -> None:
    """Negativ: Eine Ablehnung, die nichts ueber einen geaenderten Stand sagt
    (422, falsche Eingabe), laedt die Liste nicht nach."""
    res = run_app_js(
        _SETUP
        + r"""
completeStatus = 422;
await completeTask(tasks[1], "a1");
const before = listLoads();
await modalButton("Abschließen").click(); await settle();
return { reloads: listLoads() - before, listed: listed() };
""",
        tmp_path,
    )
    assert res == {"reloads": 0, "listed": True}


@needs_node
def test_revision_missed_during_a_dialog_is_caught_up_after_it_closes(
    tmp_path: Path,
) -> None:
    """Eine Revision, die bei offenem Dialog eintraf, gilt nicht als gesehen:
    Der erste Poll nach dem Schliessen zeichnet neu -- ohne weitere Revision."""
    res = run_app_js(
        _SETUP
        + r"""
openModal("Irgendein Dialog", el("p", null, "Text"), () => true, "OK");
tasks = tasks.filter((t) => t.instance_id !== "i2"); rev = 2;
await pollLiveUpdates(); await settle();
const whileOpen = listed();
await modalButton("Abbrechen").click();
await pollLiveUpdates(); await settle();
return { whileOpen, afterClose: listed(), revision: state.revision };
""",
        tmp_path,
    )
    assert res["whileOpen"] is True, "bei offenem Dialog wird nicht neu gezeichnet"
    assert res["afterClose"] is False, "die verpasste Revision muss nachgeholt werden"
    assert res["revision"] == 2


@needs_node
def test_conflict_with_unsent_input_offers_reload_instead_of_wiping_it(
    tmp_path: Path,
) -> None:
    """Grenzfall: Steht im Inhalt eine nicht abgeschickte Eingabe, zeichnet
    auch ein 409 nicht ueber sie hinweg -- es bietet „Aktualisieren“ an."""
    res = run_app_js(
        _SETUP
        + r"""
const note = el("input", { type: "text" });
content.appendChild(note);
note.value = "halb";
markContentDirty({ target: note });
await completeTask(tasks[1], "a1");
const before = listLoads();
await modalButton("Abschließen").click(); await settle();
return { reloads: listLoads() - before, value: note.value,
         hint: !!content.querySelector(".live-refresh-hint") };
""",
        tmp_path,
    )
    assert res == {"reloads": 0, "value": "halb", "hint": True}
