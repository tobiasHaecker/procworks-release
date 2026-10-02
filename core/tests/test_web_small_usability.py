# SPDX-License-Identifier: BUSL-1.1
"""Small usability gaps: locked buttons say why, read-only fields look so,
completing a step without a mask asks first, password refusals name the rule.
"""

from __future__ import annotations

import re
from pathlib import Path

from web_vm import APP_JS, needs_node, run_app_js

SRC = APP_JS.read_text(encoding="utf-8")
STYLES = (APP_JS.parent / "styles.css").read_text(encoding="utf-8")


def test_no_button_is_locked_without_a_reason() -> None:
    """Class guard: a draft-only lock always goes through ``lockedBy``."""

    assert "disabled: !draft" not in SRC
    assert SRC.count("lockedBy([!draft, DRAFT_ONLY_REASON]") >= 10


@needs_node
def test_locked_by_names_the_first_matching_reason(tmp_path: Path) -> None:
    res = run_app_js(
        r"""
return {
  draft: lockedBy([true, DRAFT_ONLY_REASON], [true, "Connector fehlt"]),
  conn: lockedBy([false, DRAFT_ONLY_REASON], [true, "Connector fehlt"]),
  free: lockedBy([false, DRAFT_ONLY_REASON]),
  btn: el("button", lockedBy([true, "Grund"]), "x").getAttribute("title"),
};
""",
        tmp_path,
    )
    assert res["draft"]["disabled"] is True and "neuen Revision" in res["draft"]["title"]
    assert res["conn"] == {"disabled": True, "title": "Connector fehlt"}
    assert res["free"] == {}
    assert res["btn"] == "Grund"


def test_read_only_fields_have_their_own_look() -> None:
    m = re.search(r"input:disabled, select:disabled, textarea:disabled \{([^}]*)\}", STYLES)
    assert m, "keine eigene Darstellung fuer Nur-Lese-Felder"
    rule = m.group(1)
    assert "background: var(--" in rule and "#" not in rule  # nur CSS-Variablen


@needs_node
def test_completing_without_mask_asks_first(tmp_path: Path) -> None:
    res = run_app_js(
        r"""
const schema = { id: "s", name: "P", version: 1, nodes: { a: { id: "a", type: "ACTIVITY",
  label: "Ware prüfen" } }, edges: [], data_elements: {}, data_accesses: [], forms: {} };
respond(() => ({ body: {} }));
await promptComplete(schema, "i1", "a", "Ware prüfen", null, async () => {}, {});
const root = byId("modal-root");
const text = textOf(root);
const before = calls.filter((c) => c.startsWith("POST")).length;
const confirm = root.querySelector("button.btn.primary");
const focused = document.activeElement === confirm;
await confirm.click();
return { text, before, focused,
  after: calls.filter((c) => c.startsWith("POST")) };
""",
        tmp_path,
    )
    assert "ist erledigt?" in res["text"]
    assert res["before"] == 0          # nichts abgeschlossen ohne Bestaetigung
    assert res["focused"] is True      # Enter bestaetigt
    assert res["after"] == ["POST /instances/i1/complete"]


@needs_node
def test_password_refusal_names_the_concrete_rule(tmp_path: Path) -> None:
    res = run_app_js(
        r"""
const d = (code, params) => describeError({ status: 400,
  detail: { message: "x", code, params: params || {} } }).title;
return { short: d("PW.too-short", { min: "8" }), same: d("PW.unchanged"),
  taken: d("USERS.login-taken", { login: "nina.wolf" }) };
""",
        tmp_path,
    )
    assert res["short"] == "Das neue Passwort ist zu kurz – mindestens 8 Zeichen."
    assert "unterscheiden" in res["same"]
    assert "„nina.wolf“ gibt es schon" in res["taken"]
    assert "zu kurz oder identisch" not in SRC
