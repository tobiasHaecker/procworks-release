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
