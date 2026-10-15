# SPDX-License-Identifier: BUSL-1.1
"""Die eigenen Skripte des Web-Clients, in der Ladereihenfolge des Browsers.

Der Web-Client ist No-Build: ``web/index.html`` laedt seine Skripte als
klassische ``<script src=...>``-Tags, und der Browser fuehrt sie in genau
dieser Reihenfolge aus. ``web/app.js`` ist dabei das **letzte** eigene Skript
-- es endet mit den Startzeilen (``wireNav(); watchInert(); boot();``), die
erst laufen duerfen, wenn alle Deklarationen geladen sind. Danach folgen nur
noch die Skripte der gefuehrten Tour (``tour/*``), die auf ``app.js`` aufbauen.

Die Web-Waechter lesen den Quelltext des Clients; dieses Modul ist ihre eine
Quelle dafuer. Die Reihenfolge kommt **aus** ``index.html`` statt aus einer
eigenen Liste, damit Tests und Auslieferung nicht auseinanderlaufen koennen:
Wird der Client auf mehrere Skripte verteilt, sehen alle Waechter automatisch
den ganzen Client.

Bewusst ohne Zwischenspeicher: Jeder Aufruf liest die Dateien neu, damit eine
Aenderung waehrend eines Laufs nie hinter einem alten Stand verschwindet.

Verwendet von den Testmodulen, die den Web-Client pruefen; flach importieren
(``from web_source import ...``).
"""

from __future__ import annotations

import re
from pathlib import Path

#: Ordner des Web-Clients (``web/`` neben ``core/``).
WEB_DIR = Path(__file__).resolve().parents[2] / "web"

#: Die Seite, deren ``<script>``-Tags die Ladereihenfolge festlegen.
INDEX_HTML = WEB_DIR / "index.html"

#: Das letzte eigene Skript; es traegt die Startzeilen des Clients.
APP_ENTRY = "app.js"

_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_SCRIPT_SRC_RE = re.compile(r"<script\b[^>]*?\bsrc\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)


def script_sources() -> list[str]:
    """Alle ``src``-Angaben der ``<script>``-Tags in ``index.html``, in Reihenfolge.

    :returns: die Pfade relativ zu ``web/`` genau so, wie sie im Tag stehen
        (z. B. ``["app.js", "tour/fixtures.js", ...]``) -- einschliesslich der
        Tour-Skripte.

    HTML-Kommentare werden vorher entfernt: Ein auskommentiertes Skript laedt
    der Browser nicht, also zaehlt es auch hier nicht. Inline-Skripte ohne
    ``src`` tauchen nicht auf.
    """
    html = _COMMENT_RE.sub("", INDEX_HTML.read_text(encoding="utf-8"))
    return _SCRIPT_SRC_RE.findall(html)


def app_scripts() -> list[Path]:
    """Die eigenen Skripte des Clients in Ladereihenfolge, bis einschliesslich ``app.js``.

    :returns: absolute Pfade; das letzte Element ist immer ``web/app.js``.
        Die Teilskripte unter ``web/js/`` stehen davor, in der Reihenfolge
        von ``index.html``.
    :raises ValueError: wenn ``index.html`` kein ``app.js`` laedt -- dann
        gibt es keinen Client, den ein Waechter pruefen koennte.
    :raises FileNotFoundError: wenn ein bis dahin geladenes Skript nicht
        existiert (der Browser bekaeme ein 404 und alle Deklarationen darin
        fehlten); die Meldung nennt die Datei.

    Die Tour-Skripte nach ``app.js`` gehoeren nicht dazu: Sie sind eigene
    Bausteine, und die Tests, die sie pruefen, lesen sie direkt.
    """
    sources = script_sources()
    if APP_ENTRY not in sources:
        raise ValueError(f"{INDEX_HTML} laedt kein {APP_ENTRY} -- Ladereihenfolge unbekannt")
    own = sources[: sources.index(APP_ENTRY) + 1]
    paths = [WEB_DIR / src for src in own]
    for src, path in zip(own, paths, strict=True):
        if not path.is_file():
            raise FileNotFoundError(f"index.html laedt web/{src}, die Datei fehlt")
    return paths


def app_js_source() -> str:
    """Der Quelltext aller eigenen Skripte, in Ladereihenfolge aneinandergehaengt.

    :returns: die Inhalte von :func:`app_scripts`, mit ``"\\n"`` verbunden.
        Solange der Client nur aus ``app.js`` besteht, ist das exakt dessen
        Inhalt.

    Fuer Quelltext-Waechter (Regex, Funktionsrumpf suchen), die nur fragen,
    *ob* etwas im Client steht. Wer eine Zeilennummer meldet, rechnet sie mit
    :func:`line_label` auf die richtige Datei um (oder prueft, wenn jede Datei
    fuer sich gelten muss, ueber :func:`app_scripts` einzeln); wer
    Verhalten prueft, nimmt ``web_vm.run_app_js`` (dort laeuft jedes Skript
    einzeln, wie im Browser).
    """
    return "\n".join(path.read_text(encoding="utf-8") for path in app_scripts())


def locate_line(line: int) -> tuple[Path, int]:
    """Ordnet eine Zeile von :func:`app_js_source` ihrem Skript zu.

    :param line: Zeilennummer (ab 1) im aneinandergehaengten Quelltext.
    :returns: ``(Skriptpfad, Zeile im Skript ab 1)``. Solange der Client nur
        aus ``app.js`` besteht, ist das ``(web/app.js, line)``.
    :raises ValueError: wenn ``line`` ausserhalb des Quelltexts liegt.

    Fuer Waechter, die den *ganzen* Client analysieren muessen (etwa weil eine
    Funktion in einem Skript definiert und in einem anderen aufgerufen wird),
    ihre Funde aber mit der richtigen Datei melden sollen. Jedes Skript belegt
    im verbundenen Text seine eigenen Zeilen plus die eine, die das
    Verbindungs-``"\\n"`` abschliesst; genau so wird hier zurueckgerechnet.
    """
    start = 1
    for path in app_scripts():
        span = path.read_text(encoding="utf-8").count("\n") + 1
        if start <= line < start + span:
            return path, line - start + 1
        start += span
    raise ValueError(f"Zeile {line} liegt ausserhalb des Client-Quelltexts")


def line_label(line: int) -> str:
    """Die Fundstelle einer Zeile von :func:`app_js_source` als ``web/<datei>:<zeile>``.

    :param line: Zeilennummer (ab 1) im aneinandergehaengten Quelltext.
    :returns: z. B. ``"web/app.js:1234"`` -- der Pfad relativ zum Repo, damit
        die Meldung direkt die zu oeffnende Datei nennt.
    :raises ValueError: wie :func:`locate_line`.
    """
    path, local = locate_line(line)
    return f"web/{path.relative_to(WEB_DIR).as_posix()}:{local}"
