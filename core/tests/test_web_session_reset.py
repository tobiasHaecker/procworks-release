# SPDX-License-Identifier: BUSL-1.1
"""Nach Abmelden oder Login-Wechsel bleibt nichts von der vorigen Sitzung sichtbar.

Der Kern liest einzelne Vorgaenge nur fuer Beteiligte. Der Web-Client darf
diese Zusage nicht dadurch unterlaufen, dass er einen zuvor geladenen Vorgang
nach dem Abmelden weiter anzeigt. Geprueft wird das Verhalten von
``web/app.js`` in Node (siehe ``web_vm.py``), nicht nur der Quelltext.
"""

from __future__ import annotations

import re
from pathlib import Path

from web_source import app_js_source
from web_vm import needs_node, run_app_js

# Ein geladener fremder Vorgang samt Pruefinstanz-Auswahl und Inhalt auf dem
# Schirm -- so stand es nach dem Abmelden bisher fuer die naechste Person da.
_LOADED_SESSION = r"""
state.passwordLogin = true;
state.principal = { subject: "admin", roles: ["admin"] };
state.instanceIds = ["instance_7"];
state.instanceId = "instance_7";
state.instance = { id: "instance_7", data: { kunde: "Muster GmbH" } };
state.worklist = [{ node_id: "n1" }];
state.testInstanceId = "instance_9"; state.testStarter = "a1";
state.testAgentA = "a2"; state.testAgentB = "a3"; persistTestState();
state.agentDirectory = { a1: { agent_id: "a1", name: "Erika" } };
state.schemaNames = { s1: "Geheim" };
state.schema = { id: "s1" }; state.schemaId = "s1"; state.view = "run";
localStorage.setItem("sessionOwner", "admin");
byId("content").appendChild(el("div", null, "Kunde: Muster GmbH"));
"""

_SNAPSHOT = r"""
return {
  instanceId: state.instanceId, instance: state.instance, worklist: state.worklist,
  instanceIds: state.instanceIds, testInstanceId: state.testInstanceId,
  testStarter: state.testStarter, testAgentA: state.testAgentA, testAgentB: state.testAgentB,
  storedTest: localStorage.getItem("testInstanceId"),
  storedOwner: localStorage.getItem("sessionOwner"),
  directory: Object.keys(state.agentDirectory), schemaNames: Object.keys(state.schemaNames),
  schema: state.schema, schemaId: state.schemaId, view: state.view,
  content: textOf(byId("content")), toasts: textOf(byId("toast-root")),
};
"""


def _me(subject: str, role: str) -> str:
    """API-Antwort fuer ``/auth/me`` mit genau diesem Login; alles andere 404."""
    return (
        'respond((p) => p === "/auth/me"\n'
        f'  ? {{ body: {{ subject: "{subject}", roles: ["{role}"] }} }}\n'
        "  : { status: 404 });\n"
    )


@needs_node
def test_logout_clears_every_trace_of_the_previous_session(tmp_path: Path) -> None:
    """Abmelden leert Vorgang, Pruefinstanz, Verzeichnis und Inhalt.

    Sicht und gewaehlter Prozess bleiben (sie tragen keine Daten), die Bindung
    der gemerkten Daten an den Login ist geloescht.
    """
    snap = run_app_js(
        _LOADED_SESSION
        + 'respond(() => ({ status: 204 }));\nawait logout();\n'
        + _SNAPSHOT,
        tmp_path,
    )
    assert snap["instanceId"] is None
    assert snap["instance"] is None
    assert snap["worklist"] is None
    assert snap["instanceIds"] == []
    assert snap["testInstanceId"] is None and snap["storedTest"] is None
    assert snap["testStarter"] is None and snap["testAgentA"] is None and snap["testAgentB"] is None
    assert snap["directory"] == [] and snap["schemaNames"] == []
    assert snap["schema"] is None
    assert "Muster GmbH" not in snap["content"]
    assert snap["storedOwner"] is None
    # Bewusst erhalten: Sicht und Prozessauswahl (keine Daten der Person).
    assert snap["view"] == "run" and snap["schemaId"] == "s1"


@needs_node
def test_login_of_another_person_discards_remembered_session(tmp_path: Path) -> None:
    """Meldet sich eine andere Person an (ohne vorheriges Abmelden, etwa nach
    abgelaufener Sitzung), verwirft ``loadPrincipal`` den alten Stand."""
    snap = run_app_js(
        _LOADED_SESSION
        + 'state.principal = null;\n'
        + _me("erika.sander", "operator")
        + "await loadPrincipal();\n"
        + _SNAPSHOT,
        tmp_path,
    )
    assert snap["instance"] is None and snap["instanceId"] is None
    assert snap["testInstanceId"] is None and snap["storedTest"] is None
    assert "Muster GmbH" not in snap["content"]
    assert snap["storedOwner"] == "erika.sander"


@needs_node
def test_same_person_keeps_session_after_reload(tmp_path: Path) -> None:
    """Grenzfall: Derselbe Login (Neuladen) behaelt die gemerkte Pruefinstanz."""
    snap = run_app_js(
        _LOADED_SESSION
        + 'state.principal = null;\n'
        + _me("admin", "admin")
        + "await loadPrincipal();\n"
        + _SNAPSHOT,
        tmp_path,
    )
    assert snap["testInstanceId"] == "instance_9"
    assert snap["storedTest"] == "instance_9"
    assert snap["instanceId"] == "instance_7"
    assert snap["storedOwner"] == "admin"


_POLL = r"""
state.principal = { subject: "erika.sander", roles: ["operator"] };
state.view = "run"; state.revision = 1;
byId("auth-overlay").style.display = "none";  // angemeldet, keine Maske offen
state.instanceId = "instance_7";
state.instance = { id: "instance_7", data: { kunde: "Muster GmbH" } };
state.worklist = [];
respond((p) => {
  if (p === "/monitoring/revision") return { body: { revision: 2 } };
  if (p.startsWith("/instances/instance_7")) return INSTANCE_ANSWER;
  return { status: 404, body: { detail: "nope" } };
});
await pollLiveUpdates();
return {
  instanceId: state.instanceId, instance: state.instance, toasts: textOf(byId("toast-root")),
};
"""


@needs_node
def test_live_update_drops_instance_the_server_no_longer_hands_out(tmp_path: Path) -> None:
    """404 beim Nachladen: Vorgang verschwindet, Hinweis nennt den Grund."""
    snap = run_app_js(
        _POLL.replace("INSTANCE_ANSWER", '{ status: 404, body: { detail: "Instance not found" } }'),
        tmp_path,
    )
    assert snap["instance"] is None and snap["instanceId"] is None
    assert "Vorgang nicht mehr verfügbar" in snap["toasts"]


@needs_node
def test_live_update_drops_instance_on_forbidden(tmp_path: Path) -> None:
    """403 gilt genauso: die Anmeldung darf den Vorgang nicht (mehr) sehen."""
    snap = run_app_js(
        _POLL.replace("INSTANCE_ANSWER", '{ status: 403, body: { detail: "forbidden" } }'),
        tmp_path,
    )
    assert snap["instance"] is None
    assert "Vorgang nicht mehr verfügbar" in snap["toasts"]


@needs_node
def test_live_update_keeps_instance_on_server_hiccup(tmp_path: Path) -> None:
    """Negativfall: Ein 5xx ist kein Beleg, dass der Vorgang weg ist -- der
    Stand bleibt, es erscheint kein irrefuehrender Hinweis."""
    snap = run_app_js(
        _POLL.replace("INSTANCE_ANSWER", '{ status: 503, body: { detail: "busy" } }'),
        tmp_path,
    )
    assert snap["instanceId"] == "instance_7"
    assert snap["instance"] is not None
    assert "Vorgang nicht mehr verfügbar" not in snap["toasts"]


# Gemerkte Werte, die bewusst NICHT an der Person haengen und den Wechsel der
# Anmeldung ueberleben -- je mit Grund. Alles andere muss beim Sitzungswechsel
# verschwinden. Ein neuer Schluessel in app.js schlaegt hier fehl, bis er
# eingeordnet ist: so kann kein personenbezogener Wert still durchrutschen.
_DEVICE_KEYS = {
    "apiBase": "Serveradresse des Geraets",
    "authToken": "wird von logout selbst entfernt",
    "sessionOwner": "Bindung selbst; logout entfernt sie",
    "theme": "Farbschema",
    "sidebarCollapsed": "Menue-Modus",
    "modelUx": "gewaehlte Modellier-Oberflaeche",
    "cardOpen": "aufgeklappte Abschnitte der Schritt-Karte",
    "view": "gewaehlte Sicht (traegt keine Daten)",
    "schemaId": "gewaehlter Prozess (wird mit den Rechten der neuen Person geladen)",
    "taskFilter": "Filter der Aufgabenliste (Darstellung)",
    "demoBannerCollapsed": "Darstellung des Demo-Banners",
}

_KEY_RE = re.compile(
    r'localStorage(?:\.(?:getItem|setItem|removeItem)\(|, *)"([A-Za-z_]+)"'
)


def _app_storage_keys() -> set[str]:
    """Alle localStorage-Schluessel, die app.js als Literal verwendet."""
    return set(_KEY_RE.findall(app_js_source()))


def test_every_stored_key_is_classified() -> None:
    """Wächter über die Klasse: Jeder gemerkte Schlüssel ist entweder
    Gerätewahl (oben, mit Grund) oder wird beim Sitzungswechsel verworfen."""
    unclassified = _app_storage_keys() - set(_DEVICE_KEYS) - set(_SESSION_KEYS)
    assert not unclassified, (
        f"localStorage-Schluessel ohne Einordnung: {sorted(unclassified)} -- "
        "in SESSION_STORAGE_KEYS (app.js) oder als Geraetewahl in _DEVICE_KEYS eintragen"
    )


_SESSION_KEYS = {"agentId", "testInstanceId", "testStarter", "testAgentA", "testAgentB"}


@needs_node
def test_logout_removes_every_personal_stored_key(tmp_path: Path) -> None:
    """Jeder personenbezogene Schlüssel ist nach dem Abmelden weg, jede
    Gerätewahl bleibt."""
    keys = sorted(_app_storage_keys() - {"authToken", "sessionOwner"})
    setup = "".join(f'localStorage.setItem("{k}", "x");\n' for k in keys)
    left = run_app_js(
        "state.passwordLogin = true;\n" + setup
        + "respond(() => ({ status: 204 }));\nawait logout();\n"
        + f"return {keys!r}.filter((k) => localStorage.getItem(k) !== null);",
        tmp_path,
    )
    assert set(left) == set(keys) & set(_DEVICE_KEYS)
    assert not set(left) & _SESSION_KEYS


@needs_node
def test_rejected_login_discards_remembered_session(tmp_path: Path) -> None:
    """Antwortet ``/auth/me`` mit 401 (Token geleert oder ungültig), bleibt
    nichts von der vorigen Person stehen -- auch die Bindung nicht."""
    snap = run_app_js(
        _LOADED_SESSION
        + 'respond(() => ({ status: 401, body: { detail: "Not authenticated" } }));\n'
        + "await loadPrincipal();\n"
        + _SNAPSHOT,
        tmp_path,
    )
    assert snap["instance"] is None and snap["instanceId"] is None
    assert snap["testInstanceId"] is None and snap["storedTest"] is None
    assert snap["storedOwner"] is None
    assert "Muster GmbH" not in snap["content"]


@needs_node
def test_network_error_on_identity_keeps_session(tmp_path: Path) -> None:
    """Grenzfall: Ein Verbindungsfehler sagt nichts über die Person -- der
    Stand bleibt, damit ein kurzer Ausfall keine Arbeit verwirft."""
    snap = run_app_js(
        _LOADED_SESSION
        + 'respond(() => { throw new Error("offline"); });\n'
        + "await loadPrincipal();\n"
        + _SNAPSHOT,
        tmp_path,
    )
    assert snap["instanceId"] == "instance_7"
    assert snap["storedOwner"] == "admin"
