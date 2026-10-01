# rosbag2 fixtures

One mobile-base recording (velocity commands, battery voltage, a status text: 18 messages, the QoS
profile text rosbag2 writes), written three ways by `make_rosbag2.py`:

| Bag | Storage | Parts |
|---|---|---|
| `mobile_base_sqlite3/` | sqlite3 (schema version 4: `message_definitions`, `type_description_hash`) | 1 |
| `mobile_base_mcap/` | MCAP (profile `ros2`, zstd chunks, QoS in channel metadata) | 1 |
| `split_sqlite3/` | sqlite3 | 2 (9 messages each) |

`oracle.json` is what the official readers read from them (`rosbags`' rosbag2 reader for every bag, the
`mcap` package for the `.mcap`); regenerate both with

    uv run --no-project --with mcap --with rosbags python tests/fixtures/rosbag2/make_rosbag2.py
    uv run --no-project --with mcap --with rosbags python tests/fixtures/rosbag2/make_rosbag2.py --oracle

Neither package is a project dependency. The sqlite files' bytes follow the SQLite library that wrote
them; the tests read them through the adapter and compare with the oracle, not byte for byte. Damaged
and unusual bags (cuts, flipped bytes, wrong types, undeclared topics, WAL, other page sizes, metadata
with gaps, repeats and unsafe paths) are built at test time from these.
