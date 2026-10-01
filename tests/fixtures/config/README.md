# Configuration fixtures

Files for the `config` adapter (`neptune.adapters.config`, ADR 0037). Every file is written by
`make_config_fixtures.py` from text in that script; run it after editing the script, never edit a
file by hand. Each is under 512 KB.

| File | Case | What the adapter must do |
|---|---|---|
| `nav2_params.yaml` | valid ROS 2 parameters (Nav2), comments, flow sequences, dotted keys | one snapshot, a value per node, comments cited; the golden package |
| `px4_params.json` | valid PX4 parameter snapshot as JSON | integers and floats as JSON spells them |
| `gripper_tool.toml` | valid TOML tool config: tables, inline tables, arrays of tables, all four date-time kinds, hex and grouped integers, literal and multi-line strings | TOML's own types, spans for header tables, comments |
| `run_a_params.yaml`, `run_b_params.yaml` | two runs' parameters: run B retunes, reorders, requotes and recomments | `compare_configurations` finds exactly the retuned paths; equal digests for equal values |
| `yaml_types.yaml` | YAML 1.1 vs 1.2 scalars, quoting, tags, a custom tag, an invalid tagged value | `Ambiguous` where the versions differ, `Known` where they agree; findings |
| `anchors.yaml` | anchors, aliases, merge keys, an undefined alias, complex keys | aliases as references, never expanded; skipped entries reported |
| `multi_document.yaml` | three documents: `%YAML 1.1`, `%YAML 1.2`, undeclared | a snapshot each, typed by its own declaration |
| `robot_config` | renamed / extensionless: UTF-8 BOM and CR LF | detected from its bytes; BOM and line endings recorded |
| `utf16.yaml` | UTF-16 LE with a BOM | decoded by the mark; spans in code points |
| `mixed_endings.yaml` | LF, CR LF and CR mixed | read, plus `config.mixed_line_endings` |
| `truncated_px4.json`, `truncated_nav2.yaml` | truncated mid-string | claimed by the probe (cut short), then `config.syntax_error`, nothing else |
| `corrupted_tool.toml` | a broken value mid-file | `config.syntax_error` in TOML; the probe does not claim it |
| `corrupted_nav2.yaml` | an `ff` byte mid-file | `config.invalid_encoding` |
| `empty.yaml`, `comments_only.toml` | empty; only comments | `config.no_document`; the probe does not claim them |
| `duplicates.json`, `duplicates.yaml` | repeated keys | every entry kept, addressed by `config:entry`, `config.duplicate_key` |
| `duplicates.toml` | a repeated key, which TOML forbids | `config.syntax_error`, as every TOML reader refuses it |
| `billion_laughs.yaml` | nine levels of nested aliases | 91 values, no expansion |
| `deep.json`, `deep.yaml` | nesting of 1,000 and 300 | `config.too_deep` |
| `huge_scalar.yaml` | a 100,000-character scalar | read; over a lowered `max_scalar_length`, `config.scalar_too_large` |
| `nonfinite.json` | `NaN`, `Infinity`, `1e400`, a 5,000-digit integer, unpaired surrogates | non-standard tokens read and reported; unrepresentable values `Unknown`; a surrogate key skipped |
