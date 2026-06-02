import os
import shutil
import subprocess
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

def compress_directory(dir_path, test_mode=False):
    """
    Compress a directory using zstd, verify the archive, and delete the original.
    """
    if not os.path.isdir(dir_path):
        return f"Directory {dir_path} does not exist or is not a directory. Skipped."
        
    parent_dir = os.path.dirname(dir_path)
    base_name = os.path.basename(dir_path)
    archive_path = f"{dir_path}.tar.zst"
    
    if test_mode:
        return f"[Dry-Run] Would compress {dir_path} into {archive_path} and then delete the original folder."
        
    # Step 1: Compress
    # We pipe tar output to zstd for universal compatibility
    compress_cmd = f'tar -cf - -C "{parent_dir}" "{base_name}" | zstd -o "{archive_path}"'
    stdout, stderr, rcode = execute_cmd(compress_cmd, shell=True)
    if rcode != 0:
        return f"Compression failed for {dir_path}. Code: {rcode}. Error: {stderr}"
        
    # Step 2: Verify archive integrity
    # We test both zstd decompression and tar content listing
    verify_cmd = f'zstd -d -c "{archive_path}" | tar -tf - > /dev/null'
    _, v_stderr, v_rcode = execute_cmd(verify_cmd, shell=True)
    
    if v_rcode == 0:
        # Step 3: Archive is valid. Delete the original directory.
        try:
            shutil.rmtree(dir_path)
            return f"Successfully compressed {dir_path} to {archive_path} and removed original."
        except Exception as e:
            return f"Compressed {dir_path} to {archive_path}, but failed to delete original folder: {e}"
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
                log_msg = "[Dry-Run] Would run 'conda clean --all -y'"
                success = True
            else:
                stdout, stderr, rcode = execute_cmd(["conda", "clean", "--all", "-y"])
                success = (rcode == 0)
                log_msg = stdout if success else f"Conda clean failed: {stderr}"
                
        elif action_type == "pip_clean":
            if test_mode:
                log_msg = "[Dry-Run] Would run 'pip cache purge'"
                success = True
            else:
                stdout, stderr, rcode = execute_cmd(["pip", "cache", "purge"])
                if rcode != 0:
                    # Fallback to python3 -m pip cache purge
                    stdout, stderr, rcode = execute_cmd(["python3", "-m", "pip", "cache", "purge"])
                success = (rcode == 0)
                log_msg = stdout if success else f"Pip cache purge failed: {stderr}"
                
        elif action_type == "journald_clean":
            if test_mode:
                log_msg = "[Dry-Run] Would run 'journalctl --vacuum-time=30d'"
                success = True
            else:
                stdout, stderr, rcode = execute_cmd(["journalctl", "--vacuum-time=30d"])
                if rcode != 0:
                    # Try with sudo if running normally failed
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
            log_msg = compress_directory(target_path, test_mode)
            success = "Successfully compressed" in log_msg or "[Dry-Run]" in log_msg
            
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
