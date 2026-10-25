# The site model on real public data (#211)

The two demo sites (Plant 7 and Harbor DC) are generated. This shows that the same code, with one mapping
file, answers questions over a robot's real export.

```
make site-demo SITE=public     # fetch into site-public/, build, confirm, insights, print the MCP command
```

## The dataset

| | |
|---|---|
| Name | **UR3 CobotOps**, UCI Machine Learning Repository dataset 963 |
| Who | Tyrovolas, Aliev, Antonelli, Stylios (2024) |
| License | **CC BY 4.0** |
| URL | `https://archive.ics.uci.edu/static/public/963/ur3+cobotops.zip` (1.7 MB) |
| sha256 | zip `233334ee…4993c8d`, `dataset_02052023.xlsx` `f0f10917…c8796091` (both in full in the script) |
| What | one Universal Robots UR3 cobot's controller export: 7,409 rows, about one per second, on 2022-10-26 (08:17–09:05 and 14:19–15:36, `Z` times). Per row: current, temperature and speed for joints J0–J5, tool current, the program `cycle`, `Robot_ProtectiveStop`, `grip_lost` |

`scripts/fetch_public_site.sh <site_dir>` downloads it into `$TMPDIR`, checks both hashes, and copies the
workbook byte for byte to `static/ur3/`, plus `scripts/public_site/{layout.yaml,neptune.mappings.yaml}`.
Nothing is committed.

Candidates passed over: ROS 2 / MCAP bags with `/diagnostics` and battery from several robots exist
mostly as large perception datasets (GBs, no health topics) or without a clear license. The compiler reads
bags, but the site mappers read only its tables and documents, so a bag would need a bag → feed bridge
first. The UR3 export is small, permissive and full of real faults.

## What mapped

| Column | Becomes | Kind |
|---|---|---|
| `Robot_ProtectiveStop` | `robot:ur3 health` `ok` / `fault:protective_stop` | observed |
| `grip_lost` | `component:ur3-gripper health` `ok` / `fault:grip_lost` | observed |
| `cycle ` | `robot:ur3 cycle` | observed |
| `Temperature_T0`, `Temperature_J1..J5` | `component:ur3-j0..j5 temperature`, **no unit** (the export states none) | observed |
| `layout.yaml` | the robot, its seven components, `robot:ur3 depends_on` each | stated |

- A fact is stated only when its value changes (`changes: true`), as a feed's are: 12,068 log entries from
  7,409 rows × 9 columns.
- Every fact cites `static/ur3/dataset_02052023.xlsx row N col "<column>"`, quoting the cell.
- Not mapped: joint currents, speeds and tool current. They have no vocabulary predicate, and a value per
  second is a signal, not a state.

Three mapping features were added for this, all generic: `changes: true` (a row per sample becomes
intervals), `values:` (a cell as written → a vocabulary value) and the `strip:` filter. The vocabulary gained
the `component` type and a `cycle` predicate. When a site has no feed, `now` is its latest observed fact.

## What `draft-mapping` got on its own

`python -m neptune_memory.site draft-mapping site-public static/ur3/dataset_02052023.xlsx` now reads an
XLSX back from the built package. As first run, it got the file, the source and all 24 columns, and
nothing else. It guessed `machine:{Num}` as the subject, missed the time (`.847Z`) and named no
predicate. After three generic heuristics (fractional or `Z` times; `temperature`, `cycle`; `stop` →
`health`), it also gets `time: Timestamp`, six `temperature`s, `cycle` and `health`. By hand: the subject,
`kind: observed`, `changes`, the `values` maps, the quoted-timestamp `strip`, the components and the
gripper.

## Gold questions (`packages/neptune-memory/tests/site/public_gold_questions.json`)

Each answer was checked against the raw workbook (openpyxl, independently of Neptune).

| | Question | Answer, cited |
|---|---|---|
| P1 | First protective stop, and how long until it cleared? | 08:17:40 → 08:17:46, 6 s (row 21 → row 26) |
| P2 | How many protective stops and grip losses? | 108 stops, 77 grip losses (first at rows 21 and 45) |
| P3 | State at 14:30:00, and what can't the data say? | health `ok` since 14:26:16 (row 3252), cycle 126 (row 3455); `mode` and `battery` **unknown** |
| P4 | Hottest joint, and when first? | J4 at 45.375 (no unit), 15:15:39 (row 6190) |
| P5 | Grip lost at 08:18:05: what does it touch, how long? | the UR3 (depends on the gripper, layout line 9); ok again at 08:18:07 (row 47) |

They are `slow` and skip until the data is fetched. CI's non-PR runs (main, nightly) fetch it first.

## What the model can and can't say

- **Can:** when each stop and grip loss began and ended, how many there were and how long they lasted
  (`fault_clear_time`), the cycle and joint temperatures at any second, and what a gripper fault touches.
  Every answer cites the cell.
- **Can't:** mode, battery, serials or firmware. The export holds none of them, so they read `unknown`
  rather than being guessed. It can't give the cause of a stop (no fault codes, no work orders), or
  temperature units.
- **Honest wrinkles from real data:** 54 rows have blank cells, and a blank never becomes a fact, so the
  last value holds. Some timestamps are wrapped in quotes (row 6190), hence `strip:"`. Times are cut to
  the second, so two changes in the same second would read `ambiguous`. The export never says what the
  session gap from 09:05 to 14:19 was, so the model does not say either. Insights find nothing here: no
  rule fires without modes, topology downstream of the robot, or work orders.
