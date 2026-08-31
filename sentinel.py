#!/usr/bin/env python3

"""
StorageSentinel
===============

Server storage monitoring and controlled cleanup.

Commands:

    ./sentinel.py status
    ./sentinel.py report
    ./sentinel.py scan
    ./sentinel.py pending
    ./sentinel.py delete
    ./sentinel.py history

Design:

    status
        Fast system-health snapshot.

    report
        Read-only storage report.
        Does NOT perform a full filesystem scan.

    scan
        Expensive deep scan.
        Finds duplicates, trash, caches, temporary files,
        and old logs.
        Writes candidates to pending_actions.csv.

    pending
        Interactive approval/rejection of candidates.

    delete
        Deletes ONLY explicitly approved candidates.

Safety:
    - Virtual filesystems are excluded.
    - Protected paths cannot be deleted.
    - Protected extensions cannot be deleted.
    - Deletion requires explicit approval.
    - Files are revalidated before deletion.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import logging
import os
import platform
import shutil
import socket
import stat
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML is required.")
    print("Install with:")
    print("    python3 -m pip install --user pyyaml")
    sys.exit(1)


# ============================================================
# Paths
# ============================================================

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = SCRIPT_DIR / "config.yaml"


# ============================================================
# Utility
# ============================================================

def now_string() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def format_bytes(value: int) -> str:
    if value < 1024:
        return f"{value} B"

    units = ["KB", "MB", "GB", "TB", "PB"]
    size = float(value)

    for unit in units:
        size /= 1024.0
        if size < 1024:
            return f"{size:.2f} {unit}"

    return f"{size:.2f} EB"


def format_gb(value: int) -> str:
    return f"{value / (1024 ** 3):.2f} GB"


def safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def safe_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value

    if isinstance(value, str):
        return value.lower() in ("1", "true", "yes", "on")

    if value is None:
        return default

    return bool(value)


# ============================================================
# Configuration
# ============================================================

class Config:
    def __init__(self, path: Path):
        self.path = path

        if not path.exists():
            raise FileNotFoundError(
                f"Configuration file not found: {path}"
            )

        with path.open("r", encoding="utf-8") as f:
            self.data = yaml.safe_load(f) or {}

        self.server = self.data.get("server", {})
        self.report = self.data.get("report", {})
        self.scan = self.data.get("scan", {})
        self.cleanup = self.data.get("cleanup", {})
        self.logging = self.data.get("logging", {})

        self.scan_root = Path(
            self.server.get("scan_root", "/")
        ).resolve()

        self.excluded_paths = [
            Path(p).resolve()
            for p in self.server.get("excluded_paths", [])
        ]

        self.protected_paths = [
            Path(p).resolve()
            for p in self.server.get("protected_paths", [])
        ]

        self.excluded_filesystems = set(
            str(x).lower()
            for x in self.server.get("excluded_filesystems", [])
        )

        self.protected_extensions = {
            str(x).lower()
            for x in self.cleanup.get(
                "protected_extensions", []
            )
        }

        self.largest_files = safe_int(
            self.report.get("largest_files", 20),
            20,
        )

        self.largest_folders = safe_int(
            self.report.get("largest_folders", 20),
            20,
        )

        self.folder_depth = safe_int(
            self.report.get("folder_depth", 2),
            2,
        )

        self.minimum_file_size = (
            safe_int(
                self.report.get(
                    "minimum_file_size_mb",
                    1,
                ),
                1,
            )
            * 1024
            * 1024
        )

        self.duplicate_min_size = (
            safe_int(
                self.scan.get(
                    "duplicate_min_size_mb",
                    10,
                ),
                10,
            )
            * 1024
            * 1024
        )

        self.partial_hash_bytes = safe_int(
            self.scan.get(
                "partial_hash_bytes",
                1048576,
            ),
            1048576,
        )

        self.max_duplicate_file_gb = safe_int(
            self.scan.get(
                "max_duplicate_file_gb",
                0,
            ),
            0,
        )

        self.old_log_days = safe_int(
            self.scan.get(
                "old_log_days",
                30,
            ),
            30,
        )

        self.cold_data_days = safe_int(
            self.scan.get(
                "cold_data_days",
                180,
            ),
            180,
        )

        self.history_file = SCRIPT_DIR / self.logging.get(
            "history_file",
            "history.log",
        )

        self.report_file = SCRIPT_DIR / self.logging.get(
            "report_file",
            "storage_report.txt",
        )

        self.pending_file = SCRIPT_DIR / self.logging.get(
            "pending_file",
            "pending_actions.csv",
        )

        self.log_file = SCRIPT_DIR / self.logging.get(
            "log_file",
            "sentinel.log",
        )

    def is_excluded(self, path: Path) -> bool:
        try:
            resolved = path.resolve(strict=False)
        except OSError:
            resolved = path

        for excluded in self.excluded_paths:
            try:
                resolved.relative_to(excluded)
                return True
            except ValueError:
                continue

        return False

    def is_protected(self, path: Path) -> bool:
        try:
            resolved = path.resolve(strict=False)
        except OSError:
            resolved = path

        for protected in self.protected_paths:
            try:
                resolved.relative_to(protected)
                return True
            except ValueError:
                continue

        return False

    def is_protected_extension(self, path: Path) -> bool:
        return path.suffix.lower() in self.protected_extensions


# ============================================================
# Logging
# ============================================================

def setup_logging(config: Config) -> logging.Logger:
    logger = logging.getLogger("StorageSentinel")
    logger.setLevel(logging.INFO)

    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s"
    )

    file_handler = logging.FileHandler(
        config.log_file,
        encoding="utf-8",
    )

    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    return logger


# ============================================================
# System information
# ============================================================

def get_cpu_usage() -> float:
    try:
        with open("/proc/stat", "r") as f:
            first = f.readline().split()

        if len(first) < 5:
            return 0.0

        values1 = list(map(int, first[1:]))
        idle1 = values1[3] + (
            values1[4] if len(values1) > 4 else 0
        )
        total1 = sum(values1)

        time.sleep(0.2)

        with open("/proc/stat", "r") as f:
            second = f.readline().split()

        values2 = list(map(int, second[1:]))
        idle2 = values2[3] + (
            values2[4] if len(values2) > 4 else 0
        )
        total2 = sum(values2)

        total_delta = total2 - total1
        idle_delta = idle2 - idle1

        if total_delta <= 0:
            return 0.0

        return (
            100.0
            * (total_delta - idle_delta)
            / total_delta
        )

    except Exception:
        return 0.0


def get_memory() -> Dict[str, float]:
    result = {
        "total": 0,
        "available": 0,
        "used": 0,
        "swap_total": 0,
        "swap_free": 0,
        "swap_used": 0,
    }

    try:
        with open("/proc/meminfo") as f:
            for line in f:
                parts = line.split()

                if len(parts) < 2:
                    continue

                key = parts[0].rstrip(":")
                value = safe_int(parts[1]) * 1024

                if key == "MemTotal":
                    result["total"] = value

                elif key == "MemAvailable":
                    result["available"] = value

                elif key == "SwapTotal":
                    result["swap_total"] = value

                elif key == "SwapFree":
                    result["swap_free"] = value

        result["used"] = (
            result["total"]
            - result["available"]
        )

        result["swap_used"] = (
            result["swap_total"]
            - result["swap_free"]
        )

    except Exception:
        pass

    return result


def get_load_average() -> Tuple[float, float, float]:
    try:
        values = os.getloadavg()
        return values[0], values[1], values[2]
    except Exception:
        return 0.0, 0.0, 0.0


def get_uptime() -> str:
    try:
        with open("/proc/uptime") as f:
            seconds = int(float(f.readline().split()[0]))

        days = seconds // 86400
        hours = (seconds % 86400) // 3600
        minutes = (seconds % 3600) // 60
        secs = seconds % 60

        return (
            f"{days}d {hours}h "
            f"{minutes}m {secs}s"
        )

    except Exception:
        return "Unknown"


# ============================================================
# Filesystem information
# ============================================================

@dataclass
class FilesystemInfo:
    mountpoint: str
    filesystem: str
    fstype: str
    total: int
    used: int
    free: int
    percent: float


def get_filesystems(
    config: Config,
) -> List[FilesystemInfo]:

    filesystems = []

    try:
        result = subprocess.run(
            [
                "findmnt",
                "-rn",
                "-o",
                "TARGET,SOURCE,FSTYPE",
            ],
            capture_output=True,
            text=True,
            check=False,
        )

        if result.returncode != 0:
            return filesystems

        seen = set()

        for line in result.stdout.splitlines():

            parts = line.split(None, 2)

            if len(parts) != 3:
                continue

            mountpoint, source, fstype = parts

            if mountpoint in seen:
                continue

            seen.add(mountpoint)

            if fstype.lower() in config.excluded_filesystems:
                continue

            try:
                usage = shutil.disk_usage(
                    mountpoint
                )

                percent = (
                    usage.used / usage.total * 100
                    if usage.total
                    else 0.0
                )

                filesystems.append(
                    FilesystemInfo(
                        mountpoint=mountpoint,
                        filesystem=source,
                        fstype=fstype,
                        total=usage.total,
                        used=usage.used,
                        free=usage.free,
                        percent=percent,
                    )
                )

            except OSError:
                continue

    except FileNotFoundError:
        pass

    return filesystems


def get_root_disk_usage(
    config: Config,
) -> Tuple[int, int, int, float]:

    usage = shutil.disk_usage(
        config.scan_root
    )

    percent = (
        usage.used / usage.total * 100
        if usage.total
        else 0.0
    )

    return (
        usage.total,
        usage.used,
        usage.free,
        percent,
    )


# ============================================================
# Fast folder sizing
# ============================================================

def directory_size(
    path: Path,
    config: Config,
) -> int:

    total = 0

    try:
        with os.scandir(path) as entries:

            for entry in entries:

                try:
                    if config.is_excluded(
                        Path(entry.path)
                    ):
                        continue

                    if entry.is_symlink():
                        continue

                    if entry.is_file(
                        follow_symlinks=False
                    ):
                        total += entry.stat(
                            follow_symlinks=False
                        ).st_size

                    elif entry.is_dir(
                        follow_symlinks=False
                    ):
                        total += directory_size(
                            Path(entry.path),
                            config,
                        )

                except (
                    PermissionError,
                    FileNotFoundError,
                    OSError,
                ):
                    continue

    except (
        PermissionError,
        FileNotFoundError,
        OSError,
    ):
        return 0

    return total


def get_top_level_folders(
    config: Config,
) -> List[Tuple[int, Path]]:

    results = []

    try:
        with os.scandir(
            config.scan_root
        ) as entries:

            for entry in entries:

                path = Path(entry.path)

                if not entry.is_dir(
                    follow_symlinks=False
                ):
                    continue

                if config.is_excluded(path):
                    continue

                try:
                    size = directory_size(
                        path,
                        config,
                    )

                    results.append(
                        (size, path)
                    )

                except OSError:
                    continue

    except (
        PermissionError,
        OSError,
    ):
        pass

    results.sort(
        key=lambda x: x[0],
        reverse=True,
    )

    return results[
        : config.largest_folders
    ]


# ============================================================
# Fast largest-file scan
# ============================================================

def iter_files(
    root: Path,
    config: Config,
) -> Iterable[Path]:

    stack = [root]

    while stack:

        current = stack.pop()

        if config.is_excluded(current):
            continue

        try:
            with os.scandir(current) as entries:

                for entry in entries:

                    path = Path(entry.path)

                    try:

                        if config.is_excluded(path):
                            continue

                        if entry.is_symlink():
                            continue

                        if entry.is_file(
                            follow_symlinks=False
                        ):
                            yield path

                        elif entry.is_dir(
                            follow_symlinks=False
                        ):
                            stack.append(path)

                    except (
                        PermissionError,
                        FileNotFoundError,
                        OSError,
                    ):
                        continue

        except (
            PermissionError,
            FileNotFoundError,
            OSError,
        ):
            continue


def get_largest_files(
    config: Config,
) -> List[Tuple[int, Path]]:

    results = []

    for path in iter_files(
        config.scan_root,
        config,
    ):

        try:
            size = path.stat(
                follow_symlinks=False
            ).st_size

            if size < config.minimum_file_size:
                continue

            results.append(
                (size, path)
            )

            results.sort(
                key=lambda x: x[0],
                reverse=True,
            )

            if len(results) > config.largest_files:
                results.pop()

        except (
            PermissionError,
            FileNotFoundError,
            OSError,
        ):
            continue

    return results


# ============================================================
# File categorization
# ============================================================

def is_trash_path(path: Path) -> bool:
    text = str(path).lower()

    return (
        "/.local/share/trash/"
        in text
        or text.endswith(
            "/.local/share/trash"
        )
    )


def is_cache_path(path: Path) -> bool:
    text = str(path).lower()

    cache_patterns = (
        "/.cache/",
        "/.cache",
        "/cache/",
        "/pip/cache/",
        "/.npm/",
        "/.gradle/caches/",
        "/.cargo/registry/cache/",
        "/pkgs/",
    )

    return any(
        pattern in text
        for pattern in cache_patterns
    )


def is_temp_path(path: Path) -> bool:
    text = str(path).lower()

    return (
        text.startswith("/tmp/")
        or text.startswith("/var/tmp/")
        or "/.cache/" in text
    )


def is_old_log(
    path: Path,
    config: Config,
) -> bool:

    if path.suffix.lower() != ".log":
        return False

    try:
        age = (
            time.time()
            - path.stat(
                follow_symlinks=False
            ).st_mtime
        )

        return (
            age
            >= config.old_log_days * 86400
        )

    except (
        PermissionError,
        FileNotFoundError,
        OSError,
    ):
        return False


# ============================================================
# Duplicate detection
# ============================================================

def partial_hash(
    path: Path,
    amount: int,
) -> Optional[str]:

    try:
        hasher = hashlib.sha256()

        with path.open(
            "rb",
            buffering=1024 * 1024,
        ) as f:

            data = f.read(amount)

            hasher.update(data)

        return hasher.hexdigest()

    except (
        PermissionError,
        FileNotFoundError,
        OSError,
    ):
        return None


def full_hash(
    path: Path,
) -> Optional[str]:

    try:
        hasher = hashlib.sha256()

        with path.open(
            "rb",
            buffering=1024 * 1024,
        ) as f:

            while True:

                block = f.read(
                    8 * 1024 * 1024
                )

                if not block:
                    break

                hasher.update(block)

        return hasher.hexdigest()

    except (
        PermissionError,
        FileNotFoundError,
        OSError,
    ):
        return None


# ============================================================
# Pending action
# ============================================================

@dataclass
class Candidate:
    action_id: int
    action_type: str
    path: str
    size: int
    risk: str
    reason: str
    approved: bool = False
    deleted: bool = False


class PendingManager:

    FIELDNAMES = [
        "id",
        "type",
        "path",
        "size_bytes",
        "risk",
        "reason",
        "approved",
        "deleted",
    ]

    def __init__(
        self,
        path: Path,
    ):
        self.path = path

    def save(
        self,
        candidates: List[Candidate],
    ):

        temp = self.path.with_suffix(
            ".tmp"
        )

        with temp.open(
            "w",
            newline="",
            encoding="utf-8",
        ) as f:

            writer = csv.DictWriter(
                f,
                fieldnames=self.FIELDNAMES,
            )

            writer.writeheader()

            for candidate in candidates:

                writer.writerow(
                    {
                        "id":
                            candidate.action_id,
                        "type":
                            candidate.action_type,
                        "path":
                            candidate.path,
                        "size_bytes":
                            candidate.size,
                        "risk":
                            candidate.risk,
                        "reason":
                            candidate.reason,
                        "approved":
                            int(candidate.approved),
                        "deleted":
                            int(candidate.deleted),
                    }
                )

        temp.replace(self.path)

    def load(self) -> List[Candidate]:

        if not self.path.exists():
            return []

        candidates = []

        try:

            with self.path.open(
                "r",
                newline="",
                encoding="utf-8",
            ) as f:

                reader = csv.DictReader(f)

                for row in reader:

                    candidates.append(
                        Candidate(
                            action_id=safe_int(
                                row.get("id")
                            ),
                            action_type=
                                row.get(
                                    "type",
                                    "",
                                ),
                            path=
                                row.get(
                                    "path",
                                    "",
                                ),
                            size=safe_int(
                                row.get(
                                    "size_bytes"
                                )
                            ),
                            risk=
                                row.get(
                                    "risk",
                                    "",
                                ),
                            reason=
                                row.get(
                                    "reason",
                                    "",
                                ),
                            approved=
                                row.get(
                                    "approved",
                                    "0",
                                )
                                == "1",
                            deleted=
                                row.get(
                                    "deleted",
                                    "0",
                                )
                                == "1",
                        )
                    )

        except (
            OSError,
            csv.Error,
        ):
            return []

        return candidates


# ============================================================
# Report
# ============================================================

def print_header(title: str):
    print("=" * 78)
    print(
        f"{title:^78}"
    )
    print("=" * 78)


def run_status(config: Config):

    cpu = get_cpu_usage()
    memory = get_memory()

    total, used, free, percent = (
        get_root_disk_usage(config)
    )

    print()
    print_header(
        "StorageSentinel Status"
    )

    print(
        f"Hostname:          {socket.gethostname()}"
    )

    print(
        f"CPU:               {cpu:.1f}%"
    )

    print(
        f"RAM:               "
        f"{format_gb(memory['used'])} / "
        f"{format_gb(memory['total'])}"
    )

    print(
        f"Swap:              "
        f"{format_gb(memory['swap_used'])} / "
        f"{format_gb(memory['swap_total'])}"
    )

    print(
        f"Disk:              "
        f"{format_gb(used)} / "
        f"{format_gb(total)}"
    )

    print(
        f"Disk utilization:  "
        f"{percent:.2f}%"
    )

    print(
        f"Scan root:         "
        f"{config.scan_root}"
    )

    print(
        f"Config:            "
        f"{config.path}"
    )

    print()


def run_report(
    config: Config,
    logger: logging.Logger,
):

    logger.info(
        "Generating read-only report for %s",
        config.scan_root,
    )

    start = time.time()

    cpu = get_cpu_usage()
    memory = get_memory()

    load1, load5, load15 = (
        get_load_average()
    )

    total, used, free, percent = (
        get_root_disk_usage(config)
    )

    filesystems = get_filesystems(
        config
    )

    print()

    print_header(
        "STORAGESENTINEL SERVER STORAGE REPORT"
    )

    print(
        f"Generated:          {now_string()}"
    )

    print(
        f"Hostname:           "
        f"{socket.gethostname()}"
    )

    print(
        f"OS:                 "
        f"{platform.platform()}"
    )

    print(
        f"Kernel:             "
        f"{platform.release()}"
    )

    print(
        f"Architecture:       "
        f"{platform.machine()}"
    )

    print()
    print("-" * 78)
    print("1. SERVER HEALTH")
    print("-" * 78)

    print(
        f"CPU Usage:          {cpu:.1f}%"
    )

    print(
        f"Load Average:       "
        f"{load1:.2f}  "
        f"{load5:.2f}  "
        f"{load15:.2f}"
    )

    print(
        f"Uptime:             "
        f"{get_uptime()}"
    )

    print(
        f"RAM Used:           "
        f"{format_gb(memory['used'])} / "
        f"{format_gb(memory['total'])}"
    )

    print(
        f"RAM Available:      "
        f"{format_gb(memory['available'])}"
    )

    swap_percent = (
        memory["swap_used"]
        / memory["swap_total"]
        * 100
        if memory["swap_total"]
        else 0
    )

    print(
        f"Swap Used:          "
        f"{format_gb(memory['swap_used'])} / "
        f"{format_gb(memory['swap_total'])}"
    )

    print(
        f"Swap Usage:         "
        f"{swap_percent:.1f}%"
    )

    print()
    print("-" * 78)
    print("2. STORAGE")
    print("-" * 78)

    print(
        f"Scan Root:          "
        f"{config.scan_root}"
    )

    print(
        f"Total Storage:      "
        f"{format_gb(total)}"
    )

    print(
        f"Used Storage:       "
        f"{format_gb(used)}"
    )

    print(
        f"Free Storage:       "
        f"{format_gb(free)}"
    )

    print(
        f"Utilization:        "
        f"{percent:.2f}%"
    )

    if percent >= 90:
        status = "CRITICAL"

    elif percent >= 80:
        status = "WARNING"

    else:
        status = "HEALTHY"

    print(
        f"STATUS:             {status}"
    )

    print()
    print("-" * 78)
    print("3. REAL FILESYSTEMS")
    print("-" * 78)

    print(
        f"{'Mount':<28}"
        f"{'Type':<14}"
        f"{'Used':>12}"
        f"{'Free':>12}"
        f"{'Usage':>9}"
    )

    print("-" * 78)

    for fs in filesystems:

        print(
            f"{fs.mountpoint[:27]:<28}"
            f"{fs.fstype[:13]:<14}"
            f"{format_bytes(fs.used):>12}"
            f"{format_bytes(fs.free):>12}"
            f"{fs.percent:>8.1f}%"
        )

    print()
    print("-" * 78)
    print("4. LARGEST TOP-LEVEL FOLDERS")
    print("-" * 78)

    print(
        "Calculating real disk directories..."
    )

    folders = get_top_level_folders(
        config
    )

    if not folders:
        print("No accessible folders found.")

    else:

        for index, (size, path) in enumerate(
            folders,
            1,
        ):

            print(
                f"{index:>3}. "
                f"{format_bytes(size):>12}  "
                f"{path}"
            )

    print()
    print("-" * 78)
    print("5. LARGEST FILES")
    print("-" * 78)

    print(
        "Finding largest files..."
    )

    largest_files = get_largest_files(
        config
    )

    if not largest_files:
        print("No files found.")

    else:

        print(
            f"{'#':>3} "
            f"{'Size':>12}  "
            f"{'Owner':<16} "
            f"Path"
        )

        print("-" * 78)

        for index, (size, path) in enumerate(
            largest_files,
            1,
        ):

            try:
                owner = path.stat().st_uid
            except OSError:
                owner = "?"

            try:
                import pwd

                owner_name = pwd.getpwuid(
                    owner
                ).pw_name

            except Exception:
                owner_name = str(owner)

            print(
                f"{index:>3} "
                f"{format_bytes(size):>12}  "
                f"{owner_name:<16} "
                f"{path}"
            )

    print()
    print("=" * 78)

    elapsed = time.time() - start

    print(
        f"Report generated in "
        f"{elapsed:.2f}s"
    )

    print("=" * 78)

    # Save a copy without duplicating the entire console machinery.
    try:

        import io
        from contextlib import redirect_stdout

        buffer = io.StringIO()

        with redirect_stdout(buffer):
            _report_for_file(config)

        config.report_file.write_text(
            buffer.getvalue(),
            encoding="utf-8",
        )

        logger.info(
            "Report saved to %s",
            config.report_file,
        )

    except Exception as exc:

        logger.warning(
            "Could not save report: %s",
            exc,
        )


def _report_for_file(
    config: Config,
):

    cpu = get_cpu_usage()
    memory = get_memory()
    load1, load5, load15 = get_load_average()
    total, used, free, percent = get_root_disk_usage(config)

    print("=" * 78)
    print(
        "STORAGESENTINEL SERVER STORAGE REPORT"
    )
    print("=" * 78)

    print(
        f"Generated: {now_string()}"
    )

    print(
        f"Hostname: {socket.gethostname()}"
    )

    print()
    print("SERVER HEALTH")
    print(
        f"CPU: {cpu:.1f}%"
    )

    print(
        f"Load: {load1:.2f} "
        f"{load5:.2f} "
        f"{load15:.2f}"
    )

    print(
        f"RAM: {format_gb(memory['used'])} / "
        f"{format_gb(memory['total'])}"
    )

    print(
        f"Swap: {format_gb(memory['swap_used'])} / "
        f"{format_gb(memory['swap_total'])}"
    )

    print(
        f"Uptime: {get_uptime()}"
    )

    print()
    print("STORAGE")

    print(
        f"Total: {format_gb(total)}"
    )

    print(
        f"Used: {format_gb(used)}"
    )

    print(
        f"Free: {format_gb(free)}"
    )

    print(
        f"Utilization: {percent:.2f}%"
    )

    print()
    print("LARGEST FOLDERS")

    for size, path in get_top_level_folders(
        config
    ):

        print(
            f"{format_bytes(size):>12} "
            f"{path}"
        )

    print()
    print("LARGEST FILES")

    for size, path in get_largest_files(
        config
    ):

        print(
            f"{format_bytes(size):>12} "
            f"{path}"
        )


# ============================================================
# Deep scan
# ============================================================

def run_scan(
    config: Config,
    logger: logging.Logger,
):

    print()
    print_header(
        "StorageSentinel Deep Scan"
    )

    print(
        f"Scan root: {config.scan_root}"
    )

    print()
    print(
        "This operation performs a deep filesystem scan."
    )

    print(
        "It does NOT delete anything."
    )

    print()

    candidates: List[Candidate] = []

    next_id = 1

    # --------------------------------------------------------
    # Filesystem traversal
    # --------------------------------------------------------

    logger.info(
        "Starting deep filesystem scan: %s",
        config.scan_root,
    )

    print(
        "Scanning files..."
    )

    scanned = 0

    duplicate_groups = defaultdict(list)

    for path in iter_files(
        config.scan_root,
        config,
    ):

        scanned += 1

        if scanned % 10000 == 0:

            print(
                f"  Scanned {scanned:,} files..."
            )

        try:
            size = path.stat(
                follow_symlinks=False
            ).st_size

        except (
            PermissionError,
            FileNotFoundError,
            OSError,
        ):
            continue

        # ----------------------------------------------------
        # Trash
        # ----------------------------------------------------

        if (
            safe_bool(
                config.scan.get(
                    "find_trash",
                    True,
                ),
                True,
            )
            and safe_bool(
                config.cleanup.get(
                    "allow_trash",
                    True,
                ),
                True,
            )
            and is_trash_path(path)
            and not config.is_protected(path)
        ):

            candidates.append(
                Candidate(
                    action_id=next_id,
                    action_type="trash",
                    path=str(path),
                    size=size,
                    risk="LOW",
                    reason=(
                        "File is located "
                        "inside a Trash directory."
                    ),
                )
            )

            next_id += 1

        # ----------------------------------------------------
        # Cache
        # ----------------------------------------------------

        elif (
            safe_bool(
                config.scan.get(
                    "find_caches",
                    True,
                ),
                True,
            )
            and safe_bool(
                config.cleanup.get(
                    "allow_caches",
                    True,
                ),
                True,
            )
            and is_cache_path(path)
            and not config.is_protected(path)
        ):

            candidates.append(
                Candidate(
                    action_id=next_id,
                    action_type="cache",
                    path=str(path),
                    size=size,
                    risk="LOW",
                    reason=(
                        "File is located "
                        "inside a recognized cache "
                        "directory."
                    ),
                )
            )

            next_id += 1

        # ----------------------------------------------------
        # Temporary files
        # ----------------------------------------------------

        elif (
            safe_bool(
                config.scan.get(
                    "find_temp_files",
                    True,
                ),
                True,
            )
            and safe_bool(
                config.cleanup.get(
                    "allow_temp_files",
                    True,
                ),
                True,
            )
            and is_temp_path(path)
            and not config.is_protected(path)
        ):

            candidates.append(
                Candidate(
                    action_id=next_id,
                    action_type="temporary",
                    path=str(path),
                    size=size,
                    risk="LOW",
                    reason=(
                        "File is located "
                        "inside a temporary directory."
                    ),
                )
            )

            next_id += 1

        # ----------------------------------------------------
        # Old logs
        # ----------------------------------------------------

        elif (
            safe_bool(
                config.scan.get(
                    "find_old_logs",
                    True,
                ),
                True,
            )
            and safe_bool(
                config.cleanup.get(
                    "allow_old_logs",
                    True,
                ),
                True,
            )
            and is_old_log(
                path,
                config,
            )
            and not config.is_protected(path)
        ):

            candidates.append(
                Candidate(
                    action_id=next_id,
                    action_type="old_log",
                    path=str(path),
                    size=size,
                    risk="MEDIUM",
                    reason=(
                        f"Log file older than "
                        f"{config.old_log_days} days."
                    ),
                )
            )

            next_id += 1

        # ----------------------------------------------------
        # Duplicate candidates
        # ----------------------------------------------------

        if (
            safe_bool(
                config.scan.get(
                    "find_duplicates",
                    True,
                ),
                True,
            )
            and safe_bool(
                config.cleanup.get(
                    "allow_duplicates",
                    True,
                ),
                True,
            )
            and size >= config.duplicate_min_size
            and not config.is_protected(path)
            and not config.is_protected_extension(
                path
            )
        ):

            if (
                config.max_duplicate_file_gb
                > 0
                and size
                > config.max_duplicate_file_gb
                * 1024
                * 1024
                * 1024
            ):
                continue

            duplicate_groups[
                size
            ].append(path)

    print(
        f"  Files scanned: {scanned:,}"
    )

    # --------------------------------------------------------
    # Duplicate detection
    # --------------------------------------------------------

    print()
    print(
        "Checking duplicate files..."
    )

    duplicate_candidates = []

    for size, paths in duplicate_groups.items():

        if len(paths) < 2:
            continue

        partial_groups = defaultdict(list)

        for path in paths:

            digest = partial_hash(
                path,
                config.partial_hash_bytes,
            )

            if digest:
                partial_groups[
                    digest
                ].append(path)

        for _, same_partial in (
            partial_groups.items()
        ):

            if len(same_partial) < 2:
                continue

            full_groups = defaultdict(list)

            for path in same_partial:

                digest = full_hash(path)

                if digest:
                    full_groups[
                        digest
                    ].append(path)

            for _, duplicates in (
                full_groups.items()
            ):

                if len(duplicates) < 2:
                    continue

                # Keep first as original.
                original = duplicates[0]

                for duplicate in duplicates[1:]:

                    duplicate_candidates.append(
                        Candidate(
                            action_id=0,
                            action_type="duplicate",
                            path=str(duplicate),
                            size=size,
                            risk="MEDIUM",
                            reason=(
                                "Exact duplicate of "
                                f"{original}"
                            ),
                        )
                    )

    for candidate in duplicate_candidates:

        candidate.action_id = next_id
        candidates.append(candidate)
        next_id += 1

    # --------------------------------------------------------
    # Remove protected candidates
    # --------------------------------------------------------

    safe_candidates = []

    for candidate in candidates:

        path = Path(candidate.path)

        if config.is_protected(path):
            continue

        if config.is_protected_extension(path):
            continue

        safe_candidates.append(candidate)

    candidates = safe_candidates

    # --------------------------------------------------------
    # Save
    # --------------------------------------------------------

    manager = PendingManager(
        config.pending_file
    )

    manager.save(candidates)

    print()
    print("-" * 78)
    print("SCAN SUMMARY")
    print("-" * 78)

    counts = defaultdict(int)
    total_size = 0

    for candidate in candidates:

        counts[
            candidate.action_type
        ] += 1

        total_size += candidate.size

    print(
        f"Files scanned:       "
        f"{scanned:,}"
    )

    print(
        f"Candidates found:    "
        f"{len(candidates):,}"
    )

    print(
        f"Potential space:     "
        f"{format_bytes(total_size)}"
    )

    for action_type in sorted(counts):

        print(
            f"{action_type:<20}"
            f"{counts[action_type]:>8}"
        )

    print()
    print(
        f"Pending actions saved to:"
    )

    print(
        f"  {config.pending_file}"
    )

    print()
    print(
        "Nothing has been deleted."
    )

    append_history(
        config,
        (
            f"{now_string()} | "
            f"scan | "
            f"files={scanned} | "
            f"candidates={len(candidates)} | "
            f"size={total_size}"
        ),
    )

    logger.info(
        "Deep scan completed: "
        "%d files, %d candidates",
        scanned,
        len(candidates),
    )


# ============================================================
# Pending approval
# ============================================================

def run_pending(
    config: Config,
):

    manager = PendingManager(
        config.pending_file
    )

    candidates = manager.load()

    if not candidates:

        print()
        print(
            "No pending actions found."
        )

        print(
            "Run:"
        )

        print(
            "  ./sentinel.py scan"
        )

        return

    print()

    print_header(
        "StorageSentinel Pending Actions"
    )

    pending = [
        c
        for c in candidates
        if not c.deleted
    ]

    print(
        f"Pending actions: "
        f"{len(pending)}"
    )

    print()

    for candidate in pending:

        print("-" * 78)

        print(
            f"ID:          {candidate.action_id}"
        )

        print(
            f"Type:        {candidate.action_type}"
        )

        print(
            f"Size:        "
            f"{format_bytes(candidate.size)}"
        )

        print(
            f"Risk:        {candidate.risk}"
        )

        print(
            f"Path:        {candidate.path}"
        )

        print(
            f"Reason:      {candidate.reason}"
        )

        print()

        while True:

            answer = input(
                "Delete this file? [y/N/s/q]: "
            ).strip().lower()

            if answer in (
                "y",
                "yes",
            ):

                candidate.approved = True
                break

            if answer in (
                "",
                "n",
                "no",
            ):

                candidate.approved = False
                break

            if answer == "s":

                break

            if answer == "q":

                manager.save(candidates)

                print(
                    "Approval process stopped."
                )

                return

            print(
                "Please enter y, n, s, or q."
            )

    manager.save(candidates)

    approved = sum(
        1
        for c in candidates
        if c.approved and not c.deleted
    )

    print()
    print("-" * 78)

    print(
        f"Approved for deletion: "
        f"{approved}"
    )

    print(
        "Run './sentinel.py delete' "
        "to execute approved deletions."
    )

    append_history(
        config,
        (
            f"{now_string()} | "
            f"pending | "
            f"approved={approved}"
        ),
    )


# ============================================================
# Delete
# ============================================================

def run_delete(
    config: Config,
    logger: logging.Logger,
):

    manager = PendingManager(
        config.pending_file
    )

    candidates = manager.load()

    approved = [
        c
        for c in candidates
        if c.approved
        and not c.deleted
    ]

    if not approved:

        print()
        print(
            "No approved actions found."
        )

        print(
            "Run './sentinel.py pending' "
            "first."
        )

        return

    total_size = sum(
        c.size
        for c in approved
    )

    print()

    print_header(
        "StorageSentinel Delete"
    )

    print(
        f"Approved actions: "
        f"{len(approved)}"
    )

    print(
        f"Estimated space:  "
        f"{format_bytes(total_size)}"
    )

    print()

    for candidate in approved:

        print(
            f"[{candidate.action_id}] "
            f"{candidate.action_type:<12} "
            f"{format_bytes(candidate.size):>12} "
            f"{candidate.path}"
        )

    print()

    answer = input(
        "Proceed with deletion? [y/N]: "
    ).strip().lower()

    if answer not in (
        "y",
        "yes",
    ):

        print(
            "Deletion cancelled."
        )

        return

    print()

    deleted_count = 0
    failed_count = 0
    freed = 0

    for index, candidate in enumerate(
        approved,
        1,
    ):

        path = Path(
            candidate.path
        )

        print(
            f"[{index}/{len(approved)}] "
            f"{path}"
        )

        # ----------------------------------------------------
        # Safety checks
        # ----------------------------------------------------

        if config.is_protected(path):

            print(
                "  BLOCKED: protected path"
            )

            failed_count += 1
            continue

        if config.is_protected_extension(
            path
        ):

            print(
                "  BLOCKED: protected extension"
            )

            failed_count += 1
            continue

        try:

            if not path.exists():

                print(
                    "  SKIPPED: file no longer exists"
                )

                candidate.deleted = True
                continue

            if not path.is_file():

                print(
                    "  BLOCKED: target is not a file"
                )

                failed_count += 1
                continue

            current_size = path.stat().st_size

            # The file should not have changed.
            if current_size != candidate.size:

                print(
                    "  BLOCKED: file size changed"
                )

                failed_count += 1
                continue

            # Final extension safety check.
            if config.is_protected_extension(
                path
            ):

                print(
                    "  BLOCKED: protected extension"
                )

                failed_count += 1
                continue

            path.unlink()

            candidate.deleted = True

            deleted_count += 1
            freed += candidate.size

            print(
                "  DELETED"
            )

        except (
            PermissionError,
            FileNotFoundError,
            OSError,
        ) as exc:

            failed_count += 1

            print(
                f"  FAILED: {exc}"
            )

    manager.save(candidates)

    print()
    print("-" * 78)
    print("DELETE SUMMARY")
    print("-" * 78)

    print(
        f"Deleted:          "
        f"{deleted_count}"
    )

    print(
        f"Failed/blocked:   "
        f"{failed_count}"
    )

    print(
        f"Space freed:      "
        f"{format_bytes(freed)}"
    )

    print()

    append_history(
        config,
        (
            f"{now_string()} | "
            f"delete | "
            f"deleted={deleted_count} | "
            f"failed={failed_count} | "
            f"freed={freed}"
        ),
    )

    logger.info(
        "Delete completed: "
        "%d deleted, %d failed, %s freed",
        deleted_count,
        failed_count,
        format_bytes(freed),
    )


# ============================================================
# History
# ============================================================

def append_history(
    config: Config,
    line: str,
):

    try:

        with config.history_file.open(
            "a",
            encoding="utf-8",
        ) as f:

            f.write(line + "\n")

    except OSError:
        pass


def run_history(
    config: Config,
):

    if not config.history_file.exists():

        print(
            "No history available."
        )

        return

    try:

        print(
            config.history_file.read_text(
                encoding="utf-8"
            )
        )

    except OSError as exc:

        print(
            f"Unable to read history: {exc}"
        )


# ============================================================
# Main
# ============================================================

def build_parser():

    parser = argparse.ArgumentParser(
        description=(
            "StorageSentinel server "
            "storage monitoring and "
            "controlled cleanup."
        )
    )

    parser.add_argument(
        "command",
        nargs="?",
        default="help",
        choices=[
            "status",
            "report",
            "scan",
            "pending",
            "delete",
            "history",
            "help",
        ],
    )

    return parser


def main():

    try:

        config = Config(
            CONFIG_FILE
        )

    except Exception as exc:

        print(
            f"Configuration error: {exc}",
            file=sys.stderr,
        )

        return 1

    logger = setup_logging(
        config
    )

    parser = build_parser()

    args = parser.parse_args()

    try:

        if args.command == "status":

            run_status(config)

        elif args.command == "report":

            run_report(
                config,
                logger,
            )

        elif args.command == "scan":

            run_scan(
                config,
                logger,
            )

        elif args.command == "pending":

            run_pending(config)

        elif args.command == "delete":

            run_delete(
                config,
                logger,
            )

        elif args.command == "history":

            run_history(config)

        elif args.command == "help":

            parser.print_help()

        return 0

    except KeyboardInterrupt:

        print()
        print(
            "Operation cancelled."
        )

        return 130

    except Exception as exc:

        logger.exception(
            "Fatal error"
        )

        print(
            f"ERROR: {exc}",
            file=sys.stderr,
        )

        return 1


if __name__ == "__main__":
    sys.exit(main())
