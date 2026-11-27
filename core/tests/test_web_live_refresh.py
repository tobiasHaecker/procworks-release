# SPDX-License-Identifier: BUSL-1.1
"""Die automatische Aktualisierung verwirft keine Eingaben.

Laufzeit-Sichten zeichnen sich bei neuem Fortschritt und alle 30 s neu. Frueher
schuetzte sie nur ein *fokussiertes* Feld: Wer eine Abwesenheit halb eintrug
und kurz daneben klickte, fand das Formular leer und die Seite oben wieder; das
Ergebnis einer Simulation verschwand ganz ohne Zutun. Geprueft wird das
Verhalten von ``web/app.js`` in Node (``web_vm.py``).
"""

from __future__ import annotations

from pathlib import Path

from web_vm import needs_node, run_app_js

# Laufzeit-Sicht mit einem Formularfeld im Inhalt; der Fokus liegt NICHT darin.
_SETUP = r"""
state.principal = { subject: "tom.berger", roles: ["operator"] };
state.view = "tasks"; state.revision = 1;
byId("auth-overlay").style.display = "none";
const content = byId("content");
const note = el("input", { type: "text" });
content.appendChild(el("div", null, note));
let renders = 0;
globalThis.render = () => { renders += 1; };
respond((p) => p === "/monitoring/revision" ? { body: { revision: 2 } } : { status: 404 });
"""


@needs_node
def test_typed_but_unfocused_form_blocks_live_refresh_and_offers_reload(
    tmp_path: Path,
) -> None:
    """Eingabe ohne Fokus: kein Neuzeichnen, stattdessen Hinweis mit Knopf."""
    res = run_app_js(
        _SETUP
        + r"""
note.value = "Halbfertige Eingabe";
markContentDirty({ target: note });
document.activeElement = null;
const busy = userIsBusy();
await pollLiveUpdates();
return { busy, renders, value: note.value,
  hint: textOf(content.querySelector(".live-refresh-hint")) };
""",
        tmp_path,
    )
    assert res["busy"] is True
    assert res["renders"] == 0
    assert res["value"] == "Halbfertige Eingabe"
    assert "Neue Daten sind da." in res["hint"] and "Aktualisieren" in res["hint"]


@needs_node
def test_untouched_view_refreshes_as_before(tmp_path: Path) -> None:
    """Negativfall: Ohne Eingabe zeichnet die Aktualisierung wie bisher neu
    -- und ohne Hinweis."""
    res = run_app_js(
        _SETUP
        + r"""
document.activeElement = null;
await pollLiveUpdates();
return { busy: userIsBusy(), renders, hint: !!content.querySelector(".live-refresh-hint") };
""",
        tmp_path,
    )
    assert res == {"busy": False, "renders": 1, "hint": False}


@needs_node
def test_live_safe_fields_and_foreign_fields_do_not_block(tmp_path: Path) -> None:
    """Grenzfaelle: Felder mit ``data-live-safe`` (Wert liegt in state) und
    Felder ausserhalb des Inhalts (Seitenleiste) halten nichts auf."""
    res = run_app_js(
        _SETUP
        + r"""
const safe = el("input", { "data-live-safe": "1" });
content.appendChild(safe);
const sidebar = el("input");
document.body.appendChild(sidebar);
markContentDirty({ target: safe });
markContentDirty({ target: sidebar });
document.activeElement = null;
return userIsBusy();
""",
        tmp_path,
    )
    assert res is False


@needs_node
def test_redraw_clears_the_unsent_mark(tmp_path: Path) -> None:
    """Nach einem echten Neuzeichnen (Abschicken, Sichtwechsel) gilt die Sicht
    wieder als unberuehrt -- sonst stuende die Aktualisierung fuer immer still."""
    res = run_app_js(
        r"""
state.principal = { subject: "tom.berger", roles: ["operator"] };
state.view = "help";
byId("auth-overlay").style.display = "none";
const f = el("input"); byId("content").appendChild(f);
markContentDirty({ target: f });
const before = contentDirty;
VIEW_META.help.fn = async () => {};
render();
await new Promise((r) => setTimeout(r, 0));
return { before, after: contentDirty };
""",
        tmp_path,
    )
    assert res == {"before": True, "after": False}


@needs_node
def test_live_redraw_keeps_scroll_position(tmp_path: Path) -> None:
    """Live-Neuzeichnen bleibt an der Stelle; ein gewoehnliches beginnt oben."""
    res = run_app_js(
        r"""
const main = el("main", { class: "main" }); document.body.appendChild(main);
state.view = "help";
VIEW_META.help.fn = async () => { main.scrollTop = 0; };
main.scrollTop = 640;
render({ keepScroll: true });
await new Promise((r) => setTimeout(r, 0));
const kept = main.scrollTop;
main.scrollTop = 640;
render();
await new Promise((r) => setTimeout(r, 0));
return { kept, plain: main.scrollTop };
""",
        tmp_path,
    )
    assert res == {"kept": 640, "plain": 0}


@needs_node
def test_simulation_inputs_and_result_survive_redraw(tmp_path: Path) -> None:
    """Eingaben und Ergebnis der Simulation liegen in state und stehen nach
    einem Neuzeichnen des Panels wieder da."""
    res = run_app_js(
        r"""
const schema = { id: "s1", nodes: {}, edges: [], data_elements: {
  betrag: { id: "betrag", name: "Betrag", data_type: "INTEGER", source: "INSTANCE" } } };
state.schemaId = "s1";
globalThis.renderSimulationResult = (_s, sim) => el("div", null, "ERGEBNIS " + sim.marker);
respond((p, m) => (m === "POST" && p === "/schemas/s1/simulate")
  ? { body: { marker: "ende-erreicht", completed: true } } : { status: 404 });
const panel1 = simulationPanel(schema);
const input1 = panel1.querySelector("input");
input1.value = "250";
await input1.dispatch("input");
await panel1.querySelectorAll("button").find((b) => b.textContent === "Simulieren").click();
const panel2 = simulationPanel(schema);   // Neuzeichnen
return { value: panel2.querySelector("input").value, text: textOf(panel2),
  safe: panel2.querySelector("input").hasAttribute("data-live-safe") };
""",
        tmp_path,
    )
    assert res["value"] == "250"
    assert "ERGEBNIS ende-erreicht" in res["text"]
    assert res["safe"] is True


@needs_node
def test_simulation_state_does_not_leak_to_another_schema(tmp_path: Path) -> None:
    """Grenzfall: Ein anderes Schema beginnt mit leerer Simulation."""
    res = run_app_js(
        r"""
state.simulation = { key: "s1", values: { betrag: "250" }, result: { marker: "x" } };
globalThis.renderSimulationResult = () => el("div", null, "ERGEBNIS");
const panel = simulationPanel({ id: "s2", nodes: {}, edges: [], data_elements: {
  betrag: { id: "betrag", name: "Betrag", data_type: "INTEGER", source: "INSTANCE" } } });
return { value: panel.querySelector("input").value, text: textOf(panel) };
""",
        tmp_path,
    )
    assert res["value"] == ""
    assert "ERGEBNIS" not in res["text"]


@needs_node
def test_simulation_result_is_dropped_when_model_changed(tmp_path: Path) -> None:
    """Ein Ergebnis gehoert zu einem Modellstand: Nach einer Aenderung des
    Entwurfs (anderer Graph) wird es verworfen, die Eingaben bleiben."""
    res = run_app_js(
        r"""
const mk = (nodes) => ({ id: "s1", version: 1, nodes, edges: [], data_elements: {
  betrag: { id: "betrag", name: "Betrag", data_type: "INTEGER", source: "INSTANCE" } } });
globalThis.renderSimulationResult = () => el("div", null, "ERGEBNIS");
simulationPanel(mk({ a: { id: "a" } }));
state.simulation.values.betrag = "250";
state.simulation.result = { marker: "x" };
const same = textOf(simulationPanel(mk({ a: { id: "a" } })));
const changed = simulationPanel(mk({ a: { id: "a" }, b: { id: "b" } }));
return { same, changed: textOf(changed), value: changed.querySelector("input").value };
""",
        tmp_path,
    )
    assert "ERGEBNIS" in res["same"]
    assert "ERGEBNIS" not in res["changed"]
    assert res["value"] == "250"


@needs_node
def test_late_simulation_answer_lands_in_the_redrawn_panel(tmp_path: Path) -> None:
    """Zeichnet die Sicht waehrend der Anfrage neu, erscheint das Ergebnis im
    neuen Panel -- nicht im verworfenen."""
    res = run_app_js(
        r"""
const schema = { id: "s1", version: 1, nodes: {}, edges: [], data_elements: {} };
state.schemaId = "s1";
globalThis.renderSimulationResult = () => el("div", null, "ERGEBNIS");
let release;
respond(() => ({ body: { completed: true } }));
const origFetch = fetch;
globalThis.fetch = (...a) => new Promise((r) => { release = () => r(origFetch(...a)); });
const content = byId("content"); document.body.appendChild(content);
const p1 = simulationPanel(schema); content.appendChild(p1);
const pending = p1.querySelectorAll("button").find((b) => b.textContent === "Simulieren").click();
clear(content);
const p2 = simulationPanel(schema); content.appendChild(p2);   // Neuzeichnen
release(); await pending;
return { newPanel: textOf(p2), oldPanel: textOf(p1) };
""",
        tmp_path,
    )
    assert "ERGEBNIS" in res["newPanel"]


@needs_node
def test_pending_answer_for_an_old_model_is_ignored(tmp_path: Path) -> None:
    """Grenzfall: Aendert sich der Entwurf, waehrend eine Simulation laeuft,
    wird deren spaete Antwort verworfen."""
    res = run_app_js(
        r"""
const mk = (nodes) => ({ id: "s1", version: 1, nodes, edges: [], data_elements: {} });
state.schemaId = "s1";
globalThis.renderSimulationResult = () => el("div", null, "ERGEBNIS");
let release;
respond(() => ({ body: { completed: true } }));
const origFetch = fetch;
globalThis.fetch = (...a) => new Promise((r) => { release = () => r(origFetch(...a)); });
const content = byId("content"); document.body.appendChild(content);
const p1 = simulationPanel(mk({ a: {} })); content.appendChild(p1);
const pending = p1.querySelectorAll("button").find((b) => b.textContent === "Simulieren").click();
clear(content);
const p2 = simulationPanel(mk({ a: {}, b: {} })); content.appendChild(p2);
release(); await pending;
return { text: textOf(p2), stored: state.simulation.result };
""",
        tmp_path,
    )
    assert "ERGEBNIS" not in res["text"]
    assert res["stored"] is None
