# SPDX-License-Identifier: BUSL-1.1
"""Eine Seite, die waehrend eines Serverausfalls geladen wird, erholt sich.

Neu laden bei stehender API: ``boot`` scheiterte an ``/health`` und gab auf.
Die Seitenleiste behielt den Text aus ``index.html`` („Modus: offen (kein
Login)“, Kopf „offen“, Token-Feld), alle Menuepunkte -- auch Administration --
standen fuer jede Person da, und nach der Wiederkehr des Servers kam keine
Anfrage mehr: Live-Aktualisierung und Wiederholung starteten nie. Das Banner
versprach trotzdem „laedt neu, sobald der Server wieder antwortet“.

Jetzt unterscheidet der Client „Server nicht erreichbar, Anmeldung noch
ungeprueft“ vom offenen Modus (keine Menuepunkte, kein Inhalt, eigener Text)
und versucht den Start mit wachsendem Abstand erneut (``scheduleBootRetry``),
bis der Server antwortet. Eine Ablehnung des Kerns (4xx) ist kein Ausfall und
loest keine Wiederholung aus.

Geprueft im Node-``vm`` (``web_vm.py``); Zeitgeber werden abgefangen, damit
kein Test wartet.
"""

from __future__ import annotations

from pathlib import Path

from web_vm import needs_node, run_app_js

# Startwiederholungen abfangen statt warten: ``retryTimers`` sammelt sie.
_SETUP = r"""
const retryTimers = [];
const realSetTimeout = setTimeout;
setTimeout = (fn, ms, ...rest) => {
  if (fn === retryBoot) { retryTimers.push(ms); return retryTimers.length; }
  return realSetTimeout(fn, ms, ...rest);
};
clearTimeout = () => {};
state.view = "monitor";
document.body.appendChild(byId("content"));
const navVisible = () => [...byId("nav").children]
  .filter((b) => b.style.display !== "none").map((b) => b.dataset.view);
// Die Menuepunkte aus index.html (der Mini-DOM kennt nur die Wurzel).
["model", "run", "tasks", "monitor", "admin", "help"].forEach((v) => {
  const b = el("button", { "data-view": v }, v);
  b.dataset.view = v;
  byId("nav").appendChild(b);
});
let up = false;
respond((p) => {
  if (!up) throw new Error("connection refused");
  if (p === "/health") return { body: { status: "ok", version: "9.9.9" } };
  if (p === "/auth/config") return { body: { mode: "token", password_login: false } };
  if (p === "/auth/me") {
    return { body: { subject: "erika", roles: ["operator"], agent_id: "a1",
                     display_name: "Erika" } };
  }
  if (p === "/monitoring/revision") return { body: { revision: 1 } };
  if (p === "/schemas" || p === "/me/tasks" || p === "/instances") return { body: [] };
  if (p === "/directory/agents") return { body: [] };
  return { body: {} };
});
const sidebar = () => ({ pill: textOf(byId("user-pill")), foot: textOf(byId("auth-user")),
  conn: textOf(byId("conn-pill")), token: byId("token-field").style.display,
  nav: navVisible(), content: textOf(byId("content")) });
"""


@needs_node
def test_reload_during_an_outage_is_not_the_open_mode(tmp_path: Path) -> None:
    """Negativ: Im Ausfall behauptet die Seite weder den offenen Modus noch
    zeigt sie Menuepunkte, deren Rechte niemand geprueft hat."""

    res = run_app_js(
        _SETUP
        + r"""
await boot();
await new Promise((r) => realSetTimeout(r, 0));
return { ...sidebar(), retries: retryTimers };
""",
        tmp_path,
    )
    assert res["conn"] == "getrennt"
    assert "offen" not in res["pill"] and "offen" not in res["foot"], res
    assert "Server nicht erreichbar" in res["foot"], res
    assert res["token"] == "none"
    assert res["nav"] == [], res
    assert "Server nicht erreichbar" in res["content"], res
    assert "Instanzen" not in res["content"]
    assert len(res["retries"]) == 1


@needs_node
def test_the_page_connects_once_the_server_is_back(tmp_path: Path) -> None:
    """Positiv: Die geplante Wiederholung startet die Anwendung, sobald der
    Server antwortet -- mit geprueftem Login, Rollenmenue und Live-Abfrage."""

    res = run_app_js(
        _SETUP
        + r"""
await boot();
const before = calls.length;
up = true;
await retryBoot();
await new Promise((r) => realSetTimeout(r, 20));
return { ...sidebar(), retries: retryTimers,
         asked: calls.slice(before).filter((c) => c === "GET /auth/me").length };
""",
        tmp_path,
    )
    assert res["conn"] == "verbunden"
    assert res["pill"] == "Erika"
    assert res["asked"] == 1
    assert "admin" not in res["nav"] and "tasks" in res["nav"], res
    assert res["token"] == "", res
    assert len(res["retries"]) == 1, "nach Erfolg keine weitere Wiederholung"


@needs_node
def test_retries_back_off_up_to_a_ceiling(tmp_path: Path) -> None:
    """Grenzfall: Bleibt der Server weg, wachsen die Abstaende bis zu einer
    Obergrenze; ein erfolgreicher Start setzt sie zurueck."""

    res = run_app_js(
        _SETUP
        + r"""
for (let i = 0; i < 6; i += 1) await retryBoot();
const failing = retryTimers.slice();
up = true; await retryBoot();
up = false; await retryBoot();
return { failing, afterSuccess: retryTimers[retryTimers.length - 1],
         first: BOOT_RETRY_FIRST_MS, max: BOOT_RETRY_MAX_MS };
""",
        tmp_path,
    )
    f = res["failing"]
    assert f[0] == res["first"]
    assert all(b >= a for a, b in zip(f, f[1:], strict=False)), f
    assert f[-1] == res["max"] and max(f) == res["max"], f
    assert res["afterSuccess"] == res["first"]


@needs_node
def test_a_rejection_at_start_up_schedules_no_retry(tmp_path: Path) -> None:
    """Negativ: Antwortet der Kern mit einer Ablehnung (401), ist das kein
    Ausfall -- keine Wiederholung, Anzeige „verbunden“."""

    res = run_app_js(
        _SETUP
        + r"""
respond((p) => p === "/health" ? { body: { status: "ok", version: "x" } }
  : { status: 401, body: { detail: "not authenticated" } });
await boot();
return { conn: textOf(byId("conn-pill")), retries: retryTimers };
""",
        tmp_path,
    )
    assert res == {"conn": "verbunden", "retries": []}
