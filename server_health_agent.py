#!/usr/bin/env python3
import argparse
import os
import sqlite3
import time
import subprocess
from datetime import datetime

try:
    import yaml
except ImportError:
    yaml = None

DEFAULT_DB_PATH = "server_health.db"
DEFAULT_CONFIG_PATH = "config.yaml"
DEFAULT_ROOT_PATH = "/"


def load_config(config_path=DEFAULT_CONFIG_PATH):
    if not os.path.exists(config_path) or yaml is None:
        return {}
    with open(config_path, "r") as f:
        return yaml.safe_load(f) or {}


def get_connection(db_path=DEFAULT_DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path=DEFAULT_DB_PATH):
    conn = get_connection(db_path)
    cursor = conn.cursor()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS server_samples (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
        root_path TEXT,
        cpu_percent REAL,
        load_1 REAL,
        load_5 REAL,
        load_15 REAL,
        ram_total_gb REAL,
        ram_used_gb REAL,
        ram_free_gb REAL,
        swap_total_gb REAL,
        swap_used_gb REAL,
        disk_total_gb REAL,
        disk_used_gb REAL,
        disk_free_gb REAL,
        disk_percent REAL,
        network_rx_bytes INTEGER,
        network_tx_bytes INTEGER,
        uptime_seconds INTEGER
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS process_samples (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sample_id INTEGER,
        pid INTEGER,
        command TEXT,
        cpu_percent REAL,
        mem_percent REAL,
        FOREIGN KEY(sample_id) REFERENCES server_samples(id) ON DELETE CASCADE
    )
    """)

    conn.commit()
    conn.close()


def _read_cpu_times():
    with open("/proc/stat", "r", encoding="utf-8") as f:
        for line in f:
            if line.startswith("cpu "):
                parts = line.split()[1:]
                values = [int(x) for x in parts]
                idle = values[3] + values[4] if len(values) >= 5 else values[3]
                return sum(values), idle
    raise RuntimeError("Unable to read /proc/stat")


def get_cpu_percent(interval=0.2):
    try:
        total1, idle1 = _read_cpu_times()
        time.sleep(interval)
        total2, idle2 = _read_cpu_times()
        busy = (total2 - total1) - (idle2 - idle1)
        total = total2 - total1
        if total <= 0:
            return 0.0
        return round(100.0 * busy / total, 1)
    except Exception:
        return 0.0


def get_load_average():
    try:
        load1, load5, load15 = os.getloadavg()
        return round(load1, 2), round(load5, 2), round(load15, 2)
    except OSError:
        return 0.0, 0.0, 0.0


def parse_meminfo():
    mem = {}
    with open("/proc/meminfo", "r", encoding="utf-8") as f:
        for line in f:
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            mem[key.strip()] = int(value.split()[0])
    return mem


def get_memory_metrics():
    try:
        meminfo = parse_meminfo()
        total_kb = meminfo.get("MemTotal", 0)
        free_kb = meminfo.get("MemFree", 0)
        buffers_kb = meminfo.get("Buffers", 0)
        cached_kb = meminfo.get("Cached", 0)
        sreclaim_kb = meminfo.get("SReclaimable", 0)
        shmem_kb = meminfo.get("Shmem", 0)
        available_kb = meminfo.get("MemAvailable", free_kb + buffers_kb + cached_kb)
        used_kb = max(0, total_kb - available_kb)
        swap_total_kb = meminfo.get("SwapTotal", 0)
        swap_free_kb = meminfo.get("SwapFree", 0)

        return {
            "ram_total_gb": round(total_kb / 1024 / 1024, 3),
            "ram_used_gb": round(used_kb / 1024 / 1024, 3),
            "ram_free_gb": round(available_kb / 1024 / 1024, 3),
            "swap_total_gb": round(swap_total_kb / 1024 / 1024, 3),
            "swap_used_gb": round(max(0, swap_total_kb - swap_free_kb) / 1024 / 1024, 3)
        }
    except Exception:
        return {
            "ram_total_gb": 0.0,
            "ram_used_gb": 0.0,
            "ram_free_gb": 0.0,
            "swap_total_gb": 0.0,
            "swap_used_gb": 0.0
        }


def get_network_bytes():
    rx = 0
    tx = 0
    try:
        with open("/proc/net/dev", "r", encoding="utf-8") as f:
            for line in f.readlines()[2:]:
                parts = line.strip().split()
                if len(parts) < 17:
                    continue
                rx += int(parts[1])
                tx += int(parts[9])
    except Exception:
        pass
    return rx, tx


def get_uptime_seconds():
    try:
        with open("/proc/uptime", "r", encoding="utf-8") as f:
            seconds = float(f.readline().split()[0])
            return int(seconds)
    except Exception:
        return 0


def get_top_processes(limit=5):
    processes = []
    try:
        raw = subprocess.check_output(
            ["ps", "-eo", "pid,comm,%cpu,%mem", "--sort=-%cpu"],
            text=True,
            stderr=subprocess.DEVNULL
        )
        lines = raw.strip().splitlines()[1:limit + 1]
        for line in lines:
            parts = line.split(None, 3)
            if len(parts) == 4:
                pid = int(parts[0])
                command = parts[1]
                cpu_percent = float(parts[2])
                mem_percent = float(parts[3])
                processes.append({
                    "pid": pid,
                    "command": command,
                    "cpu_percent": cpu_percent,
                    "mem_percent": mem_percent
                })
    except Exception:
        pass
    return processes


def get_disk_metrics(root_path=DEFAULT_ROOT_PATH):
    try:
        usage = os.statvfs(root_path)
        total_bytes = usage.f_frsize * usage.f_blocks
        free_bytes = usage.f_frsize * usage.f_bavail
        used_bytes = total_bytes - free_bytes
        percent = round(100.0 * used_bytes / total_bytes, 1) if total_bytes else 0.0
        return {
            "disk_total_gb": round(total_bytes / 1024**3, 3),
            "disk_used_gb": round(used_bytes / 1024**3, 3),
            "disk_free_gb": round(free_bytes / 1024**3, 3),
            "disk_percent": percent
        }
    except Exception:
        return {
            "disk_total_gb": 0.0,
            "disk_used_gb": 0.0,
            "disk_free_gb": 0.0,
            "disk_percent": 0.0
        }


def collect_metrics(root_path=DEFAULT_ROOT_PATH):
    cpu = get_cpu_percent()
    load1, load5, load15 = get_load_average()
    memory = get_memory_metrics()
    disk = get_disk_metrics(root_path)
    rx, tx = get_network_bytes()
    uptime = get_uptime_seconds()

    return {
        "root_path": root_path,
        "cpu_percent": cpu,
        "load_1": load1,
        "load_5": load5,
        "load_15": load15,
        **memory,
        **disk,
        "network_rx_bytes": rx,
        "network_tx_bytes": tx,
        "uptime_seconds": uptime
    }


def record_sample(db_path, metrics, top_processes=None):
    conn = get_connection(db_path)
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO server_samples (
            root_path, cpu_percent, load_1, load_5, load_15,
            ram_total_gb, ram_used_gb, ram_free_gb,
            swap_total_gb, swap_used_gb,
            disk_total_gb, disk_used_gb, disk_free_gb, disk_percent,
            network_rx_bytes, network_tx_bytes, uptime_seconds
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            metrics["root_path"],
            metrics["cpu_percent"],
            metrics["load_1"],
            metrics["load_5"],
            metrics["load_15"],
            metrics["ram_total_gb"],
            metrics["ram_used_gb"],
            metrics["ram_free_gb"],
            metrics["swap_total_gb"],
            metrics["swap_used_gb"],
            metrics["disk_total_gb"],
            metrics["disk_used_gb"],
            metrics["disk_free_gb"],
            metrics["disk_percent"],
            metrics["network_rx_bytes"],
            metrics["network_tx_bytes"],
            metrics["uptime_seconds"]
        )
    )
    sample_id = cursor.lastrowid
    if top_processes:
        for proc in top_processes:
            cursor.execute(
                """
                INSERT INTO process_samples (sample_id, pid, command, cpu_percent, mem_percent)
                VALUES (?, ?, ?, ?, ?)
                """,
                (sample_id, proc["pid"], proc["command"], proc["cpu_percent"], proc["mem_percent"])
            )
    conn.commit()
    conn.close()
    return sample_id


def collect_and_store(db_path=DEFAULT_DB_PATH, root_path=DEFAULT_ROOT_PATH):
    init_db(db_path)
    metrics = collect_metrics(root_path)
    top_processes = get_top_processes(limit=5)
    sample_id = record_sample(db_path, metrics, top_processes)
    print(f"Stored server health sample id={sample_id} in {db_path}")
    return sample_id


def main():
    parser = argparse.ArgumentParser(description="Server Health Agent for StorageSentinel")
    parser.add_argument("command", choices=["init", "collect"], help="Action to perform")
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help="Path to server health SQLite DB")
    parser.add_argument("--root", default=DEFAULT_ROOT_PATH, help="Filesystem root path to measure disk usage against")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH, help="Optional config.yaml to load monitoring settings")
    args = parser.parse_args()

    config = load_config(args.config)
    db_path = config.get("server_health_db", args.db)
    if args.command == "init":
        init_db(db_path)
        print(f"Initialized server health database at {db_path}")
    elif args.command == "collect":
        collect_and_store(db_path, args.root)


if __name__ == "__main__":
    main()
