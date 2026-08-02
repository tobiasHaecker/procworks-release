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
