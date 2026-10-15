# SPDX-License-Identifier: BUSL-1.1
"""Die Hauptsichten sind Teil des Browser-Verlaufs (Adresse ``#/<sicht>``).

Ein Sichtwechsel erzeugte keinen Verlaufseintrag: Wer gewohnt „Zurueck“
drueckte, verliess die Anwendung. Jetzt steht die Sicht als Route in der
Adresse (``#/tasks``): Zurueck und Vor wechseln die Sicht, Neuladen und
Lesezeichen oeffnen dieselbe Sicht. Die Route traegt nur den Namen der Sicht,
keine Daten. Eine unbekannte Route oder eine Sicht ohne Recht fuehrt sicher
auf eine erlaubte Sicht (die Startsicht der Rolle), die Rollen-Sperre der
Sichten bleibt also bestehen.

Geprueft im Node-``vm`` (``web_vm.py``, mit Verlaufsmodell). Die Sichten
selbst sind durch Stellvertreter ersetzt, die nur ihren Namen zeichnen --
geprueft wird die Navigation, nicht der Inhalt.
"""

from __future__ import annotations

from pathlib import Path

from web_vm import needs_node, run_app_js

# Stellvertreter-Sichten, Navigation wie in index.html, Anmeldung je Szenario.
_SETUP = r"""
document.body.appendChild(byId("content"));
document.body.appendChild(byId("modal-root"));
byId("auth-overlay").style.display = "none";
const drawn = [];
Object.keys(VIEW_META).forEach((k) => {
  VIEW_META[k].fn = async () => {
    const c = byId("content"); clear(c);
    c.appendChild(el("p", null, "Sicht " + k));
    drawn.push(k);
  };
  const b = el("button", { "data-view": k }, k);
  b.dataset.view = k;
  byId("nav").appendChild(b);
});
const settle = async () => {
  do { await new Promise((r) => setTimeout(r, 5)); } while (renderBusy);
};
const navTo = async (v) => {
  const btn = byId("nav").querySelectorAll("button").find((b) => b.dataset.view === v);
  await byId("nav").dispatch("click", { target: { closest: () => btn } });
  await settle();
};
const where = () => ({ view: state.view, hash: location.hash, title: textOf(byId("view-title")),
                       length: history.length });
wireNav();
"""

_MODELER = r"""
state.principal = { subject: "mara", roles: ["modeler"], display_name: "Mara" };
"""

_OPERATOR = r"""
state.principal = { subject: "erika", roles: ["operator"], agent_id: "a1", display_name: "Erika" };
"""


@needs_node
def test_back_and_forward_switch_between_views(tmp_path: Path) -> None:
    """Zurueck fuehrt zur vorigen Sicht, Vor wieder hin -- die Anwendung wird
    dabei nicht verlassen."""

    res = run_app_js(
        _SETUP + _MODELER
        + r"""
state.view = "model"; render(); await settle();
const start = where();
await navTo("data");
const afterNav = where();
await history.back(); await settle();
const afterBack = where();
await history.forward(); await settle();
return { start, afterNav, afterBack, afterForward: where() };
""",
        tmp_path,
    )
    assert res["start"] == {"view": "model", "hash": "#/model", "title": "Modellieren",
                            "length": 1}
    assert res["afterNav"] == {"view": "data", "hash": "#/data", "title": "Datensicht",
                               "length": 2}
    assert res["afterBack"]["view"] == "model" and res["afterBack"]["hash"] == "#/model"
    assert res["afterBack"]["title"] == "Modellieren"
    assert res["afterForward"]["view"] == "data" and res["afterForward"]["title"] == "Datensicht"


@needs_node
def test_reload_and_bookmark_open_the_view_of_the_address(tmp_path: Path) -> None:
    """Neuladen bzw. Lesezeichen: Die Route in der Adresse gewinnt vor der
    zuletzt gemerkten Sicht."""

    res = run_app_js(
        _SETUP
        + r"""
localStorage.setItem("view", "model");
history.replaceState(null, "", "#/tasks");
respond((p) => {
  if (p === "/health") return { body: { status: "ok", version: "x" } };
  if (p === "/auth/config") return { body: { mode: "token", password_login: false } };
  if (p === "/auth/me") return { body: { subject: "erika", roles: ["operator"], agent_id: "a1" } };
  if (p === "/monitoring/revision") return { body: { revision: 1 } };
  if (p === "/schema-summaries" || p === "/directory/agents") return { body: [] };
  return { body: {} };
});
await boot(); await settle();
return { ...where(), drawn };
""",
        tmp_path,
    )
    assert res["view"] == "tasks" and res["hash"] == "#/tasks", res
    assert res["drawn"] == ["tasks"]
    assert res["length"] == 1, "der Start legt keinen eigenen Verlaufseintrag an"


@needs_node
def test_first_view_without_route_replaces_instead_of_adding_an_entry(tmp_path: Path) -> None:
    """Grenzfall: Ohne Route in der Adresse (erster Aufruf) traegt die erste
    Sicht ihre Route ein, ohne einen Verlaufseintrag anzulegen -- sonst
    braeuchte Zurueck einen leeren Zwischenschritt."""

    res = run_app_js(
        _SETUP + _MODELER
        + r"""
state.view = "org"; render(); await settle();
return where();
""",
        tmp_path,
    )
    assert res == {"view": "org", "hash": "#/org", "title": "Ressourcensicht", "length": 1}


@needs_node
def test_unknown_route_falls_back_to_an_allowed_view(tmp_path: Path) -> None:
    """Negativ: Eine unbekannte Route zeichnet nichts Unbekanntes, sondern
    bleibt bei der gezeigten Sicht und berichtigt die Adresse (ohne neuen
    Eintrag)."""

    res = run_app_js(
        _SETUP + _MODELER
        + r"""
state.view = "data"; render(); await settle();
history.pushState(null, "", "#/geheim");
await dispatchWindow("hashchange"); await settle();
return { ...where(), drawn };
""",
        tmp_path,
    )
    assert res["view"] == "data" and res["hash"] == "#/data", res
    assert res["drawn"] == ["data"], "keine weitere Sicht gezeichnet"


@needs_node
def test_route_to_a_forbidden_view_leads_to_the_start_view_of_the_role(tmp_path: Path) -> None:
    """Negativ: Zurueck bzw. eine Adresse auf eine Sicht ohne Recht (Bearbeiter
    auf Administration) zeichnet sie nicht, sondern die Startsicht der Rolle."""

    res = run_app_js(
        _SETUP + _OPERATOR
        + r"""
applyRoleNav();
state.view = "tasks"; render(); await settle();
history.pushState(null, "", "#/admin");
await dispatchWindow("popstate"); await dispatchWindow("hashchange"); await settle();
return { ...where(), drawn, start: startViewOfRole() };
""",
        tmp_path,
    )
    assert "admin" not in res["drawn"], res
    # Startsicht eines Bearbeiters ist „Meine Aufgaben“ (Entscheidung E.3.7).
    assert res["view"] == res["start"] == "tasks", res
    assert res["hash"] == "#/tasks"


@needs_node
def test_forbidden_route_at_start_up_is_corrected_after_login(tmp_path: Path) -> None:
    """Negativ beim Start: Ein Lesezeichen auf eine Sicht ohne Recht oeffnet
    die Startsicht der Rolle, und die Adresse zeigt sie."""

    res = run_app_js(
        _SETUP
        + r"""
history.replaceState(null, "", "#/admin");
respond((p) => {
  if (p === "/health") return { body: { status: "ok", version: "x" } };
  if (p === "/auth/config") return { body: { mode: "token", password_login: false } };
  if (p === "/auth/me") return { body: { subject: "vera", roles: ["viewer"] } };
  if (p === "/monitoring/revision") return { body: { revision: 1 } };
  if (p === "/schema-summaries" || p === "/directory/agents") return { body: [] };
  return { body: {} };
});
await boot(); await settle();
return { ...where(), drawn };
""",
        tmp_path,
    )
    assert res["drawn"] == ["monitor"], res
    assert res["hash"] == "#/monitor"


@needs_node
def test_a_fragment_that_is_no_route_changes_nothing(tmp_path: Path) -> None:
    """Grenzfall: Ein Fragment ohne Routen-Praefix (etwa der Sprunglink
    ``#content``) wechselt keine Sicht."""

    res = run_app_js(
        _SETUP + _MODELER
        + r"""
state.view = "data"; render(); await settle();
history.pushState(null, "", "#content");
await dispatchWindow("hashchange"); await settle();
return { view: state.view, drawn };
""",
        tmp_path,
    )
    assert res == {"view": "data", "drawn": ["data"]}


@needs_node
def test_the_address_carries_no_data(tmp_path: Path) -> None:
    """Die Route nennt nur die Sicht -- weder Vorgang noch Schema noch Token."""

    res = run_app_js(
        _SETUP + _MODELER
        + r"""
state.token = "geheim-token";
state.schemaId = "schema_7"; state.instanceId = "instance_9";
state.view = "model"; render(); await settle();
await navTo("run");
return { hash: location.hash, href: location.href };
""",
        tmp_path,
    )
    assert res["hash"] == "#/run"
    for secret in ("geheim", "schema_7", "instance_9"):
        assert secret not in res["href"]


@needs_node
def test_back_while_a_dialog_is_open_keeps_the_view(tmp_path: Path) -> None:
    """Negativ: Steht ein Dialog offen, wechselt Zurueck die Sicht dahinter
    nicht (der Dialog arbeitet noch auf ihr); die Adresse zeigt weiter die
    gezeigte Sicht."""

    res = run_app_js(
        _SETUP + _MODELER
        + r"""
state.view = "model"; render(); await settle();
await navTo("data");
openModal("Probe", el("p", null, "x"), () => true, "OK");
await history.back(); await settle();
return { ...where(), dialog: byId("modal-root").children.length, drawn };
""",
        tmp_path,
    )
    assert res["view"] == "data" and res["hash"] == "#/data", res
    assert res["dialog"] == 1
    assert res["drawn"] == ["model", "data"]


@needs_node
def test_the_start_view_fits_the_role(tmp_path: Path) -> None:
    """Bearbeiter beginnen in „Meine Aufgaben“, Leser im Monitoring,
    Modellierer und Admin unveraendert im Modellieren (Entscheidung E.3.7)."""

    res = run_app_js(
        _SETUP
        + r"""
const startFor = (roles) => {
  state.principal = { subject: "x", roles };
  state.view = "model";      // die Vorgabe ohne gemerkte Sicht
  applyRoleNav();
  return state.view;
};
return {
  operator: startFor(["operator"]), viewer: startFor(["viewer"]),
  modeler: startFor(["modeler"]), admin: startFor(["admin"]),
  both: startFor(["operator", "modeler"]),
};
""",
        tmp_path,
    )
    assert res == {"operator": "tasks", "viewer": "monitor", "modeler": "model",
                   "admin": "model", "both": "model"}


@needs_node
def test_a_remembered_allowed_view_of_a_clerk_stays(tmp_path: Path) -> None:
    """Negativ: Hat die Bearbeiterin zuletzt „Ausführung“ gewaehlt, bleibt sie
    dort (Geraetewahl) -- die Startsicht gilt nur, wo keine erlaubte Wahl besteht."""

    res = run_app_js(
        _SETUP + _OPERATOR
        + r"""
state.view = "run";
applyRoleNav();
return state.view;
""",
        tmp_path,
    )
    assert res == "run"
