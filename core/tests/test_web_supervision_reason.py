# SPDX-License-Identifier: BUSL-1.1
"""Der Begruendungsdialog eines Aufsichtseingriffs nennt den richtigen Anlass.

Verlangt der Kern zum Abschluss eine Begruendung, antwortet er mit ``422``
und ``REQ.reason-required``; ``params.what`` sagt, warum: ``supervision``
(ein Login ohne Personenbindung schliesst einen Schritt mit Bearbeiterregel
ab) oder ``automatic`` (ein automatischer Schritt wird von Hand
abgeschlossen). Der Dialog sagte in beiden Faellen „Dieser Login ist keinem
Bearbeiter zugeordnet“ -- beim automatischen Schritt einer gebundenen
Modelliererin falsch. Jetzt kommt der Erklaertext je ``what`` aus dem
Meldungskatalog (``FINDING_TEXTS``).

Geprueft im Node-``vm`` (``web_vm.py``) ueber den echten Abschlussweg
``promptComplete`` -> Ablehnung -> ``askSupervisionReason``.
"""

from __future__ import annotations

from pathlib import Path

from web_vm import needs_node, run_app_js


def _dialog_after_refusal(what: str, code: str = "REQ.reason-required") -> str:
    """Szenario: Abschluss abschicken, der Kern lehnt mit ``code``/``what`` ab;
    zurueck kommen Titel und Text des danach offenen Dialogs sowie die
    Abschlussaufrufe."""

    return r"""
state.principal = { subject: "mara", roles: ["modeler"], agent_id: "a9", display_name: "Mara" };
document.body.appendChild(byId("content"));
document.body.appendChild(byId("modal-root"));
const posted = [];
respond((p, m, body) => {
  if (m === "POST" && p === "/instances/i1/complete") {
    posted.push(body);
    if (body.supervision_reason) return { body: {} };
    return { status: 422, body: { detail: { message: "x", code: "CODE",
                                            params: { what: "WHAT" } } } };
  }
  return { status: 404 };
});
const schema = { nodes: { n1: { label: "Versand buchen" } }, forms: {}, data_elements: {},
                 staff_rules: {} };
await promptComplete(schema, "i1", "n1", "Versand buchen", null, null, {});
const root = byId("modal-root");
const confirm = () => root.querySelectorAll("button")
  .find((b) => b.classList.contains("primary") || b.classList.contains("danger"));
await confirm().click();
const title = textOf(root.querySelector("h3"));
const text = textOf(root.querySelector(".card-hint"));
return { title, text, posted: posted.length };
""".replace("CODE", code).replace("WHAT", what)


@needs_node
def test_automatic_step_dialog_explains_the_manual_completion(tmp_path: Path) -> None:
    """Negativ zum frueheren Text: Beim automatischen Schritt behauptet der
    Dialog nicht, der Login sei keinem Bearbeiter zugeordnet."""

    res = run_app_js(_dialog_after_refusal("automatic"), tmp_path)
    assert res["title"] == "Aufsichtseingriff: Versand buchen"
    assert "keinem Bearbeiter" not in res["text"]
    assert res["text"].startswith("Dieser Schritt ist automatisch."), res


@needs_node
def test_unbound_login_dialog_names_the_missing_person(tmp_path: Path) -> None:
    """Positiv: Beim Abschluss ohne Personenbindung bleibt die Erklaerung, dass
    der Login keinem Bearbeiter zugeordnet ist -- jetzt aus dem Katalog."""

    res = run_app_js(_dialog_after_refusal("supervision"), tmp_path)
    assert res["title"] == "Aufsichtseingriff: Versand buchen"
    assert res["text"].startswith("Dieser Login ist keinem Bearbeiter zugeordnet."), res


@needs_node
def test_dialog_text_is_the_catalogue_text_for_each_duty(tmp_path: Path) -> None:
    """Der Dialog formuliert nicht selbst: Sein Text ist der Katalogtext von
    ``REQ.reason-required`` fuer genau dieses ``what``."""

    res = run_app_js(
        r"""
const t = (what) => findingText({ code: "REQ.reason-required", params: { what } });
return { automatic: supervisionReasonText("automatic"), catAutomatic: t("automatic"),
         supervision: supervisionReasonText("supervision"), catSupervision: t("supervision"),
         unknown: supervisionReasonText("neu"), catUnknown: t("neu") };
""",
        tmp_path,
    )
    assert res["automatic"] == res["catAutomatic"]
    assert res["supervision"] == res["catSupervision"]
    # Grenzfall: ein kuenftiges ``what`` faellt auf den allgemeinen Katalogtext.
    assert res["unknown"] == res["catUnknown"]
    assert res["automatic"] != res["supervision"]


@needs_node
def test_other_refusals_do_not_open_the_reason_dialog(tmp_path: Path) -> None:
    """Negativ: „Problem melden“ (``what: fail``) ist kein Aufsichtseingriff --
    es bleibt beim Abschlussdialog, kein Begruendungsdialog."""

    res = run_app_js(_dialog_after_refusal("fail"), tmp_path)
    assert res["title"] == "Abschließen: Versand buchen"
    assert res["posted"] == 1
