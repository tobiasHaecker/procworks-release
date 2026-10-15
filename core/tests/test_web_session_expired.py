# SPDX-License-Identifier: BUSL-1.1
"""Eine abgelaufene Sitzung führt zur Anmeldemaske, nicht zu einem Rohtext.

Endet eine Sitzung im Kern (Ablaufzeit, Passwort zurückgesetzt, Login
gelöscht, Abmelden in einem anderen Tab), antwortet jede weitere Anfrage mit
401 ``{"detail": "invalid session"}``. Der Client behandelte das nur in der
öffentlichen Demo: Sonst stand der englische Satz als Fehlermeldung da, der
Dialog blieb offen, die Daten der Sitzung blieben sichtbar, und die
Oberfläche wirkte weiter angemeldet.

Jetzt beendet jede 401 einer angemeldeten Sitzung die Sitzung im Client:
Token verwerfen, sitzungsbezogenen Zustand leeren (wie beim Abmelden),
offene Dialoge schließen, Anmeldemaske mit deutschem Hinweis zeigen. Ein
401 der Anmeldung selbst oder des Passwortwechsels (falsche Eingabe) bleibt,
was er war; 403/409 ebenso, und die Demo meldet sich weiter still neu an.

Geprüft in Node (``web_vm.py``), nicht per Regex.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from web_vm import needs_node, run_app_js

_SESSION = r"""
state.passwordLogin = true; state.authMode = "password";
state.token = "tok-alt"; localStorage.setItem("authToken", "tok-alt");
localStorage.setItem("sessionOwner", "erika.sander");
state.principal = { subject: "erika.sander", roles: ["operator"], agent_id: "a-erika",
                    display_name: "Erika Sander" };
state.view = "tasks";
state.instanceId = "instance_7";
state.instance = { id: "instance_7", data_values: { kunde: "Muster" } };
state.agentDirectory = { "a-erika": { agent_id: "a-erika", name: "Erika Sander" } };
document.body.appendChild(byId("content"));
document.body.appendChild(byId("modal-root"));
document.body.appendChild(byId("auth-overlay"));
byId("content").appendChild(el("div", null, "Antrag von Muster GmbH"));
openModal("Erledigen", el("input", { type: "text", value: "3" }), async () => false, "Abschließen");
let answer = { status: 401, body: { detail: "invalid session" } };
respond((p, m) => {
  if (p === "/auth/logout") return { status: 204 };
  return answer;
});
const settle = () => new Promise((r) => setTimeout(r, 5));
let caught = null;
"""

_SNAPSHOT = r"""
return {
  token: state.token, stored: localStorage.getItem("authToken"),
  owner: localStorage.getItem("sessionOwner"),
  principal: state.principal, instance: state.instance,
  directory: Object.keys(state.agentDirectory),
  overlayShown: byId("auth-overlay").style.display === "grid",
  overlay: textOf(byId("auth-overlay")),
  modalOpen: byId("modal-root").children.length > 0,
  content: textOf(byId("content")),
  toasts: textOf(byId("toast-root")),
  error: caught && describeError(caught).title,
  errorStatus: caught && caught.status,
};
"""


def _run(scenario: str, tmp_path: Path) -> dict[str, Any]:
    result = run_app_js(_SESSION + scenario + _SNAPSHOT, tmp_path)
    assert isinstance(result, dict)
    return result


@needs_node
def test_a_401_on_an_action_ends_the_session_with_a_german_notice(tmp_path: Path) -> None:
    """Positiv: wie im Befund -- „Abschließen“ nach Ablauf. Danach: Token weg,
    Dialog zu, Anmeldemaske mit Hinweis, Daten der Sitzung verworfen."""

    snap = _run(r"""
try { await api.post("/instances/instance_7/complete", { node_id: "n1" }); }
catch (e) { caught = e; }
await settle();
""", tmp_path)
    assert snap["token"] == "" and snap["stored"] is None and snap["owner"] is None
    assert snap["principal"] is None
    assert snap["overlayShown"] is True
    assert "Sitzung ist abgelaufen" in snap["overlay"]
    assert "bitte erneut anmelden" in snap["overlay"]
    assert snap["modalOpen"] is False
    assert snap["instance"] is None and snap["directory"] == []
    assert "Muster" not in snap["content"]
    assert snap["errorStatus"] == 401
    assert snap["error"].startswith("Ihre Sitzung ist abgelaufen")


@needs_node
def test_the_raw_core_text_never_reaches_the_screen(tmp_path: Path) -> None:
    """Negativ (der Grund): kein „invalid session“/„session expired“ -- weder
    in der Maske noch als Meldung, auch wenn die Aktion den Fehler selbst
    anzeigt (``toastError`` im ``catch`` jeder Aktion)."""

    for raw in ("invalid session", "session expired"):
        snap = _run(f"answer = {{ status: 401, body: {{ detail: {json.dumps(raw)} }} }};" + r"""
try { await api.post("/instances/instance_7/complete", { node_id: "n1" }); }
catch (e) { caught = e; toastError(e); }
await settle();
""", tmp_path)
        visible = snap["overlay"] + snap["toasts"] + snap["content"] + (snap["error"] or "")
        assert raw not in visible, raw
        assert "Sitzung ist abgelaufen" in snap["overlay"]


@needs_node
def test_a_background_poll_also_ends_the_session(tmp_path: Path) -> None:
    """Grenzfall: ohne eigene Aktion -- die Live-Aktualisierung stößt auf die
    401 und zeigt die Anmeldemaske (vorher blieb die Oberfläche stehen)."""

    snap = _run(r"""
await pollLiveUpdates();
await settle();
""", tmp_path)
    assert snap["overlayShown"] is True and snap["token"] == ""
    assert "Sitzung ist abgelaufen" in snap["overlay"]


@needs_node
def test_two_parallel_refusals_end_the_session_once(tmp_path: Path) -> None:
    """Grenzfall: zwei gleichzeitige Anfragen -- eine Maske, keine doppelten
    Meldungen, beide Aufrufer bekommen den deutschen Fehler."""

    result = run_app_js(_SESSION + r"""
const results = await Promise.allSettled([api.get("/me/tasks"), api.get("/directory/agents")]);
await settle();
return {
  titles: results.map((r) => describeError(r.reason).title),
  cards: byId("auth-overlay").children.length,
  notices: textOf(byId("auth-overlay")).split("Sitzung ist abgelaufen").length - 1,
};
""", tmp_path)
    assert all(t.startswith("Ihre Sitzung ist abgelaufen") for t in result["titles"])
    assert result["cards"] == 1 and result["notices"] == 1


@needs_node
def test_after_reload_with_an_expired_token_the_mask_says_why(tmp_path: Path) -> None:
    """Grenzfall: Neuladen mit abgelaufenem Token -- ``boot`` zeigt die Maske
    mit demselben Hinweis (vorher: Maske ohne Hinweis)."""

    snap = _run(r"""
respond((p) => {
  if (p === "/health") return { body: { status: "ok", version: "t" } };
  if (p === "/auth/config") return { body: { mode: "password", password_login: true } };
  return { status: 401, body: { detail: "invalid session" } };
});
await boot();
await settle();
""", tmp_path)
    assert snap["overlayShown"] is True
    assert "Sitzung ist abgelaufen" in snap["overlay"]
    assert "invalid session" not in snap["toasts"]


@needs_node
def test_a_wrong_password_at_login_stays_a_login_error(tmp_path: Path) -> None:
    """Negativ: der 401 der Anmeldung selbst ist eine falsche Eingabe, kein
    Sitzungsende -- die Maske sagt „Login oder Passwort ist falsch.“, ohne
    Hinweis auf eine abgelaufene Sitzung."""

    snap = _run(r"""
state.token = ""; localStorage.removeItem("authToken"); state.principal = null;
showLoginOverlay();
byId("auth-overlay").querySelectorAll("input")[0].value = "erika.sander";
byId("auth-overlay").querySelectorAll("input")[1].value = "falsch";
await byId("auth-overlay").querySelectorAll("form")[0].dispatch("submit", { preventDefault() {} });
await settle();
""", tmp_path)
    assert "Login oder Passwort ist falsch." in snap["overlay"]
    assert "Sitzung ist abgelaufen" not in snap["overlay"]


_WRONG_CURRENT = r"""
answer = { status: 401, body: { detail: { message: "invalid credentials",
  code: "AUTH.wrong-current-password", params: {} } } };
"""

# Passwort-Dialog ausfüllen und absenden; ``forced`` = Pflichtwechsel nach
# der Erstanmeldung. Liefert zusätzlich, ob das Feld „Aktuelles Passwort“
# als fehlerhaft markiert ist.
_CHANGE_DIALOG = r"""
showChangePasswordOverlay(FORCED);
const pwInputs = byId("auth-overlay").querySelectorAll("input");
pwInputs[0].value = "richtig-alt";
pwInputs[1].value = "neu-pw-123"; pwInputs[2].value = "neu-pw-123";
await byId("auth-overlay").querySelectorAll("form")[0].dispatch("submit", { preventDefault() {} });
await settle();
const currentMarked = pwInputs[0].getAttribute("aria-invalid") === "true";
"""


def _change_dialog(prefix: str, forced: bool, tmp_path: Path) -> dict[str, Any]:
    """Führt den Passwort-Dialog gegen die vorbereitete Antwort aus.

    :param prefix: Szenario-Vorlauf (setzt ``answer``).
    :param forced: Pflichtwechsel (true) oder Wechsel aus dem Menü (false).
    :param tmp_path: pytest-Arbeitsordner für ``run_app_js``.
    :returns: der Schnappschuss plus ``currentMarked``.
    """

    scenario = prefix + _CHANGE_DIALOG.replace("FORCED", "true" if forced else "false")
    result = run_app_js(
        _SESSION + scenario + _SNAPSHOT.replace("return {", "return { currentMarked,", 1),
        tmp_path,
    )
    assert isinstance(result, dict)
    return result


@needs_node
def test_a_wrong_current_password_keeps_the_session(tmp_path: Path) -> None:
    """Negativ: beim Passwortwechsel meldet der Kern das falsche aktuelle
    Passwort mit eigenem Code -- die Sitzung bleibt, keine Abmeldung."""

    snap = _run(_WRONG_CURRENT + r"""
try { await api.post("/auth/change-password", { current_password: "x", new_password: "y" }); }
catch (e) { caught = e; }
await settle();
""", tmp_path)
    assert snap["token"] == "tok-alt" and snap["overlayShown"] is False
    assert snap["modalOpen"] is True
    assert snap["error"] == "Aktuelles Passwort ist falsch."


@needs_node
def test_the_dialog_names_a_wrong_current_password_in_the_form(tmp_path: Path) -> None:
    """Positiv (beide Wege: aus dem Menü und Pflichtwechsel): falsches
    aktuelles Passwort -- Formularmeldung, Feld markiert, Token bleibt, kein
    Sitzungshinweis."""

    for forced in (False, True):
        snap = _change_dialog(_WRONG_CURRENT, forced, tmp_path)
        assert "Aktuelles Passwort ist falsch." in snap["overlay"], forced
        assert "Sitzung ist abgelaufen" not in snap["overlay"], forced
        assert snap["currentMarked"] is True, forced
        assert snap["token"] == "tok-alt", forced


@needs_node
def test_an_ended_session_in_the_password_dialog_shows_the_login_mask(
    tmp_path: Path,
) -> None:
    """Negativ mit Grund: Endet die Sitzung bei offenem Passwort-Dialog, ist
    die 401 ohne den Code des falschen Passworts ein Sitzungsende -- Anmelde-
    maske mit Hinweis statt „Aktuelles Passwort ist falsch.“. Gilt auch für
    den Pflichtwechsel nach der Erstanmeldung (dort gibt es keine Abfrage im
    Hintergrund, die die Maske später doch noch brächte)."""

    for raw in ("invalid session", "session expired"):
        for forced in (False, True):
            prefix = f"answer = {{ status: 401, body: {{ detail: {json.dumps(raw)} }} }};"
            snap = _change_dialog(prefix, forced, tmp_path)
            case = (raw, forced)
            assert snap["overlayShown"] is True, case
            assert "Sitzung ist abgelaufen" in snap["overlay"], case
            assert "Aktuelles Passwort ist falsch." not in snap["overlay"], case
            assert raw not in snap["overlay"] + snap["toasts"], case
            assert snap["token"] == "" and snap["stored"] is None, case


# Den Dialog vor dem Absenden festhalten: Meldungsfeld und Feld „Aktuelles
# Passwort“ gehören dem Dialog, auch wenn die Anmeldemaske ihn ersetzt.
_DIALOG_STATE = r"""
const dialogErr = byId("auth-overlay").querySelectorAll(".auth-err")[0];
"""

_DIALOG_SNAPSHOT = (
    "return { currentMarked, dialogError: textOf(dialogErr), "
)


@needs_node
def test_the_ended_dialog_itself_stays_silent(tmp_path: Path) -> None:
    """Negativ mit Grund: Nach einem Sitzungsende schreibt der Passwort-Dialog
    nichts mehr in sein Meldungsfeld und markiert kein Feld -- weder „Aktuelles
    Passwort ist falsch.“ noch sonst etwas. Die Anmeldemaske nennt den Grund;
    der Dialog darf ihr nicht widersprechen, auch wenn er (etwa bei einer
    anderen Überlagerung) noch zu sehen wäre."""

    for forced in (False, True):
        scenario = (
            "answer = { status: 401, body: { detail: \"invalid session\" } };"
            + _CHANGE_DIALOG.replace("FORCED", "true" if forced else "false").replace(
                'pwInputs[0].value = "richtig-alt";',
                'pwInputs[0].value = "richtig-alt";' + _DIALOG_STATE, 1)
        )
        result = run_app_js(
            _SESSION + scenario + _SNAPSHOT.replace("return {", _DIALOG_SNAPSHOT, 1),
            tmp_path,
        )
        assert isinstance(result, dict)
        assert result["overlayShown"] is True and "Sitzung ist abgelaufen" in result["overlay"]
        assert result["dialogError"] == "", forced
        assert result["currentMarked"] is False, forced


@needs_node
def test_a_401_without_the_code_never_blames_the_current_password(
    tmp_path: Path,
) -> None:
    """Negativ mit Grund (öffentliche Demo): Dort beendet eine 401 die Sitzung
    nicht, der Client versucht die stille Wiederanmeldung, der Dialog bleibt
    offen. Scheitert sie, sieht der Besucher den Dialog weiter -- er darf dann
    nicht „Aktuelles Passwort ist falsch.“ sagen und das Feld nicht markieren:
    Nur der Code des Kerns belegt ein falsches Passwort."""

    scenario = r"""
state.demo = true; state.demoPassword = "demo-procworks"; state.demoAutologin = "mara.modell";
respond((p) => {
  if (p === "/auth/logout") return { status: 204 };
  if (p === "/auth/login") return { status: 401, body: { detail: "invalid credentials" } };
  return { status: 401, body: { detail: "invalid session" } };
});
""" + _CHANGE_DIALOG.replace("FORCED", "false")
    result = run_app_js(
        _SESSION + scenario + "await settle();"
        + _SNAPSHOT.replace("return {", "return { currentMarked,", 1),
        tmp_path,
    )
    assert isinstance(result, dict)
    assert result["overlayShown"] is True
    assert "Aktuelles Passwort ist falsch." not in result["overlay"]
    assert "Änderung fehlgeschlagen." in result["overlay"]
    assert result["currentMarked"] is False


@needs_node
def test_403_and_409_keep_the_session(tmp_path: Path) -> None:
    """Negativ: Berechtigung fehlt (403) oder Stand überholt (409) -- das sagt
    nichts über die Sitzung; Token, Dialog und Daten bleiben."""

    for status in (403, 409):
        snap = _run(f"answer = {{ status: {status}, body: {{ detail: " + r"""
  { code: "EX.not-activated", params: {}, message: "x" } } };
try { await api.post("/instances/instance_7/complete", { node_id: "n1" }); }
catch (e) { caught = e; }
await settle();
""", tmp_path)
        assert snap["token"] == "tok-alt", status
        assert snap["overlayShown"] is False and snap["modalOpen"] is True, status
        assert snap["instance"] is not None, status


@needs_node
def test_the_public_demo_still_signs_in_again_silently(tmp_path: Path) -> None:
    """Negativ: in der öffentlichen Demo bleibt die stille Wiederanmeldung --
    keine Anmeldemaske, die Demo-Person ist danach wieder angemeldet."""

    snap = _run(r"""
state.demo = true; state.demoPassword = "demo-procworks"; state.demoAutologin = "mara.modell";
let loggedIn = false;
respond((p) => {
  if (p === "/auth/login") { loggedIn = true; return { body: { token: "tok-neu" } }; }
  if (p === "/health") return { body: { status: "ok" } };
  if (p === "/auth/config") return { body: { mode: "password", password_login: true, demo: true } };
  if (p === "/auth/me" && loggedIn) return { body: { subject: "mara.modell", roles: ["modeler"] } };
  if (!loggedIn) return { status: 401, body: { detail: "invalid session" } };
  return { status: 404 };
});
try { await api.get("/me/tasks"); } catch (e) { caught = e; }
await settle(); await settle();
""", tmp_path)
    assert snap["token"] == "tok-neu"
    assert "Sitzung ist abgelaufen" not in snap["overlay"]


@needs_node
def test_open_mode_without_a_token_is_untouched(tmp_path: Path) -> None:
    """Grenzfall: ohne Anmeldung (offener Modus, kein Token) gibt es keine
    Sitzung, die enden könnte -- keine Maske."""

    snap = _run(r"""
state.passwordLogin = false; state.authMode = "open"; state.token = "";
localStorage.removeItem("authToken");
try { await api.get("/me/tasks"); } catch (e) { caught = e; }
await settle();
""", tmp_path)
    assert snap["overlayShown"] is False and snap["modalOpen"] is True
