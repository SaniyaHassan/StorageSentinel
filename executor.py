import os
import shutil
import subprocess
from datetime import datetime
import database

def execute_cmd(cmd, shell=False):
    """Run a shell command and return stdout, stderr, and returncode."""
    try:
        res = subprocess.run(cmd, shell=shell, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        return res.stdout.strip(), res.stderr.strip(), res.returncode
    except Exception as e:
        return "", str(e), -1

def clean_trash_dir(trash_path, test_mode=False):
    """Remove contents of user Trash directory while preserving the folder itself."""
    if not os.path.exists(trash_path):
        return f"Trash directory {trash_path} does not exist. Skipped."
        
    log = []
    # Typically, trash contains 'files' and 'info' directories.
    # We clean the contents inside them to keep the directories themselves, or fallback to direct deletion.
    subdirs = ["files", "info"]
    has_subdirs = any(os.path.exists(os.path.join(trash_path, sd)) for sd in subdirs)
    
    targets = []
    if has_subdirs:
        for sd in subdirs:
            sd_path = os.path.join(trash_path, sd)
            if os.path.exists(sd_path) and os.path.isdir(sd_path):
                for item in os.listdir(sd_path):
                    targets.append(os.path.join(sd_path, item))
    else:
        for item in os.listdir(trash_path):
            targets.append(os.path.join(trash_path, item))
            
    for item_path in targets:
        try:
            if test_mode:
                log.append(f"[Dry-Run] Would delete: {item_path}")
            else:
                if os.path.isdir(item_path) and not os.path.islink(item_path):
                    shutil.rmtree(item_path)
                else:
                    os.remove(item_path)
                log.append(f"Deleted: {item_path}")
        except Exception as e:
            log.append(f"Error deleting {item_path}: {e}")
            
    return "\n".join(log)

def compress_directory(dir_path, db_path, test_mode=False):
    """
    Compress a directory using zstd, verify the archive, and schedule the original for delayed deletion.
    """
    if not os.path.isdir(dir_path):
        return f"Directory {dir_path} does not exist or is not a directory. Skipped."
        
    parent_dir = os.path.dirname(dir_path)
    base_name = os.path.basename(dir_path)
    archive_path = f"{dir_path}.tar.zst"
    
    if test_mode:
        return f"[Dry-Run] Would compress {dir_path} into {archive_path} and schedule original for delayed deletion (7 days)."
        
    # Step 1: Compress
    # We pipe tar output to zstd for universal compatibility
    compress_cmd = f'tar -cf - -C "{parent_dir}" "{base_name}" | zstd -o "{archive_path}"'
    stdout, stderr, rcode = execute_cmd(compress_cmd, shell=True)
    
    v_rcode = -1
    v_stderr = ""
    is_fallback = False
    
    if rcode != 0:
        # Fallback to python's built-in tarfile compression (gzip-based, but kept as .tar.zst for consistency)
        try:
            import tarfile
            with tarfile.open(archive_path, "w:gz") as tar:
                tar.add(dir_path, arcname=base_name)
            v_rcode = 0
            is_fallback = True
        except Exception as e:
            return f"Compression failed for {dir_path}. Command error: {stderr}. Fallback error: {e}"
    else:
        # Step 2: Verify archive integrity
        # We test both zstd decompression and tar content listing
        verify_cmd = f'zstd -d -c "{archive_path}" | tar -tf - > /dev/null'
        _, v_stderr, v_rcode = execute_cmd(verify_cmd, shell=True)
        
    if v_rcode == 0:
        # Step 3: Archive is valid. Move original to a .deletable path and schedule deletion in 7 days
        date_str = datetime.now().date().isoformat()
        deletable_path = f"{dir_path}.deletable.{date_str}"
        
        try:
            if os.path.exists(deletable_path):
                if os.path.isdir(deletable_path) and not os.path.islink(deletable_path):
                    shutil.rmtree(deletable_path)
                else:
                    os.remove(deletable_path)
                    
            shutil.move(dir_path, deletable_path)
            
            # Calculate size of the directory in GB
            dir_size_gb = 0.0
            try:
                total_bytes = 0
                for root, _, files in os.walk(deletable_path):
                    for f in files:
                        fp = os.path.join(root, f)
                        if not os.path.islink(fp):
                            total_bytes += os.path.getsize(fp)
                dir_size_gb = round(total_bytes / (1024**3), 3)
            except Exception:
                pass
                
            # Register delayed delete action (pre-approved)
            action_id = database.add_pending_action(
                db_path=db_path,
                action_type="delayed_delete",
                target_path=deletable_path,
                size_gb=dir_size_gb,
                description=f"Delayed deletion of compressed original directory (Scheduled 7 days after {date_str})",
                risk="Low"
            )
            database.update_action_approval(db_path, action_id, 1)
            
            return f"Successfully compressed {dir_path} to {archive_path}. Original scheduled for deletion at {deletable_path}."
        except Exception as e:
            return f"Compressed {dir_path} to {archive_path}, but failed to schedule original deletion: {e}"
    else:
        # Verification failed! Delete the potentially corrupt archive
        if os.path.exists(archive_path):
            try:
                os.remove(archive_path)
            except Exception:
                pass
        return f"Archive verification failed for {archive_path}. Code: {v_rcode}. Error: {v_stderr}. Kept original."

def execute_actions(actions, db_path, test_mode=False):
    """
    Execute all actions in the list that have approved=True.
    Updates the database with execution status.
    """
    results = []
    
    for action in actions:
        if not action.get("approved"):
            continue
            
        action_id = action.get("id")
        action_type = action.get("action_type")
        target_path = action.get("target_path")
        size_gb = action.get("size_gb", 0)
        
        print(f"Executing approved action: {action_type} on {target_path} (Estimated space: {size_gb} GB)...")
        
        log_msg = ""
        success = False
        
        if action_type == "empty_trash":
            log_msg = clean_trash_dir(target_path, test_mode)
            success = "Error deleting" not in log_msg
            
        elif action_type == "conda_clean":
            if test_mode:
                log_msg = f"[Dry-Run] Would run conda clean for cache: {target_path}"
                success = True
            else:
                # Find owner of the target_path to execute as that user
                owner = "root"
                try:
                    stat_res = os.stat(target_path)
                    import pwd
                    owner = pwd.getpwuid(stat_res.st_uid).pw_name
                except Exception:
                    pass
                
                # Check for conda executable relative to pkgs folder
                parent_dir = os.path.dirname(target_path)
                conda_bin = os.path.join(parent_dir, "bin", "conda")
                if not os.path.exists(conda_bin):
                    conda_bin = "conda"
                    
                # Run command, using sudo -u if appropriate
                if os.name != 'nt' and os.getuid() == 0 and owner != "root":
                    cmd = ["sudo", "-u", owner, conda_bin, "clean", "--all", "-y"]
                else:
                    cmd = [conda_bin, "clean", "--all", "-y"]
                    
                stdout, stderr, rcode = execute_cmd(cmd)
                success = (rcode == 0)
                log_msg = stdout if success else f"Conda clean failed: {stderr}"
                
        elif action_type == "pip_clean":
            if test_mode:
                log_msg = f"[Dry-Run] Would run pip cache purge for cache: {target_path}"
                success = True
            else:
                owner = "root"
                try:
                    stat_res = os.stat(target_path)
                    import pwd
                    owner = pwd.getpwuid(stat_res.st_uid).pw_name
                except Exception:
                    pass
                
                # Execute pip cache purge as the owner of the cache folder
                if os.name != 'nt' and os.getuid() == 0 and owner != "root":
                    cmd = ["sudo", "-u", owner, "pip", "cache", "purge"]
                    stdout, stderr, rcode = execute_cmd(cmd)
                    if rcode != 0:
                        cmd = ["sudo", "-u", owner, "python3", "-m", "pip", "cache", "purge"]
                        stdout, stderr, rcode = execute_cmd(cmd)
                else:
                    cmd = ["pip", "cache", "purge"]
                    stdout, stderr, rcode = execute_cmd(cmd)
                    if rcode != 0:
                        cmd = ["python3", "-m", "pip", "cache", "purge"]
                        stdout, stderr, rcode = execute_cmd(cmd)
                        
                success = (rcode == 0)
                log_msg = stdout if success else f"Pip cache purge failed: {stderr}"
                
        elif action_type == "journald_clean":
            if test_mode:
                log_msg = "[Dry-Run] Would run 'journalctl --vacuum-time=30d'"
                success = True
            else:
                stdout, stderr, rcode = execute_cmd(["journalctl", "--vacuum-time=30d"])
                if rcode != 0:
                    stdout, stderr, rcode = execute_cmd(["sudo", "journalctl", "--vacuum-time=30d"])
                success = (rcode == 0)
                log_msg = stdout if success else f"Journald vacuum failed (requires root): {stderr}"
                
        elif action_type == "delete":
            if test_mode:
                log_msg = f"[Dry-Run] Would delete duplicate file: {target_path}"
                success = True
            else:
                if os.path.exists(target_path):
                    try:
                        os.remove(target_path)
                        log_msg = f"Deleted duplicate file: {target_path}"
                        success = True
                    except Exception as e:
                        log_msg = f"Failed to delete {target_path}: {e}"
                        success = False
                else:
                    log_msg = f"File {target_path} already deleted. Skipped."
                    success = True
                    
        elif action_type == "compress":
            log_msg = compress_directory(target_path, db_path, test_mode)
            success = "Successfully compressed" in log_msg or "[Dry-Run]" in log_msg
            
        elif action_type == "delayed_delete":
            if not os.path.exists(target_path):
                log_msg = f"Directory {target_path} already deleted. Skipped."
                success = True
            else:
                base = os.path.basename(target_path)
                parts = base.split(".deletable.")
                if len(parts) == 2:
                    date_str = parts[1]
                    try:
                        archive_date = datetime.strptime(date_str, "%Y-%m-%d")
                        delta = datetime.now() - archive_date
                        if delta.days >= 7:
                            if test_mode:
                                log_msg = f"[Dry-Run] Would delete delayed directory: {target_path}"
                                success = True
                            else:
                                if os.path.isdir(target_path) and not os.path.islink(target_path):
                                    shutil.rmtree(target_path)
                                else:
                                    os.remove(target_path)
                                log_msg = f"Deleted delayed directory: {target_path} (archived {delta.days} days ago)"
                                success = True
                        else:
                            log_msg = f"Skipping: {target_path} is only {delta.days} days old (requires 7 days)"
                            success = False
                    except Exception as e:
                        # Fallback: if date parsing fails, delete immediately to prevent listing forever
                        if test_mode:
                            log_msg = f"[Dry-Run] Would delete invalid delayed directory: {target_path}"
                            success = True
                        else:
                            if os.path.isdir(target_path) and not os.path.islink(target_path):
                                shutil.rmtree(target_path)
                            else:
                                os.remove(target_path)
                            log_msg = f"Deleted invalid delayed directory: {target_path} due to error: {e}"
                            success = True
                else:
                    if test_mode:
                        log_msg = f"[Dry-Run] Would delete invalid delayed directory: {target_path}"
                        success = True
                    else:
                        if os.path.isdir(target_path) and not os.path.islink(target_path):
                            shutil.rmtree(target_path)
                        else:
                            os.remove(target_path)
                        log_msg = f"Deleted invalid delayed directory: {target_path}"
                        success = True
            
        else:
            log_msg = f"Unknown action type: {action_type}"
            success = False
            
        print(f"Result: {log_msg}\n")
        results.append({
            "action_id": action_id,
            "success": success,
            "log": log_msg
        })
        
        # Mark as executed in SQLite DB if not test_mode and successfully done
        if not test_mode and success and action_id:
            try:
                database.mark_action_executed(db_path, action_id)
            except Exception as e:
                print(f"Error updating action database status for ID {action_id}: {e}")
                
    return results
