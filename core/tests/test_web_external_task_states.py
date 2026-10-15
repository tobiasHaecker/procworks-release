# SPDX-License-Identifier: BUSL-1.1
"""Jeder Zustand einer externen Aufgabe hat in der Oberflaeche einen Namen.

Seit verworfene Aufgaben ``CANCELLED`` tragen (Entscheidung E.3.6, Ereignis
``task.cancelled``), kann die Oberflaeche diesen Zustand zeigen: in der
Meldung ``ET.not-locked`` (``params.state``) und als abonnierbares Ereignis.
Ein Zustand ohne Namen erschiene als technischer Bezeichner. Der Waechter
haelt die Liste des Clients am Aufzaehlungstyp des Kerns.
"""

from __future__ import annotations

import json
from pathlib import Path

from web_vm import needs_node, run_app_js

from procworks.model import ExternalTaskState


@needs_node
def test_every_external_task_state_has_a_client_name(tmp_path: Path) -> None:
    names = run_app_js("return EXTERNAL_TASK_STATE_NAMES;", tmp_path)
    missing = sorted(s.value for s in ExternalTaskState if not names.get(s.value))
    assert not missing, f"ohne Namen in EXTERNAL_TASK_STATE_NAMES: {missing}"


@needs_node
def test_a_refusal_on_a_cancelled_task_says_it_was_discarded(tmp_path: Path) -> None:
    text = run_app_js(
        "return findingText({ code: 'ET.not-locked', params: "
        + json.dumps({"state": "CANCELLED"}) + " }, { withHint: true });",
        tmp_path,
    )
    assert "verworfen" in text and "CANCELLED" not in text


@needs_node
def test_the_cancelled_event_has_a_label_and_can_be_subscribed(tmp_path: Path) -> None:
    res = run_app_js(
        "return { listed: WEBHOOK_EVENT_TYPES.includes('task.cancelled'),"
        " label: WEBHOOK_EVENT_LABELS['task.cancelled'] };",
        tmp_path,
    )
    assert res["listed"] is True
    assert res["label"] == "Externe Aufgabe verworfen"
