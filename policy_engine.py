import os

try:
    import pwd
except ImportError:
    pwd = None

def get_file_owner(uid):
    """Retrieve username from UID, fallback to UID string on error."""
    if pwd is not None:
        try:
            return pwd.getpwuid(uid).pw_name
        except Exception:
            pass
    return str(uid)

def determine_risk_score(action_type, target_path):
    """Determine the risk score of an action based on its type and target path."""
    path_lower = target_path.lower()
    
    # Critical Risk
    critical_paths = ["/var/lib/postgresql", "/var/lib/mysql", "/etc", "/boot", "/var/lib/docker", "postgresql", "postgres"]
    if any(cp in path_lower for cp in critical_paths):
        return "Critical"
        
    # Low Risk
    if action_type in ["empty_trash", "conda_clean", "pip_clean", "journald_clean"]:
        return "Low"
        
    # High Risk
    high_risk_exts = ['.bin', '.pt', '.pth', '.ckpt', '.safetensors', '.onnx', '.gguf', '.h5', '.model', '.weights',
                      '.csv', '.tsv', '.json', '.parquet', '.hdf5', '.npz', '.npy', '.xml', '.db', '.sqlite', '.sqlite3']
    _, ext = os.path.splitext(path_lower)
    if ext in high_risk_exts or "model" in path_lower or "dataset" in path_lower:
        return "High"
        
    # Medium Risk
    medium_risk_exts = {'.iso', '.img', '.dmg'}
    if ext in medium_risk_exts or "iso" in path_lower:
        return "Medium"
        
    # Default for directory compression or deletion of other files
    if action_type == "compress":
        return "Medium"
        
    return "Medium"

def is_protected_target(target_path, protected_paths=None, protected_extensions=None):
    """True if a target is within a protected path or has a protected (database) extension."""
    protected_paths = protected_paths or []
    protected_extensions = set(e.lower() for e in (protected_extensions or []))

    if target_path:
        abs_t = os.path.abspath(target_path)
        for p in protected_paths:
            ap = os.path.abspath(p)
            if abs_t == ap or abs_t.startswith(ap + os.sep):
                return True
        _, ext = os.path.splitext(target_path.lower())
        if ext in protected_extensions:
            return True
    return False


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
        
        # 1. Ephemeral Caches (Trash is safe auto, Conda/Pip are split per-user and manual)
        auto_cfg = self.config.get("auto_cleanup", {})
        caches = scan_results.get("caches", {})
        
        # Trash cleanup
        for trash in caches.get("trash", []):
            action = {
                "action_type": "empty_trash",
                "target_path": trash["path"],
                "size_gb": trash["size_gb"],
                "description": f"Empty trash directory for user at {trash['path']}",
                "risk": determine_risk_score("empty_trash", trash["path"])
            }
            # If disk usage is high enough, we can auto-cleanup, but we list it as auto_action
            if percent_used >= thresholds.get("warning", 80.0):
                auto_actions.append(action)
            else:
                manual_actions.append(action)
                
        # Conda Cache - Split per path and listed as manual actions
        if auto_cfg.get("clean_conda_cache", True):
            for conda_cache in caches.get("conda", []):
                path = conda_cache["path"]
                try:
                    stat_res = os.stat(path)
                    owner = get_file_owner(stat_res.st_uid)
                except Exception:
                    owner = "unknown"
                
                action = {
                    "action_type": "conda_clean",
                    "target_path": path,
                    "size_gb": conda_cache["size_gb"],
                    "description": f"Clean conda package cache for user {owner} at {path}",
                    "risk": determine_risk_score("conda_clean", path)
                }
                manual_actions.append(action)
                    
        # Pip Cache - Split per path and listed as manual actions
        if auto_cfg.get("clean_pip_cache", True):
            for pip_cache in caches.get("pip", []):
                path = pip_cache["path"]
                try:
                    stat_res = os.stat(path)
                    owner = get_file_owner(stat_res.st_uid)
                except Exception:
                    owner = "unknown"
                
                action = {
                    "action_type": "pip_clean",
                    "target_path": path,
                    "size_gb": pip_cache["size_gb"],
                    "description": f"Clean pip cache for user {owner} at {path}",
                    "risk": determine_risk_score("pip_clean", path)
                }
                manual_actions.append(action)
                    
        # Journald logs
        if auto_cfg.get("clean_journald_logs", True):
            action = {
                "action_type": "journald_clean",
                "target_path": "journald",
                "size_gb": 0.5, # Estimate
                "description": f"Vacuum systemd journald logs to keep last {auto_cfg.get('journald_max_age_days', 30)} days",
                "risk": determine_risk_score("journald_clean", "journald")
            }
            if percent_used >= thresholds.get("warning", 80.0):
                auto_actions.append(action)
            else:
                manual_actions.append(action)

        # 2. Large Duplicate Files (RECOMMENDED_ACTION) - Strictly Manual
        duplicates = scan_results.get("duplicates", [])
        for dup in duplicates:
            paths = dup["paths"]
            owners = dup.get("owners", ["unknown"] * len(paths))
            original = paths[0]
            orig_owner = owners[0]
            
            for path, owner in zip(paths[1:], owners[1:]):
                desc = (
                    f"Duplicate detected:\n"
                    f"  File to delete: {path} (Owner: {owner})\n"
                    f"  Keep original:  {original} (Owner: {orig_owner})\n"
                    f"  Potential saving: {dup['size_gb']:.3f} GB\n"
                    f"  Admin approval required"
                )
                manual_actions.append({
                    "action_type": "delete",
                    "target_path": path,
                    "size_gb": dup["size_gb"],
                    "description": desc,
                    "risk": determine_risk_score("delete", path)
                })

        # 3. Cold Inactive Directories (RECOMMENDED_ACTION) - Strictly Manual
        cold_dirs = scan_results.get("cold_directories", [])
        for cd in cold_dirs:
            path = cd["path"]
            # Skip if it is a top-level home directory itself
            path_parts = path.strip(os.sep).split(os.sep)
            if len(path_parts) <= 2: 
                continue
                
            manual_actions.append({
                "action_type": "compress",
                "target_path": path,
                "size_gb": cd["size_gb"],
                "description": f"Compress cold directory (inactive for {cd['days_inactive']} days)",
                "risk": determine_risk_score("compress", path)
            })

        # 4. User Quotas validation
        user_classes = self.config.get("user_classes", {})
        quota_limits = self.config.get("quotas", {})
        default_quota = quota_limits.get("default", 150.0)
        
        quota_warnings = []
        user_metrics = scan_results.get("user_metrics", [])
        for username, size_gb in user_metrics:
            user_class = user_classes.get(username, "student")
            quota_gb = quota_limits.get(user_class, default_quota)
            if size_gb > quota_gb:
                quota_warnings.append({
                    "username": username,
                    "usage_gb": size_gb,
                    "quota_gb": quota_gb,
                    "status": "Exceeded"
                })

        # Hard safety guarantee: never recommend acting on a protected path/extension,
        # and never recommend a Critical-risk action. This is enforced again in the executor.
        protected_paths = self.config.get("protected_paths", [])
        protected_extensions = self.config.get("protected_extensions", [])

        def _is_allowed(action):
            if action.get("risk") == "Critical":
                return False
            return not is_protected_target(action.get("target_path"), protected_paths, protected_extensions)

        auto_actions = [a for a in auto_actions if _is_allowed(a)]
        manual_actions = [a for a in manual_actions if _is_allowed(a)]

        return {
            "alerts": alerts,
            "auto_actions": auto_actions,
            "manual_actions": manual_actions,
            "quota_warnings": quota_warnings
        }
