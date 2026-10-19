# Debug benchmark result

- Model: `minicpm-v4.6:latest`
- Accuracy: **6/10 (60.0%)**
- Mean latency: 7.729s per image
- Diagnostic 20px-expanded-box score: 9/10 (90.0%); this is not counted as a real click success.

Compared with the fixed-center baseline, strict accuracy stayed at 60% while mean latency increased from 2.67s to 7.729s. The model returned zero or near-zero deltas for most cases; case-05 saturated both limits at `(-62,-37)`. This mode remains experimental and is not the default.

| Case | Task | Point | Hit | Time |
|---|---|---:|---:|---:|
| 01 | Click the blue Save button. | `[812, 487]` | yes | 6.714s |
| 02 | Click the green Continue button. | `[812, 112]` | yes | 9.383s |
| 03 | Click the red Delete button. | `[187, 262]` | no | 11.202s |
| 04 | Click the purple Settings button. | `[63, 111]` | no | 8.653s |
| 05 | Click the orange Submit button. | `[500, 225]` | no | 10.054s |
| 06 | Click the cyan Next button. | `[187, 487]` | yes | 7.387s |
| 07 | Click the blue Download button. | `[812, 262]` | no | 5.103s |
| 08 | Click the gray Cancel button. | `[437, 111]` | yes | 5.644s |
| 09 | Click the green Apply button. | `[437, 487]` | yes | 4.769s |
| 10 | Click the purple Upload button. | `[187, 412]` | yes | 8.378s |

## Failure analysis

| Case | Target box | Point | Outside distance | Coarse/Fine cell |
|---|---:|---:|---:|---:|
| 03 | `[82, 272, 237, 322]` | `[187, 262]` | 10.0px | `9 / 2` |
| 04 | `[78, 88, 248, 138]` | `[63, 111]` | 15.0px | `1 / 5` |
| 05 | `[420, 270, 575, 320]` | `[500, 225]` | 45.0px | `11 / 2` |
| 07 | `[750, 270, 925, 320]` | `[812, 262]` | 8.0px | `12 / 3` |
