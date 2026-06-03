#!/usr/bin/env python3
import argparse
import os
import sqlite3
import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText

try:
    import yaml
except ImportError:
    yaml = None

DEFAULT_DB_PATH = "server_health.db"
DEFAULT_CONFIG_PATH = "config.yaml"


def load_config(config_path=DEFAULT_CONFIG_PATH):
    if not os.path.exists(config_path) or yaml is None:
        return {}
    with open(config_path, "r") as f:
        return yaml.safe_load(f) or {}


def get_connection(db_path=DEFAULT_DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def get_samples(db_path, since):
    conn = get_connection(db_path)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM server_samples WHERE timestamp >= ? ORDER BY timestamp ASC",
        (since.isoformat(),)
    )
    samples = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return samples


def get_recent_processes(db_path, sample_id, limit=5):
    conn = get_connection(db_path)
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT pid, command, cpu_percent, mem_percent FROM process_samples WHERE sample_id = ? ORDER BY cpu_percent DESC LIMIT ?",
            (sample_id, limit)
        )
        procs = [dict(row) for row in cursor.fetchall()]
    except sqlite3.OperationalError:
        procs = []
    conn.close()
    return procs


def _avg(values):
    return round(sum(values) / len(values), 2) if values else 0.0


def aggregate_series(samples, key):
    values = [sample.get(key, 0.0) for sample in samples]
    return {
        "average": round(_avg(values), 2),
        "peak": round(max(values), 2) if values else 0.0,
        "lowest": round(min(values), 2) if values else 0.0,
        "last": round(values[-1], 2) if values else 0.0,
        "count": len(values)
    }


def make_text_report(period_label, samples, config, db_path=DEFAULT_DB_PATH):
    if not samples:
        return f"No server health samples available for the {period_label} window.\n"

    cpu = aggregate_series(samples, "cpu_percent")
    ram_used = aggregate_series([{"value": sample["ram_used_gb"]} for sample in samples], "value")
    disk = aggregate_series(samples, "disk_percent")
    swap_used = aggregate_series([{"value": sample["swap_used_gb"]} for sample in samples], "value")
    load1 = aggregate_series(samples, "load_1")
    top_sample = samples[-1]
    uptime_hours = round(top_sample.get("uptime_seconds", 0) / 3600, 1)

    network_delta = samples[-1]["network_rx_bytes"] - samples[0]["network_rx_bytes"] if len(samples) > 1 else 0
    network_tx_delta = samples[-1]["network_tx_bytes"] - samples[0]["network_tx_bytes"] if len(samples) > 1 else 0

    health_score = 100
    health_score -= min(cpu["average"] * 0.3, 30)
    health_score -= min(ram_used["average"] / max(top_sample.get("ram_total_gb", 1), 1) * 100 * 0.3, 30)
    health_score -= min(disk["last"] * 0.2, 20)
    health_score = max(0, round(health_score, 0))

    now = datetime.now(timezone.utc)
    lines = [
        f"Server Health Report ({period_label})",
        "============================================================",
        f"Generated: {now.isoformat()}",
        f"Sample window: {samples[0]['timestamp']} to {samples[-1]['timestamp']}",
        "",
        "CPU",
        f"  Average CPU usage: {cpu['average']}%",
        f"  Peak CPU usage:    {cpu['peak']}%",
        f"  Lowest CPU usage:  {cpu['lowest']}%",
        "",
        "Memory",
        f"  Average RAM used:  {ram_used['average']} GB",
        f"  Latest RAM used:   {top_sample['ram_used_gb']} GB",
        f"  Total RAM:         {top_sample['ram_total_gb']} GB",
        "",
        "Swap",
        f"  Average Swap used: {swap_used['average']} GB",
        f"  Latest Swap used:  {top_sample['swap_used_gb']} GB",
        "",
        "Disk",
        f"  Latest disk utilization: {disk['last']}%",
        f"  Average disk utilization: {disk['average']}%",
        f"  Peak disk utilization:    {disk['peak']}%",
        "",
        "Load Average",
        f"  1-min load:  {load1['average']}",
        "",
        "Network",
        f"  Received over window: {network_delta} bytes",
        f"  Transmitted over window: {network_tx_delta} bytes",
        "",
        "Uptime",
        f"  Current uptime: {uptime_hours} hours",
        "",
        "Health Summary",
        f"  Health Score: {health_score} / 100",
        "",
        "Recommendations",
        "  - Run the server health agent every minute to keep trends fresh.",
        "  - Investigate any sudden CPU or disk utilization spikes.",
        "  - Review swap growth and disk space over the next period.",
        "",
        "Latest Top Processes (by CPU)",
    ]

    sample_id = top_sample.get("id")
    if sample_id is not None:
        top_processes = get_recent_processes(db_path, sample_id)
        if top_processes:
            for proc in top_processes:
                lines.append(f"  - PID {proc['pid']} {proc['command']} CPU={proc['cpu_percent']}% MEM={proc['mem_percent']}%")
        else:
            lines.append("  No process sample data available.")
    else:
        lines.append("  No process sample data available.")

    lines.append("")
    return "\n".join(lines)


def send_email_report(config, subject, body):
    email_cfg = config.get("email_alerts", {})
    if not email_cfg.get("enabled", False):
        raise RuntimeError("Email sending is disabled in config.yaml")

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = email_cfg.get("from_address", "sentinel@yourdomain.com")
    msg["To"] = ", ".join(email_cfg.get("to_addresses", []))

    try:
        server = smtplib.SMTP(email_cfg.get("smtp_server", "localhost"), email_cfg.get("smtp_port", 25), timeout=10)
        server.sendmail(msg["From"], email_cfg.get("to_addresses", []), msg.as_string())
        server.quit()
        return msg["To"]
    except ConnectionRefusedError as e:
        raise RuntimeError(
            f"SMTP connection refused when connecting to {email_cfg.get('smtp_server', 'localhost')}:{email_cfg.get('smtp_port', 25)}. "
            "Check your SMTP server settings in config.yaml or disable --send until SMTP is available."
        ) from e
    except Exception as e:
        raise RuntimeError(f"Failed to send email report: {e}") from e


def get_period_range(period):
    now = datetime.now(timezone.utc)
    if period == "daily":
        return now - timedelta(days=1)
    if period == "weekly":
        return now - timedelta(days=7)
    if period == "monthly":
        return now - timedelta(days=30)
    raise ValueError("Unsupported period")


def main():
    parser = argparse.ArgumentParser(description="Generate and optionally email server health reports")
    parser.add_argument("period", choices=["daily", "weekly", "monthly"], help="Report period")
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help="Path to server health SQLite DB")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH, help="Path to config.yaml")
    parser.add_argument("--send", action="store_true", help="Send report by email")
    parser.add_argument("--output", help="Write report text to a local file")
    args = parser.parse_args()

    config = load_config(args.config)
    db_path = config.get("server_health_db", args.db)
    since = get_period_range(args.period)
    samples = get_samples(db_path, since)
    report_text = make_text_report(args.period.capitalize(), samples, config, db_path)

    if args.output:
        output_dir = os.path.dirname(args.output)
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as output_file:
            output_file.write(report_text)
        print(f"Wrote report to {args.output}")

    print(report_text)

    if args.send:
        recipients = send_email_report(config, f"{args.period.capitalize()} Server Health Report", report_text)
        print(f"Sent report to {recipients}")


if __name__ == "__main__":
    main()
