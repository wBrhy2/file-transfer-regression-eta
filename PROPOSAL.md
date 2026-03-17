# Regression-based ETA estimation for file transfers

## Problem

Every file transfer tool — rsync, cp, scp, rclone, robocopy — estimates time remaining using some variant of `bytes_remaining / recent_speed`. This produces wildly inaccurate estimates on mixed workloads (millions of small files interleaved with multi-gigabyte media files) because the per-file overhead (metadata lookups, directory creation, fsync) and per-byte transfer cost are fundamentally different, but the estimator conflates them into a single "speed" metric.

## Observed behavior (rsync case study)

When transferring ~920K files totaling ~20TB from a ZFS pool to ext4 drives using rsync `--info=progress2`:

- ETA swings from 8 hours to 60+ hours depending on whether rsync is currently processing small metadata files or large video files
- The estimate never stabilizes because the file size distribution is bimodal

## Why binning, not per-file timing

rsync's internal clock and log output only have 1-second resolution — far too coarse for per-file regression when dozens or hundreds of files complete within the same second. Instead of requiring sub-millisecond timestamps per file (which would add syscall overhead), we aggregate into fixed-width time buckets (e.g., 10 seconds). Each bucket provides one observation of (file_count, bytes, elapsed_time), and the regression operates on these aggregated observations. This works because the per-file overhead appears in the regression as a coefficient — it doesn't need to be measured directly per file. Larger buckets give better precision at the cost of slower warm-up.

## Proposed solution

Decompose the transfer cost into two independent components using online linear regression:

```
time = a * file_count + b * bytes_transferred
```

Where:
- `a` = per-file overhead (seconds/file) — covers stat, open, mkdir, metadata write, close
- `b` = per-byte transfer cost (seconds/byte) — covers pure I/O throughput

Given known remaining file count and remaining bytes, the ETA becomes:

```
ETA = a * files_remaining + b * bytes_remaining
```

## Algorithm

### Data collection

Accumulate observations in fixed-width time buckets (e.g., 10 seconds). Each bucket records:
- `n_i` = number of files completed in bucket `i`
- `s_i` = total bytes transferred in bucket `i`
- `t_i` = bucket width (constant, e.g., 10.0 seconds)

### Online OLS regression (no intercept)

For each bucket, the constraint is:

```
t_i = a * n_i + b * s_i
```

Using ordinary least squares with no intercept (time is fully explained by file count + data volume):

```
[a]       (X^T X)^{-1}  X^T y
[b]   =

where X = [[n_1, s_1], [n_2, s_2], ...],  y = [t_1, t_2, ...]
```

For a 2x2 system this reduces to closed-form expressions that can be updated incrementally without storing all historical buckets:

```
sum_nn += n_i * n_i
sum_ns += n_i * s_i
sum_ss += s_i * s_i
sum_nt += n_i * t_i
sum_st += s_i * t_i

det = sum_nn * sum_ss - sum_ns * sum_ns
a = (sum_ss * sum_nt - sum_ns * sum_st) / det
b = (sum_nn * sum_st - sum_ns * sum_nt) / det
```

This is O(1) per bucket update and O(1) memory — no history buffer needed.

### ETA computation

```
files_remaining = total_files - files_done
bytes_remaining = total_bytes - bytes_done
ETA = max(0, a * files_remaining + b * bytes_remaining)
```

### Write cache saturation detection

Early in a transfer, the OS page cache absorbs writes at RAM speed. Once dirty pages hit `vm.dirty_ratio`, the kernel forces writeback and throughput drops to actual disk speed. Buckets recorded during the cache phase would bias `b` toward an unrealistically low value (inflated throughput), producing an optimistic ETA.

To handle this, track per-bucket throughput (`s_i / t_i`) and a running mean. If a bucket's throughput drops below 50% of the running mean and at least 3 buckets have been recorded, the cache wall has been hit — reset all five running sums to zero and restart the warm-up counter. This fires at most once per transfer (the initial cache saturation) and ensures the regression trains only on steady-state disk I/O.

```
mean_throughput = running_mean(s_i / t_i)
if bucket_throughput < 0.5 * mean_throughput and bucket_count > 3:
    sum_nn = sum_ns = sum_ss = sum_nt = sum_st = 0
    bucket_count = 0
```

Implementation cost: one additional division and comparison per bucket — no buffers, no tuning parameters beyond the 0.5 threshold.

### Warm-up

Until at least 5 buckets have been accumulated (after any cache-saturation reset), fall back to the existing simple throughput estimator. This avoids degenerate regression results from insufficient data.

### Exponential decay (optional)

To adapt to changing conditions (e.g., moving from SSD to HDD mid-transfer, or network congestion changes), apply exponential decay to the running sums:

```
decay = 0.99  # per bucket
sum_nn = sum_nn * decay + n_i * n_i
...
```

This weights recent observations more heavily while retaining long-term signal.

## Empirical validation

On a real 20TB transfer (917,765 files, ZFS → ext4 via local HBA, two parallel rsyncs to separate drives):

### Regression coefficients (from 80 ten-second buckets, ~13 minutes in)

| Metric | Value |
|---|---|
| Per-file overhead (a) | 23.08 ms |
| Pure throughput (1/b) | 230.5 MB/s |

### ETA comparison

| Method | Predicted wall time | Stability |
|---|---|---|
| Naive throughput average | 31-59 hours | Fluctuated throughout |
| Regression-based | 15.0 hours | Stable within ~13 minutes |
| Actual wall time | *(pending — will be recorded at completion)* | — |

The naive estimator conflated per-file overhead with transfer throughput. When the transfer was processing small metadata files (~700 bytes), observed throughput dropped to ~2 MB/s, causing the ETA to spike. When processing large video files (~5 GB), throughput jumped to ~230 MB/s, causing the ETA to plummet. The regression-based estimator was immune to this because it models the two costs independently.

### Breakdown

| Component | Estimated time |
|---|---|
| File overhead (459K files × 23ms) | 2.9 hours |
| Data transfer (~10TB per drive at 230 MB/s) | 12.1 hours |
| **Total** | **15.0 hours** |

## Integration points in rsync

The regression estimator would slot into the existing progress reporting path:

- `progress.c` / `output_summary()` — where current ETA is computed
- Requires access to: files completed (already tracked), bytes transferred (already tracked), total file count (`--info=progress2` already computes this), total bytes (already computed during file list build)
- The 10-second bucket aggregation can use the existing `gettimeofday()` calls in the progress path

## Activation

This should be an explicit opt-in flag, not a replacement for the existing estimator:

```
rsync -av --eta=regression /src/ /dst/
```

Or as an `--info` sub-option:

```
rsync -av --info=progress2,eta2 /src/ /dst/
```

**Rationale:** The regression estimator adds value only for mixed-size workloads (media servers, backups with photos + videos + databases). For uniform transfers (e.g., mirroring a directory of same-sized log files), the existing moving-average ETA is perfectly adequate and simpler. Adding computational overhead and a warm-up period to every transfer is not justified.

The feature requires `--info=progress2` semantics (total file count scan) regardless, since the regression needs `files_remaining` and `bytes_remaining` to produce an ETA.

## Backward compatibility

- Fully opt-in — no change to default `--progress` or `--info=progress2` behavior
- Falls back to existing estimator during warm-up period (first 5 buckets after cache saturation detection)
- No additional syscalls or I/O — uses data already tracked internally
