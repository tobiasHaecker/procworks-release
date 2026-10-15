# SPDX-License-Identifier: BUSL-1.1
"""Fehlermeldungen, Dialoge und Felder sind fuer Screenreader erkennbar.

Eine fehlgeschlagene Anmeldung stand nur als Text auf dem Schirm (keine
Ansage), das Passwortfeld galt nicht als fehlerhaft; Dialoge waren keine
Dialoge (keine Rolle, keine Beschriftung); im Masken-Designer und im
Einfuegedialog trugen viele Felder nur einen Platzhalter, und eine
Feldmeldung der Aufgabenmaske war nicht mit ihrem Feld verknuepft.

Geprueft wird das Verhalten in Node (``web_vm.py``): die echten Dialoge
werden geoeffnet und ihre Felder auf einen zugeordneten Namen untersucht.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from web_vm import needs_node, run_app_js

# Hat ein Feld einen zugeordneten Namen? aria-label, aria-labelledby auf ein
# vorhandenes Element mit Text, oder ein umschliessendes <label> mit Text
# ausser dem Feld selbst. Ein Platzhalter zaehlt nicht.
_NAMED = r"""
const inLabel = (c) => {
  for (let n = c.parentNode; n; n = n.parentNode) {
    if (n.tagName === "LABEL") {
      return n.children.some((k) => k !== c && textOf(k).trim()) || !!n._text.trim();
    }
  }
  return false;
};
const named = (c) => !!(c.getAttribute("aria-label") || "").trim()
  || (c.hasAttribute("aria-labelledby") && !!textOf(byId(c.getAttribute("aria-labelledby"))).trim())
  || inLabel(c);
const unnamed = (root) => root.querySelectorAll("input, select, textarea")
  .filter((c) => !named(c))
  .map((c) => c.tagName + "["
    + (c.getAttribute("placeholder") || c.className || c.getAttribute("type")) + "]");
"""


@needs_node
@pytest.mark.parametrize(
    ("status", "detail", "invalid"),
    [(401, '"bad"', "true"), (429, '"Zu viele Versuche. Bitte 30 s warten."', None)],
)
def test_failed_login_is_announced_and_marks_the_password_only_when_wrong(
    tmp_path: Path, status: int, detail: str, invalid: str | None
) -> None:
    """Die Meldung steht in einer Live-Region (``role=alert``) und ist dem
    Passwortfeld zugeordnet. ``aria-invalid`` nur bei falschem Passwort (401);
    die Drosselung (429) sagt nichts ueber das Passwort -- Negativfall."""
    res = run_app_js(
        f"respond(() => ({{ status: {status}, body: {{ detail: {detail} }} }}));\n"
        + r"""
document.body.appendChild(byId("auth-overlay"));
showLoginOverlay();
const card = byId("auth-overlay").querySelector(".auth-card");
const form = card.querySelector("form");
const err = card.querySelector(".auth-err");
const pw = byId("login-pw");
pw.value = "falsch";
await form.dispatch("submit");
const labelledBy = card.getAttribute("aria-labelledby");
return {
  role: err.getAttribute("role"), text: textOf(err),
  invalid: pw.getAttribute("aria-invalid"),
  describedBy: pw.getAttribute("aria-describedby"), errId: err.getAttribute("id"),
  dialog: [card.getAttribute("role"), card.getAttribute("aria-modal")],
  title: labelledBy ? textOf(byId(labelledBy)) : null,
};
""",
        tmp_path,
    )
    assert res["role"] == "alert"
    assert res["text"], "die Meldung muss dastehen"
    assert res["invalid"] == invalid
    assert res["errId"] and res["describedBy"] == res["errId"]
    assert res["dialog"] == ["dialog", "true"]
    assert res["title"] == "ProcWorks"


@needs_node
def test_retry_clears_the_invalid_mark(tmp_path: Path) -> None:
    """Grenzfall: Ein neuer Versuch nimmt Meldung und Markierung zurueck,
    bevor er sendet -- sonst bliebe ein korrigiertes Feld „ungueltig“."""
    res = run_app_js(
        r"""
let status = 401;
respond(() => ({ status, body: status === 401 ? { detail: "bad" } : { token: "t" } }));
globalThis.boot = async () => {};
document.body.appendChild(byId("auth-overlay"));
showLoginOverlay();
const form = byId("auth-overlay").querySelector("form");
const pw = byId("login-pw");
await form.dispatch("submit");
const first = pw.getAttribute("aria-invalid");
status = 200;
await form.dispatch("submit");
return { first, after: pw.getAttribute("aria-invalid") };
""",
        tmp_path,
    )
    assert res == {"first": "true", "after": None}


@needs_node
def test_every_dialog_is_a_labelled_modal_dialog(tmp_path: Path) -> None:
    """``openModal`` traegt ``role=dialog``, ``aria-modal`` und
    ``aria-labelledby`` auf seine Ueberschrift; jeder Dialog eine eigene."""
    res = run_app_js(
        r"""
const info = () => {
  const m = byId("modal-root").querySelector(".modal");
  const id = m.getAttribute("aria-labelledby");
  return { role: m.getAttribute("role"), modal: m.getAttribute("aria-modal"), id,
           title: id ? textOf(byId("modal-root").querySelectorAll("h3")
             .find((h) => h.getAttribute("id") === id)) : null };
};
document.body.appendChild(byId("modal-root"));
openModal("Erster Dialog", el("p", null, "x"), () => true);
const a = info();
openModal("Zweiter Dialog", el("p", null, "y"), () => true);
const b = info();
return { a, b };
""",
        tmp_path,
    )
    assert res["a"]["role"] == "dialog" and res["a"]["modal"] == "true"
    assert res["a"]["title"] == "Erster Dialog"
    assert res["b"]["title"] == "Zweiter Dialog"
    assert res["a"]["id"] != res["b"]["id"]


_SCHEMA = r"""
state.schema = { id: "s", name: "P", version: 1, lifecycle_state: "ENTWURF",
  nodes: { st: { id: "st", type: "START", label: "Start" },
           a: { id: "a", type: "ACTIVITY", label: "Erfassen" },
           en: { id: "en", type: "END", label: "Ende" } },
  edges: [{ source: "st", target: "a", type: "CONTROL" },
          { source: "a", target: "en", type: "CONTROL" }],
  data_elements: {
    betrag: { id: "betrag", name: "Betrag", data_type: "INTEGER", source: "INSTANCE" },
    ok: { id: "ok", name: "Geprüft", data_type: "BOOLEAN", source: "INSTANCE" },
    art: { id: "art", name: "Art", data_type: "STRING", source: "INSTANCE" } },
  data_accesses: [], forms: {}, staff_rules: {}, service_bindings: {} };
state.schemaId = "s";
"""


@needs_node
def test_form_designer_fields_all_have_a_name(tmp_path: Path) -> None:
    """Jedes Feld des Masken-Designers (je Bedienelement mit seinen
    Pruefregeln) hat einen zugeordneten Namen, nicht nur einen Platzhalter."""
    res = run_app_js(
        _SCHEMA
        + _NAMED
        + r"""
state.schema.forms.a = { title: "", columns: 1, fields: [
  { element_id: "betrag", widget: "NUMBER", label: "Betrag", mode: "WRITE", required: true,
    options: [] },
  { element_id: "art", widget: "TEXT", label: "Art", mode: "WRITE", required: true, options: [] },
  { element_id: "art", widget: "DROPDOWN", label: "Art", mode: "WRITE", required: true,
    options: ["A", "B"] },
  { element_id: "ok", widget: "CHECKBOX", label: "Geprüft", mode: "WRITE", required: true,
    options: [] },
] };
openFormDesigner("a");
const root = byId("modal-root");
return { unnamed: unnamed(root.querySelector(".form-designer")),
         total: root.querySelectorAll(".fd-field")
           .flatMap((f) => f.querySelectorAll("input, select")).length };
""",
        tmp_path,
    )
    assert res["total"] > 15, "der Test muss die Feldzeilen wirklich sehen"
    assert res["unnamed"] == []


@needs_node
def test_insert_dialog_fields_all_have_a_name(tmp_path: Path) -> None:
    """Einfuegedialog, alle Varianten: parallele Zweige, Bedingt-Zeilen fuer
    Zahl, Ja/Nein und Text, Schleife -- jedes Feld hat einen Namen."""
    res = run_app_js(
        _SCHEMA
        + _NAMED
        + r"""
openInsertModal("a");
const root = byId("modal-root");
const tab = (label) => root.querySelectorAll("button")
  .find((b) => b.parentNode.classList.contains("tabs") && textOf(b) === label);
const seen = [];
const missing = new Set();
const sweep = () => {
  unnamed(root).forEach((x) => missing.add(x));
  seen.push(root.querySelectorAll("input, select, textarea").length);
};
sweep();
await tab("Parallel (UND)").click(); sweep();
await tab("Bedingt (XOR)").click();
const disc = root.querySelector(".cond-disc");
for (const id of ["betrag", "ok", "art"]) {
  disc.value = id; await disc.dispatch("change");
  root.querySelectorAll("button").find((b) => textOf(b) === "+ Zweig").click();
  sweep();
}
await tab("Schleife").click();
const loop = root.querySelector(".loop-disc");
for (const id of ["betrag", "ok", "art"]) {
  loop.value = id; await loop.dispatch("change"); sweep();
}
return { missing: [...missing], seen };
""",
        tmp_path,
    )
    assert min(res["seen"]) > 0
    assert res["missing"] == []


@needs_node
def test_field_error_is_linked_to_its_field_and_unlinked_when_fixed(tmp_path: Path) -> None:
    """``markField``: Die Meldung ist per ``aria-describedby`` mit dem Feld
    verknuepft (zusaetzlich zu einem vorhandenen Hilfetext); behoben,
    verschwinden Meldung und Verweis, der Hilfetext-Verweis bleibt."""
    res = run_app_js(
        r"""
const input = el("input", { type: "number", "aria-describedby": "hilfe-1" });
const wrap = el("label", { class: "field" }, "Anzahl", input);
markField(wrap, "höchstens 10");
const msg = wrap.querySelector(".field-error-msg");
const linked = (input.getAttribute("aria-describedby") || "").split(" ");
markField(wrap, "passt nicht zum vorgegebenen Format");
const twice = (input.getAttribute("aria-describedby") || "").split(" ").length;
markField(wrap, null);
return { linked, msgId: msg.getAttribute("id"), invalid: input.getAttribute("aria-invalid"),
         twice, after: input.getAttribute("aria-describedby"),
         msgGone: !wrap.querySelector(".field-error-msg") };
""",
        tmp_path,
    )
    assert res["msgId"] and res["msgId"] in res["linked"]
    assert "hilfe-1" in res["linked"]
    assert res["twice"] == 2, "eine neue Meldung ersetzt die alte, statt Verweise zu stapeln"
    assert res["after"] == "hilfe-1"
    assert res["msgGone"] is True and res["invalid"] is None
