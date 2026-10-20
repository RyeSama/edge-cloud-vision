# Stimulus-response bench

Model: `mobilenet_v1_1.0_224_quant`
Samples: 24

| case | n | accept rate | confidence mean | confidence std | p50 ms | p95 ms | accuracy |
|---|---:|---:|---:|---:|---:|---:|---:|
| baseline-1m-500lux | 3 | 0.000 | 0.002 | 0.000 | 9.37 | 10.89 | n/a |
| blur-2px | 3 | 0.000 | 0.002 | 0.000 | 9.32 | 15.73 | n/a |
| bright-1500lux | 3 | 0.000 | 0.002 | 0.000 | 10.48 | 20.75 | n/a |
| dim-80lux | 3 | 0.000 | 0.002 | 0.000 | 10.57 | 15.28 | n/a |
| far-3m | 3 | 0.000 | 0.002 | 0.000 | 8.67 | 11.38 | n/a |
| far-dim-blur | 3 | 0.000 | 0.001 | 0.000 | 11.76 | 14.22 | n/a |
| near-0.5m | 3 | 0.000 | 0.002 | 0.000 | 8.11 | 12.90 | n/a |
| occlusion-30pct | 3 | 0.000 | 0.002 | 0.000 | 8.99 | 10.31 | n/a |

Synthetic cases have no ground-truth label, so `accuracy` is `n/a`;
they measure timing, gate stability, and repeatability, not recognition accuracy.
