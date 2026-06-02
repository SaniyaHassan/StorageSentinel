import json
import os

class ActionReporter:
    def __init__(self, json_path="pending_actions.json"):
        self.json_path = json_path
        
    def generate_report(self, disk_usage, scan_summary, policy_results, duplicates, config=None):
        """Print a beautiful markdown terminal report."""
        print("\n" + "=" * 60)
        print("                 STORAGESENTINEL STATUS REPORT")
        print("=" * 60)
        
        # 1. Disk usage
        print("\n### 1. Filesystem Utilization")
        print(f"  Disk Usage: {disk_usage['used_size_gb']} GB / {disk_usage['total_size_gb']} GB")
        print(f"  Available:  {disk_usage['free_size_gb']} GB")
        print(f"  Utilization: {disk_usage['percent_used']}%")
        
        # Alerts
        if policy_results.get("alerts"):
            print("\n  ALERT STATUS:")
            for alert in policy_results["alerts"]:
                print(f"  [!] {alert}")
                
        # 2. User Storage Distribution with Quotas
        print("\n### 2. User Storage & Quotas")
        print(f"  {'User':<15} {'Usage (GB)':<12} {'Quota (GB)':<12} {'Status':<12}")
        print("  " + "-" * 55)
        
        user_classes = {}
        quota_limits = {}
        default_quota = 150.0
        if config:
            user_classes = config.get("user_classes", {})
            quota_limits = config.get("quotas", {})
            default_quota = quota_limits.get("default", 150.0)
            
        for user, size_gb in sorted(scan_summary["user_metrics"], key=lambda x: x[1], reverse=True):
            user_class = user_classes.get(user, "student")
            quota_gb = quota_limits.get(user_class, default_quota)
            status = "Exceeded" if size_gb > quota_gb else "OK"
            print(f"  {user:<15} {size_gb:<12.3f} {quota_gb:<12.1f} {status:<12}")
            
        # 3. File Type Analytics
        print("\n### 3. File Type Analytics")
        file_type_gb = scan_summary.get("file_type_gb", {})
        if file_type_gb:
            print(f"  {'Category':<15} {'Size (GB)':<12}")
            print("  " + "-" * 28)
            for cat, size_gb in sorted(file_type_gb.items(), key=lambda x: x[1], reverse=True):
                print(f"  {cat:<15} {size_gb:<12.3f}")
        else:
            print("  No file type analytics available.")
            
        # 4. Large files
        print("\n### 4. Large Files (>5 GB)")
        large_files = scan_summary["large_files_metrics"]
        if large_files:
            print(f"  {'Owner':<12} {'Size (GB)':<12} {'Path':<50}")
            print("  " + "-" * 80)
            for path, size_gb, owner, _ in sorted(large_files, key=lambda x: x[1], reverse=True)[:15]:
                # Truncate path for readability
                short_path = path if len(path) <= 50 else "..." + path[-47:]
                print(f"  {owner:<12} {size_gb:<12.3f} {short_path:<50}")
            if len(large_files) > 15:
                print(f"  ... and {len(large_files) - 15} more large files.")
        else:
            print("  No large files found.")
            
        # 5. Duplicate Files Summary
        print("\n### 5. Duplicate File Summary")
        if duplicates:
            print(f"  {'Size (GB)':<12} {'Duplicate File Group':<60}")
            print("  " + "-" * 80)
            total_dup_savings = 0.0
            for dup in duplicates:
                size_gb = dup["size_gb"]
                paths = dup["paths"]
                owners = dup.get("owners", ["unknown"] * len(paths))
                savings = size_gb * (len(paths) - 1)
                total_dup_savings += savings
                print(f"  {size_gb:<12.3f} Original: {os.path.basename(paths[0])} (Owner: {owners[0]})")
                for p, o in zip(paths[1:], owners[1:]):
                    print(f"               -> Duplicate: {p} (Owner: {o})")
            print(f"\n  Potential savings from duplicate removal: {total_dup_savings:.3f} GB")
        else:
            print("  No large duplicates found.")
            
        # 6. Recommended Cleanup Actions
        print("\n### 6. Recommended Cleanup Candidates")
        
        auto_actions = policy_results.get("auto_actions", [])
        manual_actions = policy_results.get("manual_actions", [])
        
        total_auto_gb = sum(a["size_gb"] for a in auto_actions)
        total_manual_gb = sum(m["size_gb"] for m in manual_actions)
        
        print(f"\n  [A] Safe Auto-Clean Actions (Potential Recovery: {total_auto_gb:.3f} GB)")
        print("  " + "-" * 80)
        if auto_actions:
            for action in auto_actions:
                risk_str = action.get("risk", "Low")
                print(f"  - [{risk_str}] {action['description']} ({action['size_gb']:.3f} GB)")
        else:
            print("  None pending.")
            
        print(f"\n  [B] Manual Approval Needed (Potential Recovery: {total_manual_gb:.3f} GB)")
        print("  " + "-" * 80)
        if manual_actions:
            for action in manual_actions:
                risk_str = action.get("risk", "Medium")
                print(f"  - [{risk_str}] {action['description']} ({action['size_gb']:.3f} GB)")
                print(f"    Target: {action['target_path']}")
        else:
            print("  None pending.")
            
        print("\n" + "=" * 60)
        print("Report generation complete.")
        print(f"Pending actions saved to: {self.json_path}")
        print("=" * 60 + "\n")
        
        # Save actions to JSON
        self._write_pending_actions_json(auto_actions, manual_actions)

    def _write_pending_actions_json(self, auto_actions, manual_actions):
        """Save actions list to pending_actions.json, merging with existing decisions."""
        existing_actions = {}
        if os.path.exists(self.json_path):
            try:
                with open(self.json_path, 'r') as f:
                    data = json.load(f)
                    for item in data:
                        # Key by action_type + target_path to preserve approval states
                        key = (item["action_type"], item["target_path"])
                        existing_actions[key] = item["approved"]
            except Exception:
                pass # If JSON is corrupt, overwrite
                
        merged_actions = []
        action_counter = 1
        
        # Combine all actions
        for item in auto_actions + manual_actions:
            key = (item["action_type"], item["target_path"])
            
            # Check if we have an existing approval decision
            approved = False
            if key in existing_actions:
                approved = existing_actions[key]
            elif item in auto_actions:
                # Safe auto actions can default to true in JSON if they are safe auto
                approved = True
                
            merged_actions.append({
                "id": action_counter,
                "action_type": item["action_type"],
                "target_path": item["target_path"],
                "size_gb": item["size_gb"],
                "description": item["description"],
                "approved": approved,
                "risk": item.get("risk", "Medium")
            })
            action_counter += 1
            
        # Write merged actions to JSON file
        with open(self.json_path, 'w') as f:
            json.dump(merged_actions, f, indent=2)
            
    def load_actions_from_json(self):
        """Load recommendations list from pending_actions.json."""
        if not os.path.exists(self.json_path):
            return []
        try:
            with open(self.json_path, 'r') as f:
                return json.load(f)
        except Exception:
            return []
            
    def save_actions_to_json(self, actions):
        """Save raw actions back to pending_actions.json (e.g. after approval edits)."""
        with open(self.json_path, 'w') as f:
            json.dump(actions, f, indent=2)
