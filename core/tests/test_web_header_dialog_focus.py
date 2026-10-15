# SPDX-License-Identifier: BUSL-1.1
"""Nach einer Anlage ueber die Kopfleiste liegt der Fokus im Inhalt, nie auf ``<body>``.

„+ Neu“, „Aus Vorlage“ und „BPMN-Import“ sitzen in der Kopfleiste
(``#schema-picker``). Nach der Anlage zeichnet ``render`` die Kopfleiste neu;
der Ausloeser des Dialogs ist dann weg, und die Rueckfaelle beim Schliessen
griffen nur fuer Ausloeser im Inhalt -- der Fokus fiel auf ``<body>``. Jetzt
bekommt der Inhaltsbereich den Fokus (das neue Modell steht dort); Escape
ohne Anlage gibt ihn wie bisher an den Ausloeser zurueck.

Geprueft im Node-``vm`` (``web_vm.py``) mit dem echten ``newSchema``.
"""

from __future__ import annotations

from pathlib import Path

from web_vm import needs_node, run_app_js

_SETUP = r"""
const settle = async () => {
  do { await new Promise((r) => setTimeout(r, 5)); } while (renderBusy);
};
state.principal = { subject: "mara", roles: ["modeler"] };
state.view = "model";
byId("auth-overlay").style.display = "none";
document.body.appendChild(byId("schema-picker"));
document.body.appendChild(byId("content"));
document.body.appendChild(byId("modal-root"));
const schema = { id: "s9", name: "QA-Fokus", version: 1, lifecycle_state: "ENTWURF",
  nodes: { start: { id: "start", type: "START", label: "Start" },
           end: { id: "end", type: "END", label: "Ende" } },
  edges: [{ source: "start", target: "end", type: "CONTROL" }],
  data_elements: {}, data_accesses: [], staff_rules: {}, service_bindings: {}, forms: {},
  org_model: { roles: {}, org_units: {}, agents: {} } };
respond((p, m) => {
  if (m === "POST" && p === "/schemas") return { body: schema };
  if (p === "/schema-summaries") {
    return { body: [{ id: "s9", name: "QA-Fokus", version: 1, lifecycle_state: "ENTWURF",
                      revision_of: null }] };
  }
  if (p === "/schemas/s9") return { body: schema };
  if (p === "/schemas/s9/validation") return { body: { correct: true, findings: [] } };
  return { body: {} };
});
renderSchemaPicker();
const trigger = byId("schema-picker").querySelectorAll("button").find((b) => textOf(b) === "+ Neu");
trigger.focus();
await trigger.click(); await settle();
const dialog = byId("modal-root").children[0];
const focusInfo = () => {
  const a = document.activeElement;
  return { onBody: !a || a === document.body, id: a && a.getAttribute ? a.getAttribute("id") : null,
           connected: !!(a && a.isConnected), text: a && a.tagName === "BUTTON" ? textOf(a) : null,
           dialogOpen: byId("modal-root").children.length > 0 };
};
"""


@needs_node
def test_creating_a_schema_from_the_header_focuses_the_content(tmp_path: Path) -> None:
    res = run_app_js(
        _SETUP
        + r"""
byId("modal-root").querySelector("input").value = "QA-Fokus";
const confirm = byId("modal-root").querySelectorAll("button").find((b) => textOf(b) === "Anlegen");
await confirm.click(); await settle();
return focusInfo();
""",
        tmp_path,
    )
    assert res["dialogOpen"] is False, res
    assert res["onBody"] is False and res["connected"] is True, res
    assert res["id"] == "content", res


@needs_node
def test_escape_without_creating_returns_to_the_trigger(tmp_path: Path) -> None:
    """Negativ: Ohne Anlage bleibt der Ausloeser stehen und bekommt den Fokus."""

    res = run_app_js(
        _SETUP
        + r"""
await dialog.dispatch("keydown", { key: "Escape" });
await settle();
return focusInfo();
""",
        tmp_path,
    )
    assert res["dialogOpen"] is False
    assert res["text"] == "+ Neu", res
