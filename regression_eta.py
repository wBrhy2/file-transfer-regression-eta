#!/usr/bin/env python3
"""
rsync-eta.py — Regression-based ETA wrapper for rsync.

Wraps rsync and provides accurate ETA by decomposing transfer cost into:
  - per-file overhead (metadata, seek, create)
  - per-byte throughput (pure I/O)

Usage:
    rsync-eta.py [rsync args...]

Example:
    rsync-eta.py -av --partial /src/ /dst/
    rsync-eta.py -av --files-from=list.txt /src/ /dst/

Requires rsync to be in PATH. Adds --out-format and --stats automatically
to capture per-file timing data.
"""

import subprocess
import sys
import time
import re
import os
import signal
import select
import fcntl


class RegressionETA:
    """Online OLS regression for ETA: time = a * files + b * bytes."""

    def __init__(self, bucket_width=10.0, warmup_buckets=5, decay=1.0):
        self.bucket_width = bucket_width
        self.warmup_buckets = warmup_buckets
        self.decay = decay

        # Running sums for incremental OLS (no intercept)
        self.sum_nn = 0.0
        self.sum_ns = 0.0
        self.sum_ss = 0.0
        self.sum_nt = 0.0
        self.sum_st = 0.0
        self.bucket_count = 0

        # Write cache saturation detection
        self.throughput_sum = 0.0
        self.cache_saturated = False

        # Current bucket accumulators
        self.bucket_files = 0
        self.bucket_bytes = 0
        self.bucket_start = None

        # Totals
        self.total_files_done = 0
        self.total_bytes_done = 0

        # Coefficients
        self.a = 0.0  # seconds per file
        self.b = 0.0  # seconds per byte

    def add_file(self, size_bytes):
        """Record a completed file transfer."""
        now = time.monotonic()
        if self.bucket_start is None:
            self.bucket_start = now

        self.bucket_files += 1
        self.bucket_bytes += size_bytes
        self.total_files_done += 1
        self.total_bytes_done += size_bytes

        # Check if bucket is full
        elapsed = now - self.bucket_start
        if elapsed >= self.bucket_width:
            self._flush_bucket(elapsed)

    def _flush_bucket(self, elapsed):
        """Close current bucket and update regression."""
        n = self.bucket_files
        s = self.bucket_bytes
        t = elapsed

        if n == 0 and s == 0:
            self.bucket_start = time.monotonic()
            return

        # Write cache saturation detection: if throughput drops below
        # 50% of running mean, the page cache wall has been hit — reset
        # regression sums so we train only on steady-state disk I/O.
        if t > 0 and not self.cache_saturated:
            bucket_throughput = s / t
            if self.bucket_count > 0:
                mean_throughput = self.throughput_sum / self.bucket_count
                if self.bucket_count >= 3 and bucket_throughput < 0.5 * mean_throughput:
                    self.sum_nn = 0.0
                    self.sum_ns = 0.0
                    self.sum_ss = 0.0
                    self.sum_nt = 0.0
                    self.sum_st = 0.0
                    self.bucket_count = 0
                    self.throughput_sum = 0.0
                    self.cache_saturated = True
            self.throughput_sum += bucket_throughput

        # Apply decay to running sums
        self.sum_nn = self.sum_nn * self.decay + n * n
        self.sum_ns = self.sum_ns * self.decay + n * s
        self.sum_ss = self.sum_ss * self.decay + s * s
        self.sum_nt = self.sum_nt * self.decay + n * t
        self.sum_st = self.sum_st * self.decay + s * t
        self.bucket_count += 1

        # Solve 2x2 OLS
        det = self.sum_nn * self.sum_ss - self.sum_ns * self.sum_ns
        if abs(det) > 1e-20:
            self.a = (self.sum_ss * self.sum_nt - self.sum_ns * self.sum_st) / det
            self.b = (self.sum_nn * self.sum_st - self.sum_ns * self.sum_nt) / det
            # Clamp to non-negative
            self.a = max(0.0, self.a)
            self.b = max(0.0, self.b)

        # Reset bucket
        self.bucket_files = 0
        self.bucket_bytes = 0
        self.bucket_start = time.monotonic()

    def eta(self, total_files, total_bytes):
        """Return ETA in seconds, or None if still warming up."""
        if self.bucket_count < self.warmup_buckets:
            return None

        files_remaining = max(0, total_files - self.total_files_done)
        bytes_remaining = max(0, total_bytes - self.total_bytes_done)

        return self.a * files_remaining + self.b * bytes_remaining

    def throughput_mbps(self):
        """Pure transfer throughput in MB/s."""
        if self.b > 0:
            return (1.0 / self.b) / 1e6
        return 0.0

    def overhead_ms(self):
        """Per-file overhead in milliseconds."""
        return self.a * 1000


def format_eta(seconds):
    """Format seconds into human-readable string."""
    if seconds is None:
        return "estimating..."
    if seconds < 0:
        return "unknown"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"{h}h {m:02d}m {s:02d}s"
    elif m > 0:
        return f"{m}m {s:02d}s"
    else:
        return f"{s}s"


def format_bytes(b):
    """Format bytes into human-readable string."""
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} PB"


def make_nonblocking(fd):
    """Set file descriptor to non-blocking mode."""
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)


def run_rsync(args, total_files=None, total_bytes=None):
    """Run rsync with regression-based ETA display."""

    # Inject --out-format to get per-file size info
    # %l = file length, %n = filename
    rsync_args = ["rsync", "--out-format=%l %n"] + args

    # If totals not provided, do a dry-run first to count
    if total_files is None or total_bytes is None:
        print("Scanning files for totals (dry-run)...")
        dry_args = ["rsync", "--dry-run", "--stats"] + args
        result = subprocess.run(dry_args, capture_output=True, text=True)
        for line in result.stdout.splitlines():
            m = re.match(r"Number of regular files transferred:\s+([\d,]+)", line)
            if m:
                total_files = int(m.group(1).replace(",", ""))
            m = re.match(r"Total file size:\s+([\d,]+)", line)
            if m:
                total_bytes = int(m.group(1).replace(",", ""))
            m = re.match(r"Total transferred file size:\s+([\d,]+)", line)
            if m:
                total_bytes = int(m.group(1).replace(",", ""))

        if total_files is None or total_bytes is None:
            print("Warning: could not determine totals from dry-run, ETA will be unavailable")
            total_files = total_files or 0
            total_bytes = total_bytes or 0
        else:
            print(f"Found {total_files:,} files, {format_bytes(total_bytes)}")

    estimator = RegressionETA(bucket_width=10.0, warmup_buckets=5, decay=0.995)
    last_display = 0
    display_interval = 2.0  # update display every 2s

    proc = subprocess.Popen(
        rsync_args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )

    make_nonblocking(proc.stdout.fileno())
    make_nonblocking(proc.stderr.fileno())

    buf = b""

    try:
        while proc.poll() is None:
            readable, _, _ = select.select(
                [proc.stdout, proc.stderr], [], [], 0.5
            )

            for stream in readable:
                try:
                    chunk = stream.read(65536)
                    if chunk:
                        if stream == proc.stdout:
                            buf += chunk
                        else:
                            sys.stderr.buffer.write(chunk)
                            sys.stderr.buffer.flush()
                except (BlockingIOError, IOError):
                    pass

            # Parse complete lines from buffer
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line_str = line.decode("utf-8", errors="replace").strip()

                # Parse --out-format=%l %n output
                m = re.match(r"^(\d+)\s+(.+)$", line_str)
                if m:
                    size = int(m.group(1))
                    estimator.add_file(size)

            # Periodic display
            now = time.monotonic()
            if now - last_display >= display_interval:
                last_display = now
                eta = estimator.eta(total_files, total_bytes)
                pct = (
                    (estimator.total_bytes_done / total_bytes * 100)
                    if total_bytes > 0
                    else 0
                )

                status = (
                    f"\r  {format_bytes(estimator.total_bytes_done)} / "
                    f"{format_bytes(total_bytes)} ({pct:.1f}%)  "
                    f"files: {estimator.total_files_done:,} / {total_files:,}  "
                    f"ETA: {format_eta(eta)}  "
                )

                if estimator.bucket_count >= estimator.warmup_buckets:
                    status += (
                        f"[{estimator.throughput_mbps():.0f} MB/s, "
                        f"{estimator.overhead_ms():.1f} ms/file]"
                    )

                sys.stderr.write(status)
                sys.stderr.flush()

    except KeyboardInterrupt:
        proc.send_signal(signal.SIGINT)
        proc.wait()
        print("\nInterrupted.")
        return 1

    # Drain remaining output
    remaining = proc.stdout.read()
    if remaining:
        buf += remaining
    remaining = proc.stderr.read()
    if remaining:
        sys.stderr.buffer.write(remaining)

    proc.wait()
    print(f"\n\nDone. Exit code: {proc.returncode}")
    print(f"  Files: {estimator.total_files_done:,}")
    print(f"  Bytes: {format_bytes(estimator.total_bytes_done)}")
    if estimator.bucket_count >= estimator.warmup_buckets:
        print(f"  Throughput: {estimator.throughput_mbps():.0f} MB/s")
        print(f"  Per-file overhead: {estimator.overhead_ms():.1f} ms")

    return proc.returncode


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    args = sys.argv[1:]

    # Check for --total-files and --total-bytes flags (ours, not rsync's)
    total_files = None
    total_bytes = None
    filtered_args = []
    i = 0
    while i < len(args):
        if args[i] == "--total-files" and i + 1 < len(args):
            total_files = int(args[i + 1])
            i += 2
        elif args[i] == "--total-bytes" and i + 1 < len(args):
            total_bytes = int(args[i + 1])
            i += 2
        else:
            filtered_args.append(args[i])
            i += 1

    sys.exit(run_rsync(filtered_args, total_files, total_bytes))


if __name__ == "__main__":
    main()
