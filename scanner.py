import os
import shutil
import hashlib
from collections import defaultdict
from datetime import datetime

try:
    import pwd
except ImportError:
    pwd = None

def get_disk_usage(path):
    """Get disk usage metrics for the filesystem containing the path."""
    total, used, free = shutil.disk_usage(path)
    percent = (used / total) * 100 if total > 0 else 0
    return {
        "total_size_gb": round(total / (1024**3), 2),
        "used_size_gb": round(used / (1024**3), 2),
        "free_size_gb": round(free / (1024**3), 2),
        "percent_used": round(percent, 2)
    }

def get_file_owner(uid):
    """Retrieve username from UID, fallback to UID string on error."""
    if pwd is not None:
        try:
            return pwd.getpwuid(uid).pw_name
        except Exception:
            pass
    return str(uid)

def calculate_sha256_partial(file_path, bytes_to_read=1024*1024):
    """Compute SHA256 hash of the first chunk of the file for fast initial matching."""
    hasher = hashlib.sha256()
    try:
        with open(file_path, 'rb') as f:
            chunk = f.read(bytes_to_read)
            hasher.update(chunk)
        return hasher.hexdigest()
    except Exception:
        return None

def calculate_sha256_full(file_path):
    """Compute full SHA256 hash of a file."""
    hasher = hashlib.sha256()
    try:
        with open(file_path, 'rb') as f:
            for chunk in iter(lambda: f.read(65536), b''):
                hasher.update(chunk)
        return hasher.hexdigest()
    except Exception:
        return None

class FileSystemScanner:
    def __init__(self, root_path, exclusions=None, large_file_threshold_gb=5.0, cold_data_days=180, min_dir_size_gb=1.0):
        self.root_path = os.path.abspath(root_path)
        self.exclusions = exclusions or []
        # Convert exclusions to absolute paths if they look like system paths
        self.exclusions = [os.path.abspath(e) if e.startswith('/') else e for e in self.exclusions]
        self.large_file_threshold = large_file_threshold_gb * (1024**3)
        self.cold_data_days = cold_data_days
        self.min_dir_size_gb = min_dir_size_gb
        self.now = datetime.now()
        
        # Scanner results
        self.user_sizes = defaultdict(int) # username -> bytes
        self.dir_sizes = defaultdict(int)  # directory_path -> bytes
        self.dir_mtimes = {}               # directory_path -> latest activity time (max of mtime and atime)
        self.large_files = []              # list of dicts
        self.size_groups = defaultdict(list) # file_size -> list of file paths (for duplicate detection, size >= 50MB)
        self.trash_paths = []              # paths to Trash directories
        self.conda_paths = []              # paths to conda pkgs cache directories
        self.pip_paths = []                # paths to pip cache directories
        self.duplicates = []               # list of duplicate group dicts
        self.file_type_sizes = defaultdict(int) # file category -> bytes
        
    def is_excluded(self, path):
        """Check if path or its basename is in exclusions."""
        abs_path = os.path.abspath(path)
        base_name = os.path.basename(path)
        
        for excl in self.exclusions:
            if excl.startswith('/'):
                # Exact path match or starts with excluded path
                if abs_path == excl or abs_path.startswith(excl + '/'):
                    return True
            else:
                # Basename match
                if base_name == excl:
                    return True
        return False

    def _categorize_file(self, file_path, file_size):
        """Categorize file size into specific analytics buckets."""
        abs_path = file_path.lower()
        
        # Check path first (highest priority)
        if "trash" in abs_path or ".local/share/trash" in abs_path:
            return "Trash"
        if "cache" in abs_path or "pkgs" in abs_path or "pip" in abs_path:
            return "Caches"
            
        # Check extensions
        _, ext = os.path.splitext(abs_path)
        
        videos = {'.mp4', '.mkv', '.avi', '.mov', '.flv', '.webm', '.mpeg', '.mpg'}
        isos = {'.iso', '.img', '.dmg'}
        models = {'.bin', '.pt', '.pth', '.ckpt', '.safetensors', '.onnx', '.gguf', '.h5', '.model', '.weights'}
        # Live databases are NOT disposable datasets - keep them in their own category.
        databases = {'.db', '.sqlite', '.sqlite3'}
        datasets = {'.csv', '.tsv', '.json', '.parquet', '.hdf5', '.npz', '.npy', '.xml'}

        if ext in videos:
            return "Videos"
        elif ext in isos:
            return "ISOs"
        elif ext in models:
            return "AI Models"
        elif ext in databases:
            return "Databases"
        elif ext in datasets:
            return "Datasets"
        else:
            return "Other"

    def scan(self):
        """Perform the recursive file system scan."""
        if not os.path.exists(self.root_path):
            print(f"Error: Scan root path {self.root_path} does not exist.")
            return []
            
        print(f"Starting scan of: {self.root_path}")
        self._scan_directory(self.root_path)
        print("Scan complete. Processing duplicate detection...")
        self.duplicates = self._find_duplicates()
        return self.duplicates

    def _scan_directory(self, dir_path):
        """Recursively scan a directory using os.scandir."""
        if self.is_excluded(dir_path):
            return 0
            
        total_size = 0
        latest_active = 0
        
        try:
            with os.scandir(dir_path) as entries:
                for entry in entries:
                    try:
                        # Skip if symlink to avoid cycles / external scans
                        if entry.is_symlink():
                            continue
                            
                        if entry.is_file():
                            stat_res = entry.stat(follow_symlinks=False)
                            file_size = stat_res.st_size
                            total_size += file_size
                            
                            # Track latest mtime/atime for the directory
                            mtime = stat_res.st_mtime
                            atime = stat_res.st_atime
                            activity_time = max(mtime, atime)
                            if activity_time > latest_active:
                                latest_active = activity_time
                                
                            # Categorize file type
                            category = self._categorize_file(entry.path, file_size)
                            self.file_type_sizes[category] += file_size
                                
                            # Track large files
                            if file_size >= self.large_file_threshold:
                                owner = get_file_owner(stat_res.st_uid)
                                self.large_files.append({
                                    "path": entry.path,
                                    "size_gb": round(file_size / (1024**3), 3),
                                    "owner": owner,
                                    "last_accessed": datetime.fromtimestamp(stat_res.st_atime).isoformat()
                                })
                                
                            # Track size groups for duplicate files (limit duplicate detection to files >= 50MB to be fast and high-impact)
                            if file_size >= 50 * (1024**2):
                                self.size_groups[file_size].append(entry.path)
                                
                        elif entry.is_dir():
                            # Detect special directories
                            name = entry.name
                            path = entry.path
                            
                            # Trash folder detection
                            if name == "Trash" or path.endswith(".local/share/Trash"):
                                self.trash_paths.append(path)
                                
                            # Conda package cache detection
                            if name == "pkgs" and ("miniconda" in path or "conda" in path or "anaconda" in path):
                                self.conda_paths.append(path)
                                
                            # Pip cache detection
                            if name == "pip" and ("/.cache/pip" in path or "/pip/cache" in path):
                                self.pip_paths.append(path)
                                
                            # Recurse
                            sub_size = self._scan_directory(path)
                            total_size += sub_size
                            
                            # Propagate latest activity time from subdirectory
                            sub_mtime = self.dir_mtimes.get(path, 0)
                            if sub_mtime > latest_active:
                                latest_active = sub_mtime
                            
                    except PermissionError:
                        # Silently skip individual files/folders we don't have access to
                        continue
                    except Exception as e:
                        print(f"Warning: Error scanning entry {entry.path}: {e}")
                        continue
                        
        except PermissionError:
            # Skip directories we don't have read access to
            return 0
        except Exception as e:
            print(f"Warning: Error scanning directory {dir_path}: {e}")
            return 0
            
        # Record directory size and mtime
        self.dir_sizes[dir_path] = total_size
        if latest_active > 0:
            self.dir_mtimes[dir_path] = latest_active
            
        # Map user usage if we are scanning user home directories
        # E.g. if root is /home, then immediate children of /home are users
        parent_dir = os.path.dirname(dir_path)
        if parent_dir == self.root_path:
            username = os.path.basename(dir_path)
            self.user_sizes[username] += total_size
            
        return total_size

    def _find_duplicates(self):
        """Find duplicate files based on size and SHA256 hashing."""
        duplicates_found = []
        
        for size, file_paths in self.size_groups.items():
            if len(file_paths) < 2:
                continue
                
            # Step 1: Compute partial SHA256 (first 1MB) to group potential duplicates
            partial_groups = defaultdict(list)
            for path in file_paths:
                p_hash = calculate_sha256_partial(path)
                if p_hash:
                    partial_groups[p_hash].append(path)
                    
            # Step 2: For groups that still have >= 2 items, check full SHA256 hash
            for p_hash, candidate_paths in partial_groups.items():
                if len(candidate_paths) < 2:
                    continue
                    
                full_groups = defaultdict(list)
                for path in candidate_paths:
                    f_hash = calculate_sha256_full(path)
                    if f_hash:
                        full_groups[f_hash].append(path)
                        
                for f_hash, dup_paths in full_groups.items():
                    if len(dup_paths) >= 2:
                        # Map owners of duplicate files
                        owners = []
                        for p in dup_paths:
                            try:
                                stat_res = os.stat(p)
                                owner = get_file_owner(stat_res.st_uid)
                            except Exception:
                                owner = "unknown"
                            owners.append(owner)
                            
                        duplicates_found.append({
                            "size_gb": round(size / (1024**3), 3),
                            "paths": dup_paths,
                            "owners": owners
                        })
                        
        return duplicates_found

    def get_summary(self):
        """Compile and format scan metrics for database insertion."""
        # 1. User usage
        user_metrics = []
        for user, size_bytes in self.user_sizes.items():
            user_metrics.append((user, round(size_bytes / (1024**3), 3)))
            
        # 2. Directory usage (only record directories with size > min_dir_size_gb to prevent DB bloat)
        directory_metrics = []
        min_dir_size_bytes = self.min_dir_size_gb * (1024**3)
        for path, size_bytes in self.dir_sizes.items():
            if size_bytes >= min_dir_size_bytes:
                mtime = self.dir_mtimes.get(path)
                mtime_str = datetime.fromtimestamp(mtime).isoformat() if mtime else None
                directory_metrics.append((path, round(size_bytes / (1024**3), 3), mtime_str))
                
        # 3. Large files
        large_files_metrics = []
        for lf in self.large_files:
            large_files_metrics.append((lf['path'], lf['size_gb'], lf['owner'], lf['last_accessed']))
            
        # Identify cold directories: directories with size > min_dir_size_gb and mtime/activity older than cold_data_days
        cold_directories = []
        for path, size_bytes in self.dir_sizes.items():
            if size_bytes >= min_dir_size_bytes:
                mtime = self.dir_mtimes.get(path)
                if mtime:
                    days_inactive = (self.now - datetime.fromtimestamp(mtime)).days
                    if days_inactive >= self.cold_data_days:
                        cold_directories.append({
                            "path": path,
                            "size_gb": round(size_bytes / (1024**3), 3),
                            "days_inactive": days_inactive
                        })
                        
        # Cache details
        caches = {
            "trash": [],
            "conda": [],
            "pip": []
        }
        
        # Calculate Trash sizes
        for path in self.trash_paths:
            size_bytes = self.dir_sizes.get(path, 0)
            if size_bytes > 0:
                caches["trash"].append({"path": path, "size_gb": round(size_bytes / (1024**3), 3)})
                
        # Calculate Conda cache sizes
        for path in self.conda_paths:
            size_bytes = self.dir_sizes.get(path, 0)
            if size_bytes > 0:
                caches["conda"].append({"path": path, "size_gb": round(size_bytes / (1024**3), 3)})
                
        # Calculate Pip cache sizes
        for path in self.pip_paths:
            size_bytes = self.dir_sizes.get(path, 0)
            if size_bytes > 0:
                caches["pip"].append({"path": path, "size_gb": round(size_bytes / (1024**3), 3)})
                
        # File type summary in GB
        categories = ["Videos", "ISOs", "AI Models", "Databases", "Datasets", "Caches", "Trash", "Other"]
        file_type_gb = {cat: 0.0 for cat in categories}
        for category, size_bytes in self.file_type_sizes.items():
            file_type_gb[category] = round(size_bytes / (1024**3), 3)

        return {
            "user_metrics": user_metrics,
            "directory_metrics": directory_metrics,
            "large_files_metrics": large_files_metrics,
            "cold_directories": cold_directories,
            "caches": caches,
            "duplicates": self.duplicates,
            "file_type_gb": file_type_gb
        }
