import os

class PolicyEngine:
    def __init__(self, config):
        self.config = config
        
    def evaluate(self, scan_results, disk_usage):
        """
        Evaluate scanned data against policies.
        scan_results: output dict from FileSystemScanner.get_summary() + duplicate list
        disk_usage: output dict from get_disk_usage()
        """
        percent_used = disk_usage["percent_used"]
        thresholds = self.config.get("alert_thresholds", {})
        
        alerts = []
        # Check alerts
        if percent_used >= thresholds.get("emergency", 95.0):
            alerts.append(f"EMERGENCY: Disk usage is at {percent_used}% (>= 95%)!")
        elif percent_used >= thresholds.get("critical", 90.0):
            alerts.append(f"CRITICAL WARNING: Disk usage is at {percent_used}% (>= 90%)!")
        elif percent_used >= thresholds.get("warning", 80.0):
            alerts.append(f"WARNING: Disk usage is at {percent_used}% (>= 80%)!")
            
        auto_actions = []
        manual_actions = []
        
        # 1. Ephemeral Caches (SAFE_AUTO)
        auto_cfg = self.config.get("auto_cleanup", {})
        caches = scan_results.get("caches", {})
        
        # Trash cleanup
        for trash in caches.get("trash", []):
            # Recommend clearing trash
            # By default, we empty files in Trash.
            action = {
                "action_type": "empty_trash",
                "target_path": trash["path"],
                "size_gb": trash["size_gb"],
                "description": f"Empty trash directory for user at {trash['path']}"
            }
            # If disk usage is high enough, we can auto-cleanup, but we list it as auto_action
            if percent_used >= thresholds.get("warning", 80.0):
                auto_actions.append(action)
            else:
                manual_actions.append(action)
                
        # Conda Cache
        if auto_cfg.get("clean_conda_cache", True):
            total_conda_gb = sum(c["size_gb"] for c in caches.get("conda", []))
            if total_conda_gb > 0:
                action = {
                    "action_type": "conda_clean",
                    "target_path": "conda",
                    "size_gb": round(total_conda_gb, 3),
                    "description": "Clean conda package caches ('conda clean --all -y')"
                }
                if percent_used >= thresholds.get("warning", 80.0):
                    auto_actions.append(action)
                else:
                    manual_actions.append(action)
                    
        # Pip Cache
        if auto_cfg.get("clean_pip_cache", True):
            total_pip_gb = sum(p["size_gb"] for p in caches.get("pip", []))
            if total_pip_gb > 0:
                action = {
                    "action_type": "pip_clean",
                    "target_path": "pip",
                    "size_gb": round(total_pip_gb, 3),
                    "description": "Purge pip install cache ('pip cache purge')"
                }
                if percent_used >= thresholds.get("warning", 80.0):
                    auto_actions.append(action)
                else:
                    manual_actions.append(action)
                    
        # Journald logs
        if auto_cfg.get("clean_journald_logs", True):
            # We don't have direct size for journald from scanning since it's in /var/log/journal
            # but we can propose a vacuum command as a safe auto-action if space is tight.
            # We assume it cleans up about 0.5 GB to 2 GB.
            action = {
                "action_type": "journald_clean",
                "target_path": "journald",
                "size_gb": 0.5, # Estimate
                "description": f"Vacuum systemd journald logs to keep last {auto_cfg.get('journald_max_age_days', 30)} days"
            }
            if percent_used >= thresholds.get("warning", 80.0):
                auto_actions.append(action)
            else:
                manual_actions.append(action)

        # 2. Large Duplicate Files (RECOMMENDED_ACTION)
        duplicates = scan_results.get("duplicates", [])
        for dup in duplicates:
            # dup is {"size_gb": X, "paths": [path1, path2, ...]}
            # Keep the first path as original, recommend deleting all subsequent paths
            paths = dup["paths"]
            original = paths[0]
            redundant_paths = paths[1:]
            
            for path in redundant_paths:
                manual_actions.append({
                    "action_type": "delete",
                    "target_path": path,
                    "size_gb": dup["size_gb"],
                    "description": f"Remove duplicate file (Keep original copy at: {original})"
                })

        # 3. Cold Inactive Directories (RECOMMENDED_ACTION)
        cold_dirs = scan_results.get("cold_directories", [])
        for cd in cold_dirs:
            # Exclude paths that represent system or config files
            path = cd["path"]
            # Skip if it is a top-level home directory itself (don't compress /home/user itself!)
            path_parts = path.strip(os.sep).split(os.sep)
            if len(path_parts) <= 2: 
                # E.g., ['home', 'user'] -> length 2. Skip this.
                continue
                
            manual_actions.append({
                "action_type": "compress",
                "target_path": path,
                "size_gb": cd["size_gb"],
                "description": f"Compress cold directory (inactive for {cd['days_inactive']} days)"
            })

        return {
            "alerts": alerts,
            "auto_actions": auto_actions,
            "manual_actions": manual_actions
        }
