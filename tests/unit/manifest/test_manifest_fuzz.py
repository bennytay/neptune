"""Seeded mutation fuzz: a manifest reader either reads or refuses, and never crashes (ADR 0047 §2).

Every mutation of a valid manifest (bytes flipped, inserted, deleted, cut short, spliced) must end
in a ``Manifest`` whose lineage can be built, or a ``ManifestError``: any other exception would
be exit 1 (internal) for a user's typo. Seeded and bounded, so it is the same few thousand cases on
every run.
"""

import json
import random

import pytest

from neptune.identity.hashing import content_id
from neptune.manifest import LoadedManifest, ManifestError, parse_manifest
from neptune.model.source import LocalPath

SEEDS = (
    b'neptune: 1\nmachines:\n  - id: ur5e\n    name: "UR5e, cell 2"  # c\n'
    b"    aliases: {serial: \"2023\", ros: [a, 'b''c']}\nsoftware:\n  - {id: d, version: 1.10}\n"
    b'runs:\n  - name: pick\n    paths: [arm/p_1.mcap, "arm/p_2.mcap"]\n    machine: ur5e\n'
    b'sources:\n  - {path: arm/n.txt, adapter: markdown}\n  - glob: "logs/**/*.csv"\n'
    b'    adapter: tabular\n    options: {csv_delimiter: ";"}\ngrouping:\n  gap_seconds: 30\n',
    json.dumps(
        {
            "neptune": 1,
            "sites": [{"id": "lab", "name": "Lab é"}],
            "runs": [{"name": "a", "paths": ["x/y"], "site": "lab"}],
            "adapters": {"text": {"options": {"block_rule": "line"}}},
        }
    ).encode(),
)
ALPHABET = b"{}[],:-#'\"\\ \n\t&*!|>?%@`~.0123456789eE+xo/\x00\xff\xc3a"
CASES = 2000


def mutate(rng: random.Random, data: bytes) -> bytes:
    out = bytearray(data)
    for _ in range(rng.randint(1, 4)):
        if not out:
            out.extend(b"{")
            continue
        at = rng.randrange(len(out))
        kind = rng.randrange(5)
        if kind == 0:
            out[at] = rng.choice(ALPHABET)
        elif kind == 1:
            out.insert(at, rng.choice(ALPHABET))
        elif kind == 2:
            del out[at]
        elif kind == 3:
            del out[at:]
        else:
            start = rng.randrange(len(out))
            out[at:at] = out[start : start + rng.randint(1, 20)]
    return bytes(out)


@pytest.mark.parametrize("json_syntax", [False, True])
def test_mutated_manifests_read_or_are_refused(json_syntax: bool) -> None:
    rng = random.Random(14 + json_syntax)
    read = refused = 0
    for _ in range(CASES):
        data = mutate(rng, rng.choice(SEEDS))
        try:
            manifest = parse_manifest(data, json_syntax=json_syntax)
        except ManifestError:
            refused += 1
            continue
        loaded = LoadedManifest(manifest, LocalPath("neptune.yaml"), content_id(data), len(data))
        assert loaded.transform.config["source"] == loaded.content_id
        read += 1
    assert read + refused == CASES and refused > 0
