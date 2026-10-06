# Ingest receipt

Receipt `rec:sha256:288cbb23cc0c134fe9ecc3e925d8ee406b4d194448c6033c5413fe9d7515ebda`. Every id below is shortened; `receipt.json` has them whole.

## Summary

- Sources: 4 seen, 4 read, 0 not read, 0 gone
- Runs: 0; streams: 0; entities: 0
- Findings: 0 errors, 0 warnings, 0 info; ambiguous fields: 0

## Sources

| Location | Bytes | Content | Read by |
|---|---|---|---|
| `amr7/calibration/amr_arm_base_camera_eob.calib` | 509 | `sha256:4b7516806dca` | calibration 0.2.0 |
| `cell2/calibration/arm_wrist_camera_pose.launch` | 526 | `sha256:239082e00191` | calibration 0.2.0 |
| `cell2/calibration/arm_wrist_easy_handeye.yaml` | 496 | `sha256:d2df69b50a51` | calibration 0.2.0 |
| `cell2/vision/wrist_camera_handeye.yml` | 696 | `sha256:4233ce8f8bce` | calibration 0.2.0 |

## Adapters

| Adapter | Version | Config | Libraries | Transform |
|---|---|---|---|---|
| `calibration` | `0.2.0` | `sha256:6310d444f1bb` | python 3.12, pyyaml 6.0.3 | `rec:48ba43351068` |

## Records

| Kind | Records |
|---|---|
| `calibration` | 4 |
| `frame_graph` | 3 |
| `frame_transform` | 3 |
| `source_artifact` | 4 |
| `source_revision` | 4 |
| `timestamp_domain` | 1 |
| `transform_record` | 1 |

## Runs

| Run | Session | Machine | First | Last | Streams |
|---|---|---|---|---|---|

## Streams

| Stream | Run | Topic | Clocks | Messages | First | Last |
|---|---|---|---|---|---|---|

## Entities

| Kind | Record | Stated ids |
|---|---|---|

## Findings

None.

## Ambiguous fields

None.
