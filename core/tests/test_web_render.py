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


def _panzoom_body() -> str:
    """Gibt ``attachPanZoom`` ohne ``//``-Kommentare zurueck.

    Ohne das Entfernen der Kommentare wuerde schon der erklaerende Fliesstext
    (der die mittlere Maustaste ausfuehrlich beschreibt) die Waechter unten
    zufriedenstellen, obwohl der Code selbst fehlt.
    """

    src = APP_JS.read_text(encoding="utf-8")
    body = re.search(r"\nfunction attachPanZoom\(wrap, svgEl\) \{.*?\n\}\n", src, re.S)
    assert body, "attachPanZoom() nicht in app.js gefunden -- Waechter angleichen"
    return re.sub(r"//[^\n]*", "", body.group(0))


def test_lost_model_can_be_brought_back_into_view() -> None:
    """Ein verschobenes/gezoomtes Modell laesst sich wieder einpassen.

    Pan und Zoom sind unbegrenzt: wer weit genug schiebt, hat den Kontrollfluss
    komplett aus dem Fenster geschoben und findet ohne Hilfe nicht zurueck (ein
    Neuzeichnen setzt die Ansicht zwar zurueck, ist aber kein Bedienelement).
    Es gibt deshalb zwei Wege zurueck, die beide auf dieselbe Funktion fuehren.
    """

    body = _panzoom_body()
    assert "function fitToView()" in body, "Die Einpass-Funktion fehlt"
    assert "fitToView," in body, "fitToView wird nicht auf _panzoom veroeffentlicht"

    src = APP_JS.read_text(encoding="utf-8")
    assert "class: \"canvas-fit\"" in src, "Der Einpassen-Knopf fehlt im Canvas"
    assert "_panzoom.fitToView()" in src, "Der Knopf ruft das Einpassen nicht auf"


def test_middle_button_double_click_fits_and_suppresses_autoscroll() -> None:
    """Doppelklick mit der mittleren Maustaste passt ein -- ohne Autoscroll.

    Zwei Fallen, die je einzeln alles kaputt machen: ``dblclick`` feuert nur
    fuer die linke Taste (die Klicks muessen also selbst gezaehlt werden), und
    ohne ``preventDefault`` auf ``mousedown`` startet der Browser den
    Autoscroll-Modus, der danach am Zeiger klebt.
    """

    body = _panzoom_body()
    assert "auxclick" in body, "Der mittlere Klick wird nicht ausgewertet"
    assert "e.button === 1" in body, "Es wird nicht auf die mittlere Taste geprueft"
    assert "midClicks >= 2" in body, "Ein einzelner mittlerer Klick passt schon ein"
    mousedown = re.search(r"\"mousedown\", \(e\) => \{[^}]*\}", body)
    assert mousedown and "preventDefault" in mousedown.group(0), (
        "Autoscroll wird nicht unterdrueckt -- der Scroll-Anker klebt am Zeiger"
    )


def test_fitting_never_magnifies_a_small_model() -> None:
    """Kleine Modelle werden eingepasst, nicht aufgeblasen (Deckel 1)."""

    body = _panzoom_body()
    assert "Math.min(1, fit)" in body, (
        "Ohne Deckel wird ein Zwei-Knoten-Prozess beim Einpassen formatfuellend "
        "vergroessert"
    )


STYLES_CSS = Path(__file__).resolve().parents[2] / "web" / "styles.css"


def _css_without_comments() -> str:
    """Gibt ``styles.css`` ohne ``/* ... */``-Kommentare zurueck.

    Sonst stellte schon der erklaerende Kommentar (der ``overflow: hidden`` und
    ``overscroll-behavior`` beim Namen nennt) die Waechter zufrieden, obwohl die
    Regel selbst fehlt.
    """

    src = STYLES_CSS.read_text(encoding="utf-8")
    return re.sub(r"/\*.*?\*/", "", src, flags=re.S)


def test_only_main_scrolls_never_the_document() -> None:
    """Nur ``.main`` scrollt -- das Dokument bleibt gesperrt.

    Ohne die Sperre kettet Safari das Mausrad am Scroll-Ende von ``.main`` ans
    Dokument weiter: die ganze ``.app`` schiebt sich nach oben und ein
    Hintergrund-Scrollbalken taucht auf (erst nach dem zweiten Scrollen sichtbar).
    Bewacht beide Haelften der Zusage: Dokument gesperrt **und** Kette gefangen.
    """

    css = _css_without_comments()

    html_body = re.search(r"html,\s*body\s*\{([^}]*)\}", css)
    assert html_body, "html, body-Regel nicht in styles.css gefunden -- Waechter angleichen"
    assert "overflow: hidden" in html_body.group(1), (
        "Ohne `overflow: hidden` auf html/body kann das Dokument scrollen -- "
        "der Hintergrund-Scrollbalken kommt zurueck"
    )

    main = re.search(r"\.main\s*\{([^}]*)\}", css)
    assert main, ".main-Regel nicht in styles.css gefunden -- Waechter angleichen"
    assert "overscroll-behavior: contain" in main.group(1), (
        "Ohne `overscroll-behavior: contain` kettet die Rollbewegung am Rand von "
        ".main ans Dokument weiter"
    )


def test_both_columns_stay_viewport_high_and_scroll_their_overflow() -> None:
    """Bei gesperrtem Dokument muss jede Grid-Spalte ihren Ueberhang selbst scrollen.

    Andernfalls schneidet ``overflow: hidden`` (siehe
    :func:`test_only_main_scrolls_never_the_document`) auf einem kurzen Fenster den
    unteren Rand ab: die Grid-Zeile waechst auf die Inhaltshoehe, die Spalten sind
    hoeher als der Viewport und ihr Ende ist unerreichbar (Sidebar-Fusszeile bzw.
    unterste Panels rechts). Drei Zutaten fangen das ab: die Zeile an die
    Container-Hoehe gebunden, plus je Spalte ein eigener Scroll-Kontext.
    """

    css = _css_without_comments()

    app = re.search(r"\.app\s*\{([^}]*)\}", css)
    assert app, ".app-Regel nicht in styles.css gefunden -- Waechter angleichen"
    assert "minmax(0, 1fr)" in app.group(1), (
        "Ohne `grid-template-rows: minmax(0, 1fr)` waechst die Grid-Zeile auf die "
        "Inhaltshoehe und der untere Rand wird bei gesperrtem Dokument abgeschnitten"
    )

    sidebar = re.search(r"\.sidebar\s*\{([^}]*)\}", css)
    assert sidebar, ".sidebar-Regel nicht in styles.css gefunden -- Waechter angleichen"
    assert "overflow-y: auto" in sidebar.group(1) and "min-height: 0" in sidebar.group(1), (
        "Ohne eigenen Scroll-Kontext ist die Sidebar-Fusszeile (Theme/API/Abmelden) "
        "auf einem kurzen Fenster nicht mehr erreichbar"
    )

    main = re.search(r"\.main\s*\{([^}]*)\}", css)
    assert main and "min-height: 0" in main.group(1), (
        "Ohne `min-height: 0` blaeht `.main` die Grid-Zeile auf und scrollt seinen "
        "Ueberhang nicht -- die unteren Panels sind abgeschnitten"
    )
