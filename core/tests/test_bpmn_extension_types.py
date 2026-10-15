# SPDX-License-Identifier: BUSL-1.1
"""The ProcWorks extension of a BPMN import is checked like every other input.

A hand-edited ``procworks:model`` payload could make the import fail with a
500: a value of the wrong type (``"abc"``, ``1.5``, ``1e400`` for an integer
setting) raised a pydantic error nobody turned into an answer. And a negative
robustness value (``retry_backoff_ms`` -1, ``retry_max`` -3) was imported,
although the API refuses it.

Now:

* every type error in the extension is a 422 ``BPMN.bad-extension`` that names
  the part of the extension and the place inside it (``params.section``,
  ``params.location``) -- nothing is stored;
* negative robustness values are a finding of the validator (``I1.negative``,
  ``params.setting``/``value``), so they are refused on every way in -- the
  import, the change operations and a library caller alike.
"""

from __future__ import annotations

import itertools
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from staffing import staffed
from web_vm import needs_node, run_app_js

from procworks import (
    CorrectnessError,
    assign_service,
    create_empty_schema,
    serial_insert,
    set_automation,
    validate,
)
from procworks.api import app
from procworks.bpmn import BpmnError, export_bpmn, import_bpmn
from procworks.migration import RUNTIME_SAFE_OLD_FINDINGS
from procworks.model import AutomationKind, ProcessSchema

client = TestClient(app)
_ids = (f"bpmn-ext-{n}" for n in itertools.count(1))

#: Per setting a valid value no default uses -- the test swaps it for the
#: literal under test.
_MARK = {"retry_max": 77, "retry_backoff_ms": 77_777, "request_timeout_ms": 7_777}


def _automatic(**values: int) -> tuple[ProcessSchema, str]:
    schema = create_empty_schema("Erweiterung", schema_id=next(_ids))
    schema = serial_insert(schema, "Lauf", after_node_id="start")
    step = next(n.id for n in schema.nodes.values() if n.label == "Lauf")
    schema = assign_service(schema, step, "Lauf", automatic=True)
    schema = set_automation(schema, step, AutomationKind.EXTERNAL_TASK, topic="t", **values)
    return staffed(schema), step


def _xml_with(setting: str, literal: str) -> str:
    """Export of an automatic step whose ``setting`` is the raw JSON ``literal``."""

    schema, _ = _automatic(**{setting: _MARK[setting]})
    xml = export_bpmn(schema)
    pattern = rf'(&quot;|"){setting}(&quot;|"): {_MARK[setting]}'
    assert re.search(pattern, xml), "export format changed"
    return re.sub(pattern, lambda m: f"{m.group(1)}{setting}{m.group(2)}: {literal}", xml)


@pytest.mark.parametrize("literal", ["1e400", '"abc"', "1.5", "null", "[1]"])
def test_a_type_error_in_the_extension_is_a_bad_extension_with_its_place(literal: str) -> None:
    with pytest.raises(BpmnError) as exc:
        import_bpmn(_xml_with("retry_backoff_ms", literal))

    assert exc.value.code == "BPMN.bad-extension"
    assert exc.value.params["section"] == "service_bindings"
    assert exc.value.params["location"].endswith("retry_backoff_ms")


def test_the_api_answers_422_with_the_code_and_stores_nothing() -> None:
    count = len(client.get("/schemas").json())

    resp = client.post("/bpmn-import", json={"xml": _xml_with("retry_max", '"drei"')})

    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "BPMN.bad-extension"
    assert detail["params"]["section"] == "service_bindings"
    assert detail["params"]["location"].endswith("retry_max")
    assert len(client.get("/schemas").json()) == count


def test_other_sections_are_covered_too() -> None:
    """Grenzfall: the same protection for every part of the extension."""

    schema, _ = _automatic()
    xml = export_bpmn(schema)
    broken = xml.replace('{"service_bindings": ', '{"data_elements": [7], "service_bindings": ', 1)
    assert broken != xml

    with pytest.raises(BpmnError) as exc:
        import_bpmn(broken)

    assert (exc.value.code, exc.value.params["section"]) == ("BPMN.bad-extension", "data_elements")


@pytest.mark.parametrize(
    ("setting", "literal"),
    [("retry_backoff_ms", "-1"), ("retry_max", "-3"), ("request_timeout_ms", "-5")],
)
def test_a_negative_robustness_value_is_refused_by_the_validator(
    setting: str, literal: str
) -> None:
    with pytest.raises(CorrectnessError) as exc:
        import_bpmn(_xml_with(setting, literal))

    [finding] = exc.value.findings
    assert (finding.rule, finding.code) == ("I1", "I1.negative")
    assert finding.params["setting"] == setting
    assert finding.params["value"] == literal


def test_the_api_refuses_a_negative_import_with_the_finding() -> None:
    resp = client.post("/bpmn-import", json={"xml": _xml_with("retry_max", "-3")})

    assert resp.status_code == 422
    assert [f["code"] for f in resp.json()["detail"]["findings"]] == ["I1.negative"]


def test_a_library_caller_cannot_set_a_negative_value_either() -> None:
    schema, step = _automatic()
    with pytest.raises(CorrectnessError) as exc:
        set_automation(schema, step, AutomationKind.EXTERNAL_TASK, topic="t", retry_max=-1)
    assert [f.code for f in exc.value.findings] == ["I1.negative"]


def test_zero_stays_allowed() -> None:
    """Positiv: ``0`` means "no retry", "at once" and "the default" -- all fine."""

    schema, _ = _automatic(retry_max=0, retry_backoff_ms=0, request_timeout_ms=0)
    assert validate(schema) == []
    imported = import_bpmn(export_bpmn(schema))
    [binding] = imported.service_bindings.values()
    assert (binding.retry_max, binding.retry_backoff_ms, binding.request_timeout_ms) == (0, 0, 0)


def test_a_stored_negative_value_is_runtime_safe() -> None:
    """The runtime counts a negative value as 0 (or the default), so an old
    finding does not block a migration onto such a schema."""

    assert "I1.negative" in RUNTIME_SAFE_OLD_FINDINGS


@needs_node
def test_the_client_names_the_place_and_the_setting(tmp_path: Path) -> None:
    res = run_app_js(
        r"""
return [
  findingText({ code: "BPMN.bad-extension",
    params: { section: "service_bindings", location: "act_1.retry_max" } }),
  findingText({ code: "BPMN.bad-extension", params: {} }),
  findingText({ code: "I1.negative",
    params: { step: "Lauf", setting: "retry_max", value: "-3" } }),
];
""",
        tmp_path,
    )
    with_place, without, negative = res
    assert "service_bindings" in with_place and "act_1.retry_max" in with_place
    assert without == "Die ProcWorks-Zusatzdaten in der Datei sind beschädigt."
    assert "„Lauf“" in negative and "Wiederholungen" in negative and "-3" in negative
