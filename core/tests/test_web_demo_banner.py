# SPDX-License-Identifier: BUSL-1.1
"""Der Demo-Hinweis bittet um fiktive Daten -- ausgeklappt wie eingeklappt.

Die oeffentliche Demo ist eine Wegwerf-Instanz auf fremdem Server. Besucher
sollen dort weder personenbezogene Daten eingeben noch ihre tatsaechlichen
Unternehmensprozesse nachbauen. Der Hinweis steht deshalb in der App selbst,
im Demo-Banner (``mountDemoBanner`` in ``web/app.js``), und nicht nur in der
Datenschutzerklaerung. Geprueft wird das Verhalten in Node (``web_vm.py``).
"""

from __future__ import annotations

from pathlib import Path

from web_source import app_js_source
from web_vm import needs_node, run_app_js

#: Eine angemeldete Modelliererin in der Demo, wie ``boot()`` sie vorfindet.
_DEMO_SESSION = r"""
state.demo = true;
state.principal = { subject: "mara.modell", display_name: "Mara Modell", roles: ["modeler"] };
state.demoPassword = "demo-procworks";
state.demoLogins = [
  { login: "mara.modell", name: "Mara Modell", role: "modeler" },
  { login: "erika.sander", name: "Erika Sander", role: "Sachbearbeiter" },
];
"""

#: Gibt Text, Hinweiszeile und Tooltip des gerade gemounteten Banners zurueck.
_SNAPSHOT = r"""
const b = byId("demo-banner");
const note = b && b.querySelector(".demo-privacy");
return {
  present: !!(b && b.isConnected !== false && document.body.children.includes(b)),
  text: b ? textOf(b) : "",
  note: note ? textOf(note) : null,
  title: b ? (b.getAttribute("title") || "") : "",
  label: b ? (b.getAttribute("aria-label") || "") : "",
  constant: DEMO_PRIVACY_NOTE,
};
"""


@needs_node
def test_privacy_note_names_personal_data_and_real_processes(tmp_path: Path) -> None:
    """Der Satz nennt beides, worum es geht: Personen und echte Prozesse.

    Gelesen wird der Wert zur Laufzeit, nicht per Textsuche im Quelltext --
    ein Semikolon im Satz schnitte ihn sonst still ab.
    """

    note = run_app_js("return DEMO_PRIVACY_NOTE;", tmp_path)
    assert "keine personenbezogenen Daten" in note
    assert "keine echten Unternehmensprozesse" in note
    assert "fiktive" in note


def test_banner_is_only_mounted_in_demo_mode() -> None:
    """Ausserhalb der Demo gibt es keinen Banner (und damit keinen Demo-Hinweis)."""

    assert "if (state.demo) mountDemoBanner();" in app_js_source()


@needs_node
def test_expanded_banner_shows_the_privacy_note(tmp_path: Path) -> None:
    """Beim ersten Besuch (nichts gespeichert) steht der Hinweis als eigene Zeile."""

    snap = run_app_js(
        _DEMO_SESSION
        + "localStorage.clear(); sessionStorage.clear(); mountDemoBanner();"
        + _SNAPSHOT,
        tmp_path,
    )
    assert snap["present"]
    assert snap["note"] == snap["constant"]
    assert "keine personenbezogenen Daten" in snap["text"]
    assert "keine echten Unternehmensprozesse" in snap["text"]


@needs_node
def test_collapsed_banner_keeps_the_note_in_its_tooltip(tmp_path: Path) -> None:
    """Eingeklappt bleibt nur ein Knopf -- sein Tooltip traegt den Hinweis weiter."""

    snap = run_app_js(
        _DEMO_SESSION
        + 'sessionStorage.clear(); localStorage.setItem("demoBannerCollapsed", "1");'
        + "mountDemoBanner();"
        + _SNAPSHOT,
        tmp_path,
    )
    assert snap["present"]
    assert snap["note"] is None                     # eingeklappt: keine volle Leiste
    assert snap["constant"] in snap["title"]
    assert snap["constant"] in snap["label"]        # Screenreader/Touch: kein Tooltip


@needs_node
def test_dismissed_banner_is_gone_for_the_session(tmp_path: Path) -> None:
    """Negativ: nach "Fuer diese Sitzung schliessen" kein Banner -- wegen des Sitzungsschalters.

    Gegenprobe im selben Lauf: Ohne den Schalter steht der Banner da; es ist
    also genau ``demoBannerDismissed``, das ihn unterdrueckt, nicht ein
    fehlender Demo-Zustand.
    """

    snap = run_app_js(
        _DEMO_SESSION
        + r"""
localStorage.clear(); sessionStorage.clear();
mountDemoBanner();
const before = !!byId("demo-banner").querySelector(".demo-privacy");
sessionStorage.setItem("demoBannerDismissed", "1");
mountDemoBanner();
const after = document.body.children.some(
  (c) => c.getAttribute && c.getAttribute("id") === "demo-banner");
return { before, after };
""",
        tmp_path,
    )
    assert snap == {"before": True, "after": False}
