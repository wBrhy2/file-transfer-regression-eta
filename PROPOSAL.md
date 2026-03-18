# Regression-based ETA estimation for file transfers

## Problem

Every file transfer tool in common use - rsync, cp, scp, rclone, robocopy - estimates time remaining using some variant of `bytes_remaining / recent_speed`. This is a single-variable model, and it falls apart the moment the workload isn't uniform. The reason is straightforward: transferring a file has two fundamentally different costs - a per-file overhead (stat, open, mkdir, metadata write, fsync, close) and a per-byte I/O cost - and these tools conflate them into one number they call "speed."

The result is that anyone who has ever migrated a media server, backed up a photo library, or moved a mixed dataset of any meaningful size has watched the ETA swing from 8 hours to 60 hours and back again, depending entirely on whether the tool happened to be processing tiny metadata files or multi-gigabyte video files at that moment. The estimate never converges because it's modeling a two-dimensional problem with one variable.

## Observed behavior (rsync case study)

A concrete example: transferring ~920K files totaling ~20TB from a ZFS pool to ext4 drives using rsync with `--info=progress2`. The file size distribution is sharply bimodal - hundreds of thousands of files under 1KB (thumbnails, NFO files, subtitles, playlists) alongside thousands of files in the 1-50GB range (video).

When rsync hits a run of small files, observed throughput drops to ~2 MB/s - not because the disk is slow, but because the bottleneck is per-file overhead, and the estimator doesn't know that. The ETA spikes to 60+ hours. Minutes later, it hits a large video file, throughput jumps to 230 MB/s, and the ETA plummets to 8 hours. Neither number is remotely accurate, because neither reflects what's actually left to do.

## Why binning, not per-file timing

The obvious approach would be to time each file individually and regress on that. But rsync's internal clock and log output have 1-second resolution - far too coarse when dozens or hundreds of files complete within the same second. Requiring sub-millisecond per-file timestamps would add syscall overhead to every file operation, which is precisely the kind of cost you want to avoid in a tool optimized for throughput.

The alternative is to aggregate into fixed-width time buckets (default: 10 seconds). Each bucket yields one observation of (files_completed, bytes_transferred, elapsed_time), and the regression operates on these aggregated observations. This works because the per-file overhead surfaces as a regression coefficient - it doesn't need to be measured directly per file. The tradeoff is that larger buckets improve precision at the cost of slower warm-up, but in practice 10 seconds is a good balance; the estimator stabilizes within a few minutes.

## Proposed solution

Decompose the transfer cost into two independent components using online linear regression:

```
time = a * file_count + b * bytes_transferred
```

Where:
- `a` = per-file overhead (seconds/file) - the cost of handling each file regardless of size
- `b` = per-byte transfer cost (seconds/byte) - the cost of moving raw data

Given known remaining file count and remaining bytes, the ETA becomes:

```
ETA = a * files_remaining + b * bytes_remaining
```

This is the core insight: the ETA is no longer a function of one variable (speed) but of two (per-file cost and per-byte cost), which is what the physical process actually looks like.

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

Using ordinary least squares with no intercept - time is fully explained by file count plus data volume, so there is no constant term to absorb:

```
[a]       (X^T X)^{-1}  X^T y
[b]   =

where X = [[n_1, s_1], [n_2, s_2], ...],  y = [t_1, t_2, ...]
```

For a 2x2 system this reduces to closed-form expressions that can be updated incrementally. Five running sums, no history buffer:

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

O(1) per bucket update. O(1) memory. No matrices, no history buffer, no allocations.

### ETA computation

```
files_remaining = total_files - files_done
bytes_remaining = total_bytes - bytes_done
ETA = max(0, a * files_remaining + b * bytes_remaining)
```

### Write cache saturation detection

Early in a transfer, the OS page cache absorbs writes at RAM speed - the kernel is happy to accept data into dirty pages as fast as the source can provide it. Once dirty pages hit `vm.dirty_ratio`, the kernel forces writeback and throughput drops to actual disk speed. This transition is abrupt and dramatic. If the regression trains on buckets recorded during the cache phase, the per-byte coefficient `b` gets biased toward an unrealistically low value (inflated throughput), and the ETA ends up optimistic by a wide margin.

The detection is simple: track per-bucket throughput (`s_i / t_i`) and maintain a running mean. If a bucket's throughput drops below 50% of the running mean and at least 3 buckets have been recorded, the cache wall has been hit. Reset all five running sums to zero and restart the warm-up counter. This fires at most once per transfer - the initial cache saturation event - and ensures the regression trains only on steady-state disk I/O.

```
mean_throughput = running_mean(s_i / t_i)
if bucket_throughput < 0.5 * mean_throughput and bucket_count > 3:
    sum_nn = sum_ns = sum_ss = sum_nt = sum_st = 0
    bucket_count = 0
```

Implementation cost: one additional division and comparison per bucket. No buffers, no tuning parameters beyond the 0.5 threshold.

### Warm-up

Until at least 5 buckets have been accumulated (after any cache-saturation reset), the regression doesn't have enough data to be reliable. During this period, fall back to the traditional simple throughput estimator. This avoids degenerate results from an underdetermined system and gives the user something reasonable to look at while the regression accumulates signal.

### Exponential decay (optional)

Transfer conditions can change mid-flight - a network link gets congested, the transfer crosses from SSD to spinning rust, or the file size distribution shifts as it moves through different directory subtrees. To handle this, apply exponential decay to the running sums:

```
decay = 0.99  # per bucket
sum_nn = sum_nn * decay + n_i * n_i
...
```

This weights recent observations more heavily while retaining long-term signal. The decay factor is conservative enough that it doesn't throw away useful history, but responsive enough to adapt within a few minutes if conditions change materially.

### Spanning file redistribution

There's a problem the basic bucketing doesn't handle. When rsync transfers a file large enough to take longer than one bucket width - a 5GB video at 230 MB/s takes about 22 seconds, spanning at least two full buckets - the buckets during that transfer record zero files and zero bytes. The tool only learns about the file when rsync reports it as complete, at which point all the credit lands in a single completion bucket. The result is a run of dead-zone buckets followed by a spike, and both distort the regression.

The fix is to detect these spanning files and redistribute their cost across the time they actually occupied.

For each run of consecutive zero-file buckets, the span is the zero-run plus the completion bucket (the first non-zero bucket that follows). If the run is 3 zeros long, the span is 4 buckets. The algorithm estimates the spanning file's size by looking at the completion bucket's contents: it has N files and B total bytes, but only one of those files is the large one that caused the zero-run. The other N - 1 files are normal-sized, so their contribution can be estimated from the median bytes-per-file of the 10 nearest non-zero buckets on each side (excluding the entire span, since the completion bucket would dilute the estimate with the large file's bytes).

```
other_bytes = (N - 1) * neighbor_median_bpf
large_file_bytes = clamp(B - other_bytes, 0, B)
```

Then redistribute: spread 1 file and `large_file_bytes` evenly across all buckets in the span. Each bucket gets `1 / n_span` files and `large_file_bytes / n_span` bytes. The completion bucket loses 1 file and `large_file_bytes` from its total, then gets back its `1/n_span` share. The remaining N - 1 files and their bytes in the completion bucket stay untouched.

This preserves totals exactly - no files or bytes are created or destroyed, just moved. Every zero bucket is eliminated, and file counts become fractional, which is correct for the regression: a bucket with 0.25 files represents 25% of a file's transfer happening in that time window.

The implementation requires buffering raw buckets rather than feeding them directly into the regression sums. When a non-zero bucket arrives after a zero-run, the span is detected, redistribution is applied, and all corrected buckets are fed into the regression. When a non-zero bucket follows another non-zero bucket (the common case - no spanning file), it feeds through immediately with no delay. A small history of recent non-zero buckets is maintained for the neighbor median calculation.

During a large file transfer (the zero-run), the regression receives no new data - but this is actually better than the alternative, because there is genuinely no new information available until the file completes. The ETA display continues showing the last estimate, which is stable by construction, and updates once the span is redistributed.

## Empirical validation

Tested on a real-world 20TB migration: 917,765 files transferred from a ZFS pool to ext4 via local HBA, split across two parallel rsyncs writing to separate 14TB drives.

### Regression coefficients (from 80 ten-second buckets, ~13 minutes into the transfer)

| Metric | Value |
|---|---|
| Per-file overhead (a) | 23.08 ms |
| Pure throughput (1/b) | 230.5 MB/s |

These coefficients are physically meaningful. The 23ms per-file overhead aligns with what you'd expect for ext4 metadata operations on spinning disk (stat + open + write metadata + close + directory sync). The 230 MB/s throughput matches the sustained sequential write speed of the target drives as measured independently.

### ETA comparison

| Method | Predicted wall time | Stability |
|---|---|---|
| Naive throughput average | 31-59 hours | Fluctuated throughout |
| Regression-based | 15.0 hours | Stable within ~13 minutes |

The naive estimator conflated per-file overhead with transfer throughput. When processing small metadata files (~700 bytes), observed throughput dropped to ~2 MB/s, causing the ETA to spike. When processing large video files (~5 GB), throughput jumped to ~230 MB/s, causing the ETA to plummet. The regression-based estimator was immune to this because it models the two costs independently - it doesn't care what kind of file is being processed right now, only what the overall ratio of files to bytes looks like in aggregate.

### Decomposition

| Component | Estimated time |
|---|---|
| File overhead (459K files × 23ms) | 2.9 hours |
| Data transfer (~10TB per drive at 230 MB/s) | 12.1 hours |
| **Total** | **15.0 hours** |

This decomposition is itself useful. It tells you immediately that about 19% of the total transfer time is pure overhead - not I/O, not throughput-limited, just the cost of handling individual files. If you were looking to optimize, you'd know exactly where to focus.

## Integration points (rsync)

For rsync specifically, the regression estimator would slot into the existing progress reporting path with minimal disruption:

- `progress.c` / `output_summary()` - where the current ETA is computed
- All required inputs are already tracked internally: files completed, bytes transferred, total file count (computed by `--info=progress2`), and total bytes (computed during file list build)
- The 10-second bucket aggregation can reuse the existing `gettimeofday()` calls in the progress path

No new syscalls. No new I/O. Just arithmetic on numbers rsync is already tracking.

## Activation

This should be an explicit opt-in, not a replacement for the existing estimator:

```
rsync -av --eta=regression /src/ /dst/
```

Or as an `--info` sub-option:

```
rsync -av --info=progress2,eta2 /src/ /dst/
```

The rationale is simple: the regression estimator adds real value only for mixed-size workloads - media servers, backup sets with photos and videos and databases, NAS migrations. For uniform transfers (mirroring a directory of identically-sized log files, for instance), the existing moving-average ETA is perfectly adequate and has no warm-up period. There's no reason to add computational overhead and a convergence delay to every transfer when most transfers don't need it.

The feature requires `--info=progress2` semantics regardless, since the regression needs `files_remaining` and `bytes_remaining` - both of which require the total file count scan that `progress2` already performs.

## Backward compatibility

- Fully opt-in - no change to default `--progress` or `--info=progress2` behavior
- Falls back to the existing estimator during warm-up (first 5 buckets after cache saturation detection)
- No additional syscalls or I/O - operates on data already tracked internally
