# SPDX-License-Identifier: BUSL-1.1
"""Dialoge des Web-Clients: Folgedialoge, Zugangsdaten, Benutzerverwaltung.

``openModal`` hat **einen** Container und leert ihn beim Schliessen. Gibt der
Bestaetigen-Rueckruf etwas anderes als ``false`` zurueck, schliesst
``openModal`` danach -- und nimmt einen Dialog mit, den der Rueckruf gerade
selbst geoeffnet hat. So verschwanden die Zugangsdaten eines neu angelegten
Logins, bevor jemand sie sah. Der erste Waechter hier deckt die ganze Klasse
ab: jeder Rueckruf, der einen Dialog oeffnet, gibt danach ``false`` zurueck.
Die uebrigen pruefen das Verhalten in Node (``web_vm.py``).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from web_source import app_js_source, line_label
from web_vm import needs_node, run_app_js


def _mask(src: str) -> str:
    """Ersetzt Kommentare und Zeichenketten durch Leerzeichen (gleiche Laenge).

    So stoeren Klammern oder ``openModal(`` in Texten die Klammerzaehlung
    nicht. Template-Ausdruecke ``${...}`` bleiben Code. Regulaere Ausdruecke
    werden nicht erkannt; app.js nutzt in ihnen keine Anfuehrungszeichen an
    Stellen, die das stoeren (sonst schluege der Klammer-Abgleich unten fehl).
    """
    out = list(src)
    i, n = 0, len(src)
    stack: list[str] = []  # offene Template-Literale (fuer ``${`` ... ``}``)
    depth: list[int] = []

    def blank(a: int, b: int) -> None:
        for k in range(a, b):
            if out[k] != "\n":
                out[k] = " "

    while i < n:
        c = src[i]
        if stack and depth and c == "}" and depth[-1] == 0:
            depth.pop()
            i += 1
            j = i
            while j < n and src[j] != "`":
                if src[j] == "\\":
                    j += 1
                elif src[j] == "$" and j + 1 < n and src[j + 1] == "{":
                    break
                j += 1
            blank(i, j)
            if j < n and src[j] == "`":
                stack.pop()
                i = j + 1
            else:
                depth.append(0)
                i = j + 2
            continue
        if depth and c == "{":
            depth[-1] += 1
        elif depth and c == "}":
            depth[-1] -= 1
        if src.startswith("//", i):
            j = src.find("\n", i)
            j = n if j < 0 else j
            blank(i, j)
            i = j
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2) + 2
            blank(i, j)
            i = j
        elif c in "'\"":
            j = i + 1
            while j < n and src[j] != c:
                j += 2 if src[j] == "\\" else 1
            blank(i + 1, j)
            i = j + 1
        elif c == "`":
            j = i + 1
            while j < n and src[j] != "`":
                if src[j] == "\\":
                    j += 1
                elif src[j] == "$" and j + 1 < n and src[j + 1] == "{":
                    break
                j += 1
            blank(i + 1, j)
            if j < n and src[j] == "`":
                i = j + 1
            else:
                stack.append("`")
                depth.append(0)
                i = j + 2
        else:
            i += 1
    return "".join(out)


def _close(code: str, start: int, open_: str, close: str) -> int:
    """Index der zu ``code[start]`` (= ``open_``) passenden Klammer."""
    level = 0
    for k in range(start, len(code)):
        if code[k] == open_:
            level += 1
        elif code[k] == close:
            level -= 1
            if level == 0:
                return k
    raise AssertionError(f"Klammer bei {start} nicht geschlossen")


def _top_level_args(code: str, open_paren: int) -> list[tuple[int, int]]:
    """Spannen der Argumente eines Aufrufs (ohne verschachtelte Kommas)."""
    end = _close(code, open_paren, "(", ")")
    spans, level, begin = [], 0, open_paren + 1
    for k in range(open_paren + 1, end):
        ch = code[k]
        if ch in "([{":
            level += 1
        elif ch in ")]}":
            level -= 1
        elif ch == "," and level == 0:
            spans.append((begin, k))
            begin = k + 1
    spans.append((begin, end))
    return spans


def _dialog_functions(code: str) -> set[str]:
    """Funktionen, deren Rumpf selbst ``openModal(`` aufruft."""
    names = set()
    for m in re.finditer(r"\bfunction\s+(\w+)\s*\([^)]*\)\s*\{", code):
        body_end = _close(code, m.end() - 1, "{", "}")
        if "openModal(" in code[m.end():body_end] and m.group(1) != "openModal":
            names.add(m.group(1))
    return names


def _enclosing_block_end(code: str, pos: int, limit: int) -> int:
    """Ende des innersten ``{...}``-Blocks um ``pos`` (hoechstens ``limit``)."""
    level = 0
    for k in range(pos, limit):
        if code[k] == "{":
            level += 1
        elif code[k] == "}":
            if level == 0:
                return k
            level -= 1
    return limit


def follow_up_dialogs_without_return_false(
    src: str, where: Callable[[int], str] | None = None
) -> list[str]:
    """Rueckrufe, die einen Folgedialog oeffnen und nicht ``false`` liefern.

    :param src: der zu pruefende Quelltext (ganzer Client oder Miniatur).
    :param where: macht aus einer Zeilennummer von ``src`` die Fundstelle;
        ohne Angabe ``"Zeile N"``. Der Waechter uebergibt
        ``web_source.line_label``, damit ein Fund die richtige Skriptdatei nennt.
    :returns: je Fund ``"<Fundstelle>: name(...)"`` -- leer, wenn alles stimmt.
    """
    code = _mask(src)
    dialogs = _dialog_functions(code)
    found = []
    for m in re.finditer(r"\bopenModal\(", code):
        if code[max(0, m.start() - 9):m.start()].endswith("function "):
            continue  # die Definition selbst
        args = _top_level_args(code, m.end() - 1)
        if len(args) < 3:
            continue
        a, b = args[2]
        callback = code[a:b]
        for call in re.finditer(r"\b(\w+)\(", callback):
            if call.group(1) not in dialogs and call.group(1) != "openModal":
                continue
            after_start = a + call.end()
            block_end = _enclosing_block_end(code, after_start, b)
            if not re.search(r"\breturn\s+false\b", code[after_start:block_end]):
                line = src.count("\n", 0, a + call.start()) + 1
                label = where(line) if where else f"Zeile {line}"
                found.append(f"{label}: {call.group(1)}(...)")
    return found


def test_every_dialog_opening_callback_returns_false() -> None:
    """Waechter ueber die Klasse: kein Folgedialog wird sofort wieder geschlossen."""
    found = follow_up_dialogs_without_return_false(app_js_source(), line_label)
    assert not found, (
        "openModal-Rueckruf oeffnet einen Dialog, gibt danach aber nicht false zurueck "
        "(openModal schliesst sonst den neuen Dialog gleich wieder): " + ", ".join(found)
    )


def test_guard_finds_the_original_mistake() -> None:
    """Gegenprobe des Waechters an einem Miniatur-Beispiel des alten Fehlers."""
    bad = (
        "function showCreds(r) { openModal('Zugang', x, async () => true, 'OK'); }\n"
        "function create() {\n"
        "  openModal('Anlegen', y, async () => {\n"
        "    try { const res = await api.post('/users', {}); showCreds(res); }\n"
        "    catch (err) { return false; }\n"
        "  }, 'Anlegen');\n"
        "}\n"
    )
    assert follow_up_dialogs_without_return_false(bad) == ["Zeile 4: showCreds(...)"]
    good = bad.replace("showCreds(res); }", "showCreds(res); return false; }")
    assert follow_up_dialogs_without_return_false(good) == []


def test_guard_ignores_dialog_calls_in_strings_and_comments() -> None:
    """Grenzfall: ``showCreds(`` in Text oder Kommentar ist kein Aufruf."""
    src = (
        "function showCreds(r) { openModal('Zugang', x, async () => true); }\n"
        "openModal('A', y, async () => { // showCreds(res) spaeter\n"
        "  toast('ok', 'showCreds(x)'); const t = `${a} showCreds(b)`; });\n"
    )
    assert follow_up_dialogs_without_return_false(src) == []


# --- Verhalten in Node --------------------------------------------------------

_ADMIN = r"""
state.passwordLogin = true;
state.principal = { subject: "admin", roles: ["admin"] };
"""


@needs_node
def test_created_login_shows_password_in_open_dialog(tmp_path: Path) -> None:
    """Nach „Anlegen“ steht das Initialpasswort im offenen Dialog."""
    res = run_app_js(
        _ADMIN
        + r"""
respond((p, m) => (m === "POST" && p === "/users")
  ? { status: 201, body: { login: "nina.wolf", initial_password: "Init-1234-xyz", user: {} } }
  : { body: [] });
provisionLogin({ id: "a-nina", name: "Nina Wolf" });
const root = byId("modal-root");
const confirm = root.querySelector("button.btn.primary");
await confirm.click();
await new Promise((r) => setTimeout(r, 0));
const inputs = root.querySelectorAll("input").map((i) => i.getAttribute("value"));
const buttons = root.querySelectorAll("button").map((b) => b.textContent);
return { open: root.children.length, text: textOf(root), inputs, buttons };
""",
        tmp_path,
    )
    assert res["open"] == 1
    assert "Init-1234-xyz" in res["inputs"]
    assert "nina.wolf" in res["inputs"]
    assert "Abbrechen" not in res["buttons"]
    assert "Kopieren" in res["buttons"]


@needs_node
def test_failed_creation_keeps_form_open_without_password(tmp_path: Path) -> None:
    """Negativfall: Lehnt der Kern ab, bleibt das Formular stehen -- mit Grund
    in der Meldung, ohne Zugangsdaten-Dialog."""
    res = run_app_js(
        _ADMIN
        + r"""
respond(() => ({ status: 400, body: { detail: "login 'nina.wolf' already exists" } }));
provisionLogin({ id: "a-nina", name: "Nina Wolf" });
const root = byId("modal-root");
await root.querySelector("button.btn.primary").click();
await new Promise((r) => setTimeout(r, 0));
return { title: textOf(root.querySelector("h3")), toasts: textOf(byId("toast-root")) };
""",
        tmp_path,
    )
    assert res["title"].startswith("Login anlegen")
    assert "already exists" in res["toasts"]


@needs_node
def test_example_password_dialog_has_single_button_and_copy(tmp_path: Path) -> None:
    """Beispiel-Passwort: ein Knopf „Verstanden“, Kopierknopf, Verweis aufs Panel."""
    res = run_app_js(
        r"""
showExamplePassword("Ex-Pw-42");
const root = byId("modal-root");
return {
  buttons: root.querySelectorAll("button").map((b) => b.textContent),
  inputs: root.querySelectorAll("input").map((i) => i.getAttribute("value")),
  text: textOf(root),
};
""",
        tmp_path,
    )
    assert res["buttons"] == ["Kopieren", "Verstanden"]
    assert res["inputs"] == ["Ex-Pw-42"]
    assert "Administration → Benutzer" in res["text"]


_USERS = r"""
const USERS = [
  { login: "admin", subject: "admin", roles: ["admin"], display_name: "Ada", must_change: false },
  { login: "erika.sander", subject: "erika.sander", agent_id: "a-erika",
    roles: ["operator"], display_name: "Erika Sander", must_change: true },
];
state.agentDirectory = { "a-erika": { agent_id: "a-erika", name: "Erika Sander" } };
"""


@needs_node
def test_users_panel_lists_logins_and_locks_own_delete(tmp_path: Path) -> None:
    """Liste mit Rollen, Person und Zustand; eigener Login nicht loeschbar."""
    res = run_app_js(
        _ADMIN + _USERS
        + r"""
respond((p) => p === "/users" ? { body: USERS } : { status: 404 });
const body = el("div");
await loadUsersPanel(body);
const rows = body.querySelectorAll("tr").slice(1);
return rows.map((r) => ({
  text: textOf(r),
  del: r.querySelectorAll("button").filter((b) => b.textContent === "Löschen")
        .map((b) => ({ disabled: b.hasAttribute("disabled"), title: b.getAttribute("title") }))[0],
}));
""",
        tmp_path,
    )
    admin, erika = res
    assert admin["del"]["disabled"] is True
    assert "ausgesperrt" in admin["del"]["title"]
    assert erika["del"]["disabled"] is False
    assert "Bearbeiter" in erika["text"] and "Erika Sander" in erika["text"]
    assert "muss Passwort ändern" in erika["text"]


@needs_node
def test_users_panel_resets_password_and_shows_it_once(tmp_path: Path) -> None:
    """„Passwort zurücksetzen“ ruft den Kern und zeigt das neue Passwort."""
    res = run_app_js(
        _ADMIN + _USERS
        + r"""
respond((p, m) => {
  if (m === "POST" && p === "/users/erika.sander/reset-password")
    return { body: { login: "erika.sander", initial_password: "New-Init-77" } };
  if (p === "/users") return { body: USERS };
  return { status: 404 };
});
confirmUserPasswordReset(USERS[1], () => {});
const root = byId("modal-root");
await root.querySelector("button.btn.danger").click();   // beendet Anmeldungen: Gefahr-Knopf
await new Promise((r) => setTimeout(r, 0));
return { calls, title: textOf(root.querySelector("h3")),
  inputs: root.querySelectorAll("input").map((i) => i.getAttribute("value")) };
""",
        tmp_path,
    )
    assert "POST /users/erika.sander/reset-password" in res["calls"]
    assert res["title"] == "Neues Initialpasswort"
    assert "New-Init-77" in res["inputs"]


@needs_node
def test_users_panel_delete_asks_first_and_reports_core_refusal(tmp_path: Path) -> None:
    """Löschen fragt nach (Fokus auf „Abbrechen“); eine Ablehnung des Kerns
    erscheint mit ihrem Grund, der Dialog bleibt offen."""
    res = run_app_js(
        _ADMIN + _USERS
        + r"""
respond((p, m) => (m === "DELETE")
  ? { status: 409, body: { detail: { message: "x", code: "USERS.last-admin",
      params: { login: "erika.sander" } } } }
  : { body: USERS });
confirmUserDelete(USERS[1], () => {});
const root = byId("modal-root");
const focused = document.activeElement && document.activeElement.textContent;
await root.querySelector("button.btn.danger").click();
await new Promise((r) => setTimeout(r, 0));
return { calls, focused, open: root.children.length, toasts: textOf(byId("toast-root")) };
""",
        tmp_path,
    )
    assert "DELETE /users/erika.sander" in res["calls"]
    assert res["focused"] == "Abbrechen"
    assert res["open"] == 1
    assert "letzte Login mit der Rolle Administrator" in res["toasts"]


@needs_node
def test_resource_view_shows_existing_login_instead_of_button(tmp_path: Path) -> None:
    """Person mit Login: „Login: …“ statt Knopf; ohne Login: Knopf „Login“."""
    res = run_app_js(
        _ADMIN + _USERS
        + r"""
respond((p) => p === "/users" ? { body: USERS } : { status: 404 });
const slots = { "a-erika": { slot: el("span"), agent: { id: "a-erika", name: "Erika" } },
                "a-tom": { slot: el("span"), agent: { id: "a-tom", name: "Tom" } } };
await fillLoginSlots(slots);
return { erika: textOf(slots["a-erika"].slot),
         erikaButtons: slots["a-erika"].slot.querySelectorAll("button").length,
         tom: textOf(slots["a-tom"].slot) };
""",
        tmp_path,
    )
    assert res["erika"] == "Login: erika.sander"
    assert res["erikaButtons"] == 0
    assert res["tom"] == "Login"


def test_users_panel_is_admin_only_and_password_mode_only() -> None:
    """Quelltext-Waechter: Das Panel haengt an viewAdmin (nur admin) und am
    Passwort-Modus; ohne Passwort-Modus gibt es keine Logins zu verwalten."""
    src = app_js_source()
    view = src[src.index("async function viewAdmin()"):]
    view = view[:view.index("\n}\n")]
    assert 'if (!hasRole("admin"))' in view
    guard = view.index("if (state.passwordLogin)")
    assert guard < view.index("loadUsersPanel(usersBody)")
    assert '"/users/${encodeURIComponent(user.login)}/reset-password"' in src.replace("`", '"')
    assert "api.del(`/users/${encodeURIComponent(user.login)}`)" in src


_BACKDROP = r"""
const field = el("input", { type: "text" });
openModal("Maske gestalten", el("div", null, field), async () => true, "Speichern");
const root = byId("modal-root");
const backdrop = root.children[0];
const clickBesides = () =>
  backdrop.dispatch("click", { target: backdrop, currentTarget: backdrop });
"""


@needs_node
def test_click_besides_keeps_an_edited_dialog_open(tmp_path: Path) -> None:
    """Mit Eingaben schließt ein Klick daneben nicht -- der Hinweis nennt den
    bewussten Weg (Abbrechen/Esc)."""
    res = run_app_js(
        _BACKDROP
        + r"""
field.value = "Titel";
await backdrop.dispatch("input", { target: field });
document.activeElement = null;   // der Klick daneben nimmt den Fokus weg
await clickBesides();
return { open: root.children.length, hint: textOf(root.querySelector(".modal-keep-hint")),
  focusBack: backdrop.contains(document.activeElement) };
""",
        tmp_path,
    )
    assert res["open"] == 1
    assert "Abbrechen" in res["hint"] and "Esc" in res["hint"]
    assert res["focusBack"] is True  # sonst erreichte Esc den Dialog nicht mehr


@needs_node
def test_click_besides_closes_an_untouched_dialog(tmp_path: Path) -> None:
    """Negativfall: ohne Eingabe schließt der Klick daneben wie bisher."""
    res = run_app_js(_BACKDROP + "await clickBesides();\nreturn root.children.length;", tmp_path)
    assert res == 0


@needs_node
def test_escape_still_discards_an_edited_dialog(tmp_path: Path) -> None:
    """Esc bleibt der bewusste Weg zum Verwerfen, auch mit Eingaben."""
    res = run_app_js(
        _BACKDROP
        + r"""
await backdrop.dispatch("input", { target: field });
await backdrop.dispatch("keydown", { key: "Escape", isComposing: false, target: field });
return root.children.length;
""",
        tmp_path,
    )
    assert res == 0
