# SPDX-License-Identifier: BUSL-1.1
"""Waechter fuer die gefuehrte Tour des Web-Clients (``docs/Tutorial-Konzept.md``).

Die Tour traegt **keine** Korrektheitslogik -- aber drei Zusagen, die still
brechen koennen, weil sie ueber die Grenze zwischen Web-Client und Kern laufen:

1. Jeder Anker, auf den ein Tour-Schritt zeigt, existiert wirklich im GUI.
2. Kein Schritt spielt in einer Sicht, die seine Rolle gar nicht sehen darf.
3. **Waehrend der Tour wird nichts geschrieben** -- die wichtigste Zusage
   ueberhaupt (keine dauerhaften Daten durch Tutorial-Eingaben).
4. Die abgespielte Aufzeichnung stimmt noch mit dem echten Kern ueberein.

Diese Tests lesen die Web-Dateien vom Dateisystem (wie der bestehende
Dockerfile-Waechter der Demo), weil die Suite in ``core/`` laeuft und es im
Projekt bewusst keinen JS-Build und keinen JS-Testlauf gibt.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

# Flach importiert, nicht als ``tests.tour_fixture_build``: ``tests/`` ist
# bewusst kein Paket (kein ``__init__.py``), pytest legt daher das
# Testverzeichnis selbst in den Suchpfad -- ein Paketname ``tests`` existiert
# nur, wenn zufaellig ``core/`` im Suchpfad liegt (lokal ja, in der CI nein).
from tour_fixture_build import build_payload

WEB = Path(__file__).resolve().parents[2] / "web"
TOUR = WEB / "tour"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def tours_js() -> str:
    return _read(TOUR / "tours.js")


@pytest.fixture(scope="module")
def app_js() -> str:
    return _read(WEB / "app.js")


@pytest.fixture(scope="module")
def index_html() -> str:
    return _read(WEB / "index.html")


def test_tour_anchors_exist(tours_js: str, app_js: str, index_html: str) -> None:
    """Jeder referenzierte ``data-tour``-Anker kommt im GUI auch vor.

    Die Sichten werden bei jedem Rendern neu aufgebaut; ein umbenanntes oder
    entferntes Bedienelement wuerde die Tour sonst still ins Leere zeigen
    lassen (der Nutzer saehe ein Popup in der Bildschirmmitte). Das faellt in
    keinem anderen Test auf -- deshalb hier.
    """

    # Nur echte anchor-Felder auswerten -- der Kopfkommentar der Datei nennt die
    # Konvention [data-tour="…"] ebenfalls und ist kein Anker.
    anchors = re.findall(r"^\s*anchor:\s*(.+?),\s*$", tours_js, re.M)
    referenced = {
        ref for line in anchors
        for ref in re.findall(r'\[data-tour="([^"]+)"\]', line)
    }
    assert referenced, "tours.js referenziert gar keinen Anker -- das kann nicht stimmen"
    defined = set(
        re.findall(r'data-tour="([^"]+)"', index_html)
        + re.findall(r'"data-tour":\s*"([^"]+)"', app_js)
        # Zusammengesetzte Anker (z. B. "model.tab." + id) als Praefix erfassen.
        + re.findall(r'"data-tour":\s*"([^"]+)"\s*\+', app_js)
    )
    missing = {
        ref for ref in referenced
        if ref not in defined and not any(d and ref.startswith(d) for d in defined)
    }
    assert not missing, f"Tour zeigt auf nicht (mehr) vorhandene Anker: {sorted(missing)}"


def test_tour_steps_respect_view_roles(tours_js: str, app_js: str) -> None:
    """Kein Schritt spielt in einer Sicht, die seine Rolle nicht sehen darf.

    ``VIEW_ROLES`` in ``app.js`` blendet Sichten je Rolle aus. Ein Schritt, der
    dorthin zeigt, koennte vom Zielpublikum nie erreicht werden.
    """

    block = re.search(r"const VIEW_ROLES = \{(.*?)\n\};", app_js, re.S)
    assert block, "VIEW_ROLES nicht in app.js gefunden -- Waechter angleichen"
    view_roles = {
        view: set(re.findall(r'"(\w+)"', roles))
        for view, roles in re.findall(r"(\w+):\s*\[([^\]]*)\]", block.group(1))
    }

    for tour_id, role, body in _iter_tours(tours_js):
        for step_id, view in re.findall(
            r'id:\s*"([^"]+)",\s*\n\s*view:\s*("(?:[^"]+)"|null)', body
        ):
            if view == "null":
                continue
            name = view.strip('"')
            assert name in view_roles, f"{tour_id}/{step_id}: unbekannte Sicht {name!r}"
            assert role in view_roles[name], (
                f"Tour {tour_id} (Rolle {role}) fuehrt in Sicht {name!r}, "
                f"die nur {sorted(view_roles[name])} sehen duerfen"
            )


def test_tour_writes_nothing(app_js: str) -> None:
    """Der Sperrpunkt sitzt VOR dem Netzwerkaufruf -- die zentrale Zusage.

    Statischer Nachweis, dass ``request()`` die Tour zuerst fragt und erst
    danach ``fetch`` erreicht. Rutscht die Abfrage hinter das ``fetch`` (oder
    verschwindet sie), wuerden Tutorial-Eingaben echte, dauerhafte Daten
    erzeugen -- genau das, was das Konzept ausschliesst.
    """

    body = re.search(r"async function request\(.*?\n\}", app_js, re.S)
    assert body, "request() nicht in app.js gefunden"
    src = body.group(0)
    guard = src.find("Tour.intercept")
    call = src.find("await fetch(")
    assert guard != -1, "Der Sandkasten-Sperrpunkt fehlt in request()"
    assert call != -1, "request() ruft kein fetch mehr auf -- Waechter angleichen"
    assert guard < call, "Der Sandkasten-Sperrpunkt steht HINTER dem fetch"


def test_tour_popup_keeps_its_buttons_reachable() -> None:
    """Das Popup passt immer ins Fenster -- sonst ist die Tour nicht beendbar.

    Ohne Hoehendeckel wuchs das Popup bei langem Text (oder niedrigem Fenster)
    ueber den unteren Rand hinaus, und genau dort sitzt die Fusszeile mit
    „Weiter"/„Ueberspringen". Die Platzierung in ``engine.js`` kann das nicht
    auffangen: Ist das Popup hoeher als das Fenster, hat ihre Klemmung
    ``min(top, innerHeight - h - pad)`` keinen gueltigen Wert mehr.

    Drei Zusagen, die zusammen wirken muessen -- faellt eine weg, ist der
    Schritt wieder eine Sackgasse.
    """

    css = _read(TOUR / "tour.css")
    engine = _read(TOUR / "engine.js")

    pop = re.search(r"\.tour-pop \{(.*?)\}", css, re.S)
    assert pop, ".tour-pop nicht in tour.css gefunden -- Waechter angleichen"
    rules = pop.group(1)
    assert "max-height" in rules, "Das Popup hat keinen Hoehendeckel"
    assert "dvh" in rules, "Hoehendeckel ohne dvh -- auf iOS liegt das Ende hinter der Leiste"
    assert "flex-direction: column" in rules, "Ohne Spaltenfluss rollt der Textteil nicht"

    # Der veraenderliche Teil rollt, Kopf und Fusszeile nicht.
    body_rule = re.search(r"\.tour-pop-b \{(.*?)\}", css, re.S)
    assert body_rule and "overflow-y: auto" in body_rule.group(1), (
        "Der Textteil des Popups rollt nicht"
    )
    assert "min-height: 0" in body_rule.group(1), (
        "Ohne min-height:0 schrumpft der Flex-Bereich nicht -- der Deckel wirkt nicht"
    )
    assert re.search(r"\.tour-pop-h,\s*\n\.tour-foot \{ flex: none", css), (
        "Kopf/Fusszeile duerfen nicht mitschrumpfen, sonst verschwinden die Knoepfe"
    )

    # ... und das Markup liefert diesen Behaelter ueberhaupt.
    assert 'class: "tour-pop-b"' in engine, (
        "popup() umschliesst Text/Hinweis nicht mit .tour-pop-b -- dann rollt das "
        "ganze Popup inklusive Fusszeile aus dem Bild"
    )


def test_tour_popup_keeps_clear_of_the_demo_banner() -> None:
    """Der Demo-Banner darf das Popup nicht verdecken.

    ``#demo-banner`` klebt am unteren Rand und traegt denselben ``z-index`` wie
    das Tour-Overlay -- weil er spaeter ins DOM kommt, malt er darueber. Genau
    dort sass die Fusszeile mit „Weiter", der Schritt war nicht abschliessbar.

    Geloest wird das nicht ueber die Stapelreihenfolge (dann verdeckte das Popup
    die Rollen-Umschaltung), sondern indem der Platz freigehalten wird: engine.js
    misst den Banner und reicht die Hoehe als ``--tour-bottom-inset`` weiter.
    Alle drei Platzierungsarten muessen sie beruecksichtigen.
    """

    css = _read(TOUR / "tour.css")
    engine = _read(TOUR / "engine.js")

    assert "function bottomInset()" in engine, "Die Banner-Messung fehlt"
    assert '"demo-banner"' in engine, "bottomInset() misst nicht den Demo-Banner"
    assert "--tour-bottom-inset" in engine, "Die gemessene Hoehe erreicht das Stylesheet nicht"
    assert re.search(r"usableBottom\s*=\s*window\.innerHeight\s*-\s*inset", engine), (
        "position() rechnet den belegten Rand nicht heraus"
    )

    # Angeheftet, mittig und mobil -- jede Variante muss den Rand freihalten.
    for rule, why in [
        (r"\.tour-pop \{[^}]*?--tour-bottom-inset", "der Hoehendeckel"),
        (r"\.tour-pop\.centered \{[^}]*?--tour-bottom-inset", "das mittige Popup"),
        (r"bottom: calc\(8px \+ var\(--tour-bottom-inset", "das mobile Bottom-Sheet"),
    ]:
        assert re.search(rule, css, re.S), f"{why} beruecksichtigt den Demo-Banner nicht"


def test_tour_insert_label_matches_fixture(tours_js: str) -> None:
    """``TOUR_NEW_STEP_LABEL`` und die Beschriftung in der Konserve sind gleich.

    Eine stille Kopplung ueber zwei Sprachen hinweg: ``engine.js`` sucht den
    aufgezeichneten Knoten **ueber seine Beschriftung**, um ihn auf den vom
    Nutzer getippten Text umzubenennen (``applyLabel``). Weichen die beiden
    Konstanten voneinander ab, findet die Engine nichts -- der Nutzer tippt eine
    Bezeichnung ein und der Knoten heisst danach weiter wie in der Konserve.
    Kein Fehler, keine Meldung, nur ein verwirrender Widerspruch.
    """

    from tour_fixture_build import LABEL_RESTURLAUB

    found = re.search(r"const TOUR_NEW_STEP_LABEL = \"([^\"]+)\"", tours_js)
    assert found, "TOUR_NEW_STEP_LABEL nicht in tours.js gefunden"
    assert found.group(1) == LABEL_RESTURLAUB, (
        f"tours.js sagt „{found.group(1)}\", die Konserve „{LABEL_RESTURLAUB}\" -- "
        "die Umbenennung des eingefuegten Schritts greift dann nicht mehr"
    )


def test_simulate_steps_reach_every_element_they_ask_for(tours_js: str) -> None:
    """Ein „simulate"-Schritt muss jede Stelle freilassen, zu der er auffordert.

    Bei ``action: "simulate"`` blockt der Scrim **alles ausserhalb der
    Aussparung**, und die Aussparung kennt nur den einen ``anchor``. Fordert der
    Hinweistext zusaetzlich dazu auf, im Kontrollfluss einen Schritt zu waehlen
    („waehle links den neuen Schritt"), liegt der Graph unter dem Scrim -- der
    Klick kommt nie an, und der Schritt laesst sich nicht abschliessen. Genau so
    war der Ablehnungs-Schritt der Modellierer-Tour blockiert.

    Der Ausweg ist ``also``: weitere Bereiche, die bedienbar bleiben. Dieser
    Waechter prueft, dass jeder simulate-Schritt, dessen Hinweis auf die Auswahl
    im Graph zeigt, den Graph auch tatsaechlich freilaesst.
    """

    steps = re.findall(r"\{\s*id: \"[^\"]+\".*?\n      \}", tours_js, re.S)
    assert steps, "Keine Schritte in tours.js gefunden -- Waechter angleichen"

    checked = 0
    for step in steps:
        if 'action: "simulate"' not in step:
            continue
        hint = re.search(r"hint: \"([^\"]*)\"", step)
        if not hint:
            continue
        # Formulierungen, die eine Auswahl im Kontrollfluss verlangen.
        wants_graph = any(
            phrase in hint.group(1).lower()
            for phrase in ("links den", "im graph", "waehle den schritt", "wähle den schritt")
        )
        # ... oder ein Bindungsziel, das ohne gewaehlten Knoten gar nicht existiert.
        wants_graph = wants_graph or "an den neuen schritt" in hint.group(1).lower()
        if not wants_graph:
            continue
        checked += 1
        step_id = re.search(r"id: \"([^\"]+)\"", step)
        assert "model.graph" in step, (
            f"Schritt „{step_id.group(1) if step_id else '?'}\" fordert zur Auswahl im "
            "Kontrollfluss auf, laesst ihn aber nicht frei (also: "
            "['[data-tour=\"model.graph\"]'] fehlt) -- der Scrim schluckt den Klick"
        )
    assert checked, "Kein passender simulate-Schritt gefunden -- Waechter angleichen"


def test_tour_keyboard_defers_to_input_fields() -> None:
    """Die Tour nimmt einem fokussierten Eingabefeld die Tasten nicht weg.

    ``onKey`` laeuft in der Capture-Phase und verbraucht Esc sowie die
    Pfeiltasten, bevor das fokussierte Element sie sieht. Genau diese Tasten
    bedienen ein ``<input type="date">``: die Pfeile wechseln zwischen Tag,
    Monat und Jahr, Esc schliesst den Kalender. Ohne die Ausnahme liess sich
    das Abwesenheits-Datum bei laufender Tour nicht auswaehlen -- jeder
    Pfeiltastendruck blaetterte stattdessen die Tour weiter.

    Geprueft wird die Reihenfolge: die Feld-Ausnahme muss VOR dem ersten
    ``stopPropagation`` stehen, sonst ist die Taste schon verbraucht.
    """

    src = _read(TOUR / "engine.js")
    body = re.search(r"function onKey\(.*?\n  \}", src, re.S)
    assert body, "onKey() nicht in engine.js gefunden"
    # Kommentare raus: ein blosser Hinweis auf typingInField im Fliesstext
    # wuerde den Waechter sonst zufrieden stellen, obwohl der Aufruf fehlt.
    handler = re.sub(r"//[^\n]*", "", body.group(0))
    guard = handler.find("typingInField(")
    consumed = handler.find("stopPropagation")
    assert guard != -1, "Die Ausnahme fuer fokussierte Eingabefelder fehlt in onKey()"
    assert consumed != -1, "onKey() verbraucht keine Taste mehr -- Waechter angleichen"
    assert guard < consumed, "Die Feld-Ausnahme steht HINTER dem Tastenverbrauch"

    # Die Ausnahme muss alle Eingabearten abdecken, nicht nur das Datumsfeld.
    checker = re.search(r"function typingInField\(.*?\n  \}", src, re.S)
    assert checker, "typingInField() nicht gefunden"
    for tag in ("INPUT", "SELECT", "TEXTAREA", "isContentEditable"):
        assert tag in checker.group(0), f"typingInField() beruecksichtigt {tag} nicht"


def test_tour_files_never_call_the_api_directly() -> None:
    """Die Tour-Dateien umgehen ``request()`` nicht.

    Ein direkter ``api.post``-Aufruf aus der Tour heraus liefe am Sperrpunkt
    vorbei und koennte doch etwas speichern.
    """

    for name in ("engine.js", "tours.js", "fixtures.js"):
        src = _read(TOUR / name)
        assert not re.search(r"\bapi\.(post|put|patch|del)\b", src), (
            f"{name} ruft die API schreibend auf -- das umgeht den Sperrpunkt"
        )
        assert "fetch(" not in src, f"{name} ruft fetch direkt auf"


def test_tour_fixtures_match_core() -> None:
    """Die abgespielte Aufzeichnung entspricht noch dem, was der Kern liefert.

    Die Tour zeigt Schema, Befunde und die vorgefuehrte Ablehnung aus einer
    Konserve. Aendert sich der Kern (Modellfelder, Regeltexte, Validierung),
    wuerde die Tour etwas vorfuehren, was so nicht mehr passiert. Dann faellt
    dieser Test -- neu erzeugen mit::

        cd core && PYTHONPATH=src python -m tests.tour_fixture_build
    """

    src = _read(TOUR / "fixtures.js")
    match = re.search(r"const TOUR_FIXTURES = (\{.*\});\s*$", src, re.S)
    assert match, "fixtures.js hat nicht die erwartete Form (erzeugt?)"
    stored = json.loads(match.group(1))
    assert stored == build_payload(), (
        "Die Tour-Aufzeichnung weicht vom Kern ab. Neu erzeugen: "
        "cd core && PYTHONPATH=src python -m tests.tour_fixture_build"
    )


def test_tour_rejection_is_a_real_core_rejection() -> None:
    """Die vorgefuehrte Ablehnung stammt wirklich vom Validator.

    Die Pointe der Modellierer-Tour ist, dass der Kern eine Lesebindung vor dem
    Schreiber zurueckweist. ``build_rejection`` faehrt genau das nach und wirft,
    falls der Kern die Operation eines Tages annimmt -- dann waere die
    Erzaehlung erfunden statt vorgefuehrt.
    """

    findings = build_payload()["rejection"]
    assert findings, "Es gibt keine Ablehnung -- die Tour wuerde ins Leere laufen"
    assert any(f["rule"] == "D1" for f in findings), (
        f"Erwartet wurde eine D1-Ablehnung, bekommen: {[f['rule'] for f in findings]}"
    )


def _iter_tours(tours_js: str) -> list[tuple[str, str, str]]:
    """Zerlegt ``tours.js`` in ``(tour_id, role, rumpf)``.

    Bewusst per regulaerem Ausdruck statt mit einem JS-Parser: Die Datei ist
    reine Datenauszeichnung in fester Form, und der Waechter soll ohne
    JS-Werkzeugkette in der Python-Suite laufen.

    :param tours_js: Inhalt von ``web/tour/tours.js``.
    :return: Ein Eintrag je Tour.
    """

    heads = list(re.finditer(r'\n    id:\s*"([^"]+)",\n    role:\s*"([^"]+)"', tours_js))
    out: list[tuple[str, str, str]] = []
    for i, head in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(tours_js)
        out.append((head.group(1), head.group(2), tours_js[head.end():end]))
    assert out, "In tours.js wurde keine Tour gefunden -- Waechter angleichen"
    return out
