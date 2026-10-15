# SPDX-License-Identifier: BUSL-1.1
"""Wer keine Entwuerfe bearbeitet, bekommt keinen Entwurf vorgewaehlt.

Die Prozessauswahl ist bewusst Geraetewahl und ueberlebt einen Login-Wechsel
(sie traegt keine Daten). Meldete sich nach einer Modelliererin aber eine
Bearbeiterin an, stand deren Entwurf weiter oben gewaehlt, und die Ausfuehrung
sagte nur „Nur freigegebene Schemata sind instanziierbar.“ Jetzt waehlt
``loadSchemas`` fuer Logins ohne Modellier-/Admin-Rolle statt eines Entwurfs
den ersten freigegebenen Prozess. Geprueft in Node (``web_vm.py``).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from web_vm import needs_node, run_app_js

_SCHEMAS = r"""
const lifecycle = { s1: "ENTWURF", s2: "RELEASED", s3: "RELEASED", s4: "ENTWURF" };
let listed = ["s1", "s2", "s3"];
respond((p) => {
  if (p === "/schema-summaries") {
    return { body: listed.map((id) => ({ id, name: "P " + id, version: 1,
                                         lifecycle_state: lifecycle[id], revision_of: null })) };
  }
  return { status: 404 };
});
"""


@needs_node
@pytest.mark.parametrize(
    ("roles", "selected", "only", "expected"),
    [
        # Bearbeiterin nach einer Modelliererin: kein Entwurf, sondern der erste freigegebene.
        (["operator"], "s1", None, "s2"),
        (["viewer"], "s1", None, "s2"),
        # Negativ: Modellierer und Admin behalten ihren Entwurf (Neuladen derselben Person).
        (["modeler"], "s1", None, "s1"),
        (["admin"], "s1", None, "s1"),
        # Negativ: Eine freigegebene Auswahl bleibt auch fuer die Bearbeiterin.
        (["operator"], "s3", None, "s3"),
        # Grenzfall: Gibt es nichts Freigegebenes, bleibt die Auswahl stehen.
        (["operator"], "s1", ["s1", "s4"], "s1"),
    ],
)
def test_preselection_fits_the_role(
    tmp_path: Path, roles: list[str], selected: str, only: list[str] | None, expected: str
) -> None:
    res = run_app_js(
        _SCHEMAS
        + f"state.principal = {{ subject: 'x', roles: {roles!r} }};\n"
        + f"state.schemaId = {selected!r};\n"
        + (f"listed = {only!r};\n" if only else "")
        + "await loadSchemas();\nreturn state.schemaId;",
        tmp_path,
    )
    assert res == expected
