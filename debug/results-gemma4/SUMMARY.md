# Debug benchmark result

- Model: `gemma4:latest`
- Accuracy: **6/10 (60.0%)**
- Mean latency: 16.266s per image
- Diagnostic 20px-expanded-box score: 9/10 (90.0%); this is not counted as a real click success.

| Case | Task | Point | Hit | Time |
|---|---|---:|---:|---:|
| 01 | Click the blue Save button. | `[812, 487]` | yes | 13.57s |
| 02 | Click the green Continue button. | `[812, 112]` | yes | 20.566s |
| 03 | Click the red Delete button. | `[187, 262]` | no | 11.795s |
| 04 | Click the purple Settings button. | `[187, 112]` | yes | 9.92s |
| 05 | Click the orange Submit button. | `[562, 262]` | no | 16.138s |
| 06 | Click the cyan Next button. | `[187, 487]` | yes | 18.099s |
| 07 | Click the blue Download button. | `[812, 337]` | no | 25.776s |
| 08 | Click the gray Cancel button. | `[437, 112]` | yes | 9.398s |
| 09 | Click the green Apply button. | `[437, 487]` | yes | 17.429s |
| 10 | Click the purple Upload button. | `[187, 337]` | no | 19.969s |

## Failure analysis

| Case | Target box | Point | Outside distance | Coarse/Fine cell |
|---|---:|---:|---:|---:|
| 03 | `[82, 272, 237, 322]` | `[187, 262]` | 10.0px | `9 / 2` |
| 05 | `[420, 270, 575, 320]` | `[562, 262]` | 8.0px | `11 / 2` |
| 07 | `[750, 270, 925, 320]` | `[812, 337]` | 17.0px | `12 / 7` |
| 10 | `[80, 365, 235, 415]` | `[187, 337]` | 28.0px | `13 / 2` |
