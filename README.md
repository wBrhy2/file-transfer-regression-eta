# regression-eta

Accurate ETA for file transfers using online linear regression. Decomposes transfer time into per-file overhead and per-byte throughput instead of averaging speed across mixed workloads.

## The problem

Every file transfer tool (rsync, cp, scp, rclone, robocopy) estimates time remaining using some variant of `bytes_remaining / recent_speed`. This breaks badly on mixed workloads -- millions of small config files interleaved with multi-gigabyte video files -- because the per-file overhead (stat, open, mkdir, fsync) and the per-byte I/O cost are fundamentally different. The ETA swings wildly depending on which kind of file the tool happens to be processing right now.

## The solution

Model transfer time as two independent costs:

```
time = a * file_count + b * bytes_transferred
```

Where `a` is the per-file overhead (seconds/file) and `b` is the per-byte transfer cost (seconds/byte). Given known remaining files and bytes:

```
ETA = a * files_remaining + b * bytes_remaining
```

The coefficients are estimated using online OLS regression over fixed-width time buckets (default 10 seconds). The algorithm is O(1) memory and O(1) per update -- five running sums, no history buffer.

## What's here

- **`regression_eta.py`** -- Standalone rsync wrapper with regression-based ETA. Drop-in replacement for `rsync` with live progress display showing decomposed throughput and per-file overhead. The `RegressionETA` class is tool-agnostic and can be embedded in any file transfer tool.
- **`PROPOSAL.md`** -- Full technical write-up of the algorithm, including the math, design decisions (why binning instead of per-file timing), write cache saturation detection, and empirical validation data.

## Usage

```bash
# Basic -- does a dry-run first to count files, then transfers with regression ETA
./regression_eta.py -av --partial /src/ /dst/

# Skip dry-run if you already know the totals
./regression_eta.py --total-files 917765 --total-bytes 21990232555520 -av /src/ /dst/
```

Output:

```
  4.2 TB / 20.0 TB (21.0%)  files: 312,441 / 917,765  ETA: 11h 42m 03s  [230 MB/s, 23.1 ms/file]
```

## Key features

- **Write cache saturation detection** -- Automatically detects when the OS page cache stops absorbing writes and resets the regression to train only on steady-state disk I/O. No tuning required.
- **Exponential decay** -- Optionally weights recent observations more heavily to adapt to changing I/O conditions mid-transfer.
- **Warm-up fallback** -- Falls back to simple throughput estimation for the first 50 seconds while the regression accumulates data.

## Empirical validation

Tested on a 20TB migration (917,765 files, ZFS to ext4, two parallel rsyncs to separate 14TB drives via local HBA):

| Method | Predicted wall time | Stability |
|---|---|---|
| Naive throughput average | 31-59 hours | Fluctuated throughout |
| Regression-based | 15.0 hours | Stable within ~13 minutes |

The regression decomposed the transfer into 2.9 hours of per-file overhead (459K files x 23ms) and 12.1 hours of pure I/O (~10TB per drive at 230 MB/s).

## License

AGPL-3.0. See [LICENSE](LICENSE).

For commercial licensing inquiries, open an issue or contact the maintainer.
