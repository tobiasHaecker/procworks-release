# SPDX-License-Identifier: BUSL-1.1
"""Abmelden nimmt das Tour-Angebot und eine laufende Tour mit.

Das Angebot „Kurze Einführung?“ liegt hoeher als die Anmeldemaske. Wer sich
abmeldete, waehrend es offen war, sah es danach ueber der Anmeldung -- und
konnte die Tour ohne Sitzung starten. ``resetSessionState`` ruft jetzt
``Tour.dismiss``. Geprueft in Node (``web_vm.py``) mit der echten
Tour-Engine (``web/tour/*.js``), die hier nachgeladen wird.
"""

from __future__ import annotations

import json
from pathlib import Path

from web_source import WEB_DIR
from web_vm import needs_node, run_app_js

# Die Tour-Skripte laden wie im Browser nach app.js; ``Tour`` wird global,
# damit ``typeof Tour`` in app.js sie sieht.
_TOUR_SRC = "\n".join(
    (WEB_DIR / "tour" / name).read_text(encoding="utf-8")
    for name in ("fixtures.js", "tours.js", "engine.js")
) + "\nglobalThis.Tour = Tour;"

_SETUP = (
    "(0, eval)(" + json.dumps(_TOUR_SRC) + ");\n"
    + r"""
state.principal = { subject: "vera", roles: ["viewer"] };
state.passwordLogin = true;
document.body.appendChild(byId("auth-overlay"));
document.body.appendChild(byId("tour-root"));
byId("auth-overlay").style.display = "none";
respond(() => ({ body: {} }));
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const offered = () => !!byId("tour-root").querySelector(".tour-offer");
"""
)


@needs_node
def test_logout_removes_a_visible_offer(tmp_path: Path) -> None:
    """Positiv: Angebot sichtbar, Abmelden -> Anmeldemaske ohne Angebot."""
    res = run_app_js(
        _SETUP
        + r"""
Tour.maybeOffer(); await wait(500);
const before = offered();
await logout();
return { before, after: offered(), login: byId("auth-overlay").style.display,
         root: byId("tour-root").children.length };
""",
        tmp_path,
    )
    assert res["before"] is True, "der Test braucht ein sichtbares Angebot"
    assert res["after"] is False and res["root"] == 0
    assert res["login"] == "grid"


@needs_node
def test_logout_during_the_offer_delay_shows_nothing_later(tmp_path: Path) -> None:
    """Grenzfall: Abmelden, bevor das Angebot erscheint -- es erscheint auch
    danach nicht ueber der Anmeldemaske. Nach erneutem Anmelden kommt es wie
    gewohnt (Negativfall: das Abmelden zaehlt nicht als „gefragt“)."""
    res = run_app_js(
        _SETUP
        + r"""
Tour.maybeOffer();
await logout();
await wait(600);
const overLogin = offered();
hideOverlay();
state.principal = { subject: "vera", roles: ["viewer"] };
Tour.maybeOffer(); await wait(500);
return { overLogin, afterRelogin: offered() };
""",
        tmp_path,
    )
    assert res == {"overLogin": False, "afterRelogin": True}


@needs_node
def test_logout_ends_a_running_tour_quietly(tmp_path: Path) -> None:
    """Eine laufende Tour endet mit der Sitzung -- ohne „Tour beendet“-Meldung
    ueber der Anmeldung und ohne Nachladen mit der beendeten Sitzung."""
    res = run_app_js(
        _SETUP
        + r"""
globalThis.render = () => {};
const tour = Tour.availableTours()[0];
Tour.start(tour.id);
const running = Tour.running;
const before = calls.length;
await logout();
await wait(20);
return { running, after: Tour.running, root: byId("tour-root").children.length,
         toasts: textOf(byId("toast-root")), extraCalls: calls.slice(before) };
""",
        tmp_path,
    )
    assert res["running"] is True
    assert res["after"] is False and res["root"] == 0
    assert "Tour beendet" not in res["toasts"]
    assert res["extraCalls"] == ["POST /auth/logout"]
