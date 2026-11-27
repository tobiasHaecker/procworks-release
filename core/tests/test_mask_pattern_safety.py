# SPDX-License-Identifier: BUSL-1.1
"""A mask pattern can never make a completion run for minutes.

Python's ``re`` backtracks. A pattern such as ``(a+)+$`` against ``"a" * n +
"!"`` takes time doubling with every character -- and so does a plain chain
``(a|aa)(a|aa)…`` without any quantifier. Instead of listing dangerous
shapes, U2 bounds the work itself: :func:`pattern_step_bound` counts, on the
tree of Python's own parser, how many steps a backtracking match can take for
a value of ``MAX_PATTERN_VALUE_LENGTH`` (300) characters, and only patterns
within ``MAX_PATTERN_STEPS`` are admitted (``U2.pattern-too-complex``
otherwise). Further limits: at most ``MAX_PATTERN_LENGTH`` (200) characters
and no back-references.

At runtime a value is matched against a pattern only up to
``MAX_PATTERN_VALUE_LENGTH`` characters, the length the bound assumes; a
pattern stored before the rule existed and failing it is not run at all
(logged).
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from planted import plant
from staffing import staff_via_api
from web_vm import needs_node, run_app_js

from procworks import api as api_module
from procworks.validator import (
    MAX_PATTERN_LENGTH,
    MAX_PATTERN_STEPS,
    MAX_PATTERN_VALUE_LENGTH,
    pattern_safety_problem,
    pattern_step_bound,
)

client = TestClient(api_module.app)


# --- the structural check ---------------------------------------------------


_KELVIN = "\u212a"  # case-insensitively the same letter as "k"


@pytest.mark.parametrize(
    ("pattern", "code"),
    [
        # a repetition of a repetition
        ("(a+)+$", "U2.pattern-too-complex"),
        ("(.*a){20}", "U2.pattern-too-complex"),
        ("(a*)*", "U2.pattern-too-complex"),
        ("(x?y?)+", "U2.pattern-too-complex"),
        ("(\\w+\\s?)*$", "U2.pattern-too-complex"),
        # overlapping alternatives under a repetition
        ("(a|a)*$", "U2.pattern-too-complex"),
        ("(a|ab)*c", "U2.pattern-too-complex"),
        ("(foo|f.o)+", "U2.pattern-too-complex"),
        ("(?:a|aa)+$", "U2.pattern-too-complex"),
        ("(?i:ab|AB)+", "U2.pattern-too-complex"),
        # overlapping alternatives without any quantifier: exponential as well
        ("(a|aa)" * 24, "U2.pattern-too-complex"),
        ("(a|aa)" * 28, "U2.pattern-too-complex"),
        ("(a|aa)" * 33, "U2.pattern-too-complex"),
        ("(?:a|aa)" * 20, "U2.pattern-too-complex"),
        # polynomial with a high exponent
        ("\\d*\\d*\\d*\\d*x", "U2.pattern-too-complex"),
        ("\\d*\\d*\\d*\\d?\\d?\\d?x", "U2.pattern-too-complex"),
        ("[A-Za-z]+-?[A-Za-z]+-?[A-Za-z]+", "U2.pattern-too-complex"),
        # case folding joins characters of different classes
        (f"(?:(?i:k)a|{_KELVIN}a)+", "U2.pattern-too-complex"),
        (f"(?i)(?:ka|{_KELVIN}a)+", "U2.pattern-too-complex"),
        ("(a)\\1", "U2.pattern-backreference"),
        ("(a)?(?(1)b|c)", "U2.pattern-backreference"),
        ("([", "U2.pattern-invalid"),
        ("a" * (MAX_PATTERN_LENGTH + 1), "U2.pattern-too-long"),
    ],
)
def test_dangerous_patterns_are_named_by_their_reason(pattern: str, code: str) -> None:
    assert pattern_safety_problem(pattern) == code


@pytest.mark.parametrize(
    "pattern",
    [
        "^[A-Z]{2}-\\d{4}$",
        "^[A-Z]+$",
        "[A-Z]{2}-\\d{4}",
        "https?://\\S+",
        "([A-Z]{2}-)+",
        "(ab|cd)+",
        "(?:x|y)+",
        "[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\\.[A-Za-z]{2,}",
        "\\+?[0-9 ]+(-[0-9]+)?",
        "DE\\d{2}[0-9 ]{18,30}",
        "a" * MAX_PATTERN_LENGTH,
        # repetitions inside repetitions that cannot overlap
        "^[\\w.+-]+@[\\w-]+(\\.[\\w-]+)+$",
        "^[A-Z][a-z]+( [A-Z][a-z]+)*$",
        "^\\d{1,3}(\\.\\d{3})*(,\\d{2})?$",
        # case-insensitive and lookahead patterns
        "(?i)[a-z]+@[a-z]+\\.[a-z]{2,}",
        "^(?=.*\\d)(?=.*[a-z]).{8,}$",
        "^.*foo.*$",
    ],
)
def test_usual_patterns_stay_allowed(pattern: str) -> None:
    assert pattern_safety_problem(pattern) is None


# --- modelling: U2 refuses the pattern, with its code -------------------------


def _schema(name: str) -> tuple[str, str]:
    sid = client.post("/schemas", json={"name": name}).json()["id"]
    schema = client.post(
        f"/schemas/{sid}/serial-insert", json={"label": "Erfassen", "after_node_id": "start"}
    ).json()
    node = next(n["id"] for n in schema["nodes"].values() if n["label"] == "Erfassen")
    client.post(
        f"/schemas/{sid}/data-elements",
        json={"name": "Kennung", "data_type": "STRING", "element_id": "s"},
    )
    return sid, node


def _form(sid: str, node: str, pattern: str) -> Any:
    return client.post(
        f"/schemas/{sid}/nodes/{node}/form",
        json={"fields": [{"element_id": "s", "widget": "TEXT", "pattern": pattern}]},
    )


@pytest.mark.parametrize(
    ("pattern", "code"),
    [
        ("(a+)+$", "U2.pattern-too-complex"),
        ("(a|a)*$", "U2.pattern-too-complex"),
        ("(.*a){20}", "U2.pattern-too-complex"),
        ("(a|aa)" * 28, "U2.pattern-too-complex"),
        ("x" * 201, "U2.pattern-too-long"),
    ],
)
def test_setting_a_dangerous_pattern_is_refused(pattern: str, code: str) -> None:
    sid, node = _schema(f"Muster {code}")
    resp = _form(sid, node, pattern)
    assert resp.status_code == 422
    assert [f["code"] for f in resp.json()["detail"]["findings"]] == [code]
    assert client.get(f"/schemas/{sid}").json()["forms"] == {}


# --- runtime: bounded work per value ------------------------------------------


def _running(name: str, pattern: str) -> tuple[str, str]:
    sid, node = _schema(name)
    assert _form(sid, node, pattern).status_code == 200
    staff_via_api(client, sid)
    assert client.post(f"/schemas/{sid}/release").status_code == 200
    return client.post(f"/schemas/{sid}/instances").json()["id"], node


#: The most expensive pattern of each family that U2 still admits. Each of
#: them sits at the step bound: one more part and it is refused.
_EDGE_PATTERNS = [
    "(?:a|aa)" * 19,
    "(a|aa)" * 17,
    "(a)" * 40 + "((a)|(aa)){0,15}",
    "(a)?" * 15 + "a" * 15,
    "(?:a|aa){0,17}",
    "a*a*a{0,1}",
]
_WORST_ALLOWED = _EDGE_PATTERNS[0]


def _hard_values() -> list[str]:
    """Values of exactly the cap that make a backtracking matcher work hardest
    on the edge patterns: long runs that fail only at the last character."""

    n = MAX_PATTERN_VALUE_LENGTH
    return [
        "a" * (n - 1) + "!",
        "a" * n,
        "a" * (n - 2) + "!!",
        "ab" * (n // 2 - 1) + "a!",
    ]


def test_the_edge_patterns_sit_exactly_at_the_bound() -> None:
    """Structural half of the time guarantee, independent of the machine:
    every edge pattern is admitted and within the bound, one part more is
    not -- so no admitted pattern needs more steps than these."""

    for pattern in _EDGE_PATTERNS:
        assert pattern_safety_problem(pattern) is None, pattern
        assert pattern_step_bound(pattern) <= MAX_PATTERN_STEPS, pattern
    assert pattern_safety_problem("(?:a|aa)" * 20) == "U2.pattern-too-complex"
    assert pattern_step_bound("(?:a|aa)" * 20) > MAX_PATTERN_STEPS


@pytest.mark.parametrize("pattern", _EDGE_PATTERNS)
def test_the_worst_admitted_patterns_check_a_value_of_the_cap_quickly(pattern: str) -> None:
    """Measured half: a value of the cap against an edge pattern in < 100 ms.

    Locally the worst of them takes about 8 ms; the best of three runs is
    taken so a busy machine does not decide the result.
    """

    compiled = re.compile(pattern)
    for value in _hard_values():
        best = min(_timed(compiled, value) for _ in range(3))
        assert best < 0.1, f"{pattern!r} on {value[:12]!r}…: {best * 1000:.1f} ms"


def _timed(compiled: re.Pattern[str], value: str) -> float:
    started = time.perf_counter()
    compiled.fullmatch(value)
    return time.perf_counter() - started


def test_a_value_up_to_the_cap_is_checked_quickly() -> None:
    """Worst admitted pattern, failing value of exactly the cap, end to end."""

    iid, node = _running("Muster – Länge genau", _WORST_ALLOWED)
    value = "a" * (MAX_PATTERN_VALUE_LENGTH - 1) + "!"
    started = time.perf_counter()
    resp = client.post(f"/instances/{iid}/complete", json={"node_id": node, "data": {"s": value}})
    assert time.perf_counter() - started < 0.5  # generous for CI; the match is < 0.01 s
    assert resp.status_code == 422
    assert [f["code"] for f in resp.json()["detail"]["findings"]] == ["U4.pattern"]
    ok = client.post(
        f"/instances/{iid}/complete",
        json={"node_id": node, "data": {"s": "a" * 38}},
    )
    assert ok.status_code == 200, ok.text


def test_a_longer_value_is_refused_without_matching() -> None:
    iid, node = _running("Muster – zu lang", "^[A-Z]+$")
    resp = client.post(
        f"/instances/{iid}/complete",
        json={"node_id": node, "data": {"s": "A" * (MAX_PATTERN_VALUE_LENGTH + 1)}},
    )
    assert resp.status_code == 422
    [finding] = resp.json()["detail"]["findings"]
    assert finding["code"] == "U4.pattern-value-too-long"
    assert finding["params"]["max"] == str(MAX_PATTERN_VALUE_LENGTH)


def test_a_stored_dangerous_pattern_is_not_run(caplog: pytest.LogCaptureFixture) -> None:
    """A released schema from before the rule keeps working and never hangs."""

    iid, node = _running("Muster – Altbestand", "^[a!]+$")
    instance = api_module._instances.get(iid)
    assert instance is not None
    stored = api_module._store.get(instance.schema_id)
    assert stored is not None
    legacy = stored.model_copy(deep=True)
    legacy.forms[node].fields[0].pattern = "(a+)+$"
    plant(api_module._store, legacy)  # as if stored before the rule existed
    try:
        started = time.perf_counter()
        with caplog.at_level(logging.WARNING, logger="procworks.validator"):
            resp = client.post(
                f"/instances/{iid}/complete",
                json={"node_id": node, "data": {"s": "a" * 40 + "!"}},
            )
        assert time.perf_counter() - started < 0.5
        assert resp.status_code == 200, resp.text
        assert "not checked" in caplog.text
    finally:
        plant(api_module._store, stored)


# --- the task mask in the browser ----------------------------------------------


@needs_node
def test_the_browser_runs_a_pattern_only_up_to_the_cap(tmp_path: Path) -> None:
    """The mask in the browser applies the same cap before running the pattern.

    Beyond the cap the bound on the matcher's work does not hold, so a longer
    value gets the length hint at once instead of the pattern being run (with
    a pattern that would hang the tab, the call would not come back).
    """

    result = run_app_js(
        f"""
        const cap = {MAX_PATTERN_VALUE_LENGTH};
        const hanging = {{ pattern: "(a+)+$" }};
        const usual = {{ pattern: "[A-Z]{{2}}-\\\\d{{4}}" }};
        return {{
          over: fieldRuleProblem(hanging, "a".repeat(cap) + "!"),
          atCapWrong: fieldRuleProblem(usual, "A".repeat(cap)),
          atCapRight: fieldRuleProblem(usual, "AB-1234"),
          jsCap: PATTERN_VALUE_MAX,
        }};
        """,
        tmp_path,
    )
    assert result["jsCap"] == MAX_PATTERN_VALUE_LENGTH
    assert result["over"] == f"höchstens {MAX_PATTERN_VALUE_LENGTH} Zeichen"
    assert result["atCapWrong"] == "passt nicht zum vorgegebenen Format"
    assert result["atCapRight"] is None
