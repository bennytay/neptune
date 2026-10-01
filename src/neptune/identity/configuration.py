"""The identity of a configuration snapshot's values (ADR 0037 §5).

A snapshot's bytes are identified by their content id like any source. Its *values* are
identified by ``configuration_digest``: the sha256 of the canonical JSON of every value's path and
``comparison_key``, in path order. Two snapshots have equal digests exactly when
``compare_configurations`` finds no change between them, so a binding to a digest (MVL-38) says
"these runs ran the same configuration" whatever its layout, comments, format or key order.
"""

import hashlib
from collections.abc import Iterable
from typing import TYPE_CHECKING, Final

from neptune.identity import canonical_json
from neptune.model.configuration import (
    ConfigurationValue,
    ValueDigest,
    comparison_key,
    snapshot_of,
)

if TYPE_CHECKING:
    from neptune.model.jsonvalue import JsonValue

# Hashed into every digest so the derivation can be versioned without ambiguity.
DIGEST_SCHEME: Final = "neptune.configuration-digest/1"


def configuration_digest(values: Iterable[ConfigurationValue]) -> ValueDigest:
    """The identity of one snapshot's values: equal for snapshots that declare equal values."""
    entries: list[JsonValue] = [
        [list(value.path), comparison_key(value)] for value in snapshot_of(values)
    ]
    payload: dict[str, JsonValue] = {"scheme": DIGEST_SCHEME, "values": entries}
    return ValueDigest("sha256:" + hashlib.sha256(canonical_json.dumps(payload)).hexdigest())
