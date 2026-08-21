# Debug benchmark result

- Model: `minicpm-v4.6:latest`
- Accuracy: **6/10 (60.0%)**
- Mean latency: 2.67s per image
- All four misses were near misses: 17px, 10px, 8px, and 8px outside the target box.
- With a diagnostic 20px expanded target box the score is 10/10, but this is not counted as a real click success.

| Case | Task | Point | Hit | Time |
|---|---|---:|---:|---:|
| 01 | Click the blue Save button. | `[812, 487]` | yes | 2.361s |
| 02 | Click the green Continue button. | `[937, 112]` | no | 1.965s |
| 03 | Click the red Delete button. | `[187, 262]` | no | 3.46s |
| 04 | Click the purple Settings button. | `[187, 112]` | yes | 2.028s |
| 05 | Click the orange Submit button. | `[562, 262]` | no | 2.158s |
| 06 | Click the cyan Next button. | `[187, 487]` | yes | 4.4s |
| 07 | Click the blue Download button. | `[812, 262]` | no | 2.074s |
| 08 | Click the gray Cancel button. | `[437, 112]` | yes | 2.94s |
| 09 | Click the green Apply button. | `[437, 487]` | yes | 2.421s |
| 10 | Click the purple Upload button. | `[187, 412]` | yes | 2.893s |

## Failure analysis

| Case | Target box | Point | Outside distance | Coarse/Fine cell |
|---|---:|---:|---:|---:|
| 02 | `[755,88,920,138]` | `[937,112]` | 17px | `8 / 4` |
| 03 | `[82,272,237,322]` | `[187,262]` | 10px | `9 / 2` |
| 05 | `[420,270,575,320]` | `[562,262]` | 8px | `11 / 2` |
| 07 | `[750,270,925,320]` | `[812,262]` | 8px | `12 / 3` |

All misses selected the correct semantic target neighborhood. The remaining error comes from using the center of a 125×75 fine cell when the target crosses a fine-cell boundary.
