# SPDX-License-Identifier: BUSL-1.1
"""The chosen instance survives a reload -- for the same login only.

After F5 the run view said "no instance loaded". The choice is now kept in
``sessionStorage`` together with the login. It must never reach another
person: signing out clears it, a different login (even one made in another
tab) does not restore it, and an instance the core no longer hands out ends
with the usual hint.
"""

from __future__ import annotations

from pathlib import Path

from web_vm import needs_node, run_app_js

_SETUP = r"""
const inst = { id: "i7", schema_id: "s", schema_version: 1, state: "RUNNING",
  node_states: {}, data_values: {} };
state.schemaId = "s";
globalThis.refreshSchema = async () => {}; globalThis.renderSchemaPicker = () => {};
let gone = false;
respond((p) => {
  if (p === "/instances/i7") {
    return gone ? { status: 404, body: { detail: "not found" } } : { body: inst };
  }
  if (p === "/instances/i7/worklist") return { body: [] };
  return { body: {} };
});
const reload = () => { state.instanceId = null; state.instance = null; state.worklist = null; };
"""


@needs_node
def test_same_login_gets_the_instance_back(tmp_path: Path) -> None:
    res = run_app_js(
        _SETUP
        + r"""
state.principal = { subject: "erika" };
await loadInstance("i7");
reload();
const restored = await restoreInstance();
return { restored, id: state.instanceId };
""",
        tmp_path,
    )
    assert res == {"restored": True, "id": "i7"}


@needs_node
def test_other_login_never_sees_it(tmp_path: Path) -> None:
    """Negativ: Grund ist der fremde Besitzer, nicht ein Ladefehler."""

    res = run_app_js(
        _SETUP
        + r"""
state.principal = { subject: "erika" };
await loadInstance("i7");
reload();
state.principal = { subject: "tom" };     // z. B. in einem anderen Tab angemeldet
const before = calls.length;
const restored = await restoreInstance();
return { restored, id: state.instanceId, fetched: calls.length - before,
  left: sessionStorage.getItem("selectedInstance") };
""",
        tmp_path,
    )
    assert res["restored"] is False and res["id"] is None
    assert res["fetched"] == 0          # nicht einmal angefragt
    assert res["left"] is None          # und verworfen


@needs_node
def test_sign_out_clears_the_remembered_instance(tmp_path: Path) -> None:
    res = run_app_js(
        _SETUP
        + r"""
state.principal = { subject: "erika" };
await loadInstance("i7");
resetSessionState();
return { left: sessionStorage.getItem("selectedInstance") };
""",
        tmp_path,
    )
    assert res["left"] is None


@needs_node
def test_instance_no_longer_readable_shows_the_hint(tmp_path: Path) -> None:
    res = run_app_js(
        _SETUP
        + r"""
state.principal = { subject: "erika" };
await loadInstance("i7");
reload();
gone = true;
const restored = await restoreInstance();
return { restored, id: state.instanceId, toasts: textOf(byId("toast-root")),
  left: sessionStorage.getItem("selectedInstance") };
""",
        tmp_path,
    )
    assert res["restored"] is False and res["id"] is None
    assert "Vorgang nicht mehr verfügbar" in res["toasts"]
    assert res["left"] is None


@needs_node
def test_failing_schema_reload_does_not_abort_the_start(tmp_path: Path) -> None:
    res = run_app_js(
        _SETUP
        + r"""
state.principal = { subject: "erika" };
await loadInstance("i7");
reload();
state.schemaId = "other";
const loaded = [];
globalThis.refreshSchema = async () => {
  loaded.push(state.schemaId);
  if (state.schemaId === "s") throw { status: 500 };   // Prozess des Vorgangs scheitert
};
let threw = false, restored = null;
try { restored = await restoreInstance(); } catch (_e) { threw = true; }
return { threw, restored, id: state.instanceId, schema: state.schemaId, loaded };
""",
        tmp_path,
    )
    assert res == {"threw": False, "restored": False, "id": None, "schema": "other",
                   "loaded": ["s", "other"]}   # vorigen Prozess vollstaendig neu geladen
