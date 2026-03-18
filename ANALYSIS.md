# Analysis

## Dataset

Source: `data/combined_buckets_interpolated.csv`

This file contains 10-second time buckets from a real 20TB NAS migration - two parallel rsyncs (one per destination drive) transferring from a ZFS pool to ext4. The two disk streams are combined into a single time series. Spanning file redistribution has been applied (see PROPOSAL.md) so that large files that took multiple buckets to transfer have their bytes and file counts spread across the buckets they actually occupied.

Columns:
- `bucket` - sequential bucket index
- `time_end` - wall time at bucket close (seconds from transfer start)
- `files_combined` - files completed in this bucket (fractional after redistribution)
- `bytes_combined` - bytes transferred in this bucket

The dataset excludes a ~1 hour pause (machine crash and recovery mid-transfer). Bucket timestamps are continuous - the pause has been squished out.

## Transfer summary

| Metric | Value |
|---|---|
| Total files | 910,454 |
| Total bytes | 20.72 TB |
| Active transfer time | 56,995s (15.83h) |
| Bucket width | 10s |
| Total buckets | 5,700 |
