# SPDX-License-Identifier: BUSL-1.1
"""A webhook dry-run result is valid only for the input it ran with.

After a rejected dry run for ``http://127.0.0.1:9/`` the finding about
"127.0.0.1" stayed under a URL that had long been changed. Editing the URL,
the secret or the events now clears the result, and an answer that arrives
after such an edit is dropped.
"""

from __future__ import annotations

from pathlib import Path

from web_vm import needs_node, run_app_js

_OPEN = r"""
respond(() => ({ body: { allowed: false, reason_code: "OUT.private", url: "x",
  headers: {}, body: "{}" } }));
addWebhook();
const root = byId("modal-root");
document.body.appendChild(root);
const inputs = root.querySelectorAll("input");
const url = inputs.find((i) => i.getAttribute("type") === "url");
const secret = inputs.find((i) => i.getAttribute("type") === "text");
const cb = inputs.find((i) => i.getAttribute("type") === "checkbox");
const box = root.querySelector(".wh-preview");
const btn = root.querySelectorAll("button").find((b) => textOf(b).startsWith("Probelauf"));
"""


@needs_node
def test_editing_the_url_clears_the_old_result(tmp_path: Path) -> None:
    res = run_app_js(
        _OPEN
        + r"""
url.value = "http://127.0.0.1:9/";
await btn.click();
const before = box.children.length;
url.value = "https://example.invalid/hook";
await url.dispatch("input");
const afterUrl = box.children.length;
await btn.click();
await secret.dispatch("input");
const afterSecret = box.children.length;
await btn.click();
await cb.dispatch("change");
return { before, afterUrl, afterSecret, afterEvent: box.children.length };
""",
        tmp_path,
    )
    assert res["before"] > 0  # das Ergebnis stand da
    assert res["afterUrl"] == 0 and res["afterSecret"] == 0 and res["afterEvent"] == 0


@needs_node
def test_answer_arriving_after_an_edit_is_dropped(tmp_path: Path) -> None:
    res = run_app_js(
        _OPEN
        + r"""
url.value = "http://127.0.0.1:9/";
const run = btn.click();          // Anfrage laeuft, Antwort noch nicht verarbeitet
url.value = "https://example.invalid/hook";
url.dispatch("input");            // Aenderung, bevor die Antwort ankommt
await run;
return { children: box.children.length };
""",
        tmp_path,
    )
    assert res["children"] == 0
