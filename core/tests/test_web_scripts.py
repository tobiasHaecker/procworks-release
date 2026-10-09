# SPDX-License-Identifier: BUSL-1.1
"""Waechter ueber die Ladereihenfolge der Client-Skripte in ``web/index.html``.

Der Web-Client ist No-Build: Was ``index.html`` nicht per ``<script src=...>``
laedt, existiert im Browser nicht, und was es laedt, aber fehlt, endet in
einem 404 samt fehlender Deklarationen. ``app.js`` muss das letzte eigene
Skript sein, weil seine Startzeilen (``wireNav(); watchInert(); boot();``)
sofort laufen und dann alle Deklarationen brauchen. Danach kommen nur die
Tour-Skripte (``tour/*``), die auf dem Client aufbauen.

Teilskripte des Clients liegen unter ``web/js/``. Jede Datei dort muss geladen
werden -- sonst sieht kein Web-Waechter ihren Inhalt (sie lesen den Client
ueber ``web_source``, also ueber dieselbe Liste wie der Browser) und der
Browser kennt ihre Funktionen nicht.

Die Pruefungen sind reine Funktionen ueber Listen, damit die Gegenproben
unten zeigen, dass ein Fund die betroffene Datei beim Namen nennt.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import web_source
from web_source import (
    APP_ENTRY,
    WEB_DIR,
    app_js_source,
    app_scripts,
    line_label,
    locate_line,
    script_sources,
)

#: Ordner der Teilskripte des Clients (darf fehlen, solange es keine gibt).
JS_DIR = WEB_DIR / "js"

#: Praefix der Tour-Skripte; sie duerfen (und muessen) nach ``app.js`` laden.
TOUR_PREFIX = "tour/"


def unloaded_scripts(js_files: list[str], loaded: list[str]) -> list[str]:
    """Teilskripte, die ``index.html`` nicht laedt.

    :param js_files: Pfade relativ zu ``web/`` (z. B. ``"js/api.js"``) aller
        Dateien unter ``web/js/``.
    :param loaded: die ``src``-Angaben aus ``index.html``.
    :returns: die nicht geladenen Pfade, sortiert -- leer, wenn alles stimmt.
    """
    return sorted(set(js_files) - set(loaded))


def missing_scripts(sources: list[str], web_dir: Path) -> list[str]:
    """Von ``index.html`` geladene Skripte, die es als Datei nicht gibt.

    :param sources: die ``src``-Angaben aus ``index.html``.
    :param web_dir: der Ordner, relativ zu dem sie aufgeloest werden.
    :returns: die fehlenden Pfade in Ladereihenfolge -- leer, wenn alles stimmt.
    """
    return [src for src in sources if not (web_dir / src).is_file()]


def misplaced_scripts(sources: list[str]) -> list[str]:
    """Verstoesse gegen „``app.js`` ist das letzte eigene Skript“.

    :param sources: die ``src``-Angaben aus ``index.html``.
    :returns: je Verstoss ein Text mit dem Dateinamen: ein fehlendes oder
        doppeltes ``app.js`` bzw. jedes eigene (Nicht-Tour-)Skript, das erst
        nach ``app.js`` laedt -- dessen Deklarationen fehlten den Startzeilen.
        Leer, wenn alles stimmt.
    """
    count = sources.count(APP_ENTRY)
    if count != 1:
        return [f"{APP_ENTRY} wird {count}-mal geladen statt genau einmal"]
    after = sources[sources.index(APP_ENTRY) + 1 :]
    return [
        f"{src} laedt nach {APP_ENTRY}" for src in after if not src.startswith(TOUR_PREFIX)
    ]


# --- der echte Client --------------------------------------------------------


def test_every_client_script_under_web_js_is_loaded_by_index_html() -> None:
    js_files = (
        [f"js/{p.name}" for p in sorted(JS_DIR.glob("*.js"))] if JS_DIR.is_dir() else []
    )
    missing = unloaded_scripts(js_files, script_sources())
    assert not missing, (
        "web/index.html laedt diese Client-Skripte nicht (im Browser fehlen ihre "
        "Funktionen, kein Web-Waechter sieht sie): " + ", ".join(missing)
    )


def test_every_script_loaded_by_index_html_exists() -> None:
    missing = missing_scripts(script_sources(), WEB_DIR)
    assert not missing, "web/index.html laedt nicht vorhandene Skripte: " + ", ".join(missing)


def test_app_js_is_the_last_own_script() -> None:
    problems = misplaced_scripts(script_sources())
    assert not problems, (
        "app.js muss das letzte eigene Skript sein, seine Startzeilen brauchen "
        "alle Deklarationen: " + "; ".join(problems)
    )
    assert app_scripts()[-1] == WEB_DIR / APP_ENTRY


def test_client_source_is_the_scripts_in_load_order() -> None:
    """``app_js_source`` ist genau der Inhalt der Skripte, mit ``\\n`` verbunden."""
    expected = "\n".join(p.read_text(encoding="utf-8") for p in app_scripts())
    assert app_js_source() == expected
    assert "function boot(" in app_js_source()


# --- Gegenproben: ein Fund nennt die Datei -----------------------------------


def test_guard_names_an_unloaded_script() -> None:
    assert unloaded_scripts(["js/a.js", "js/b.js"], ["js/a.js", "app.js"]) == ["js/b.js"]


def test_guard_names_a_missing_script(tmp_path: Path) -> None:
    (tmp_path / "app.js").write_text("", encoding="utf-8")
    assert missing_scripts(["js/weg.js", "app.js"], tmp_path) == ["js/weg.js"]


def test_guard_names_a_script_loaded_after_app_js() -> None:
    sources = ["js/a.js", "app.js", "js/spaet.js", "tour/engine.js"]
    assert misplaced_scripts(sources) == ["js/spaet.js laedt nach app.js"]


def test_guard_rejects_a_missing_or_doubled_app_js() -> None:
    assert misplaced_scripts(["js/a.js"]) == ["app.js wird 0-mal geladen statt genau einmal"]
    assert misplaced_scripts(["app.js", "app.js"]) == [
        "app.js wird 2-mal geladen statt genau einmal"
    ]


# --- web_source an einem Miniatur-Client -------------------------------------


@pytest.fixture
def mini_web(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Ein Miniatur-``web/`` mit ``js/eins.js`` vor ``app.js`` und einem Tour-Skript.

    :returns: der Ordner; ``web_source`` liest fuer die Dauer des Tests von dort.
    """
    (tmp_path / "js").mkdir()
    (tmp_path / "js" / "eins.js").write_text("const a = 1;\nconst b = 2;\n", encoding="utf-8")
    (tmp_path / "app.js").write_text("function boot() {}\nboot();\n", encoding="utf-8")
    (tmp_path / "index.html").write_text(
        '<!-- <script src="js/alt.js"></script> -->\n'
        '<script src="js/eins.js"></script>\n'
        '<script src="app.js"></script>\n'
        '<script src="tour/engine.js"></script>\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(web_source, "WEB_DIR", tmp_path)
    monkeypatch.setattr(web_source, "INDEX_HTML", tmp_path / "index.html")
    return tmp_path


def test_app_scripts_follow_index_html_and_stop_at_app_js(mini_web: Path) -> None:
    # Das auskommentierte Skript zaehlt nicht, das Tour-Skript gehoert nicht dazu.
    assert web_source.app_scripts() == [mini_web / "js" / "eins.js", mini_web / "app.js"]
    assert web_source.app_js_source() == (
        "const a = 1;\nconst b = 2;\n\nfunction boot() {}\nboot();\n"
    )


def test_line_numbers_map_back_to_the_right_script(mini_web: Path) -> None:
    # eins.js belegt im verbundenen Text die Zeilen 1-3 (inkl. Verbindungszeile).
    assert web_source.locate_line(2) == (mini_web / "js" / "eins.js", 2)
    assert web_source.locate_line(4) == (mini_web / "app.js", 1)
    assert web_source.line_label(5) == "web/app.js:2"
    lines = web_source.app_js_source().split("\n")
    assert lines[4 - 1] == "function boot() {}"
    with pytest.raises(ValueError, match="Zeile 99 liegt ausserhalb"):
        web_source.locate_line(99)


def test_app_scripts_name_a_missing_file(mini_web: Path) -> None:
    (mini_web / "js" / "eins.js").unlink()
    with pytest.raises(FileNotFoundError, match=r"web/js/eins\.js"):
        web_source.app_scripts()


def test_app_scripts_require_app_js(mini_web: Path) -> None:
    (mini_web / "index.html").write_text('<script src="js/eins.js"></script>\n', encoding="utf-8")
    with pytest.raises(ValueError, match=r"laedt kein app\.js"):
        web_source.app_scripts()


def test_real_client_line_numbers_span_all_scripts() -> None:
    """Erste Zeile des Clients liegt im ersten Skript, letzte in ``app.js``."""
    last = app_js_source().count("\n") + 1
    assert locate_line(1) == (app_scripts()[0], 1)
    assert locate_line(last)[0] == WEB_DIR / APP_ENTRY
    assert line_label(last).startswith("web/app.js:")
