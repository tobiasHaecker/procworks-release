# SPDX-License-Identifier: BUSL-1.1
"""Waechter fuer den Renderzyklus des Web-Clients (``web/app.js``).

Der Client traegt **keine** Korrektheitslogik -- aber eine Zusage, die still
brechen kann: **zwei Renderlaeufe duerfen sich nie ueberlappen.**

Alle Sichtfunktionen sind asynchron und folgen demselben Muster: erst
``clear(content)``, dann ``await api.get(...)``, dann anhaengen. Starten zwei
Laeufe kurz nacheinander, leert der zweite den Inhalt, waehrend der erste noch
auf die API wartet -- und danach haengen *beide* ihre Panels an. Sichtbar wurde
das als doppelte Bereiche in „Meine Aufgaben" (zweimal „Offene Aufgaben",
zweimal „Abwesenheit") nach dem Erledigen einer Aufgabe, weil dort vier
Ausloeser zusammentreffen: Klick-Callback, Revisions-Poll, Zeit-Tick und -- im
Tutorial -- der Tour-Tick.

Wie ``test_tour_web.py`` liest dieser Test die Web-Datei vom Dateisystem: die
Suite laeuft in ``core/``, und im Projekt gibt es bewusst keinen JS-Build und
keinen JS-Testlauf.
"""

from __future__ import annotations

import re
from pathlib import Path

APP_JS = Path(__file__).resolve().parents[2] / "web" / "app.js"


def _render_body() -> str:
    """Gibt den Quelltext von ``render()`` ohne Kommentare zurueck.

    Kommentare werden entfernt, weil ein blosser Hinweis auf ``renderBusy`` im
    Fliesstext den Waechter sonst zufrieden stellen wuerde, obwohl die Sperre
    selbst fehlt.
    """

    src = APP_JS.read_text(encoding="utf-8")
    body = re.search(r"\nfunction render\(\) \{.*?\n\}", src, re.S)
    assert body, "render() nicht in app.js gefunden -- Waechter angleichen"
    return re.sub(r"//[^\n]*", "", body.group(0))


def test_released_schema_offers_the_way_back_into_editing() -> None:
    """Ein freigegebenes Schema bietet die neue Revision dort an, wo man ansteht.

    Freigegebene Revisionen sind unveraenderlich; bearbeitet wird ueber eine
    neue Revision. Der Hinweis im Knoten-Inspektor nannte diese Loesung, bot sie
    aber nicht an -- der Knopf stand als letztes Panel der rechten Spalte, und
    in der Kopfzeile kam der Weg gar nicht vor. Beides ist leicht wieder
    wegzurefaktorieren, ohne dass ein Test es merkt.
    """

    src = APP_JS.read_text(encoding="utf-8")
    assert "function newRevisionAction()" in src, "Der gemeinsame Revisions-Knopf fehlt"

    inspector = re.search(r"function nodeInspectorPanel\(\).*?\n\}", src, re.S)
    assert inspector, "nodeInspectorPanel() nicht gefunden -- Waechter angleichen"
    assert inspector.group(0).count("newRevisionAction()") >= 2, (
        "Der Knoten-Inspektor bietet die neue Revision nicht an -- weder mit noch "
        "ohne gewaehlten Knoten"
    )

    # Kopfzeile: der Knopf steht neben „Zur Ausfuehrung" (dem Nicht-Entwurf-Zweig).
    header = re.search(r"\"Zur Ausf\\u00FChrung\"|\"Zur Ausführung\"", src)
    assert header, "Kopfzeilen-Knopf „Zur Ausfuehrung\" nicht gefunden"
    around = src[max(0, header.start() - 800):header.start()]
    assert "newRevision" in around, "Die Kopfzeile bietet keinen Weg zurueck ins Bearbeiten"


def test_render_runs_are_serialised() -> None:
    """Ein zweiter Renderlauf wird vorgemerkt, nicht parallel gestartet."""

    body = _render_body()
    guard = body.find("if (renderBusy)")
    assert guard != -1, "Die Ueberlappungssperre fehlt in render()"
    # Die Sperre muss ganz am Anfang stehen: alles davor liefe doppelt.
    assert guard < body.find("VIEW_META[state.view]"), (
        "Die Sperre steht HINTER dem Sichtaufbau -- Laeufe koennen sich ueberlappen"
    )
    assert "renderQueued = true" in body, "Ein verdraengter Lauf wird nicht vorgemerkt"


def test_render_lock_is_released_even_on_error() -> None:
    """Die Sperre faellt in ``finally`` -- sonst friert die Oberflaeche ein.

    Bliebe ``renderBusy`` nach einem Fehler stehen, wuerde die Anwendung nie
    wieder neu zeichnen: jeder weitere ``render()`` liefe in die Sperre. Das
    waere schlimmer als der doppelte Bereich, den der Merker verhindert.
    """

    body = _render_body()
    assert ".finally(" in body, "Die Sperre wird nicht in finally() freigegeben"
    release = body.find("renderBusy = false")
    assert release != -1, "renderBusy wird nie zurueckgesetzt"
    assert body.find(".finally(") < release, (
        "renderBusy wird ausserhalb von finally() freigegeben -- ein Fehler friert die GUI ein"
    )
    # Der vorgemerkte Lauf wird genau dort nachgeholt.
    assert "renderQueued = false; render();" in body, (
        "Ein vorgemerkter Lauf wird nicht nachgeholt -- der letzte Zustand fehlt"
    )
