# Third-Party Notices

ProcWorks itself is © 2026 Tobias Häcker and licensed under the Business Source
License 1.1 (see [LICENSE](LICENSE)). ProcWorks **does not vendor, embed or
redistribute** any third-party source code: no external library is copied into
this repository, and the web client and prototype load **only local**
first-party assets (no CDN, no bundled fonts, no minified vendor files).

The packages below are **declared dependencies** that are installed separately
from their original distributors (PyPI) at build/run time. They are used
**unmodified** through their public interfaces. This file documents their
licenses for good-faith compliance and provenance; it is informational and not
a substitute for an authoritative SBOM (see "Verifying" below). License names
follow each project's own published metadata at the time of writing.

## Runtime dependencies (`core`)

| Package | License | Use |
| --- | --- | --- |
| FastAPI | MIT | API framework |
| Starlette (via FastAPI) | BSD-3-Clause | ASGI toolkit |
| Pydantic / pydantic-core | MIT | data modelling & validation |
| Uvicorn (`uvicorn[standard]`) | BSD-3-Clause | ASGI server |
| uvloop (via uvicorn extra) | MIT / Apache-2.0 | event loop |
| httptools (via uvicorn extra) | MIT | HTTP parsing |
| websockets (via uvicorn extra) | BSD-3-Clause | WebSocket protocol |
| python-dotenv (via uvicorn extra) | BSD-3-Clause | env loading |
| watchfiles (via uvicorn extra) | MIT | reload watcher |
| PyYAML (via uvicorn extra) | MIT | config parsing |
| NetworkX | BSD-3-Clause | reachability (K3), cycle checks (H3) |
| SQLAlchemy | MIT | persistence (optional) |
| anyio / sniffio | MIT / (MIT or Apache-2.0) | async runtime |
| h11 | MIT | HTTP/1.1 |
| idna | BSD-3-Clause | hostname encoding |
| typing-extensions | PSF-2.0 | typing back-ports |

## Optional PostgreSQL driver (`core[postgres]`)

| Package | License | Notes |
| --- | --- | --- |
| psycopg (psycopg 3) | **LGPL-3.0-only** | PostgreSQL driver. See note below. |
| libpq (bundled in `psycopg[binary]`) | PostgreSQL License | C client library, by the PostgreSQL Global Development Group |
| Alembic | MIT | database migrations |
| Mako (via Alembic) | MIT | migration templates |

> **psycopg / LGPL-3.0 note.** The PostgreSQL driver `psycopg` is licensed under
> the LGPL-3.0 (weak copyleft). ProcWorks uses it **unmodified** as a separate,
> dynamically imported library (it is not statically linked into, copied into, or
> derived in ProcWorks' own source). Under the LGPL this constitutes use of the
> library, not a derivative work of it, so it does not impose copyleft terms on
> ProcWorks' own code. The driver is **optional** (only the `postgres` extra) and
> interchangeable: the in-memory store needs no driver, and any other
> PostgreSQL/DBAPI driver can be substituted via `DATABASE_URL`. Anyone shipping
> the LGPL component remains able to replace it, as the LGPL requires.

## Development / build tools (not shipped at runtime)

| Package | License |
| --- | --- |
| pytest | MIT |
| httpx | BSD-3-Clause |
| ruff | MIT |
| mypy / mypy-extensions | MIT |
| hatchling (build backend) | MIT |
| certifi (test transitive) | MPL-2.0 |

## Standards, concepts and citations (no code)

- **BPMN 2.0 / ISO 19510** is an open OMG specification. ProcWorks implements a
  semantic, block-structured subset from the published standard; no OMG text or
  schema files are redistributed here.
- **ADEPT2** (University of Ulm; Dadam, Reichert, Rinderle-Ma et al.) is the
  *research idea* ProcWorks draws on. Only published **concepts** (block
  structure, correctness criteria, high-level change operations, marking
  semantics, migration criteria) are referenced and re-implemented from scratch
  in original Python — **no ADEPT source code** is used, copied or
  derived. Academic sources are cited in the ProcWorks architecture concept,
  §15. "ADEPT" is referenced only nominatively/descriptively
  to credit the underlying research; ProcWorks claims no affiliation with or
  endorsement by their owners.
- **Business Source License 1.1** text is © 2017 MariaDB Corporation Ab;
  "Business Source License" is a trademark of MariaDB Corporation Ab. The
  license text is reproduced in [LICENSE](LICENSE) as permitted.

## Verifying

For an authoritative, machine-readable inventory, generate an SBOM from the
actually installed environment, e.g.:

```bash
pip install pip-licenses
pip-licenses --format=markdown --with-urls --with-license-file
# or, for a CycloneDX SBOM:
pip install cyclonedx-bom && cyclonedx-py environment
```

All declared dependencies use OSI-approved licenses that are compatible with
ProcWorks' BUSL-1.1 distribution; the only weak-copyleft component (psycopg,
LGPL-3.0) is optional, unmodified and dynamically linked as described above.
