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


def test_selecting_a_step_keeps_its_data_provenance_in_view() -> None:
    """Die gestrichelte Datenherkunft darf beim Einrasten nicht aus dem Bild fallen.

    Ein Klick auf einen Schritt zeichnet die Herkunftsboegen seiner gelesenen
    Datenelemente **und** rueckt den Schritt in die Mitte der Canvas. Beides
    zusammen war der Fehler: der Schreiber liegt typischerweise mehrere hundert
    Pixel weiter links, das Zentrieren allein auf den Leseknoten schob ihn samt
    Bogen und Beschriftung aus dem ``overflow: hidden``-Fenster -- die Pfeile
    waren gezeichnet, aber nicht zu sehen ("es erscheinen gar keine Pfeile").

    Der Bereich muss deshalb (a) in ``renderGraph`` **beide** beteiligten Knoten
    umfassen -- nicht nur die Bogenenden -- und (b) beim Einrasten mitgegeben
    werden, wobei der Massstab bei Bedarf verkleinert wird.
    """

    src = APP_JS.read_text(encoding="utf-8")
    graph = re.search(r"\nfunction renderGraph\(schema, opts\) \{.*?\n\}\n", src, re.S)
    assert graph, "renderGraph() nicht in app.js gefunden -- Waechter angleichen"
    graph_body = re.sub(r"//[^\n]*", "", graph.group(0))

    assert "provBounds" in graph_body, "renderGraph bestimmt keinen Herkunfts-Bereich"
    assert "pv.from.x" in graph_body and "pv.to.x" in graph_body, (
        "Der Herkunfts-Bereich umfasst nicht beide beteiligten Knoten -- der "
        "Schreiber bleibt dann ausserhalb des Fensters"
    )
    assert "wrap._provBounds = provBounds" in graph_body, (
        "Der Herkunfts-Bereich wird nicht an die Canvas weitergereicht"
    )

    # Beide Modellier-Oberflaechen (Karte und klassisch) muessen das leisten --
    # sie zeichnen denselben Graphen und rasten beide auf den gewaehlten Knoten
    # ein.
    for fn in ("viewModelCard", "viewModelClassic"):
        view = re.search(r"\nfunction " + fn + r"\(\) \{.*?\n\}\n", src, re.S)
        assert view, f"{fn}() nicht in app.js gefunden -- Waechter angleichen"
        view_body = re.sub(r"//[^\n]*", "", view.group(0))
        assert "centerCanvasOnNode(graph, focusPos, graph._provBounds)" in view_body, (
            f"{fn} rueckt nur den Knoten ins Bild, nicht seine Herkunft"
        )

    body = _panzoom_body()
    center = re.search(r"centerOn\(pos, region\) \{.*?\n    \}", body, re.S)
    assert center, "centerOn nimmt keinen zusaetzlichen Bereich entgegen"
    assert "region.x0" in center.group(0) and "region.y1" in center.group(0), (
        "Der zusaetzliche Bereich geht nicht in die Zentrierung ein"
    )
    assert "Math.min(scale," in center.group(0) and "Math.min(1, fit)" in center.group(0), (
        "Der Massstab wird nicht verkleinert, wenn Knoten und Herkunft zusammen "
        "nicht ins Fenster passen (und/oder er wird ueber 1 vergroessert)"
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


# ---------------------------------------------------------------------------
# Modellieren im Kontrollfluss (Schritt-Karte)
#
# Waechter fuer die Zusagen aus docs/Modellieren-im-Kontrollfluss-Konzept.md.
# Sie pruefen die Quelle (wie die uebrigen Web-Tests): es gibt bewusst keinen
# JS-Build und keinen Browser in der CI.
# ---------------------------------------------------------------------------


def _fn_body(name: str) -> str:
    """Gibt den Quelltext einer Top-Level-Funktion ohne ``//``-Kommentare zurueck."""

    src = APP_JS.read_text(encoding="utf-8")
    body = re.search(r"\n(?:async )?function " + name + r"\(.*?\n\}\n", src, re.S)
    assert body, f"{name}() nicht in app.js gefunden -- Waechter angleichen"
    return re.sub(r"//[^\n]*", "", body.group(0))


def test_both_modelling_surfaces_stay_available() -> None:
    """Karten- und klassische Sicht bleiben gleichwertig nebeneinander.

    Die Karten-Sicht ersetzt die gewohnte Zwei-Spalten-Oberflaeche nicht,
    sondern tritt neben sie; umgeschaltet wird in der Kopfzeile. Faellt eine der
    beiden beim Aufraeumen weg, verliert ein Teil der Nutzer seine Arbeitsweise
    -- und der Umschalter zeigte ins Leere.
    """

    src = APP_JS.read_text(encoding="utf-8")
    assert "function viewModelCard()" in src, "Die Karten-Sicht fehlt"
    assert "function viewModelClassic()" in src, "Die klassische Sicht fehlt"
    assert "function modelUxToggle()" in src, "Der Umschalter fehlt"

    dispatch = _fn_body("viewModel")
    assert "viewModelClassic()" in dispatch and "viewModelCard()" in dispatch, (
        "viewModel() waehlt nicht zwischen beiden Oberflaechen"
    )
    # Die Wahl ueberlebt einen Reload -- sonst faellt der Nutzer bei jedem
    # Seitenaufruf in die andere Oberflaeche zurueck.
    assert 'localStorage.setItem("modelUx"' in src, "Die Wahl der Oberflaeche wird nicht gemerkt"

    # Die klassische Sicht behaelt ihre vier Panels der rechten Spalte.
    classic = _fn_body("viewModelClassic")
    for panel in ("nodeInspectorPanel()", "bindingPalette(", "findingsPanel()", "revisionPanel()"):
        assert panel in classic, f"Der klassischen Sicht fehlt {panel}"


def test_loop_support_is_shared_by_both_surfaces() -> None:
    """Schleifen (K6) sind in beiden Modellier-Oberflaechen bedienbar.

    Der Einfuege-Dialog traegt den Tab "Schleife" (spricht den
    loop-insert-Endpunkt an), und die Schleifen-Begrenzer haben EIN geteiltes
    Panel (loopNodePanel), das Karte und klassische Sicht aufrufen -- dieselbe
    Regel wie fuer jede Knotenoperation. Die Knotentypen sind dem Client
    bekannt, damit ein per API gebautes Schleifenmodell nicht als
    Unbekannt-Typ strandet.
    """

    src = APP_JS.read_text(encoding="utf-8")
    assert "LOOP_START" in src and "LOOP_END" in src, "Loop-Knotentypen fehlen im Client"
    insert = _fn_body("openInsertModal")
    assert "loop-insert" in insert, "Der Einfuege-Dialog spricht den loop-insert-Endpunkt nicht an"
    assert "Schleife" in insert, "Der Einfuege-Dialog hat keinen Schleifen-Tab"
    assert src.count("function loopNodePanel(") == 1, (
        "loopNodePanel muss genau einmal existieren (geteilte Funktion)"
    )
    assert "loopNodePanel(schema, node, draft)" in _fn_body("stepCard"), (
        "Die Schritt-Karte zeigt den Schleifenblock nicht"
    )
    assert "loopNodePanel(schema, node, draft)" in _fn_body("nodeInspectorPanel"), (
        "Die klassische Sicht zeigt den Schleifenblock nicht"
    )


def test_loop_back_arc_and_iteration_counter_are_drawn() -> None:
    """S2: Der Ruecksprung-Bogen wird gezeichnet, Iterationen werden gezaehlt.

    Die Ruecksprungkante existiert bewusst nicht als Datum (der Graph bleibt
    azyklisch) -- gezeichnet wird sie aus der LOOP_START/LOOP_END-Paarung
    (loopPairsOf, der Client-Spiegel von model.loop_block). renderGraph ist die
    EINE Kontrollfluss-Darstellung aller Sichten, deshalb erscheinen Bogen und
    Iterationszaehler in Modellieren, Ausfuehrung und Monitoring zugleich.
    Verhalten (Geometrie, Beschriftung, Zaehlerstand) wurde per Node-vm-Lauf
    belegt; dieser Waechter sichert die Zusagen.
    """

    src = APP_JS.read_text(encoding="utf-8")
    assert src.count("function loopPairsOf(") == 1, (
        "loopPairsOf muss genau einmal existieren (Spiegel von model.loop_block)"
    )
    graph = _fn_body("renderGraph")
    assert "loopPairsOf(schema)" in graph and "gloop" in graph, (
        "renderGraph zeichnet keinen Ruecksprung-Bogen"
    )
    assert "loop_iterations" in graph and "wiederholt" in graph, (
        "renderGraph zeigt den Iterationszaehler nicht"
    )
    css = (APP_JS.parent / "styles.css").read_text(encoding="utf-8")
    assert ".gloop" in css and ".gloop-iter" in css, (
        "Die Stilklassen des Schleifen-Bogens fehlen"
    )
    assert "var(--" in css.split(".gloop", 1)[1][:400], (
        "Der Bogen muss seine Farben ueber CSS-Variablen beziehen (helle Variante)"
    )


def test_loop_partition_cells_share_one_caption_and_reach_the_api() -> None:
    """S3: THRESHOLD/ENUM-Abbruchpartitionen sind bedienbar und einheitlich.

    Die Wiederhol-Bedingung hat EINE geteilte Beschriftungsquelle
    (loopConditionCaption), die Bogen (renderGraph) und Schleifen-Panel
    (loopNodePanel) gleichermassen nutzen -- sonst driften die Sichten. Der
    Einfuege-Dialog laesst neben BOOLEAN auch Zahlen- und Text-Merkmale zu und
    baut daraus die repeat/exit-Zellen des loop-insert-Payloads. Verhalten
    (alle Beschriftungsfaelle inkl. Bereichs- und Sonst-Zellen) wurde per
    Node-vm-Lauf belegt; dieser Waechter sichert die Zusagen.
    """

    src = APP_JS.read_text(encoding="utf-8")
    assert src.count("function loopConditionCaption(") == 1, (
        "loopConditionCaption muss genau einmal existieren (geteilte Quelle)"
    )
    assert "loopConditionCaption(schema, d, 14)" in _fn_body("renderGraph"), (
        "Der Ruecksprung-Bogen nutzt die geteilte Beschriftung nicht"
    )
    assert "loopConditionCaption(schema, d, 24)" in _fn_body("loopNodePanel"), (
        "Das Schleifen-Panel nutzt die geteilte Beschriftung nicht"
    )
    insert = _fn_body("openInsertModal")
    assert '"BOOLEAN", "INTEGER", "FLOAT", "STRING"' in insert.split("loopable", 1)[1][:200], (
        "Der Schleifen-Tab laesst nur BOOLEAN-Merkmale zu (S3 fehlt)"
    )
    assert "payload.cells" in insert, (
        "Der Schleifen-Tab baut keine repeat/exit-Zellen fuer loop-insert"
    )
    # max_iterations (Notbremse): Eingabe im Dialog, Anzeige an Bogen + Panel.
    assert "loop-max" in insert and "payload.max_iterations" in insert, (
        "Der Schleifen-Tab bietet die Hoechstzahl der Durchlaeufe nicht an"
    )
    assert "max_iterations" in _fn_body("renderGraph"), (
        "Der Ruecksprung-Bogen zeigt die Hoechstzahl der Durchlaeufe nicht"
    )
    assert "max_iterations" in _fn_body("loopNodePanel"), (
        "Das Schleifen-Panel zeigt die Hoechstzahl der Durchlaeufe nicht"
    )


def test_worklist_claiming_is_wired_in_the_tasks_view() -> None:
    """E1: Uebernehmen/Zuruecklegen sind bedienbar und geteilt.

    claimTask/returnTask existieren genau einmal (geteilte Funktionen fuer
    jede Aufrufstelle), die Aufgabenliste zeigt den Zustand (uebernommen/
    angeboten) und bietet je nach Inhaberschaft den passenden Knopf; die
    Instanz-Detailsicht nennt den Inhaber transparent. Die eigentliche
    Logik (Exklusivitaet W1, Berechtigung W2, Withdrawn-Filter) lebt im
    Kern und ist dort getestet (test_worklist_claiming.py).
    """

    src = APP_JS.read_text(encoding="utf-8")
    assert src.count("async function claimTask(") == 1, "claimTask fehlt/doppelt"
    assert src.count("async function returnTask(") == 1, "returnTask fehlt/doppelt"
    assert "/claim" in _fn_body("claimTask") and "/return" in _fn_body("returnTask"), (
        "Die Funktionen sprechen die E1-Endpunkte nicht an"
    )
    view = _fn_body("viewTasks")
    assert "claimTask(t, agentId)" in view and "returnTask(t, agentId)" in view, (
        "Die Aufgabenliste bietet Uebernehmen/Zuruecklegen nicht an"
    )
    assert "claimed_by" in view, "Die Aufgabenliste kennt den Inhaber nicht"
    # Der Quelltext schreibt Umlaute teils als \uXXXX-Escape, daher ohne "ü".
    assert "bernommen von" in src, "Die Instanz-Sicht nennt den Inhaber nicht"


def test_escalation_editor_is_shared_by_both_surfaces() -> None:
    """T3/E9: die Eskalations-Bedienung ist geteilt und sichtbar.

    escalationBlock existiert genau einmal und wird von der Schritt-Karte
    (cardTimeSection) UND dem klassischen Inspektor (nodePerformSections)
    aufgerufen; der Dialog setEscalationFor spricht den
    escalation-policy-Endpunkt an, und die Aufgabenliste kennzeichnet
    eskalierte Eintraege. Die Regellogik (T3a-T3c, Sweep) lebt im Kern
    (test_escalation.py).
    """

    src = APP_JS.read_text(encoding="utf-8")
    assert src.count("function escalationBlock(") == 1, "escalationBlock fehlt/doppelt"
    assert "escalationBlock(body, schema, node" in _fn_body("cardTimeSection"), (
        "Die Schritt-Karte zeigt die Eskalation nicht"
    )
    assert "escalationBlock(body, schema, node" in _fn_body("nodePerformSections"), (
        "Der klassische Inspektor zeigt die Eskalation nicht"
    )
    assert "escalation-policy" in _fn_body("setEscalationFor"), (
        "Der Eskalations-Dialog spricht den Endpunkt nicht an"
    )
    assert "escalated_stage" in _fn_body("viewTasks"), (
        "Die Aufgabenliste kennzeichnet eskalierte Eintraege nicht"
    )


def test_activity_detail_actions_are_wired_in_the_tasks_view() -> None:
    """E2: Anhalten/Weiterarbeiten/Problem/Wiederanlauf sind bedienbar.

    Die vier geteilten Funktionen existieren genau einmal und sprechen die
    E2-Endpunkte an; die Aufgabenliste zeigt die Detailzustaende (angehalten/
    gescheitert) und bietet je Zustand die passenden Aktionen; die
    Instanz-Sicht nennt Detail samt Begruendung. Die Regeln (V1-V4) leben im
    Kern (test_activity_detail.py).
    """

    src = APP_JS.read_text(encoding="utf-8")
    for fn, endpoint in (
        ("suspendTask", "/suspend"),
        ("resumeTask", "/resume"),
        ("failTask", "/fail"),
        ("resetTask", "/reset"),
    ):
        assert src.count(f"function {fn}(") == 1, f"{fn} fehlt/doppelt"
        assert endpoint in _fn_body(fn), f"{fn} spricht {endpoint} nicht an"
    view = _fn_body("viewTasks")
    for needle in ("SUSPENDED", "FAILED", "resumeTask(t, agentId)", "resetTask(t, agentId)"):
        assert needle in view, f"Der Aufgabenliste fehlt {needle}"
    assert "node_details" in src and "node_detail_reason" in src, (
        "Die Instanz-Sicht kennt die Detailzustaende nicht"
    )


def test_simulation_panel_is_wired_into_the_test_view() -> None:
    """E6: die Was-waere-wenn-Simulation ist in der Pruefinstanz-Sicht.

    simulationPanel existiert genau einmal, erscheint in BEIDEN Zustaenden
    der Sicht (Startkarte ohne Instanz und Cockpit mit Instanz), spricht den
    simulate-Endpunkt an und rendert das Ergebnis mit demselben renderGraph
    wie jede Laufzeit-Sicht (synthetische Instanz). Die Semantik (Weg, Kappe,
    Dauer, Reinheit) ist im Kern getestet (test_simulation.py).
    """

    src = APP_JS.read_text(encoding="utf-8")
    assert src.count("function simulationPanel(") == 1, "simulationPanel fehlt/doppelt"
    view = _fn_body("viewTestRun")
    assert view.count("simulationPanel(") == 2, (
        "Die Simulation muss in beiden Zustaenden der Pruefinstanz-Sicht stehen"
    )
    assert "/simulate" in _fn_body("simulationPanel"), (
        "Das Panel spricht den simulate-Endpunkt nicht an"
    )
    assert "renderGraph(schema, { instance: sim })" in _fn_body("renderSimulationResult"), (
        "Das Ergebnis nutzt nicht die geteilte Laufzeit-Darstellung"
    )


def test_z4_filter_rho_bar_and_overdue_summary_are_wired() -> None:
    """Z4 (Priorisierungs-Konzept §7): die drei UI-Verfeinerungen sind da.

    Kritikalitaets-Filter in "Meine Aufgaben" (persistiert, rein clientseitig
    ueber die vorhandenen API-Felder), Rho-Verbrauchsbalken in der
    Faellig-Zelle (Farbe = Band, ueber Themen-Variablen) und die
    Ueberfaellig-Kachel im Monitoring.
    """

    view = _fn_body("viewTasks")
    assert 'localStorage.getItem("taskFilter")' in view, "Filter wird nicht gemerkt"
    assert '"overdue"' in view and '"critical"' in view, "Filterstufen fehlen"
    assert "visible.map((t)" in view, "Die Tabelle nutzt die gefilterte Liste nicht"
    due = _fn_body("dueCell")
    assert "rho-bar" in due and "target_seconds" in due, "Rho-Balken fehlt in dueCell"
    monitor = _fn_body("viewMonitor")
    assert "OVERDUE" in monitor, "Monitoring zaehlt keine ueberfaelligen Aufgaben"
    css = (APP_JS.parent / "styles.css").read_text(encoding="utf-8")
    assert ".rho-fill" in css and "var(--" in css.split(".rho-fill", 1)[1][:200], (
        "Rho-Balken-Stile fehlen oder nutzen keine Themen-Variablen"
    )


def test_sync_edges_are_drawn_and_editable_in_both_surfaces() -> None:
    """K4: Sync-Kanten sind sichtbar und in beiden Oberflaechen bedienbar.

    controlEdges/syncEdges trennen Struktur- von Warte-Kanten (jede
    Strukturlogik -- Layout, Schleifenpaarung, Verschiebe-Ziele, Nachbarn --
    arbeitet kontrollfluss-rein), renderGraph zeichnet Sync gestrichelt,
    syncBlock ist die EINE geteilte Bedienung (Karte + klassischer
    Inspektor), und der Querschritt (insertBetweenNodeSets) haengt an beiden
    AND-Split-Panels. Regeln (K4) und Warte-Semantik sind im Kern getestet
    (test_sync_edges.py).
    """

    src = APP_JS.read_text(encoding="utf-8")
    assert src.count("function controlEdges(") == 1, "controlEdges fehlt/doppelt"
    assert src.count("function syncEdges(") == 1, "syncEdges fehlt/doppelt"
    assert "gsyncedge" in _fn_body("renderGraph"), "Sync-Kanten werden nicht gezeichnet"
    for fn in ("loopPairsOf", "moveTargetsFor", "emptyBranchJoin", "ancestorsOf"):
        assert "controlEdges(schema)" in _fn_body(fn), (
            f"{fn} arbeitet nicht kontrollfluss-rein"
        )
    assert src.count("function syncBlock(") == 1, "syncBlock fehlt/doppelt"
    assert 'cardSection("sync"' in _fn_body("stepCard"), (
        "Die Schritt-Karte zeigt die Synchronisation nicht"
    )
    assert "syncBlock(body, schema, node, true)" in _fn_body("nodePerformSections"), (
        "Der klassische Inspektor zeigt die Synchronisation nicht"
    )
    assert "/sync-edge" in _fn_body("addSyncEdgeFor"), "Setzen-Dialog ohne Endpunkt"
    assert "/sync-edge/remove" in _fn_body("removeSyncEdgeFor"), "Loesen ohne Endpunkt"
    assert src.count("insertBetweenDialog()") >= 2, (
        "Der Querschritt-Dialog haengt nicht an beiden Split-Panels"
    )
    assert "/insert-between" in _fn_body("insertBetweenDialog"), (
        "Der Querschritt-Dialog spricht den Endpunkt nicht an"
    )
    css = (APP_JS.parent / "styles.css").read_text(encoding="utf-8")
    assert ".gsyncedge" in css and "var(--" in css.split(".gsyncedge", 1)[1][:200], (
        "Sync-Kanten-Stil fehlt oder nutzt keine Themen-Variablen"
    )


def test_model_hints_are_visible_in_both_surfaces() -> None:
    """Die beratenden Modellhinweise (G-Gruppe, /metrics) sind sichtbar.

    Der Kern berechnet die 7PMG-Hinweise plus G8 (Soll-Zeit-Luecke) seit jeher,
    aber der Client fragte /metrics nie ab -- die Hinweise waren unsichtbar.
    Jetzt laedt refreshSchema sie best-effort (ein Fehler blockiert das
    Modellieren nie), die Statusleiste der Karten-Sicht zeigt sie als neutralen
    Zaehler (grau -- ein Hinweis ist kein Befund), und das Korrektheits-Panel
    der klassischen Sicht listet sie aus.
    """

    refresh = _fn_body("refreshSchema")
    assert "/metrics" in refresh, "refreshSchema laedt die Modellhinweise nicht"
    assert "catch" in refresh, "Der Hinweis-Abruf muss best-effort sein"

    bar = _fn_body("modelStatusBar")
    assert "state.hints" in bar and "Hinweis(e)" in bar, (
        "Die Statusleiste (Karten-Sicht) zeigt die Hinweise nicht"
    )
    assert 'pill-gray' in bar, "Hinweise muessen neutral wirken, nicht wie Befunde"

    panel = _fn_body("findingsPanel")
    assert "state.hints" in panel, (
        "Das Korrektheits-Panel (klassische Sicht) listet die Hinweise nicht"
    )


def test_move_node_action_is_shared_by_both_surfaces() -> None:
    """„Verschieben…" ist EINE geteilte Funktion, die beide Sichten aufrufen.

    Ein Schritt laesst sich samt aller Bindungen umhaengen (moveNode, Kern-
    Endpunkt ``POST /schemas/{id}/nodes/{nid}/move``). Wie bei jeder
    Knotenoperation gilt: eine gemeinsame Funktion fuer Karte und klassische
    Sicht -- sonst driften die Oberflaechen auseinander. Die Zielliste ist nur
    eine Anzeige-Vorauswahl; die Korrektheitsentscheidung trifft der Kern
    (validate-before-commit).
    """

    src = APP_JS.read_text(encoding="utf-8")
    assert src.count("function moveNodeDialog(") == 1, (
        "moveNodeDialog muss genau einmal existieren (geteilte Funktion)"
    )
    dialog = _fn_body("moveNodeDialog")
    assert "/move" in dialog and "after_node_id" in dialog, (
        "moveNodeDialog spricht nicht den moveNode-Endpunkt an"
    )
    assert "moveNodeDialog(node.id)" in _fn_body("cardFlowSection"), (
        "Die Schritt-Karte bietet kein Verschieben an"
    )
    assert "moveNodeDialog(node.id)" in _fn_body("nodeInspectorPanel"), (
        "Die klassische Sicht bietet kein Verschieben an"
    )
    # Die Vorauswahl schliesst nur offensichtlich Unmoegliches aus: Splits
    # (mehrere Ausgaenge), das Ende, den Schritt selbst und den No-op-Anker.
    targets = _fn_body("moveTargetsFor")
    assert "NODE_TYPE.END" in targets, "Das Ende darf kein Anker sein"


def test_step_card_offers_every_binding_at_the_node() -> None:
    """An der Karte laesst sich jede Bindung des Schritts selbst setzen.

    Der Kern der Umstellung: Binden war ein Zwei-Orte-Vorgang (Schritt links
    waehlen, ⊕ rechts in der Palette klicken). In der Karte muss der Knopf dort
    stehen, wo der Schritt steht -- sonst ist nichts gewonnen.
    """

    src = APP_JS.read_text(encoding="utf-8")
    assert "function bindDataDialog(" in src, "Der Bindungsdialog fuer Daten fehlt"
    assert "function bindStaffDialog(" in src, "Der Bindungsdialog fuer Bearbeiter fehlt"

    data = _fn_body("cardDataSection")
    assert "bindDataDialog(node.id)" in data, "Der Daten-Abschnitt bietet kein Binden an"
    staff = _fn_body("cardStaffSection")
    assert "bindStaffDialog(node.id)" in staff, "Der Bearbeiter-Abschnitt bietet kein Zuordnen an"
    service = _fn_body("cardServiceSection")
    assert "assignServiceFor(node.id)" in service, "Der Dienst-Abschnitt bietet keine Zuweisung an"

    # Anlegen eines fehlenden Datenelements ohne Umweg ueber die Datensicht --
    # und zurueck in den Bindungsdialog, statt den Nutzer stehen zu lassen.
    binder = _fn_body("bindDataDialog")
    assert "addDataElement(() => bindDataDialog(nodeId))" in binder, (
        "Aus dem Bindungsdialog heraus laesst sich kein neues Datenelement anlegen"
    )


def test_card_view_never_sends_the_user_somewhere_else_to_bind() -> None:
    """Kein Abschnitt der Karte verweist zum Binden auf eine andere Stelle.

    Regressionsschutz gegen das zurueckkehrende Zwei-Orte-Modell: Die alten
    Hinweistexte lauteten woertlich „rechts unter ‚Binden' ein Datenelement mit
    ⊕ an diesen Schritt zuweisen" -- eine Anleitung, die die Oberflaeche selbst
    geben sollte.
    """

    for name in ("cardDataSection", "cardStaffSection", "cardMailSection", "cardServiceSection"):
        body = _fn_body(name)
        assert "rechts unter" not in body, (
            f"{name} verweist zum Binden wieder auf eine andere Stelle der Oberflaeche"
        )


def test_card_appears_only_with_a_selection() -> None:
    """Ohne gewaehlten Schritt gehoert die Flaeche dem Kontrollfluss."""

    card = _fn_body("stepCard")
    assert re.search(r"if \(!node\) return null;", card), (
        "stepCard liefert auch ohne Auswahl eine Karte -- dann ist die Flaeche "
        "dauerhaft belegt und nichts gewonnen"
    )
    view = _fn_body("viewModelCard")
    assert "if (card) graphBody.appendChild(card);" in view, (
        "Die Karte wird unbedingt eingehaengt"
    )
    assert "grid-2" not in view, (
        "Die Karten-Sicht baut wieder ein Zwei-Spalten-Raster -- der Kontrollfluss "
        "bekommt dann nicht die volle Breite"
    )


def test_card_survives_a_rerender_without_losing_focus() -> None:
    """Fokus und Schreibmarke im Bezeichnungsfeld ueberleben ein Neuzeichnen.

    Die Sichten bauen ihr DOM bei jedem ``render()`` komplett neu auf, und auf
    der Modellieren-Sicht rendert auch der Tour-Tick. Ohne Sicherung verschluckt
    ein Hintergrund-Lauf mitten im Tippen Fokus und Cursorposition.
    """

    view = _fn_body("viewModelCard")
    assert view.find("captureCardNameFocus()") < view.find("clear(content)"), (
        "Der Fokus wird erst NACH dem Leeren gesichert -- dann ist er schon weg"
    )
    assert "applyCardFocus(content, nameFocus)" in view, (
        "Der gesicherte Fokus wird nach dem Aufbau nicht wiederhergestellt"
    )
    apply_body = _fn_body("applyCardFocus")
    assert "setSelectionRange" in apply_body, "Die Schreibmarke wird nicht wiederhergestellt"


def test_findings_are_shown_at_their_node() -> None:
    """Befunde des Kerns erscheinen am betroffenen Knoten, nicht nur als Liste.

    ``ValidationFinding`` traegt ein optionales ``node_id``; der Client gruppiert
    nur und zeigt an. Befunde OHNE Knotenbezug (modellweit, z. B. T2) muessen
    trotzdem sichtbar bleiben -- sie landen in der Statusleiste.
    """

    src = APP_JS.read_text(encoding="utf-8")
    assert "function findingsByNode()" in src and "function globalFindings()" in src

    mark = _fn_body("renderNodeFindingMark")
    assert "opts.findings" in mark and "gfind" in mark, "Der Befund-Marker fehlt"

    view = _fn_body("viewModelCard")
    assert "findings: findingsByNode()" in view, "Die Sicht reicht die Befunde nicht an den Graphen"
    status = _fn_body("modelStatusBar")
    assert "globalFindings()" in status, (
        "Modellweite Befunde (ohne node_id) tauchen nirgends auf -- sie haben "
        "keinen Knoten, an dem sie stehen koennten"
    )


def test_quick_ring_only_in_the_modelling_view() -> None:
    """Ausfuehrung, Monitoring und Pruefinstanz bleiben unveraendert.

    Alle Sichten teilen sich ``renderGraph``. Der Schnellring haengt deshalb an
    einer Option, die nur die Modellieren-Sicht setzt -- sonst erschienen
    Bearbeiten-Knoepfe auf einer laufenden Instanz.
    """

    ring = _fn_body("renderNodeRing")
    assert "if (!opts.onNodeAction || opts.selectedId !== node.id) return;" in ring, (
        "Der Schnellring prueft nicht, ob die Sicht ihn ueberhaupt angefordert hat"
    )
    # Nur die Karten-Sicht reicht die Aktion herein.
    assert "onNodeAction" in _fn_body("viewModelCard")
    assert "onNodeAction" not in _fn_body("viewModelClassic")
    assert "onNodeAction" not in _fn_body("renderInstanceDetail")


def test_card_leaves_the_fit_button_and_the_node_visible() -> None:
    """Die Karte darf weder den Einpassen-Knopf noch den Knoten verdecken.

    Sie liegt als Overlay rechts oben ueber dem Canvas -- genau dort, wo auch
    ``.canvas-fit`` sitzt, und genau dorthin zentriert ``centerOn`` den
    gewaehlten Knoten. Beides muss ausweichen, sonst bearbeitet man einen
    Schritt, den man nicht sieht.
    """

    css = _css_without_comments()
    card = re.search(r"\.step-card\s*\{([^}]*)\}", css)
    assert card, "Die Schritt-Karte fehlt in styles.css"
    # Ohne eigenen Bezugsrahmen haengt die absolut positionierte Karte am
    # naechsten positionierten Vorfahren -- im Zweifel am Fenster.
    assert re.search(r"\.model-canvas \.graph-body \{[^}]*position: relative", css), (
        "Der Karte fehlt ihr Bezugsrahmen -- sie landet irgendwo auf der Seite"
    )
    # Der Einpassen-Knopf sitzt oben rechts im Canvas, also unter der Karte.
    assert re.search(r"\.graph-body:has\(\.step-card\) \.canvas-fit", css), (
        "Der Einpassen-Knopf weicht der Karte nicht aus -- er liegt darunter und "
        "ist nicht mehr klickbar"
    )

    view = _fn_body("viewModelCard")
    assert "reserveCardWidth(graph, card)" in view, (
        "Die Canvas erfaehrt nichts von der Karte -- sie zentriert den gewaehlten "
        "Knoten dann unter das Overlay"
    )
    panzoom = _panzoom_body()
    assert "setReserve(px)" in panzoom, "Der Pan/Zoom-Controller kennt keine Reserve"
    assert panzoom.count("vwFree") >= 3, (
        "Die Reserve geht nicht in Einpassen UND Zentrieren ein"
    )


def test_empty_process_offers_the_first_step() -> None:
    """Ein leerer Prozess bietet den Einstieg an, statt leer dazustehen."""

    empty = _fn_body("canvasEmptyState")
    assert "openInsertModal(start.id)" in empty, (
        "Die Einstiegskarte fuehrt nicht auf denselben Einfuege-Dialog"
    )
    assert "activitiesOf(schema).length" in empty, (
        "Die Einstiegskarte erscheint nicht nur beim wirklich leeren Prozess"
    )


def test_arrow_keys_move_the_selection_in_both_surfaces() -> None:
    """Die Tastaturnavigation gehoert beiden Modellier-Oberflaechen.

    Stufe U4 des Konzepts (§5.2): Pfeiltasten bewegen die Auswahl im
    Kontrollfluss. Weil beide Sichten dieselbe Auswahl fuehren, liegt der Weg
    bewusst **nicht** in einer Sichtfunktion, sondern in einer geteilten
    Operation (``moveSelection``) am globalen Tastenweg -- sonst haette die
    klassische Sicht die Tasten still verloren.
    """

    src = APP_JS.read_text(encoding="utf-8")
    assert "function graphNeighbor(" in src, "Die Nachbarschafts-Berechnung fehlt"
    assert "function moveSelection(" in src, "Die gemeinsame Auswahl-Operation fehlt"

    move = _fn_body("moveSelection")
    assert "graphNeighbor(layoutSchema(schema)" in move, (
        "moveSelection() rechnet nicht auf dem Layout -- dann kennt es die Bahnen nicht"
    )
    assert "state.selectedNode = next" in move, "moveSelection() setzt die Auswahl nicht"

    # Der Tastenweg haengt nicht an einer der beiden Oberflaechen.
    assert "ARROW_DIRS" in src, "Die Pfeiltasten sind nicht zugeordnet"
    for key in ("ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"):
        assert key in src, f"{key} ist nicht belegt"
    assert 'modelUx() === "card"' not in _fn_body("moveSelection"), (
        "Die Bewegung ist auf eine Oberflaeche eingeschraenkt"
    )


def test_arrow_keys_yield_to_typing_and_to_dialogs() -> None:
    """Die Tastenwege stehlen weder Schreibmarke noch Dialog-Tasten.

    Der Tastenweg haengt am Dokument und feuert damit ueberall. Ohne diese drei
    Ausnahmen wandert die Auswahl, waehrend im Bezeichnungsfeld getippt wird
    (Pfeiltasten), verschluckt Enter den Knopfdruck und raeumt die Sicht hinter
    einem offenen Dialog um.
    """

    src = APP_JS.read_text(encoding="utf-8")
    handler = re.search(
        r"const dir = ARROW_DIRS\[e\.key\];.*?\n  \}\);", src, re.S
    )
    assert handler, "Der Tastenweg fuer die Navigation wurde nicht gefunden"
    body = re.sub(r"//[^\n]*", "", handler.group(0))
    assert "isTypingTarget()" in body, "Tippen in einem Feld wird nicht ausgenommen"
    assert 'byId("modal-root")' in body, "Ein offener Dialog wird nicht ausgenommen"
    assert 'tag === "BUTTON"' in body, (
        "Enter wuerde dem fokussierten Knopf weggenommen"
    )
    assert "e.ctrlKey" in body and "e.metaKey" in body, (
        "Browser-Kombinationen mit Zusatztaste werden nicht durchgelassen"
    )
    assert 'state.view !== "model"' in body, (
        "Die Tasten wirken ausserhalb der Modellieren-Sicht"
    )
    assert "e.preventDefault()" in body, (
        "Die Seite scrollt weiter mit, waehrend die Auswahl wandert"
    )

    # Escape und die Navigation teilen sich dieselbe Tipp-Erkennung.
    assert "function isTypingTarget()" in src, "Die gemeinsame Tipp-Erkennung fehlt"
    assert src.count("isTypingTarget()") >= 3, (
        "Escape nutzt die gemeinsame Tipp-Erkennung nicht mit"
    )


def test_task_mask_is_prefilled_from_the_instance_data() -> None:
    """Die Aufgabenmaske zeigt die bereits erfassten Werte.

    ``promptComplete`` baute jedes Bedienelement mit ``maskControl(..., null)``,
    also ohne aktuellen Wert. Ein **Nur-Lese-Feld** ist aber per Definition die
    Entscheidungsgrundlage des Schritts (der Auftragswert bei der Freigabe, der
    Rechnungsbetrag beim Zahlungsabgleich) -- leer war es wertlos, und ein aus
    einem Vor- oder Elternprozess uebernommener Wert musste abgetippt werden.

    Der Waechter haelt die Kette fest: die Aufrufer reichen die Instanzdaten
    durch, und ``promptComplete`` gibt sie je Feld an ``maskControl`` weiter --
    in **beiden** Zweigen (gestaltete Maske und generischer Rueckfall).
    """

    src = APP_JS.read_text(encoding="utf-8")
    body = re.search(
        r"\nasync function promptComplete\(.*?\n\}\n", src, re.S
    )
    assert body, "promptComplete() nicht gefunden -- Waechter angleichen"
    code = re.sub(r"//[^\n]*", "", body.group(0))

    assert "dataValues" in code.split("\n")[1], (
        "promptComplete nimmt die Instanzdaten nicht entgegen"
    )
    assert code.count("maskControl(") == 2, (
        "Die Maske wird nicht mehr an genau zwei Stellen gebaut -- Waechter pruefen"
    )
    assert "maskControl(elem, f.widget, f.options, values[f.element_id])" in code, (
        "Die gestaltete Maske wird nicht aus den Instanzdaten vorbelegt"
    )
    assert "maskControl(elem, widget, null, values[a.element_id])" in code, (
        "Der generische Rueckfall wird nicht aus den Instanzdaten vorbelegt"
    )

    # Jeder Aufrufer muss die Werte auch tatsaechlich mitgeben, sonst bleibt die
    # Vorbelegung im Einzelfall wirkungslos.
    for caller in ("completeActivity", "completeTask", "completeTestTask"):
        fn = re.search(rf"\nasync function {caller}\(.*?\n\}}\n", src, re.S)
        assert fn, f"{caller}() nicht gefunden -- Waechter angleichen"
        assert "data_values" in fn.group(0), (
            f"{caller} reicht die Instanzdaten nicht an die Maske durch"
        )


def test_empty_required_field_blocks_the_completion() -> None:
    """Ein leeres Pflichtfeld wird an der Maske abgefangen, nicht spaeter.

    Der Kern erzwingt beim Abschluss **nicht**, dass die Pflicht-Schreibwerte
    eines Schritts wirklich mitkommen -- ``complete_activity`` uebernimmt, was
    da ist. Ein leer gelassenes Feld faellt darum erst an ganz anderer Stelle
    auf: an der XOR-Verzweigung, die den Diskriminator braucht, oder an einer
    Folgeprozess-Bedingung -- und dort als Laufzeitfehler eines *anderen*
    Schritts. Das Modell erklaert die Pflicht bereits (``FormField.required``),
    also haelt die Maske sie auch ein.
    """

    src = APP_JS.read_text(encoding="utf-8")
    body = re.search(r"\nasync function promptComplete\(.*?\n\}\n", src, re.S)
    assert body, "promptComplete() nicht gefunden -- Waechter angleichen"
    code = re.sub(r"//[^\n]*", "", body.group(0))

    assert "required" in code, "Die Pflichtangabe der Maske wird nicht ausgewertet"
    assert "missing.length" in code, "Fehlende Pflichtfelder werden nicht geprueft"
    assert re.search(r"if \(missing\.length\) \{\s*toast\(", code), (
        "Fehlende Pflichtfelder werden nicht gemeldet"
    )
    absenden = code.index("api.post(")
    pruefung = code.index("missing.length")
    assert pruefung < absenden, (
        "Die Pflichtfeldpruefung steht hinter dem Absenden -- sie kaeme zu spaet"
    )


def test_release_names_the_steps_that_block_it() -> None:
    """Fehlt einem Schritt der Bearbeiter, nennt die Oberflaeche ihn beim Namen.

    Entschieden wird im Kern: ``operations.release`` lehnt ab (Stufe B, Regel
    B2), weil ein solcher Schritt zur Laufzeit zwar aktiviert wird, aber in
    **keiner** Arbeitsliste auftaucht -- der Vorgang saehe gestartet aus und
    stuende still. Die Pruefung im Client ist deshalb kein zweiter Entscheider,
    sondern erspart den vergeblichen Aufruf und die Zuordnung roher Befunde:
    sie benennt die Schritte und schickt gar nicht erst ab.
    """

    src = APP_JS.read_text(encoding="utf-8")
    body = re.search(r"\nasync function releaseSchema\(\) \{.*?\n\}\n", src, re.S)
    assert body, "releaseSchema() nicht gefunden -- Waechter angleichen"
    code = re.sub(r"//[^\n]*", "", body.group(0))

    assert "releaseFindings()" in code, (
        "releaseSchema fragt die Stufe-B-Befunde des Kerns nicht ab"
    )
    assert "nodeLabelOf" in code, "Die blockierenden Schritte werden nicht benannt"
    pruefung = code.index("missing.length")
    absenden = code.index("api.post(")
    assert pruefung < absenden, (
        "Die Pruefung steht hinter dem Absenden -- sie kaeme zu spaet"
    )
    # Nach dem Befund wird zurueckgekehrt, BEVOR ueberhaupt gesendet wird.
    # (Textuell statt per Klammer-Regex geprueft: der Block enthaelt
    # Template-Literale mit `}`, an denen ein Regex zerbraeche.)
    assert code.index("return;", pruefung) < absenden, (
        "Bei fehlenden Bearbeitern wird trotzdem abgeschickt"
    )


def test_release_readiness_comes_from_the_core_not_the_client() -> None:
    """Die Befunde stammen aus der Validierung des Kerns, nicht aus dem Client.

    Der Web-Client traegt keine Korrektheits- und keine Reifegrad-Logik: er
    zeigt an, was ``GET /schemas/{id}/validation`` liefert. Rechnete er selbst
    nach, gaebe es zwei Wahrheiten, die auseinanderlaufen koennen.
    """

    src = APP_JS.read_text(encoding="utf-8")
    body = re.search(r"\nfunction releaseFindings\(\) \{.*?\n\}\n", src, re.S)
    assert body, "releaseFindings() nicht gefunden -- Waechter angleichen"

    assert "release_findings" in body.group(0), (
        "releaseFindings liest nicht das Feld des Kerns (release_findings)"
    )
    assert "staff_rules" not in body.group(0), (
        "Der Client leitet die Freigabe-Reife selbst her statt sie zu lesen"
    )



def test_status_bar_separates_correctness_from_release_readiness() -> None:
    """„korrekt" und „freigabereif" stehen nebeneinander, nicht vermischt.

    Stufe A ist eine Invariante (gilt nach jeder Operation), Stufe B ein
    Reifegrad (darf im Entwurf offen sein). Wuerde die Oberflaeche beides in
    eine Anzeige werfen, sähe ein unfertiger -- aber voellig korrekter --
    Entwurf wie ein fehlerhafter aus.
    """

    src = APP_JS.read_text(encoding="utf-8")
    body = re.search(r"\nfunction modelStatusBar\(.*?\n\}\n", src, re.S)
    assert body, "modelStatusBar() nicht gefunden -- Waechter angleichen"
    code = re.sub(r"//[^\n]*", "", body.group(0))

    assert "pill-green" in code and "pill-red" in code, "Stufe A fehlt in der Leiste"
    assert "releaseFindings()" in code, "Stufe B fehlt in der Leiste"
    assert "pill-amber" in code, (
        "Die Freigabe-Reife wird nicht als eigener, milderer Zustand gezeigt"
    )


def test_time_dialog_offers_the_net_time_opt_in() -> None:
    """Der geteilte Frist-Dialog traegt das Netto-Zeit-Opt-in (E2 Stufe C).

    ``pause_stops_clock`` ist ein bewusstes Opt-in des Modellierers: ohne
    Haekchen laeuft die Uhr in Pausen weiter (Anti-Schlupfloch-Linie des
    Detailzustaende-Konzepts §4). Der Waechter sichert zu, dass das Feld im
    EINEN geteilten Dialog ``setTimeConstraintFor`` lebt -- beide
    Modellier-Oberflaechen rufen ihn auf, keine formuliert ihn selbst aus.
    """

    src = APP_JS.read_text(encoding="utf-8")
    dialog = re.search(r"function setTimeConstraintFor\(.*?\n\}", src, re.S)
    assert dialog, "setTimeConstraintFor() nicht gefunden -- Waechter angleichen"
    body = dialog.group(0)
    assert "pause_stops_clock" in body, "Das Netto-Zeit-Opt-in fehlt im Frist-Dialog"
    assert "Netto-Zeit" in body, "Die Beschriftung des Opt-ins fehlt"
    # Beide Oberflaechen nutzen den geteilten Dialog (keine Drift).
    assert src.count("setTimeConstraintFor(") >= 3, (
        "setTimeConstraintFor muss von beiden Modellier-Oberflaechen aufgerufen werden"
    )


def test_monitoring_shows_the_escalation_view() -> None:
    """Das Monitoring traegt die Eskalations-Sicht (Eskalations-Konzept §8 C).

    Kachel „Eskalierte Aufgaben" + Panel „Eskalationen" (Aufgaben mit
    gefeuerten Stufen, klickbar zur Instanz) leben in ``viewMonitor`` und
    speisen sich aus denselben Task-Listen wie die Ueberfaellig-Kachel --
    keine neue Datenquelle, rein anzeigend.
    """

    src = APP_JS.read_text(encoding="utf-8")
    monitor = re.search(r"async function viewMonitor\(\) \{.*?\n\}", src, re.S)
    assert monitor, "viewMonitor() nicht gefunden -- Waechter angleichen"
    body = monitor.group(0)
    assert "escalated_stage" in body, "Die Eskalations-Sicht fehlt im Monitoring"
    assert "Eskalierte Aufgaben" in body, "Die Kachel 'Eskalierte Aufgaben' fehlt"
    assert "Eskalationen" in body, "Das Panel 'Eskalationen' fehlt"
    assert "openInstanceFromMonitor" in body, (
        "Eskalierte Aufgaben muessen zur Instanz verlinken"
    )


def test_simulation_playback_animates_the_executed_order() -> None:
    """Die Simulation traegt die Abspiel-Animation (Simulations-Konzept §6 B).

    Zusagen: (1) ``renderGraph`` adressiert jede Knoten-Gruppe ueber
    ``data-node-id`` -- darauf baut die Animation auf. (2) Das
    Simulationsergebnis bietet ``simulationPlaybackControls`` an, das die
    Abschlussreihenfolge ``executed`` abspielt. (3) Der Takt raeumt sich
    selbst, wenn das SVG den DOM verlaesst (kein Timer-Leck nach einem
    Re-Render). (4) Die CSS-Klassen der Animation existieren.
    """

    src = APP_JS.read_text(encoding="utf-8")
    assert '"data-node-id": id' in src, (
        "renderGraph muss die Knoten-Gruppen per data-node-id adressierbar machen"
    )
    controls = re.search(
        r"function simulationPlaybackControls\(.*?\n\}", src, re.S
    )
    assert controls, "simulationPlaybackControls() fehlt"
    body = controls.group(0)
    assert "executed" in body and "setInterval" in body
    assert "isConnected" in body, (
        "Die Animation muss abbrechen, wenn das SVG den DOM verlaesst (Timer-Leck)"
    )
    result_fn = re.search(r"function renderSimulationResult\(.*?\n\}", src, re.S)
    assert result_fn and "simulationPlaybackControls(" in result_fn.group(0), (
        "Das Simulationsergebnis bietet die Abspiel-Animation nicht an"
    )
    css = STYLES_CSS.read_text(encoding="utf-8")
    assert ".gnode.sim-dim" in css and ".gnode.sim-current" in css, (
        "Die CSS-Klassen der Abspiel-Animation fehlen"
    )


def test_oidc_redirect_login_is_wired_with_pkce() -> None:
    """Der OIDC-Redirect-Login (Auth-Konzept §12.4, Opt-in) haengt korrekt.

    Zusagen: (1) PKCE mit S256 -- der Verifier verlaesst den Browser nie
    unhehasht Richtung Authorize-Endpunkt. (2) Der Ruecksprung wird gegen den
    gespeicherten State geprueft (CSRF-Schutz des Code-Flows). (3) Der Code
    wird per grant_type=authorization_code am konfigurierten Token-Endpunkt
    eingeloest und das Token landet im EINEN bestehenden Bearer-Weg
    (state.token + localStorage). (4) boot() loest den Ruecksprung ein, BEVOR
    eine Gate-Entscheidung faellt, und zeigt ohne Token die Anmeldekarte nur
    im JWT-Modus mit Konfiguration.
    """

    src = APP_JS.read_text(encoding="utf-8")
    start = re.search(r"async function startOidcLogin\(\) \{.*?\n\}", src, re.S)
    assert start, "startOidcLogin() fehlt"
    assert "SHA-256" in start.group(0) and "S256" in start.group(0), (
        "PKCE muss den S256-Challenge-Hash nutzen"
    )
    assert "code_challenge" in start.group(0)

    complete = re.search(r"async function completeOidcLogin\(\) \{.*?\n\}", src, re.S)
    assert complete, "completeOidcLogin() fehlt"
    body = complete.group(0)
    assert "returnedState !== expected" in body, "Die State-Pruefung fehlt (CSRF)"
    assert "authorization_code" in body and "code_verifier" in body
    assert "localStorage.setItem(\"authToken\"" in body, (
        "Das eingeloeste Token muss in den bestehenden Bearer-Weg"
    )

    boot_body = re.search(r"async function boot\(\) \{.*?\n\}", src, re.S)
    assert boot_body, "boot() nicht gefunden"
    b = boot_body.group(0)
    assert b.find("completeOidcLogin()") != -1, "boot() loest den Ruecksprung nicht ein"
    assert b.find("completeOidcLogin()") < b.find("showOidcLoginOverlay()"), (
        "Der Ruecksprung muss vor der Anmeldekarte eingeloest werden"
    )
    assert b.find("completeOidcLogin()") < b.find("state.passwordLogin && !state.token"), (
        "Der Ruecksprung muss vor der Passwort-Gate-Entscheidung liegen"
    )


def test_detail_states_are_visible_in_the_process_map() -> None:
    """Angehalten/gescheitert erscheinen direkt in der Prozesslandkarte (E2 C).

    ``renderGraph`` liest ``instance.node_details`` und markiert den Knoten
    (Statustext + Randklasse) -- der Detailzustand ist damit in JEDER
    Laufzeit-Sicht sichtbar, die den Kontrollfluss zeichnet (Instanz-Detail,
    Monitoring), nicht nur in der Aufgabenliste.
    """

    src = APP_JS.read_text(encoding="utf-8")
    graph = re.search(r"function renderGraph\(schema, opts\) \{.*?\n\}", src, re.S)
    assert graph, "renderGraph() nicht gefunden -- Waechter angleichen"
    body = graph.group(0)
    assert "node_details" in body, "renderGraph liest die Detailzustaende nicht"
    assert "angehalten" in body and "gescheitert" in body, (
        "Die Statustexte des Overlays fehlen"
    )
    css = STYLES_CSS.read_text(encoding="utf-8")
    assert ".gnode.d-suspended" in css and ".gnode.d-failed" in css, (
        "Die CSS-Klassen des Status-Overlays fehlen"
    )


def test_audit_names_the_login_and_supervision_asks_for_a_reason() -> None:
    """Acceptance test 2026-09: the audit showed "System" for a human completion.

    The timeline must resolve the actor through ``auditActorLabel`` (agent ->
    login in ``detail.actor`` -> "System" only as the last resort), and a
    completion refused as Aufsichtseingriff must lead to the reason dialog
    instead of a dead-end error toast.
    """

    src = APP_JS.read_text(encoding="utf-8")
    assert 'ev.agent_id ? agentNameOf(ev.agent_id) : "System"' not in src
    assert 'el("span", { class: "tl-actor" }, auditActorLabel(ev))' in src
    assert "ev.detail && ev.detail.actor" in src
    assert "ACTIVITY_SUPERVISED:" in src
    # promptComplete reacts to the core's 422 by asking, and resends with reason.
    assert "if (isSupervisionRequired(err)) { askSupervisionReason(" in src
    assert "payload.supervision_reason = supervisionReason" in src


# ---------------------------------------------------------------------------
# Bedienbarkeit (1.17.1): Dialoge, Meldungen, Demo-Leiste, Tour. Quelltext-
# Waechter wie oben; das Verhalten (Platzierung, Enter, Filter) ist zusaetzlich
# ad hoc im Node-vm belegt worden.
# ---------------------------------------------------------------------------

TOURS_JS = Path(__file__).resolve().parents[2] / "web" / "tour" / "tours.js"
TOUR_ENGINE_JS = Path(__file__).resolve().parents[2] / "web" / "tour" / "engine.js"


def _function_body(src: str, signature: str) -> str:
    """Quelltext einer Top-Level-Funktion bis zur naechsten Top-Level-Definition."""

    start = src.index(signature)
    rest = src[start + len(signature):]
    nxt = re.search(r"\n(?:async )?function |\n(?:const|let) [A-Z_]+ =", rest)
    return src[start: start + len(signature) + (nxt.start() if nxt else len(src))]


def test_service_hint_matches_release_rule_in_both_surfaces() -> None:
    """Die Karte behauptete, jeder Schritt brauche fuer die Freigabe
    einen Dienst (B1). Der Kern erzwingt B1 bewusst nicht. Beide Oberflaechen
    beziehen den Text aus EINER Konstante, damit sie nicht auseinanderlaufen."""

    src = APP_JS.read_text(encoding="utf-8")
    assert "braucht jeder Schritt einen ausführbaren Dienst (B1)" not in src
    assert src.count("SERVICE_OPTIONAL_HINT") == 3  # Definition + Karte + Inspektor
    hint = re.search(r'const SERVICE_OPTIONAL_HINT =\s*"([^"]+)"', src)
    assert hint and "(B2)" in hint.group(1) and "optional" in hint.group(1)


def test_enter_confirms_the_shared_dialog_but_not_textareas_or_buttons() -> None:
    """Enter sendet den Dialog ab -- einmal in ``openModal``, nicht je
    Aufrufer; mehrzeilige Felder und fokussierte Knoepfe behalten Enter."""

    body = _function_body(APP_JS.read_text(encoding="utf-8"), "function openModal(")
    assert 'e.key !== "Enter"' in body and "confirmBtn.click()" in body
    for tag in ('"TEXTAREA"', '"BUTTON"', '"SELECT"'):
        assert tag in body, f"Enter darf in {tag} nicht den Dialog absenden"
    assert "e.isComposing" in body
    assert "return { modal, confirmBtn, close }" in body


def test_error_toasts_stay_until_closed() -> None:
    """Fehler verschwanden nach 7 s. Sie bleiben jetzt bis zum Schliessen;
    nur Erfolg/Info laufen noch per Timer ab."""

    body = _function_body(APP_JS.read_text(encoding="utf-8"), "function toast(")
    assert "7000" not in body
    assert 'const sticky = kind === "err"' in body
    assert '"t-close"' in body
    assert "setTimeout(() => t.remove(), 3500)" in body


def test_monitoring_instance_list_is_filterable_and_not_called_active() -> None:
    """Die Liste "Aktive Instanzen" zeigte auch abgeschlossene."""

    src = APP_JS.read_text(encoding="utf-8")
    assert '"Aktive Instanzen"' not in src
    assert 'el("h2", null, "Instanzen")' in src
    assert "state.monitorFilter" in src
    for key in ('key: "all"', 'key: "RUNNING"', 'key: "COMPLETED"'):
        assert key in src


def test_insert_dialog_blocks_variants_without_their_data_element() -> None:
    """M4: Ohne Datenelement blieb "Einfügen" aktiv und meldete Fachjargon."""

    src = APP_JS.read_text(encoding="utf-8")
    body = _function_body(src, "function openInsertModal(")
    assert "Kein g\\u00FCltiger Diskriminator" not in body
    assert "function syncInsertEnabled()" in body
    assert 'dialog = openModal("Schritt einf' in body
    assert "dialog.confirmBtn.disabled = blocked" in body
    assert body.count("insert-blocked") == 2  # Verzweigung + Schleife


def test_created_schemas_open_in_the_modelling_view() -> None:
    """Nach "Aus Vorlage" blieb die Ansicht auf der vorigen Seite. Anlegen,
    Import und Vorlage oeffnen das neue Schema jetzt in der Modellieren-Sicht."""

    src = APP_JS.read_text(encoding="utf-8")
    helper = _function_body(src, "async function openCreatedSchema(")
    assert 'state.view = "model"' in helper
    for fn in (
        "async function newSchema(",
        "async function importBpmn(",
        "async function newFromTemplate(",
    ):
        assert "openCreatedSchema(schema.id)" in _function_body(src, fn), fn


def test_bpmn_import_accepts_a_file() -> None:
    """Import ging nur ueber ein Textfeld. Die Datei wird im Browser gelesen und
    geht ueber denselben Endpunkt (keine neue Serverflaeche)."""

    body = _function_body(APP_JS.read_text(encoding="utf-8"), "async function importBpmn(")
    assert 'type: "file"' in body and "new FileReader()" in body
    assert 'api.post("/bpmn-import"' in body


def test_loop_max_iterations_is_prefilled() -> None:
    """Hoechstzahl vorbelegt, aber nicht erzwungen (keine Verschaerfung von K6b)."""

    src = APP_JS.read_text(encoding="utf-8")
    assert "const LOOP_MAX_DEFAULT = 10;" in src
    assert "value: String(LOOP_MAX_DEFAULT)" in src
    # Leeren bleibt erlaubt: der Payload wird nur bei gesetztem Wert befuellt.
    assert 'if (loopMax.value.trim() !== "")' in src


def test_demo_banner_reserves_space_instead_of_covering_the_app() -> None:
    """Der Demo-Banner lag ueber Schritt-Karte, Dialogen und Toasts."""

    src = APP_JS.read_text(encoding="utf-8")
    assert "reserveDemoBannerSpace(banner)" in src
    assert "DEMO_BANNER_COLLAPSED_KEY" in src
    css = _css_without_comments()
    banner = re.search(r"\.demo-banner\s*\{([^}]*)\}", css)
    assert banner
    z = int(re.search(r"z-index:\s*(\d+)", banner.group(1)).group(1))
    modal_z = int(re.search(r"\.modal-backdrop\s*\{[^}]*z-index:\s*(\d+)", css).group(1))
    assert z < modal_z, "Der Banner darf nie ueber einem Dialog liegen"
    app_rule = re.search(r"\.app\s*\{([^}]*)\}", css).group(1)
    assert "var(--demo-banner-h, 0px)" in app_rule
    assert "var(--demo-banner-h, 0px)" in re.search(r"\.toast-root\s*\{([^}]*)\}", css).group(1)


def test_tour_uses_a_fallback_anchor_and_side_placement() -> None:
    """Tutorial Schritt 5/7: Anker erst nach der verlangten Auswahl vorhanden ->
    "nicht sichtbar" und mittiges Popup ueber dem Knoten. Jetzt: Ersatzanker aus
    ``also`` und Platzierung neben dem Ziel; kein "links" mehr im Hinweis."""

    engine = TOUR_ENGINE_JS.read_text(encoding="utf-8")
    assert "anchor = extra; fallback = true" in engine
    assert 'placement === "side"' in engine and "function sidePosition(" in engine
    tours = TOURS_JS.read_text(encoding="utf-8")
    assert "Wähle links" not in tours
    assert tours.count('placement: "side"') == 2


# ---------------------------------------------------------------------------
# Meldungskatalog und Migrationsassistent (1.18.0)
# ---------------------------------------------------------------------------


def test_every_finding_display_goes_through_the_catalog() -> None:
    """Befunde erschienen englisch und mit interner Knoten-ID. Jede Anzeigestelle
    nutzt jetzt ``findingText``; die rohe Kernmeldung liest nur noch der
    Rueckfall in ``findingText`` selbst."""

    src = APP_JS.read_text(encoding="utf-8")
    assert src.count("f.message") == 1, "Befundtext bitte ueber findingText(f) anzeigen"
    assert "(f && f.message)" in _function_body(src, "function findingText(")
    assert '` [${f.node_id}]`' not in src, "keine internen Knoten-IDs in Befundzeilen"
    assert "Vom Kern abgelehnt (Regelverletzung)" not in src


def test_catalog_covers_every_code_the_core_emits() -> None:
    """Jeder Befund-Code, den der Kern vergibt, hat einen deutschen Text --
    sonst faellt er unbemerkt auf die englische Meldung zurueck."""

    src_dir = Path(__file__).resolve().parents[1] / "src" / "procworks"
    emitted: set[str] = set()
    for py in src_dir.glob("*.py"):
        text = py.read_text(encoding="utf-8")
        emitted |= set(re.findall(r'code="([A-Z][A-Z0-9]*\.[a-z-]+)"', text))
    assert emitted, "keine Befund-Codes im Kern gefunden -- Waechter angleichen"
    app = APP_JS.read_text(encoding="utf-8")
    catalog = set(re.findall(r'^  "([A-Z][A-Z0-9]*\.[a-z-]+)": ', app, re.M))
    missing = sorted(emitted - catalog)
    assert not missing, "ohne deutschen Text im Katalog: " + ", ".join(missing)


def test_migration_assistant_is_wired_in_both_surfaces_and_the_run_view() -> None:
    """Die Migration war im Client nicht auffindbar. Der Knopf haengt an der
    gemeinsamen Kopfzeile (beide Oberflaechen), die Instanz-Ansicht bietet sie
    einzeln an -- beides fuehrt in denselben Assistenten."""

    src = APP_JS.read_text(encoding="utf-8")
    assert "migrationHeaderButton(schema, draft)" in _function_body(src, "function modelHeader(")
    detail = _function_body(src, "async function renderInstanceDetail(")
    assert "await instanceMigrationPanel(inst)" in detail
    body = _function_body(src, "async function openMigrationAssistant(")
    # Trockenlauf vor Ausfuehrung
    assert "execute: false" in body and "execute: true" in body
    assert "/migration-report" in body and "/migrate-instances" in body
    assert "findingText(f, { withHint: true })" in body


# ---------------------------------------------------------------------------
# Integration vorführbar, Monitoring mit Dauern und Soll/Ist (1.19.0)
# ---------------------------------------------------------------------------


def test_webhook_dialog_offers_a_preview_that_sends_nothing() -> None:
    src = APP_JS.read_text(encoding="utf-8")
    body = _function_body(src, "function addWebhook(")
    assert '"/v1/webhooks/preview"' in body and "renderWebhookPreview(result, p)" in body
    preview = _function_body(src, "function renderWebhookPreview(")
    assert "p.egress_locked" in preview and "p.reason" in preview
    # Die Demo erklaert die Egress-Sperre, statt sie wie einen Defekt wirken zu lassen.
    hint = _function_body(src, "async function webhookPanel(")
    assert "ausgehende Verbindungen gesperrt" in hint


def test_bottleneck_view_shows_lead_time_wait_and_processing() -> None:
    """Die Engpass-Tabelle stand ueberall auf „–“ (nur gestartet -> erledigt)."""

    src = APP_JS.read_text(encoding="utf-8")
    for field in ("avg_total_seconds", "avg_wait_seconds", "avg_duration_seconds"):
        assert f"fmtStepDuration(s.{field})" in src
    helper = _function_body(src, "function fmtStepDuration(")
    assert '"keine Zeitdaten"' in helper and '"< 1 s"' in helper


def test_process_map_is_drawn_over_the_model_with_deviations() -> None:
    """Die entdeckte Prozesskarte war nur eine Tabelle ohne Bezug zum Modell."""

    src = APP_JS.read_text(encoding="utf-8")
    panel = _function_body(src, "async function conformancePanel(")
    assert "/conformance`" in panel and "renderGraph(schema, { observed })" in panel
    assert "report.deviations" in panel and "report.foreign_steps" in panel
    graph = _function_body(src, "function renderGraph(")
    assert "const obs = opts.observed && opts.observed[id];" in graph
    monitor = _function_body(src, "async function viewMonitor(")
    assert "await conformancePanel(instances, pmap)" in monitor


# ---------------------------------------------------------------------------
# Ausbau: Zustaendigkeit, Mehrfachzuordnung, Vorlagen, Einpassen (1.20.0)
# ---------------------------------------------------------------------------


def test_run_view_names_the_responsible_and_offers_completion_accordingly() -> None:
    """Die Instanz-Sicht bot jedem „Abschliessen“ an, egal wer zustaendig war."""

    src = APP_JS.read_text(encoding="utf-8")
    detail = _function_body(src, "async function renderInstanceDetail(")
    assert "/tasks`" in detail
    assert "completionActionFor(inst, nid, node, eligibleOf[nid], runSchema)" in detail
    action = _function_body(src, "function completionActionFor(")
    assert '"nicht deine Aufgabe"' in action
    assert "Als Aufsicht abschlie" in action and "!inst.is_test" in action


def test_staff_rule_can_be_applied_to_several_steps_in_both_surfaces() -> None:
    """Zuordnung ging nur einzeln je Schritt. ``bindStaffDialog`` ist beiden
    Oberflaechen gemeinsam; jede Zuordnung bleibt eine eigene Kern-Operation."""

    src = APP_JS.read_text(encoding="utf-8")
    dialog = _function_body(src, "function bindStaffDialog(")
    assert "otherStepsBox(schema, nodeId)" in dialog and "others.selected()" in dialog
    assert "for (const other of extra)" in dialog  # one request per step, no bulk shortcut
    box = _function_body(src, "function otherStepsBox(")
    assert "Alle ohne Bearbeiter" in box and "automatic" in box


def test_template_gallery_shows_who_does_what() -> None:
    body = _function_body(APP_JS.read_text(encoding="utf-8"), "async function newFromTemplate(")
    assert "t.roles" in body and '"tpl-roles"' in body and "t.step_count" in body


def test_fit_to_view_keeps_a_readable_scale_first() -> None:
    """Einpassen machte grosse Modelle unleserlich klein."""

    body = _function_body(APP_JS.read_text(encoding="utf-8"), "function attachPanZoom(")
    assert "const FIT_READABLE = 0.6;" in body
    assert "fit < FIT_READABLE && !overview" in body
    # the fit button must not reset the two-step state it relies on
    assert 'closest(".canvas-fit")' in body


def test_mask_layout_is_shared_and_the_designer_keeps_help_texts() -> None:
    """Gruppen/Spalten ueber EINE Funktion fuer Vorschau und Aufgabenmaske --
    und der Designer verlor beim Speichern bisher die Hilfetexte."""

    src = APP_JS.read_text(encoding="utf-8")
    assert "maskLayout(" in _function_body(src, "async function promptComplete(")
    designer = _function_body(src, "function openFormDesigner(")
    assert "maskLayout(" in designer and "columns," in designer
    assert designer.count("help_text: f.help_text || null") == 2  # load + save
    css = _css_without_comments()
    assert re.search(r"@media \(max-width: 720px\)\s*\{\s*\.mask-cols", css)


def test_every_operation_precondition_carries_a_code_the_client_can_word() -> None:
    """Die Vorbedingungen der Operationen (Regel OP) erschienen englisch.

    Jede OP-Meldung im Kern traegt einen Code; jede Objekt- und Knotenart, die als
    Parameter mitkommt, hat einen deutschen Namen im Client."""

    ops_path = Path(__file__).resolve().parents[1] / "src" / "procworks" / "operations.py"
    ops_src = ops_path.read_text(encoding="utf-8")
    blocks = re.findall(r"ValidationFinding\((.*?)\n\s*\)", ops_src, re.S)
    op_blocks = [b for b in blocks if 'rule="OP"' in b]
    assert op_blocks, "keine OP-Befunde gefunden -- Waechter angleichen"
    assert all("code=" in b for b in op_blocks), "OP-Befund ohne code= in operations.py"

    app = APP_JS.read_text(encoding="utf-8")
    kinds = set(re.findall(r'"kind": "(\w+)"', ops_src))
    whats = set(re.findall(r'"what": "(\w+)"', ops_src))
    kind_map = app[app.index("const OP_KIND_NAMES = {"): app.index("const OP_WRONG_KIND = {")]
    what_map = app[app.index("const OP_WRONG_KIND = {"): app.index("const FINDING_TEXTS = {")]
    assert not {k for k in kinds if f"{k}:" not in kind_map}, "Objektart ohne deutschen Namen"
    assert not {w for w in whats if f"{w}:" not in what_map}, "Knotenart ohne deutschen Text"


# ---------------------------------------------------------------------------
# Nachtest 2026-09-22: die vier Maengel, die den Alltag der Sachbearbeitung
# betreffen. Diese Waechter halten fest, woran sie lagen -- jeder von ihnen
# scheitert, wenn die Ursache zurueckkehrt.
# ---------------------------------------------------------------------------


def test_sample_read_shows_its_result_instead_of_closing_over_it() -> None:
    """Mangel 1: Der Server lieferte die Datensaetze, die Oberflaeche nicht.

    Das Ergebnis wurde in demselben Modal-Container geoeffnet, den ``openModal``
    unmittelbar danach leerte, weil der Rueckruf nicht ``false`` zurueckgab. Das
    Ergebnis bleibt deshalb jetzt IM Dialog, und der Rueckruf haelt ihn offen.
    """

    src = APP_JS.read_text(encoding="utf-8")
    body = _function_body(src, "function sampleReadConnector(")
    assert "renderSampleRecords(out" in body
    assert "showSampleRecords" not in src  # kein zweiter Dialog mehr
    # Der Rueckruf endet auf `return false` -- nur so bleibt der Dialog stehen.
    # (Das fruehe `return false` der leeren Eingabe allein genuegt nicht.)
    assert re.search(r"\n\s*return false;[^\n]*\n\s*\}, \"Lesen\"\);", body), body[-400:]
    assert "openModal(" not in _function_body(src, "function renderSampleRecords(")
    # und die Tabelle muss nicht mehr geraten werden
    assert "wireEntitySuggestions(entity" in body
    assert "/entities`" in _function_body(src, "async function fillEntitySuggestions(")


def test_unstaffed_ready_step_is_named_as_such() -> None:
    """Mangel 2: Nach einem Aufsichtseingriff fand die Vier-Augen-Regel
    niemanden -- die Sicht sah aber aus wie ein Schritt ohne Regel."""

    src = APP_JS.read_text(encoding="utf-8")
    detail = _function_body(src, "async function renderInstanceDetail(")
    assert "unstaffedTag(runSchema, nid)" in detail
    tag = _function_body(src, "function unstaffedTag(")
    assert "niemand zust" in tag and "staff_rules" in tag
    assert "ruleIsRelative(rule)" in tag  # nennt den haeufigsten Grund
    action = _function_body(src, "function completionActionFor(")
    assert "ruled && !staffed" in action
    # Und der Eingriff warnt vorher, wenn er genau diese Luecke reissen wuerde.
    ask = _function_body(src, "function askSupervisionReason(")
    assert "ruleRefersToPerformerOf(rule, nodeId)" in ask
    assert "warn-banner" in ask


def test_personal_worklist_does_not_depend_on_the_selected_process() -> None:
    """Mangel 3: „Meine Aufgaben" haengt nicht am oben gewaehlten Prozess.

    Die Liste reicht ueber alle Prozesse; sie darf weder an dessen
    Organisationsmodell scheitern noch Namen daraus aufloesen."""

    src = APP_JS.read_text(encoding="utf-8")
    tasks = _function_body(src, "async function viewTasks(")
    assert "loadAgentDirectory()" in tasks
    assert "state.agentDirectory" in tasks
    # kein Abbruch mehr wegen des gewaehlten Schemas
    assert "Kein Schema ausgew" not in tasks
    assert "Lege zuerst Agenten in der Ressourcensicht an." in tasks  # nur fuer Modellierer
    assert "hasRole(\"modeler\", \"admin\")" in tasks
    name = _function_body(src, "function agentNameOf(")
    assert "state.agentDirectory[id]" in name
    loader = _function_body(src, "async function loadAgentDirectory(")
    assert "/directory/agents" in loader
