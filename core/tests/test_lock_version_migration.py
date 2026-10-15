# SPDX-License-Identifier: BUSL-1.1
"""The version counter arrives by Alembic migration, with the stored data intact.

The conditional save needs a ``lock_version`` column on ``process_instance``
and ``process_schema``. An installation that updates runs ``alembic upgrade
head`` (the API container does it at start); its stored cases and models must
come through unchanged, start at version 1 and be savable afterwards. The
downgrade removes the column again without touching the data.

Alembic runs in a subprocess, as in the container: its logging configuration
would otherwise reconfigure the loggers of the test process.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text

from procworks.db import Base, SqlAlchemyInstanceStore, SqlAlchemySchemaStore
from procworks.unit_of_work import ConcurrentModification

pytest.importorskip("alembic")

_CORE = Path(__file__).resolve().parents[1]


def _alembic(url: str, *args: str) -> None:
    env = {**os.environ, "DATABASE_URL": url, "PYTHONPATH": str(_CORE / "src")}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=_CORE,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def _columns(url: str, table: str) -> set[str]:
    engine = create_engine(url)
    try:
        return {c["name"] for c in inspect(engine).get_columns(table)}
    finally:
        engine.dispose()


def _seed_before_the_column(url: str) -> None:
    """One case and one model as a version without the counter stored them."""

    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO process_schema (id, version, name, lifecycle_state, document) "
                "VALUES ('schema_1', 1, 'Alt', 'ENTWURF', :doc)"
            ),
            {"doc": json.dumps({"id": "schema_1", "name": "Alt"})},
        )
        conn.execute(
            text(
                "INSERT INTO process_instance (id, schema_id, schema_version, state, document) "
                "VALUES ('instance_1', 'schema_1', 1, 'RUNNING', :doc)"
            ),
            {"doc": json.dumps({"id": "instance_1", "schema_id": "schema_1",
                                "data_values": {"tage": 3}})},
        )
    engine.dispose()


def test_upgrade_keeps_the_stored_data_and_starts_it_at_version_one(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'upgrade.db'}"
    _alembic(url, "upgrade", "0013_id_claim")
    assert "lock_version" not in _columns(url, "process_instance")
    _seed_before_the_column(url)

    _alembic(url, "upgrade", "head")

    instances = SqlAlchemyInstanceStore(url)
    schemas = SqlAlchemySchemaStore(url)
    case = instances.get("instance_1")
    model = schemas.get("schema_1")
    assert case is not None and case.data_values == {"tage": 3}
    assert model is not None and model.name == "Alt"
    # Version 1 for the old rows: a save from the loaded state succeeds, a
    # second save from the same old state is refused.
    stale = instances.get("instance_1")
    case.data_values["tage"] = 4
    instances.put(case)
    with pytest.raises(ConcurrentModification):
        instances.put(stale)
    saved = instances.get("instance_1")
    assert saved is not None and saved.data_values == {"tage": 4}


def test_downgrade_drops_the_counter_and_keeps_the_data(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'downgrade.db'}"
    _alembic(url, "upgrade", "0013_id_claim")
    _seed_before_the_column(url)
    _alembic(url, "upgrade", "head")

    _alembic(url, "downgrade", "0013_id_claim")

    assert "lock_version" not in _columns(url, "process_instance")
    assert "lock_version" not in _columns(url, "process_schema")
    engine = create_engine(url)
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT id FROM process_instance")).scalars().all()
    engine.dispose()
    assert rows == ["instance_1"]

    _alembic(url, "upgrade", "head")  # and up again
    assert "lock_version" in _columns(url, "process_instance")


def test_every_orm_column_exists_after_upgrade_head(tmp_path: Path) -> None:
    """A column only ``create_all`` knows would be missing after an update."""

    url = f"sqlite:///{tmp_path / 'head.db'}"
    _alembic(url, "upgrade", "head")

    missing = {
        f"{name}.{column.name}"
        for name, table in Base.metadata.tables.items()
        for column in table.columns
        if column.name not in _columns(url, name)
    }
    assert not missing, sorted(missing)
