# SPDX-License-Identifier: BUSL-1.1
"""Ein Neuzeichnen der Laufzeit-Sichten behaelt Fokus und aufgeklappte Bereiche.

„Meine Aufgaben“ zeichnet sich bei neuem Fortschritt alle paar Sekunden neu.
Frueher baute jedes Neuzeichnen den Inhalt komplett neu auf: Der fokussierte
Knopf verschwand, der Tastaturfokus fiel auf ``<body>``, ein Enter danach ging
ins Leere, und aufgeklappte Bereiche klappten zu. Seitdem merkt ``render`` den
Ansichtszustand (``captureViewState``) und stellt ihn danach wieder her
(``restoreViewState``): Fokus auf demselben Knopf derselben Zeile
(``data-row-key``), sonst auf dem Knopf an derselben Stelle der Zeile, sonst
auf der Liste (``data-focus-region``), nie auf ``<body>``.

Geprueft wird das Verhalten in Node (``web_vm.py``) mit der echten
Aufgabenliste (``viewTasks``) hinter ``pollLiveUpdates``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from web_vm import needs_node, run_app_js

# Gebundener Login mit zwei offenen Aufgaben; die Revision steigt je Poll.
_TASKS = r"""
state.principal = { subject: "erika", roles: ["operator"], agent_id: "a1", display_name: "Erika" };
state.view = "tasks"; state.revision = 1;
byId("auth-overlay").style.display = "none";
// Wie im Browser haengen Inhalt und Dialogwurzel im Dokument.
document.body.appendChild(byId("content"));
document.body.appendChild(byId("modal-root"));
let rev = 1;
let tasks = [
  { instance_id: "i1", node_id: "n1", schema_id: "s1", label: "Antrag erfassen",
    eligible_agents: ["a1"] },
  { instance_id: "i2", node_id: "n1", schema_id: "s1", label: "Antrag erfassen",
    eligible_agents: ["a1"] },
];
respond((p, m) => {
  if (p === "/monitoring/revision") return { body: { revision: rev } };
  if (p === "/directory/agents") return { body: [{ agent_id: "a1", name: "Erika" }] };
  if (p === "/me/tasks") return { body: tasks };
  if (p === "/schemas/s1") return { body: { name: "Urlaub", version: 1 } };
  if (m === "POST" && p === "/instances/i2/claim") {
    tasks = tasks.map((t) => t.instance_id === "i2" ? { ...t, claimed_by: "a1" } : t);
    return { body: {} };
  }
  return { status: 404 };
});
const settle = async () => {
  do { await new Promise((r) => setTimeout(r, 5)); } while (renderBusy);
};
const content = byId("content");
const rowOf = (n) => {
  for (let x = n; x; x = x.parentNode) {
    if (x.hasAttribute && x.hasAttribute("data-row-key")) return x.getAttribute("data-row-key");
  }
  return null;
};
const buttonIn = (key, label) => content.querySelectorAll("button")
  .find((b) => rowOf(b) === key && textOf(b) === label);
const liveTick = async () => { rev += 1; await pollLiveUpdates(); await settle(); };
const focusInfo = () => {
  const a = document.activeElement;
  return { connected: !!(a && a.isConnected), tag: a ? a.tagName : null,
           text: a && a.tagName === "BUTTON" ? textOf(a) : null, row: rowOf(a),
           region: a && a.getAttribute ? a.getAttribute("data-focus-region") : null };
};
render(); await settle();
"""


@needs_node
def test_focused_button_survives_three_live_updates_and_enter_still_acts(
    tmp_path: Path,
) -> None:
    """Positiv: Fokus auf „Übernehmen“ der zweiten Zeile bleibt ueber drei
    Live-Aktualisierungen auf genau diesem Knopf; ausgeloest landet er beim
    Kern."""
    res = run_app_js(
        _TASKS
        + r"""
buttonIn("i2:n1", "Übernehmen").focus();
const seen = [];
for (let i = 0; i < 3; i++) { await liveTick(); seen.push(focusInfo()); }
await document.activeElement.click();
return { seen, claimed: calls.includes("POST /instances/i2/claim"),
         wrongRow: calls.includes("POST /instances/i1/claim") };
""",
        tmp_path,
    )
    for info in res["seen"]:
        assert info == {"connected": True, "tag": "BUTTON", "text": "Übernehmen",
                        "row": "i2:n1", "region": None}, info
    assert res["claimed"] is True and res["wrongRow"] is False


@needs_node
def test_after_claim_focus_stays_in_the_row_at_the_same_place(tmp_path: Path) -> None:
    """Nach dem Uebernehmen heisst der Knopf „Zurücklegen“ -- der Fokus liegt
    auf ihm (gleiche Stelle derselben Zeile), nicht auf ``<body>``."""
    res = run_app_js(
        _TASKS
        + r"""
const btn = buttonIn("i2:n1", "Übernehmen");
btn.focus();
await btn.click(); await settle();
return focusInfo();
""",
        tmp_path,
    )
    assert res == {"connected": True, "tag": "BUTTON", "text": "Zurücklegen",
                   "row": "i2:n1", "region": None}


@needs_node
def test_vanished_row_hands_focus_to_the_list_not_to_body(tmp_path: Path) -> None:
    """Negativ: Erledigt jemand anderes die fokussierte Aufgabe, landet der
    Fokus auf der Aufgabenliste -- nicht auf einer fremden Zeile, nicht auf
    ``<body>``."""
    res = run_app_js(
        _TASKS
        + r"""
buttonIn("i2:n1", "Erledigen").focus();
tasks = tasks.filter((t) => t.instance_id !== "i2");
await liveTick();
return focusInfo();
""",
        tmp_path,
    )
    assert res["connected"] is True
    assert res["region"] == "tasks", res
    assert res["row"] is None, "Fokus darf nicht in eine andere Zeile springen"


@needs_node
def test_focus_outside_the_content_is_not_taken_away(tmp_path: Path) -> None:
    """Grenzfall: Steht der Fokus ausserhalb des Inhalts (Seitenleiste), holt
    das Neuzeichnen ihn nicht in die Liste."""
    res = run_app_js(
        _TASKS
        + r"""
buttonIn("i2:n1", "Übernehmen").focus();
const side = el("button", null, "Hilfe");
document.body.appendChild(side);
side.focus();
await liveTick();
return document.activeElement === side;
""",
        tmp_path,
    )
    assert res is True


@needs_node
def test_open_details_stay_open_across_a_live_update(tmp_path: Path) -> None:
    """Ein aufgeklappter Bereich (Uebergangstabelle der Prozesskarte) bleibt
    nach dem Neuzeichnen offen; ein zugeklappter bleibt zu."""
    res = run_app_js(
        r"""
state.principal = { subject: "vera", roles: ["viewer"] };
state.view = "monitor"; state.revision = 1;
byId("auth-overlay").style.display = "none";
document.body.appendChild(byId("content"));
let rev = 1;
respond((p) => p === "/monitoring/revision" ? { body: { revision: rev } } : { status: 404 });
VIEW_META.monitor.fn = async () => {
  const c = byId("content"); clear(c);
  await Promise.resolve();
  c.appendChild(el("details", null, el("summary", null, "Alle beobachteten Übergänge"), "Tabelle"));
  c.appendChild(el("details", null, el("summary", null, "Weitere Kennzahlen"), "x"));
};
const settle = async () => {
  do { await new Promise((r) => setTimeout(r, 5)); } while (renderBusy);
};
render(); await settle();
byId("content").querySelectorAll("details")[0].setAttribute("open", "");
rev = 2; await pollLiveUpdates(); await settle();
const d = byId("content").querySelectorAll("details");
return { first: d[0].hasAttribute("open"), second: d[1].hasAttribute("open"), count: d.length };
""",
        tmp_path,
    )
    assert res == {"first": True, "second": False, "count": 2}


@needs_node
def test_dialog_returns_focus_to_the_redrawn_opener(tmp_path: Path) -> None:
    """Fokus-Rueckgabe: Zeichnet die Sicht hinter einem Dialog neu, ist der
    alte Ausloeser weg -- „Abbrechen“ gibt den Fokus dann an den gleichen Knopf
    der neu gezeichneten Zeile statt an ``<body>``."""
    res = run_app_js(
        _TASKS
        + r"""
const opener = buttonIn("i2:n1", "Übernehmen");
opener.focus();
openModal("Test", el("p", null, "Text"), () => true, "OK");
render({ keepScroll: true }); await settle();
const stillInDialog = byId("modal-root").contains(document.activeElement);
const cancel = byId("modal-root").querySelectorAll("button").find((b) => textOf(b) === "Abbrechen");
await cancel.click();
return { stillInDialog, after: focusInfo(), sameNode: document.activeElement === opener };
""",
        tmp_path,
    )
    assert res["stillInDialog"] is True, "waehrend der Dialog offen ist, bleibt der Fokus dort"
    assert res["sameNode"] is False
    assert res["after"] == {"connected": True, "tag": "BUTTON", "text": "Übernehmen",
                            "row": "i2:n1", "region": None}


# --------------------------------------------------------------------------
# Fokus nach einem Dialog, dessen Ausloeser waehrend des Ladens gesperrt war
# --------------------------------------------------------------------------
#
# Im Browser gibt ein fokussierter Knopf den Fokus an ``<body>`` ab, sobald er
# ``disabled`` wird -- genau das tut die Doppelklick-Sperre, waehrend
# „Erledigen“ Vorgang und Schema laedt. Der Mini-DOM tut das nicht von selbst;
# ``_BROWSER_FOCUS`` bildet es nach (und dass ein gesperrter Knopf keinen
# Fokus annimmt).
_BROWSER_FOCUS = r"""
const P = Object.getPrototypeOf(el("button"));
const realSetAttribute = P.setAttribute;
P.setAttribute = function (k, v) {
  realSetAttribute.call(this, k, v);
  if (k === "disabled" && document.activeElement === this) document.activeElement = document.body;
};
const realFocus = P.focus;
P.focus = function () { if (!this.hasAttribute("disabled")) realFocus.call(this); };
"""

# Abschluss-Dialog fuer i2/n1; ``completeAnswer`` steuert die Antwort des Kerns.
_COMPLETE = r"""
let completeAnswer = 200;
const baseRespond = (p, m) => {
  if (p === "/instances/i2") return { body: { id: "i2", schema_id: "s1", data_values: {} } };
  if (p === "/schemas/s1") {
    return { body: { name: "Urlaub", version: 1,
                     nodes: { n1: { id: "n1", label: "Antrag erfassen" } },
                     data_elements: {}, data_accesses: [] } };
  }
  if (m === "POST" && p === "/instances/i2/complete") {
    // Erfolg wie Konflikt: Die Aufgabe ist danach nicht mehr in der Liste.
    tasks = tasks.filter((t) => t.instance_id !== "i2");
    return completeAnswer === 200 ? { body: {} }
      : { status: 409, body: { detail: "Der Schritt ist nicht mehr offen." } };
  }
  return null;
};
"""


def _complete_scenario(action: str) -> str:
    """Szenario: „Erledigen“ der Zeile i2 per Tastatur, Dialog, dann ``action``.

    :param action: JavaScript, das den offenen Dialog beendet (``dialog`` ist
        der Dialog, ``buttonInDialog(text)`` sucht einen Knopf darin).
    :returns: der Szenario-Rumpf; er liefert ``focusInfo()`` nach dem Schliessen
        plus ``onBody`` und ``dialogOpen``.
    """
    return (
        _TASKS
        + _BROWSER_FOCUS
        + _COMPLETE
        + r"""
const tasksRespond = (p, m) => {
  if (p === "/monitoring/revision") return { body: { revision: rev } };
  if (p === "/directory/agents") return { body: [{ agent_id: "a1", name: "Erika" }] };
  if (p === "/me/tasks") return { body: tasks };
  return { status: 404 };
};
respond((p, m) => baseRespond(p, m) || tasksRespond(p, m));
const trigger = buttonIn("i2:n1", "Erledigen");
trigger.focus();
await trigger.click(); await settle();
const dialog = byId("modal-root").children[0];
const buttonInDialog = (t) => byId("modal-root").querySelectorAll("button")
  .find((b) => textOf(b) === t);
"""
        + action
        + r"""
await settle();
const a = document.activeElement;
return { ...focusInfo(), onBody: a === document.body || !a,
         dialogOpen: byId("modal-root").children.length > 0, sameNode: a === trigger };
"""
    )


@needs_node
def test_escape_after_locked_trigger_returns_focus_to_that_trigger(tmp_path: Path) -> None:
    """Escape im Abschluss-Dialog: Der Fokus kehrt auf „Erledigen“ derselben
    Zeile zurueck, obwohl der Knopf beim Oeffnen gesperrt war und den Fokus
    abgegeben hatte (der Dialog kannte sonst nur ``<body>`` als Ausloeser)."""
    res = run_app_js(
        _complete_scenario('await dialog.dispatch("keydown", { key: "Escape" });'), tmp_path)
    assert res["dialogOpen"] is False
    assert res["onBody"] is False, res
    assert res["sameNode"] is True, res
    assert (res["text"], res["row"]) == ("Erledigen", "i2:n1")


@needs_node
def test_cancel_after_locked_trigger_returns_focus_to_that_trigger(tmp_path: Path) -> None:
    """„Abbrechen“ wirkt wie Escape: Fokus zurueck auf „Erledigen“."""
    res = run_app_js(_complete_scenario('await buttonInDialog("Abbrechen").click();'), tmp_path)
    assert res["onBody"] is False and res["sameNode"] is True, res


@needs_node
def test_completion_hands_focus_to_the_list_when_the_row_is_gone(tmp_path: Path) -> None:
    """Erfolgreicher Abschluss: Die Zeile verschwindet, der Fokus liegt auf
    der Aufgabenliste -- nicht auf ``<body>`` und nicht in der Nachbarzeile."""
    res = run_app_js(_complete_scenario('await buttonInDialog("Abschließen").click();'), tmp_path)
    assert res["dialogOpen"] is False
    assert res["onBody"] is False, res
    assert res["region"] == "tasks" and res["row"] is None, res


@needs_node
def test_conflict_then_cancel_hands_focus_to_the_list(tmp_path: Path) -> None:
    """Negativ: Der Kern lehnt mit 409 ab (jemand anderes war schneller), der
    Dialog bleibt offen, die Liste dahinter ist schon neu gezeichnet. Nach
    „Abbrechen“ liegt der Fokus auf der Liste, nicht auf ``<body>``."""
    res = run_app_js(
        _complete_scenario(
            'completeAnswer = 409;\n'
            'await buttonInDialog("Abschließen").click(); await settle();\n'
            'const stillOpen = byId("modal-root").children.length > 0;\n'
            'if (!stillOpen) throw new Error("Dialog nach 409 geschlossen");\n'
            'await buttonInDialog("Abbrechen").click();'),
        tmp_path,
    )
    assert res["onBody"] is False, res
    assert res["region"] == "tasks", res


@needs_node
def test_locked_button_gets_its_focus_back_when_its_action_ends(tmp_path: Path) -> None:
    """Die Doppelklick-Sperre gibt den Fokus, den sie dem Knopf entzogen hat,
    nach der Aktion zurueck -- aber nur, wenn ihn niemand sonst genommen hat."""
    res = run_app_js(
        _BROWSER_FOCUS
        + r"""
let release;
const pending = () => new Promise((r) => { release = r; });
const mk = (t) => {
  const b = el("button", { onClick: () => pending() }, t);
  document.body.appendChild(b);
  return b;
};
const a = mk("Speichern"), other = mk("Hilfe");
a.focus();
let done = a.click();
const lostWhileBusy = document.activeElement === document.body;
release(); await done;
const back = document.activeElement === a;
a.focus();
done = a.click();
other.focus();          // die Person geht waehrenddessen weiter
release(); await done;
return { lostWhileBusy, back, notStolen: document.activeElement === other };
""",
        tmp_path,
    )
    assert res == {"lostWhileBusy": True, "back": True, "notStolen": True}


# --------------------------------------------------------------------------
# Monitoring: zeichnet erst nach dem Laden neu, Filterknopf ueber stabile Kennung
# --------------------------------------------------------------------------

_MONITOR = r"""
state.principal = { subject: "vera", roles: ["viewer"] };
state.view = "monitor"; state.revision = 1;
byId("auth-overlay").style.display = "none";
document.body.appendChild(byId("content"));
document.body.appendChild(byId("modal-root"));
let rev = 1;
let ids = ["i1"];
respond((p) => {
  if (p === "/monitoring/revision") return { body: { revision: rev } };
  const doc = (id) => ({ id, state: "RUNNING", schema_id: "s1", schema_version: 1,
                         node_states: {} });
  if (p === "/monitoring/instances") return { body: ids.map(doc) };
  const one = /^\/instances\/(i\d+)$/.exec(p);
  if (one) return { body: doc(one[1]) };
  if (p === "/monitoring/tasks") return { body: [] };
  if (p === "/schemas/s1") return { body: { name: "Urlaub", version: 1 } };
  if (p === "/instance-titles") return { body: {} };
  if (p === "/monitoring/unstaffed") return { body: [] };
  return { status: 404 };
});
// Laden anhalten: Solange ``gate`` steht, kommt die Instanzliste nicht an.
let gate = null;
const realFetch = fetch;
fetch = async (u, o) => {
  if (gate && /\/monitoring\/instances$/.test(String(u))) await gate.p;
  return realFetch(u, o);
};
const hold = () => {
  let r;
  const p = new Promise((x) => { r = x; });
  gate = { p, open: () => { gate = null; r(); } };
};
const settle = async () => {
  do { await new Promise((r) => setTimeout(r, 5)); } while (renderBusy);
};
const content = byId("content");
const filterBtn = (label) => content.querySelectorAll("button")
  .find((b) => textOf(b).startsWith(label + " ("));
render(); await settle();
"""


@needs_node
def test_monitor_filter_button_keeps_focus_when_its_count_changes(tmp_path: Path) -> None:
    """Die Beschriftung „Laufend (1)“ wird nach einem neuen Vorgang zu
    „Laufend (2)“ -- der Fokus bleibt trotzdem auf diesem Filterknopf (stabile
    Kennung statt Beschriftung)."""
    res = run_app_js(
        _MONITOR
        + r"""
filterBtn("Laufend").focus();
ids = ["i1", "i2"]; rev = 2;
await pollLiveUpdates(); await settle();
const a = document.activeElement;
return { text: a && a.tagName === "BUTTON" ? textOf(a) : null, connected: !!(a && a.isConnected) };
""",
        tmp_path,
    )
    assert res == {"text": "Laufend (2)", "connected": True}


@needs_node
def test_monitor_keeps_old_content_and_focus_while_reloading(tmp_path: Path) -> None:
    """Waehrend die Aktualisierung laedt, bleibt der bisherige Inhalt stehen
    (als ``aria-busy`` markiert) und der Fokus auf seinem Knopf; erst danach
    wird ausgetauscht."""
    res = run_app_js(
        _MONITOR
        + r"""
const btn = filterBtn("Laufend");
btn.focus();
hold(); ids = ["i1", "i2"]; rev = 2;
const poll = pollLiveUpdates();
await new Promise((r) => setTimeout(r, 30));
const during = { stillThere: btn.isConnected, focus: document.activeElement === btn,
                 busy: content.getAttribute("aria-busy"), panels: content.children.length };
gate.open(); await poll; await settle();
const a = document.activeElement;
return { during, after: { busy: content.getAttribute("aria-busy"), text: textOf(a),
                          replaced: !btn.isConnected } };
""",
        tmp_path,
    )
    assert res["during"]["stillThere"] is True and res["during"]["focus"] is True, res
    assert res["during"]["busy"] == "true" and res["during"]["panels"] > 0, res
    assert res["after"] == {"busy": None, "text": "Laufend (2)", "replaced": True}


@needs_node
def test_switching_to_monitor_does_not_show_the_previous_view_while_loading(
    tmp_path: Path,
) -> None:
    """Grenzfall: Wer aus einer anderen Sicht ins Monitoring wechselt, sieht
    waehrend des Ladens nicht den Inhalt der alten Sicht unter dem neuen Titel."""
    res = run_app_js(
        _MONITOR
        + r"""
state.view = "admin";
VIEW_META.admin.fn = async () => {
  const c = byId("content"); clear(c);
  c.appendChild(el("p", { "data-old": "1" }, "Alt"));
};
render(); await settle();
state.view = "monitor"; hold(); render();
await new Promise((r) => setTimeout(r, 30));
const old = content.querySelectorAll("[data-old]").length;
gate.open(); await settle();
return { old, monitorDrawn: !!filterBtn("Laufend") };
""",
        tmp_path,
    )
    assert res == {"old": 0, "monitorDrawn": True}


@needs_node
def test_logout_during_reload_drops_the_staged_content(tmp_path: Path) -> None:
    """Negativ: Endet die Sitzung, waehrend das Monitoring nachlaedt, landet
    der fertig geladene Stand nicht mehr auf dem Schirm."""
    res = run_app_js(
        _MONITOR
        + r"""
hold(); ids = ["i1", "i2"]; rev = 2;
const poll = pollLiveUpdates();
await new Promise((r) => setTimeout(r, 30));
resetSessionState();
gate.open(); await poll; await settle();
return { children: content.children.length, busy: content.getAttribute("aria-busy") };
""",
        tmp_path,
    )
    assert res == {"children": 0, "busy": None}


@needs_node
def test_logout_during_first_draw_shows_nothing_of_the_old_session(tmp_path: Path) -> None:
    """Negativ: Endet die Sitzung, waehrend eine Laufzeit-Sicht zum ersten Mal
    laedt (Wechsel ins Monitoring), landet ihr Stand nicht im Inhalt -- auch
    nicht, wenn die Antworten erst nach dem Abmelden eintreffen."""
    res = run_app_js(
        _MONITOR
        + r"""
state.view = "admin";
VIEW_META.admin.fn = async () => { clear(byId("content")); };
render(); await settle();
state.view = "monitor"; hold(); render();
await new Promise((r) => setTimeout(r, 30));
resetSessionState();
gate.open(); await settle();
return { text: textOf(content), busy: content.getAttribute("aria-busy"),
         filter: !!filterBtn("Laufend") };
""",
        tmp_path,
    )
    assert res == {"text": "", "busy": None, "filter": False}


@needs_node
def test_first_draw_without_logout_still_appears(tmp_path: Path) -> None:
    """Gegenstueck: Ohne Abmelden erscheint die erste Zeichnung wie bisher."""
    res = run_app_js(
        _MONITOR
        + r"""
state.view = "admin";
VIEW_META.admin.fn = async () => { clear(byId("content")); };
render(); await settle();
state.view = "monitor"; hold(); render();
await new Promise((r) => setTimeout(r, 30));
gate.open(); await settle();
return { filter: !!filterBtn("Laufend"), busy: content.getAttribute("aria-busy") };
""",
        tmp_path,
    )
    assert res == {"filter": True, "busy": None}


@needs_node
def test_render_queued_before_logout_is_dropped(tmp_path: Path) -> None:
    """Negativ: Hat ein Zeit-Tick waehrend des Nachladens einen weiteren Lauf
    vorgemerkt, holt ``render`` ihn nach dem Abmelden nicht mehr nach -- sonst
    zeichnete er ein leeres Geruest mit einer Fehlermeldung der beendeten
    Sitzung und fragte den Kern ohne Anmeldung."""
    res = run_app_js(
        _MONITOR
        + r"""
hold(); ids = ["i1", "i2"]; rev = 2;
const poll = pollLiveUpdates();
await new Promise((r) => setTimeout(r, 30));
render({ keepScroll: true });       // vorgemerkt (Zeit-Tick)
const queuedBefore = renderQueued;
resetSessionState();
const before = calls.length;
gate.open(); await poll; await settle();
const later = calls.slice(before).filter((c) => !c.startsWith("GET /instances/i"));
return { queuedBefore, text: textOf(content), later };
""",
        tmp_path,
    )
    assert res["queuedBefore"] is True
    assert res["text"] == ""
    # Kein nachgeholter Lauf: Die Vorgangsliste wird nach dem Abmelden nur noch
    # vom angehaltenen Abruf erreicht (der Mini-fetch zaehlt ihn erst nach dem
    # Freigeben), nicht ein zweites Mal.
    assert res["later"].count("GET /monitoring/instances") == 1, res["later"]


@needs_node
def test_test_run_focus_follows_its_task_not_the_same_label_elsewhere(
    tmp_path: Path,
) -> None:
    """Pruefinstanz: Zwei Aufgaben mit gleich beschriftetem „Erledigen“. Faellt
    die erste weg, bleibt der Fokus bei der zweiten (Kennung je Aufgabe) --
    frueher zaehlte nur die Stelle unter gleichen Beschriftungen, und ein Enter
    haette die falsche Aufgabe abgeschlossen."""
    res = run_app_js(
        r"""
state.principal = { subject: "mara", roles: ["modeler"] };
state.view = "testrun"; state.revision = 1;
byId("auth-overlay").style.display = "none";
document.body.appendChild(byId("content"));
document.body.appendChild(byId("modal-root"));
state.schemaId = "s";
state.schema = { id: "s", name: "Pruef", version: 1, lifecycle_state: "ENTWURF",
  nodes: { n1: { id: "n1", label: "Erfassen" }, n2: { id: "n2", label: "Pruefen" } },
  edges: [], data_elements: {}, data_accesses: [], forms: {},
  staff_rules: {}, service_bindings: {},
  org_model: { agents: { a1: { id: "a1", name: "Anna" } } } };
state.testInstanceId = "instance_7"; persistTestState();
let rev = 1;
let open = ["n1", "n2"];
respond((p) => {
  if (p === "/monitoring/revision") return { body: { revision: rev } };
  if (p === "/instances/instance_7") {
    return { body: { id: "instance_7", schema_id: "s", state: "RUNNING", is_test: true,
                     node_states: {}, data_values: {} } };
  }
  if (p === "/instances/instance_7/tasks") {
    return { body: open.map((n) => ({ instance_id: "instance_7", node_id: n, label: n,
                                      eligible_agents: ["a1"] })) };
  }
  return { body: [] };
});
const settle = async () => {
  do { await new Promise((r) => setTimeout(r, 5)); } while (renderBusy);
};
const content = byId("content");
const rowOf = (n) => {
  for (let x = n; x; x = x.parentNode) {
    if (x.hasAttribute && x.hasAttribute("data-row-key")) return x.getAttribute("data-row-key");
  }
  return null;
};
render(); await settle();
const target = content.querySelectorAll("button")
  .find((b) => textOf(b) === "Erledigen" && rowOf(b) === "agent-A:n2");
target.focus();
open = ["n2"]; rev = 2;
await pollLiveUpdates(); await settle();
const a = document.activeElement;
return { row: rowOf(a), text: textOf(a), connected: a.isConnected, fresh: a !== target };
""",
        tmp_path,
    )
    assert res == {"row": "agent-A:n2", "text": "Erledigen", "connected": True, "fresh": True}


# --------------------------------------------------------------------------
# Begruendungsdialog nach einer Ablehnung: Fokus zurueck auf den Ausloeser
# --------------------------------------------------------------------------
#
# Lehnt der Kern den Abschluss mit ``REQ.reason-required`` ab, ersetzt der
# Begruendungsdialog den Abschlussdialog. Die Doppelklick-Sperre hatte dem
# „Abschließen“-Knopf da schon den Fokus entzogen; ``openModal`` erkannte kein
# „Dialog ersetzt Dialog“ und merkte sich den verschwindenden Knopf -- beim
# Schliessen landete der Fokus auf ``<body>``.

_REASON_REFUSAL = (
    'respond((p, m) => (m === "POST" && p === "/instances/i2/complete")\n'
    '  ? { status: 422, body: { detail: { message: "x", code: "REQ.reason-required",\n'
    '                                      params: { what: "supervision" } } } }\n'
    '  : (baseRespond(p, m) || tasksRespond(p, m)));\n'
    'await buttonInDialog("Abschließen").click(); await settle();\n'
    'const reasonOpen = textOf(byId("modal-root")).includes("Begründung");\n'
    'if (!reasonOpen) throw new Error("kein Begründungsdialog: " + textOf(byId("modal-root")));\n'
    'const reasonDialog = byId("modal-root").children[0];\n'
)


@needs_node
@pytest.mark.parametrize(
    "close",
    [
        'await reasonDialog.dispatch("keydown", { key: "Escape" });',
        'await buttonInDialog("Abbrechen").click();',
        'await reasonDialog.dispatch("click",\n'
        '  { target: reasonDialog, currentTarget: reasonDialog });',
    ],
    ids=["escape", "abbrechen", "daneben"],
)
def test_closing_the_reason_dialog_returns_focus_to_the_trigger(
    tmp_path: Path, close: str
) -> None:
    res = run_app_js(_complete_scenario(_REASON_REFUSAL + close), tmp_path)
    assert res["dialogOpen"] is False, res
    assert res["onBody"] is False, res
    assert res["sameNode"] is True, res
