# SPDX-License-Identifier: BUSL-1.1
"""Auch in Modellier- und Verwaltungssichten faellt der Fokus nie auf ``<body>``.

Die Laufzeit-Sichten finden ein verschwundenes Fokusziel ueber seine Kennung
wieder (``restoreFocusKey``). Die Modellier-Sichten waren davon bewusst
ausgenommen -- mit der Folge, dass nach „Freigeben“ (der Knopf wird durch
„Neue Revision“ ersetzt) der Fokus auf ``<body>`` lag; Tab begann wieder am
Seitenanfang. Jetzt gilt: Verschwindet der Ausloeser eines Dialogs oder einer
gesperrten Aktion im Inhalt, bekommt in diesen Sichten der Inhaltsbereich den
Fokus (``focusContentFallback``) -- bewusst nicht ein gleich beschrifteter
Knopf, der dort zu einem anderen Schritt gehoeren koennte.

Geprueft im Node-``vm`` (``web_vm.py``). Der Mini-DOM gibt den Fokus beim
Sperren oder Entfernen eines Knopfs nicht selbst ab; ``_BROWSER_FOCUS`` bildet
das Sperren nach, ein entfernter Knopf zaehlt wie im Browser als „kein Fokus“.
"""

from __future__ import annotations

from pathlib import Path

from web_vm import needs_node, run_app_js

_BROWSER_FOCUS = r"""
const P = Object.getPrototypeOf(el("button"));
const realSetAttribute = P.setAttribute;
P.setAttribute = function (k, v) {
  realSetAttribute.call(this, k, v);
  if (k === "disabled" && document.activeElement === this) document.activeElement = document.body;
};
const realFocus = P.focus;
P.focus = function () { if (!this.hasAttribute("disabled")) realFocus.call(this); };
const settle = async () => {
  do { await new Promise((r) => setTimeout(r, 5)); } while (renderBusy);
};
const focusInfo = () => {
  const a = document.activeElement;
  // Wie im Browser: Ein entfernter Knopf haelt keinen Fokus, das ist <body>.
  const onBody = !a || a === document.body || !a.isConnected;
  return { onBody, isContent: a === byId("content"),
           text: a && a.tagName === "BUTTON" ? textOf(a) : null };
};
"""

_MODEL = r"""
state.principal = { subject: "mara", roles: ["modeler"], display_name: "Mara" };
state.view = "model";
byId("auth-overlay").style.display = "none";
document.body.appendChild(byId("content"));
document.body.appendChild(byId("modal-root"));
let released = false;
const schema = () => ({ id: "s1", name: "Urlaub", version: 1,
  lifecycle_state: released ? "RELEASED" : "ENTWURF",
  nodes: { st: { id: "st", type: "START", label: "" },
           a: { id: "a", type: "ACTIVITY", label: "Antrag" },
           en: { id: "en", type: "END", label: "" } },
  edges: [{ source: "st", target: "a", type: "CONTROL" },
          { source: "a", target: "en", type: "CONTROL" }],
  data_elements: {}, data_accesses: [], staff_rules: { a: { kind: "ROLE", role: "r" } },
  service_bindings: {}, forms: {} });
respond((p, m) => {
  if (p === "/schemas/s1") return { body: schema() };
  if (p === "/schemas/s1/validation") return { body: { correct: true, findings: [] } };
  if (p === "/schemas/s1/metrics") return { body: { hints: [] } };
  if (m === "POST" && p === "/schemas/s1/release") { released = true; return { body: schema() }; }
  return { body: {} };
});
state.schemaId = "s1";
await refreshSchema();
render(); await settle();
const contentButton = (t) => byId("content").querySelectorAll("button")
  .find((b) => textOf(b) === t);
"""


@needs_node
def test_focus_after_release_lands_in_the_content_not_on_body(tmp_path: Path) -> None:
    """„Freigeben“ bestaetigt: Der Knopf ist danach durch „Neue Revision“
    ersetzt; der Fokus liegt auf dem Inhaltsbereich statt auf ``<body>``."""

    res = run_app_js(
        _BROWSER_FOCUS + _MODEL
        + r"""
const rel = contentButton("Freigeben");
rel.focus();
await rel.click();
const confirm = byId("modal-root").querySelectorAll("button")
  .find((b) => textOf(b) === "Freigeben");
await confirm.click(); await settle();
return { ...focusInfo(), released, replaced: !rel.isConnected,
         dialogOpen: byId("modal-root").children.length > 0,
         revision: !!contentButton("Neue Revision") };
""",
        tmp_path,
    )
    assert res["released"] is True and res["replaced"] is True and res["revision"] is True, res
    assert res["dialogOpen"] is False
    assert res["onBody"] is False, res
    assert res["isContent"] is True, res


@needs_node
def test_cancelled_release_returns_focus_to_its_button(tmp_path: Path) -> None:
    """Gegenstueck: Steht der Ausloeser noch (Abbrechen), bekommt er den Fokus
    zurueck -- der Rueckfall greift nur, wenn er verschwunden ist."""

    res = run_app_js(
        _BROWSER_FOCUS + _MODEL
        + r"""
const rel = contentButton("Freigeben");
rel.focus();
await rel.click();
const cancel = byId("modal-root").querySelectorAll("button").find((b) => textOf(b) === "Abbrechen");
await cancel.click(); await settle();
return { ...focusInfo(), same: document.activeElement === rel, released };
""",
        tmp_path,
    )
    assert res["released"] is False
    assert res["same"] is True and res["onBody"] is False, res


@needs_node
def test_locked_action_whose_button_vanishes_hands_focus_to_the_content(
    tmp_path: Path,
) -> None:
    """Ohne Dialog: Eine Aktion in einer Modellier-Sicht zeichnet neu, ihr Knopf
    ist danach weg (Speichern, Loeschen). Die Doppelklick-Sperre hatte ihm den
    Fokus genommen; danach liegt er auf dem Inhaltsbereich, nicht auf
    ``<body>``."""

    res = run_app_js(
        _BROWSER_FOCUS + _MODEL
        + r"""
const content = byId("content");
const btn = el("button", { onClick: async () => {
  await Promise.resolve();
  clear(content);
  content.appendChild(el("p", null, "neu gezeichnet"));
} }, "Speichern");
content.appendChild(btn);
btn.focus();
await btn.click();
return focusInfo();
""",
        tmp_path,
    )
    assert res == {"onBody": False, "isContent": True, "text": None}


@needs_node
def test_focus_taken_elsewhere_during_the_action_is_not_moved(tmp_path: Path) -> None:
    """Negativ: Hat die Person waehrend der Aktion selbst etwas anderes
    fokussiert, bleibt der Fokus dort -- der Rueckfall greift nur auf
    ``<body>``."""

    res = run_app_js(
        _BROWSER_FOCUS + _MODEL
        + r"""
const content = byId("content");
let release;
const btn = el("button", { onClick: () => new Promise((r) => { release = r; }) }, "Speichern");
content.appendChild(btn);
const other = el("button", null, "Hilfe");
document.body.appendChild(other);
btn.focus();
const done = btn.click();
other.focus();
clear(content);
release(); await done;
return focusInfo();
""",
        tmp_path,
    )
    assert res == {"onBody": False, "isContent": False, "text": "Hilfe"}


@needs_node
def test_vanished_trigger_outside_the_content_is_left_alone(tmp_path: Path) -> None:
    """Grenzfall: Lag der gesperrte Knopf ausserhalb des Inhalts (Seitenleiste),
    holt die Sperre den Fokus nicht in den Inhalt -- dort entscheidet, wer die
    Seitenleiste neu zeichnet."""

    res = run_app_js(
        _BROWSER_FOCUS + _MODEL
        + r"""
const side = el("div");
document.body.appendChild(side);
const btn = el("button",
  { onClick: async () => { await Promise.resolve(); clear(side); } }, "Abmelden");
side.appendChild(btn);
btn.focus();
await btn.click();
return focusInfo();
""",
        tmp_path,
    )
    assert res["isContent"] is False
