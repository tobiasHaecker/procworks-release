# SPDX-License-Identifier: BUSL-1.1
"""Ein Doppelklick loest eine Aktion genau einmal aus.

Dreimal schnell „Instanz starten“ legte drei Vorgaenge an, zweimal
„Anlegen“ zwei Schemata -- jeder Klick schickte seinen eigenen Aufruf, weil
weder der Knopf noch der Bestaetigen-Knopf des Dialogs waehrend der laufenden
Aktion gesperrt war. Die Sperre sitzt jetzt an EINER Stelle: ``el`` sperrt
jeden Klick-Ausloeser, dessen Handler ein Promise liefert, bis es erledigt
ist (``guardAsyncClick``). Geprueft in Node (``web_vm.py``).
"""

from __future__ import annotations

from pathlib import Path

from web_vm import needs_node, run_app_js

# Ein Handler, der so lange laeuft, bis der Test ihn freigibt.
_GATE = r"""
let release;
const gate = () => new Promise((r) => { release = r; });
"""


@needs_node
def test_triple_click_on_start_instance_sends_one_request(tmp_path: Path) -> None:
    res = run_app_js(
        r"""
state.principal = { subject: "erika", roles: ["operator"] };
state.schema = { id: "s1", name: "Urlaub", version: 1, lifecycle_state: "RELEASED",
  nodes: {}, edges: [], data_elements: {} };
state.schemaId = "s1"; state.view = "run";
respond((p, m) => m === "POST" && p === "/schemas/s1/instances"
  ? { status: 201, body: { id: "instance_1", schema_id: "s1", is_test: false } }
  : { status: 404 });
await viewRun();
const btn = byId("content").querySelectorAll("button")
  .find((b) => textOf(b).includes("Instanz starten"));
const clicks = [btn.click(), btn.click(), btn.click()];
const busy = btn.hasAttribute("disabled");
await Promise.all(clicks);
return { posts: calls.filter((c) => c === "POST /schemas/s1/instances").length, busy };
""",
        tmp_path,
    )
    assert res["posts"] == 1
    assert res["busy"] is True


@needs_node
def test_double_click_on_a_dialog_confirm_runs_it_once(tmp_path: Path) -> None:
    res = run_app_js(
        _GATE
        + r"""
let runs = 0;
const { confirmBtn } = openModal("Neues Schema", el("input"),
  async () => { runs += 1; await gate(); }, "Anlegen");
const a = confirmBtn.click();
const b = confirmBtn.click();
const busy = confirmBtn.hasAttribute("disabled") && confirmBtn.getAttribute("aria-busy") === "true";
release();
await Promise.all([a, b]);
return { runs, busy, closed: byId("modal-root").children.length === 0 };
""",
        tmp_path,
    )
    assert res == {"runs": 1, "busy": True, "closed": True}


@needs_node
def test_enter_twice_confirms_once(tmp_path: Path) -> None:
    res = run_app_js(
        _GATE
        + r"""
let runs = 0;
const { modal } = openModal("Freigeben", el("p", null, "Sicher?"),
  async () => { runs += 1; await gate(); }, "Freigeben");
const k1 = modal.dispatch("keydown", { key: "Enter" });
const k2 = modal.dispatch("keydown", { key: "Enter" });
release();
await Promise.all([k1, k2]);
await new Promise((r) => setTimeout(r, 0));
return runs;
""",
        tmp_path,
    )
    assert res == 1


@needs_node
def test_after_a_failure_the_button_works_again_and_the_input_stays(tmp_path: Path) -> None:
    res = run_app_js(
        r"""
respond(() => ({ status: 422,
  body: { detail: { code: "REQ.nul-character", params: { where: "body" } } } }));
const input = el("input"); input.value = "Mein Schema";
let runs = 0;
const { confirmBtn } = openModal("Neues Schema", input, async () => {
  runs += 1;
  try { await api.post("/schemas", { name: input.value }); return true; }
  catch (err) { toastError(err); return false; }
}, "Anlegen");
await confirmBtn.click();
const after = { disabled: confirmBtn.hasAttribute("disabled"),
  busy: confirmBtn.getAttribute("aria-busy"), open: byId("modal-root").children.length };
await confirmBtn.click();
return { runs, after, value: input.value };
""",
        tmp_path,
    )
    assert res["runs"] == 2
    assert res["after"] == {"disabled": False, "busy": None, "open": 1}
    assert res["value"] == "Mein Schema"


@needs_node
def test_a_rejected_action_also_unlocks(tmp_path: Path) -> None:
    res = run_app_js(
        r"""
let runs = 0;
const btn = el("button", { onClick: async () => { runs += 1; throw new Error("boom"); } }, "Los");
try { await btn.click(); } catch (_e) { /* der Fehler gehoert dem Aufrufer */ }
try { await btn.click(); } catch (_e) { /* ebenso */ }
return { runs, disabled: btn.hasAttribute("disabled") };
""",
        tmp_path,
    )
    assert res == {"runs": 2, "disabled": False}


@needs_node
def test_synchronous_handlers_are_not_locked(tmp_path: Path) -> None:
    """Grenzfall: Ein Handler ohne Promise (Umschalter, Filter) bleibt
    beliebig oft klickbar."""

    res = run_app_js(
        r"""
let runs = 0;
const btn = el("button", { onClick: () => { runs += 1; } }, "Filter");
btn.click(); btn.click(); btn.click();
return { runs, disabled: btn.hasAttribute("disabled") };
""",
        tmp_path,
    )
    assert res == {"runs": 3, "disabled": False}
