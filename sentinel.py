import os
import sys
import yaml
import json
import argparse
from datetime import datetime

import database
import scanner
import policy_engine
import reporter
import executor

def load_config(config_path):
    """Load configuration from YAML file."""
    if not os.path.exists(config_path):
        # Return sensible defaults if config is missing
        return {
            "scan_root": "/home",
            "alert_thresholds": {"warning": 80.0, "critical": 90.0, "emergency": 95.0},
            "large_file_threshold_gb": 5.0,
            "cold_data_days": 180,
            "auto_cleanup": {
                "trash_max_age_days": 30,
                "clean_conda_cache": True,
                "clean_pip_cache": True,
                "clean_journald_logs": True,
                "journald_max_age_days": 30
            },
            "exclusions": [".git", "node_modules", "venv", ".venv"]
        }
    with open(config_path, 'r') as f:
        return yaml.safe_load(f)

def sync_actions_to_db(db_path, actions):
    """Sync json file actions into SQLite database pending_actions table."""
    conn = database.get_connection(db_path)
    cursor = conn.cursor()
    for item in actions:
        # Check if already in database
        cursor.execute("""
        SELECT id, approved, executed FROM pending_actions 
        WHERE target_path = ? AND action_type = ?
        """, (item["target_path"], item["action_type"]))
        row = cursor.fetchone()
        
        approved_int = 1 if item["approved"] else 0
        risk_val = item.get("risk", "Medium")
        
        if row:
            # Sync approved flag if not already executed
            if row["executed"] == 0:
                cursor.execute("""
                UPDATE pending_actions 
                SET approved = ?, size_gb = ?, description = ?, risk = ?
                WHERE id = ?
                """, (approved_int, item["size_gb"], item["description"], risk_val, row["id"]))
        else:
            # Insert new action
            cursor.execute("""
            INSERT INTO pending_actions (action_type, target_path, size_gb, description, approved, executed, risk)
            VALUES (?, ?, ?, ?, ?, 0, ?)
            """, (item["action_type"], item["target_path"], item["size_gb"], item["description"], approved_int, risk_val))
    conn.commit()
    conn.close()

def sync_db_approvals_to_json(db_path, json_path):
    """Update json file with approvals recorded in SQLite database."""
    db_actions = database.get_pending_actions(db_path)
    db_approvals = { (a["action_type"], a["target_path"]): (a["approved"] == 1) for a in db_actions }
    
    if os.path.exists(json_path):
        try:
            with open(json_path, 'r') as f:
                actions = json.load(f)
            for a in actions:
                key = (a["action_type"], a["target_path"])
                if key in db_approvals:
                    a["approved"] = db_approvals[key]
            with open(json_path, 'w') as f:
                json.dump(actions, f, indent=2)
        except Exception:
            pass

def interactive_approve(json_path, db_path):
    """Interactive CLI menu to approve recommended cleanup actions."""
    if not os.path.exists(json_path):
        print(f"No pending actions file found at {json_path}. Run a scan first.")
        return
        
    try:
        with open(json_path, 'r') as f:
            actions = json.load(f)
    except Exception as e:
        print(f"Error loading {json_path}: {e}")
        return
        
    if not actions:
        print("No pending actions to approve.")
        return
        
    print("\n" + "=" * 60)
    print("           STORAGESENTINEL INTERACTIVE APPROVAL")
    print("=" * 60)
    print("Navigate the recommendations list below:")
    print("  [y] Approve action")
    print("  [n] Reject/Decline action")
    print("  [s] Skip for now")
    print("  [q] Save decisions and quit")
    print("-" * 60)
    
    modified = False
    for item in actions:
        status_str = "APPROVED" if item["approved"] else "PENDING"
        print(f"\nID: {item['id']}")
        print(f"Action: {item['action_type'].upper()}")
        print(f"Risk:   {item.get('risk', 'Medium').upper()}")
        print(f"Target: {item['target_path']}")
        print(f"Space:  {item['size_gb']} GB")
        print(f"Reason: {item['description']}")
        print(f"Current Status: [{status_str}]")
        
        while True:
            choice = input("Approve? (y/n/s/q): ").strip().lower()
            if choice == 'y':
                item["approved"] = True
                modified = True
                break
            elif choice == 'n':
                item["approved"] = False
                modified = True
                break
            elif choice == 's':
                print("Skipped.")
                break
            elif choice == 'q':
                break
            else:
                print("Invalid option. Enter y, n, s, or q.")
                
        if choice == 'q':
            break
            
    if modified:
        with open(json_path, 'w') as f:
            json.dump(actions, f, indent=2)
        # Sync back to SQLite DB
        sync_actions_to_db(db_path, actions)
        print("\nDecisions saved to file and synced to SQLite database.")
    else:
        print("\nNo changes made.")

def send_email_alert(config, disk_usage, scan_summary, policy_results):
    """Send SMTP email alert or CLI alert if storage thresholds are exceeded."""
    email_cfg = config.get("email_alerts", {})
    cli_alerts = email_cfg.get("cli_alerts", False)
    smtp_enabled = email_cfg.get("enabled", False)
    if not cli_alerts and not smtp_enabled:
        return
        
    percent_used = disk_usage["percent_used"]
    thresholds = config.get("alert_thresholds", {})
    
    severity = None
    if percent_used >= thresholds.get("emergency", 95.0):
        severity = "Emergency"
    elif percent_used >= thresholds.get("critical", 90.0):
        severity = "Critical"
    elif percent_used >= thresholds.get("warning", 80.0):
        severity = "Warning"
        
    if not severity:
        return
        
    subject = f"Storage {severity} Alert: Disk Usage at {percent_used}%"
    
    # Top consumers with quota warning
    user_classes = config.get("user_classes", {})
    quota_limits = config.get("quotas", {})
    default_quota = quota_limits.get("default", 150.0)
    
    top_consumers = []
    for user, size_gb in sorted(scan_summary["user_metrics"], key=lambda x: x[1], reverse=True)[:5]:
        user_class = user_classes.get(user, "student")
        quota_gb = quota_limits.get(user_class, default_quota)
        status_suffix = " (Exceeded)" if size_gb > quota_gb else ""
        top_consumers.append(f"  {user:<15} {size_gb:<10.1f} GB{status_suffix}")
        
    top_consumers_str = "\n".join(top_consumers)
    
    # Calculate potential recovery
    auto_actions = policy_results.get("auto_actions", [])
    manual_actions = policy_results.get("manual_actions", [])
    total_recovery = sum(a["size_gb"] for a in auto_actions) + sum(m["size_gb"] for m in manual_actions)
    
    body = (
        f"StorageSentinel Alert\n"
        f"=====================\n\n"
        f"Disk Usage: {disk_usage['used_size_gb']} GB / {disk_usage['total_size_gb']} GB ({percent_used}%)\n"
        f"Status:     {severity}\n\n"
        f"Top Consumers:\n"
        f"{top_consumers_str}\n\n"
        f"Potential Recovery: {total_recovery:.1f} GB\n\n"
        f"Please run 'sentinel.sh approve' to review and execute cleanup actions."
    )

    if cli_alerts:
        print("\n*** STORAGE SENTINEL ALERT (CLI) ***")
        print(subject)
        print(body)
        print("*** END OF ALERT ***\n")

    if smtp_enabled:
        try:
            import smtplib
            from email.mime.text import MIMEText
            msg = MIMEText(body)
            msg["Subject"] = subject
            msg["From"] = email_cfg.get("from_address", "sentinel@yourdomain.com")
            msg["To"] = ", ".join(email_cfg.get("to_addresses", []))
            
            server = smtplib.SMTP(email_cfg.get("smtp_server", "localhost"), email_cfg.get("smtp_port", 25), timeout=10)
            server.sendmail(msg["From"], email_cfg.get("to_addresses", []), msg.as_string())
            server.quit()
            print(f"Email alert sent successfully to {msg['To']}.")
        except Exception as e:
            print(f"Warning: Failed to send email alert: {e}")

def main():
    parser = argparse.ArgumentParser(description="StorageSentinel: Storage Lifecycle Management System")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    parser.add_argument("--db", default=database.DEFAULT_DB_PATH, help="Path to SQLite sentinel.db")
    parser.add_argument("--json", default="pending_actions.json", help="Path to pending_actions.json")
    
    subparsers = parser.add_subparsers(dest="command", help="StorageSentinel commands")
    
    # 1. Scan command
    scan_parser = subparsers.add_parser("scan", help="Scan storage and log metrics")
    scan_parser.add_argument("--root", help="Override root directory to scan")
    
    # 2. Approve command
    subparsers.add_parser("approve", help="Interactively review and approve cleanup recommendations")
    
    # 3. Clean command
    clean_parser = subparsers.add_parser("clean", help="Execute approved cleanups")
    clean_parser.add_argument("--dry-run", action="store_true", help="Simulate cleanups without deleting")
    clean_parser.add_argument("--auto-only", action="store_true", help="Only run safe automatic actions")
    
    # 4. History command
    subparsers.add_parser("history", help="Print historical storage utilization and growth analysis")
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        sys.exit(1)
        
    # Ensure DB is initialized
    database.init_db(args.db)
    
    config = load_config(args.config)
    
    if args.command == "scan":
        root_to_scan = args.root if args.root else config.get("scan_root", "/home")
        
        # Disk usage of the volume containing target root
        disk_usage = scanner.get_disk_usage(root_to_scan)
        
        # Scan filesystem
        s = scanner.FileSystemScanner(
            root_path=root_to_scan,
            exclusions=config.get("exclusions", []),
            large_file_threshold_gb=config.get("large_file_threshold_gb", 5.0),
            cold_data_days=config.get("cold_data_days", 180),
            min_dir_size_gb=config.get("min_dir_size_gb", 1.0)
        )
        duplicates = s.scan()
        summary = s.get_summary()
        
        # Apply policies
        pe = policy_engine.PolicyEngine(config)
        policies = pe.evaluate(summary, disk_usage)
        policies["duplicates"] = duplicates
        
        # Save scan data to database
        try:
            database.record_scan(
                db_path=args.db,
                system_metrics=disk_usage,
                user_metrics=summary["user_metrics"],
                directory_metrics=summary["directory_metrics"],
                large_files_metrics=summary["large_files_metrics"],
                file_type_metrics=summary.get("file_type_gb")
            )
        except Exception as e:
            print(f"Warning: Failed to log metrics to SQLite DB: {e}")
            
        # Report & Save to JSON
        rep = reporter.ActionReporter(args.json)
        rep.generate_report(disk_usage, summary, policies, duplicates, config)
        
        # Sync newly identified actions to database
        actions = rep.load_actions_from_json()
        sync_actions_to_db(args.db, actions)
        
        # Dispatch SMTP alerts if warning threshold exceeded
        send_email_alert(config, disk_usage, summary, policies)
        
    elif args.command == "approve":
        # Pull approvals from database first to sync in case admin deleted or modified elsewhere
        sync_db_approvals_to_json(args.db, args.json)
        interactive_approve(args.json, args.db)
        
    elif args.command == "clean":
        rep = reporter.ActionReporter(args.json)
        actions = rep.load_actions_from_json()
        
        actions_to_run = []
        for a in actions:
            # If auto-only flag, we skip manual recommendations, cache purges and compressions
            if args.auto_only and a["action_type"] in ["compress", "delete", "conda_clean", "pip_clean"]:
                continue
                
            # Must be approved or safe auto
            if a["approved"]:
                actions_to_run.append(a)
                
        # Also query database for any approved, unexecuted delayed_delete actions
        try:
            db_actions = database.get_pending_actions(args.db)
            for da in db_actions:
                if da["action_type"] == "delayed_delete":
                    actions_to_run.append({
                        "id": da["id"],
                        "action_type": "delayed_delete",
                        "target_path": da["target_path"],
                        "size_gb": da["size_gb"],
                        "description": da.get("description", "Delayed delete"),
                        "approved": True
                    })
        except Exception as e:
            print(f"Warning: Could not fetch delayed delete actions from database: {e}")
            
        if not actions_to_run:
            print("No actions are approved for execution.")
            sys.exit(0)
            
        print(f"Found {len(actions_to_run)} approved actions. Starting cleanup...")
        results = executor.execute_actions(
            actions_to_run,
            args.db,
            test_mode=args.dry_run,
            protected_paths=config.get("protected_paths", []),
            protected_extensions=config.get("protected_extensions", [])
        )
        
        # Re-run sync to update approvals & execution status in JSON
        if not args.dry_run:
            # Remove successfully executed actions from JSON
            remaining_actions = []
            executed_ids = [r["action_id"] for r in results if r["success"]]
            
            for a in actions:
                if a["id"] not in executed_ids:
                    remaining_actions.append(a)
                    
            rep.save_actions_to_json(remaining_actions)
            print("Successfully updated pending actions JSON list.")
            
    elif args.command == "history":
        print("\n" + "=" * 60)
        print("             STORAGESENTINEL HISTORICAL LOGS")
        print("=" * 60)
        
        history = database.get_historical_usage(args.db)
        if not history:
            print("No historical scans found. Run 'scan' first.")
            sys.exit(0)
            
        print("\n### System Disk Usage Trend (Latest First)")
        print(f"  {'Timestamp':<25} {'Total (GB)':<12} {'Used (GB)':<12} {'Utilization':<12}")
        print("  " + "-" * 65)
        for h in history:
            print(f"  {h['timestamp']:<25} {h['total_size_gb']:<12.1f} {h['used_size_gb']:<12.1f} {h['percent_used']}%")
            
        growth = database.get_growth_report(args.db)
        if growth:
            print("\n### Storage Growth Analysis")
            print(f"  Comparing latest scan ({growth['latest_time']}) with previous ({growth['prev_time']})")
            
            print("\n  User Directory Growth:")
            print(f"    {'User':<15} {'Latest Size':<15} {'Growth (GB)':<15}")
            print("    " + "-" * 48)
            for ug in growth["user_growth"]:
                sign = "+" if ug['growth_gb'] >= 0 else ""
                print(f"    {ug['username']:<15} {ug['latest_gb']:<15.3f} {sign}{ug['growth_gb']:<15.3f}")
                
            print("\n  Directory Growth (Top 10 Growers):")
            print(f"    {'Path':<50} {'Latest (GB)':<12} {'Growth (GB)':<12}")
            print("    " + "-" * 78)
            for dg in growth["dir_growth"][:10]:
                sign = "+" if dg['growth_gb'] >= 0 else ""
                short_path = dg['path'] if len(dg['path']) <= 50 else "..." + dg['path'][-47:]
                print(f"    {short_path:<50} {dg['latest_gb']:<12.3f} {sign}{dg['growth_gb']:<12.3f}")
        else:
            print("\nGrowth analysis requires at least 2 historical scans.")
        print("\n" + "=" * 60 + "\n")

if __name__ == "__main__":
    main()
