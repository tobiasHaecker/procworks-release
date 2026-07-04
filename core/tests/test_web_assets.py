# SPDX-License-Identifier: BUSL-1.1
"""Static syntax guard for the no-build web client (``web/app.js``).

Background: ``web/app.js`` is shipped verbatim (no bundler, no transpiler) and
loaded by the browser as a single classic script. A single syntax error --
notably a ``return`` statement that ends up at *module scope* because a
function header was accidentally deleted -- aborts the parsing of the **entire**
file. The browser then throws ``Uncaught SyntaxError: Illegal return statement``,
``boot()`` never runs, the API field stays empty and the client is permanently
stuck on "getrennt" even though the backend is perfectly healthy. Exactly this
regression once shipped unnoticed.

There is no JavaScript test infrastructure in this repository (and no Node.js in
CI), so this guard is implemented in pure Python: a small, string/template/
comment-aware scanner that tracks brace depth and flags any ``return`` keyword
found at the outermost (module) scope, plus a brace-balance check. When a
``node`` binary happens to be available, an authoritative ``node --check`` runs
in addition. The scanner is itself covered by a self-test so the guard cannot
silently rot.

Stability premise: the tool must always work. This test makes the specific
"whole client dies on load" failure mode impossible to merge undetected.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_APP_JS = _REPO_ROOT / "web" / "app.js"


def scan_js(src: str) -> tuple[list[int], int, int]:
    """Scan JavaScript source for module-level ``return`` statements.

    Returns ``(module_return_lines, final_brace_depth, final_frame_count)``.

    A well-formed classic script has no ``return`` at module scope, a final
    brace depth of ``0`` and a single (outermost) context frame. The scanner is
    aware of line/block comments, single/double-quoted strings and template
    literals including nested ``${ ... }`` expressions, so ``return`` occurring
    inside those never triggers a false positive. It is intentionally simple
    rather than a full parser; its only job is to catch the "dangling function
    body at module scope" failure class.
    """
    n = len(src)
    i = 0
    line = 1
    depth = 0
    # Context frames. ('code', expr_start_depth | None) or ('tmpl', None).
    # The outermost frame carries expr_start_depth=None; a ('code', d) frame is
    # a ``${ ... }`` expression opened inside a template literal at brace depth d.
    frames: list[tuple[str, int | None]] = [("code", None)]
    module_returns: list[int] = []

    while i < n:
        c = src[i]
        kind, expr_start = frames[-1]

        if kind == "code":
            # Comments.
            if c == "/" and i + 1 < n and src[i + 1] == "/":
                j = src.find("\n", i + 2)
                i = n if j < 0 else j
                continue
            if c == "/" and i + 1 < n and src[i + 1] == "*":
                j = src.find("*/", i + 2)
                end = n if j < 0 else j + 2
                line += src.count("\n", i, end)
                i = end
                continue
            if c == "\n":
                line += 1
                i += 1
                continue
            # String literals.
            if c in ("'", '"'):
                i += 1
                while i < n:
                    if src[i] == "\\":
                        i += 2
                        continue
                    if src[i] == "\n":
                        line += 1
                        i += 1
                        continue
                    if src[i] == c:
                        i += 1
                        break
                    i += 1
                continue
            # Template literal opens.
            if c == "`":
                frames.append(("tmpl", None))
                i += 1
                continue
            if c == "{":
                depth += 1
                i += 1
                continue
            if c == "}":
                # Does this close the enclosing ``${ ... }`` expression?
                if expr_start is not None and depth == expr_start:
                    frames.pop()
                    i += 1
                    continue
                if depth > 0:
                    depth -= 1
                i += 1
                continue
            # Identifier / keyword (maximal munch => whole-word match).
            if c.isalpha() or c in ("_", "$"):
                j = i + 1
                while j < n and (src[j].isalnum() or src[j] in ("_", "$")):
                    j += 1
                word = src[i:j]
                if word == "return" and len(frames) == 1 and depth == 0:
                    module_returns.append(line)
                i = j
                continue
            i += 1
            continue

        # Inside a template literal (raw text between the code expressions).
        if c == "\n":
            line += 1
            i += 1
            continue
        if c == "\\":
            i += 2
            continue
        if c == "`":
            frames.pop()
            i += 1
            continue
        if c == "$" and i + 1 < n and src[i + 1] == "{":
            frames.append(("code", depth))
            i += 2
            continue
        i += 1

    return module_returns, depth, len(frames)


def test_app_js_exists() -> None:
    assert _APP_JS.is_file(), f"expected web client at {_APP_JS}"


def test_app_js_has_no_module_level_return() -> None:
    """The exact regression: a ``return`` at module scope kills the whole file.

    This happens when a function header (e.g. ``function maskControl(...) {``)
    is accidentally removed and its body -- ``return`` statements included --
    dangles at the top level. In the browser this is a fatal
    ``Illegal return statement`` SyntaxError.
    """
    src = _APP_JS.read_text(encoding="utf-8")
    module_returns, _depth, _frames = scan_js(src)
    assert not module_returns, (
        "web/app.js has 'return' at module scope on line(s) "
        f"{module_returns} -- this is an 'Illegal return statement' that aborts "
        "the entire client on load (status stays 'getrennt'). A function header "
        "was most likely deleted, leaving its body dangling at top level."
    )


def test_app_js_braces_are_balanced() -> None:
    """Unbalanced braces are another way the whole file fails to parse."""
    src = _APP_JS.read_text(encoding="utf-8")
    _returns, depth, frames = scan_js(src)
    assert depth == 0, f"web/app.js has unbalanced braces (residual depth {depth})"
    assert frames == 1, (
        f"web/app.js has an unterminated string/template context ({frames} open)"
    )


def test_scanner_detects_illegal_module_return() -> None:
    """Guard the guard: the scanner must flag the failure it exists to catch."""
    good = "function f(){ if(x){ return 1; } return 2; }\nconst g = () => { return 3; };\n"
    bad = "function f(){ /* ok */ }\n\n  const dtype = 'x';\n  return { control: 1 };\n}\n"
    assert scan_js(good)[0] == [], "scanner produced a false positive on valid JS"
    assert scan_js(bad)[0], "scanner failed to detect a module-level return"


def test_scanner_ignores_return_in_strings_templates_and_comments() -> None:
    """No false positives from the word 'return' inside non-code regions."""
    src = (
        "// return here is a comment\n"
        "/* return in block comment */\n"
        "const a = 'return in string';\n"
        'const b = "also return";\n'
        "const c = `template return ${1 + 2} still text`;\n"
        "const d = `${ (function(){ return 7; })() }`;\n"
    )
    assert scan_js(src)[0] == []


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js not available")
def test_app_js_passes_node_syntax_check() -> None:
    """Authoritative parser check when a Node.js runtime is present."""
    result = subprocess.run(  # noqa: S603 - fixed args, trusted repo file
        [shutil.which("node") or "node", "--check", str(_APP_JS)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"node --check failed:\n{result.stderr}"
