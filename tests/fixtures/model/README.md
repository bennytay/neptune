# Worked examples

Four robots, each with the sources such a team typically has, and the canonical records those
sources declare (MVL-70). `make examples` rebuilds everything here from `make_examples.py`, and
`tests/integration/test_worked_examples.py` checks that the committed files are exactly its output.

| Example | Sources | What it shows |
|---|---|---|
| `drone` | `flight.ulg` (PX4 ULog) | boot time and GPS time as separate clocks; the vehicle by its `sys_uuid`; board, sensor ids, firmware commit and accelerometer calibration; a dropout and an unstated release as findings |
| `quadruped` | `bag/metadata.yaml` + `bag/walk_0.mcap` (ROS 2 bag), `robot.urdf`, `meshes/body.stl` | a run declared by the bag's metadata; joint states and a body-pose trajectory, each with three clocks; the URDF as frames, transforms and components, naming no machine; the mesh as geometry |
| `manipulator` | `session.mcap`, `handeye.yaml`, `cell/records.json` | camera frames inside a log as a stream; a hand-eye calibration whose transform direction is `Ambiguous` and whose unit is missing; the cell's commissioning, risk assessment, joint-drive replacement and requalification as stated lifecycle records |
| `mobile_robot` | `drive.bag` (ROS 1), `sites.csv`, `photos/dock.png`, `deployment/records.json` | a register with blank cells, its rows and the sites they name, each id and name citing its cell; a photo's pixels and its EXIF capture time, position and camera serial; the warehouse deployment's commissioning, authorisation envelope, remote assist, incident, change and risk assessment |

Each example directory holds `sources/` (the files, as an ingest root) and `records/<kind>.jsonl`:
one canonical-JSON line per record, sorted by id (ADR 0002), with the ledger (`source_artifact`,
`source_revision`, `transform_record`) beside the records. The adapters these records stand in for
arrive in M4 to M6. Series rows are Parquet and are not written here (MVL-5, MVL-16).

## Choices a real adapter will document

- Byte ranges cite whole records of the file (a ULog message, an MCAP record, a bag connection);
  `formats.py` returns every record's range, and the test checks each citation lands on one.
- Adapter steps name parts the core steps cannot: `ulog:field`, `mcap:time_field`,
  `ros2msg:field`, `ros1msg:field`, `rosbag1:time_field`, `exif:tag`.
- YAML sources are written in flow style (valid YAML that also parses as JSON), so the test can
  resolve JSON pointers into them with the standard library.
- rosbag2's `duration` is the last message time minus the first, so the run's `last` is the start
  plus the duration, on the metadata's own clock.
- EXIF states latitude and longitude as degrees, minutes and seconds with a hemisphere; the photo's
  position holds them as signed degrees, read exactly and rounded once to a float.
- Deployment records (ADR 0051) are JSON exports cited value by value with JSON pointers, all
  `stated`. Their date-times state an offset, so their ticks are POSIX seconds on one document
  clock. Severities, scores and decisions stay text; numbers keep their declared unit.
- EXIF `DateTimeOriginal` has no zone. Its ticks count the stated civil seconds from
  1970-01-01T00:00:00 on the camera's own clock, whose timescale stays `Unknown`.

## Checked against the official readers

The binary sources were read once with each format's official library, outside the project
(`uv run --no-project --with <package>`), so no dependency was added:

- `mcap` reads both MCAP files with CRC checks on;
- `rosbags` reads the ROS 1 bag and the ROS 2 bag, and decodes every CDR message;
- `pyulog` reads the ULog's info, parameters, dropout and both topics;
- `Pillow` reads the PNG and its EXIF, Exif and GPS directories.
