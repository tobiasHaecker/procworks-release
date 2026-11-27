# SPDX-License-Identifier: BUSL-1.1
"""Test helper: plant a schema or case in a store, over whatever is stored.

The stores save conditionally: an object must carry the version it was read
with, and a new object never replaces a stored one. Some tests deliberately
store data *behind* the operations -- a model as it was stored before a rule
existed, or the original restored after such a test. ``plant`` does that
explicitly: it saves a copy of the object as the next version of whatever is
stored under its id. Only for tests; the product has no such path.

Not a test module (no ``test_`` prefix), imported flat -- ``tests/`` is
deliberately not a package.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from procworks.store import remember_version, stored_version


def plant[ModelT: BaseModel](store: Any, obj: ModelT) -> ModelT:
    """Save ``obj`` as it is, replacing what is stored under its id.

    :param store: a schema or instance store (in memory or SQL).
    :param obj: the schema or instance to store; it is not changed.
    :returns: the stored copy, carrying its new version.
    """

    copy = type(obj).model_validate(obj.model_dump())  # a fresh object, no version
    current = store.get(copy.id)
    if current is not None:
        version = stored_version(current)
        assert version is not None
        remember_version(copy, version)
    store.put(copy)
    return copy
