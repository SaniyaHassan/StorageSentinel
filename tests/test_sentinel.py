import os
import shutil
import unittest
import time
import json
import yaml
from datetime import datetime

# Import modules to test
import database
import scanner
import policy_engine
import reporter
import executor
import sentinel

TEST_ENV_ROOT = "./test_env"
TEST_DB_PATH = "./test_env/test_sentinel.db"
TEST_JSON_PATH = "./test_env/test_pending_actions.json"
TEST_CONFIG_PATH = "./test_env/test_config.yaml"

class TestStorageSentinel(unittest.TestCase):
    def setUp(self):
        """Set up a mock filesystem environment and configuration."""
        if os.path.exists(TEST_ENV_ROOT):
            shutil.rmtree(TEST_ENV_ROOT)
        os.makedirs(TEST_ENV_ROOT)
        
        # Create user folders
        self.user1_path = os.path.join(TEST_ENV_ROOT, "user1")
        self.user2_path = os.path.join(TEST_ENV_ROOT, "user2")
        os.makedirs(self.user1_path)
        os.makedirs(self.user2_path)
        
        # 1. Create a dummy large file (e.g. 6 MB for testing, but let's change threshold to 5 MB in test config)
        self.large_file_path = os.path.join(self.user1_path, "large_file.dat")
        with open(self.large_file_path, "wb") as f:
            f.write(b"\0" * (6 * 1024 * 1024)) # 6 MB
            
        # 2. Create duplicate files (must be >= 50MB to trigger duplicate check, or let's override size group limit during test,
        # but we can also write 50MB files using sparse files to save time and disk space!)
        self.dup1_path = os.path.join(self.user1_path, "dup1.bin")
        self.dup2_path = os.path.join(self.user2_path, "dup2.bin")
        
        # Use seek to create sparse files of 51MB (takes almost 0 seconds and uses 0 disk space, but reports 51MB size!)
        with open(self.dup1_path, "wb") as f:
            f.write(b"DUPLICATE_CONTENT_HEADER")
            f.seek(51 * 1024 * 1024 - 1)
            f.write(b"\0")
            
        with open(self.dup2_path, "wb") as f:
            f.write(b"DUPLICATE_CONTENT_HEADER")
            f.seek(51 * 1024 * 1024 - 1)
            f.write(b"\0")
            
        # 3. Create expired Trash folder in user1
        self.trash_dir = os.path.join(self.user1_path, ".local/share/Trash")
        self.trash_files_dir = os.path.join(self.trash_dir, "files")
        os.makedirs(self.trash_files_dir)
        self.old_trash_file = os.path.join(self.trash_files_dir, "old_cricket.mp4")
        with open(self.old_trash_file, "w") as f:
            f.write("cricket match video")
            
        # Set modification time of old trash file to 40 days ago
        past_time = time.time() - (40 * 24 * 3600)
        os.utime(self.old_trash_file, (past_time, past_time))
        os.utime(self.trash_files_dir, (past_time, past_time))
        os.utime(self.trash_dir, (past_time, past_time))
        
        # 4. Create a cold directory in user2
        self.cold_dir = os.path.join(self.user2_path, "cold_research_project")
        os.makedirs(self.cold_dir)
        self.cold_file = os.path.join(self.cold_dir, "dataset.csv")
        with open(self.cold_file, "w") as f:
            f.write("id,value\n1,100\n")
            
        # Set mtime/atime of cold dir and its files to 200 days ago
        cold_time = time.time() - (200 * 24 * 3600)
        os.utime(self.cold_file, (cold_time, cold_time))
        os.utime(self.cold_dir, (cold_time, cold_time))
        
        # Create test config yaml file
        self.config_data = {
            "scan_root": TEST_ENV_ROOT,
            "min_dir_size_gb": 0.0,
            "alert_thresholds": {
                "warning": 80.0,
                "critical": 90.0,
                "emergency": 95.0
            },
            "large_file_threshold_gb": 0.005, # 5 MB (so our 6MB file triggers it)
            "cold_data_days": 180,
            "auto_cleanup": {
                "trash_max_age_days": 30,
                "clean_conda_cache": True,
                "clean_pip_cache": True,
                "clean_journald_logs": False # skip system journald for unit test
            },
            "exclusions": [".git", "test_sentinel.db"]
        }
        
        with open(TEST_CONFIG_PATH, "w") as f:
            yaml.dump(self.config_data, f)
            
        # Init SQLite Database
        database.init_db(TEST_DB_PATH)

    def tearDown(self):
        """Clean up dummy environment."""
        if os.path.exists(TEST_ENV_ROOT):
            shutil.rmtree(TEST_ENV_ROOT)

    def test_scanner(self):
        """Verify that scanner correctly maps directories, large files, and duplicates."""
        s = scanner.FileSystemScanner(
            root_path=TEST_ENV_ROOT,
            exclusions=self.config_data["exclusions"],
            large_file_threshold_gb=self.config_data["large_file_threshold_gb"],
            cold_data_days=self.config_data["cold_data_days"],
            min_dir_size_gb=0.0
        )
        duplicates = s.scan()
        summary = s.get_summary()
        
        # Assert large file detected (6MB is > 5MB threshold)
        large_files = summary["large_files_metrics"]
        large_file_paths = [lf[0] for lf in large_files]
        self.assertIn(os.path.abspath(self.large_file_path), large_file_paths)
        
        # Assert duplicate files detected (51MB files have same content hash and size)
        self.assertEqual(len(duplicates), 1)
        dup_paths = duplicates[0]["paths"]
        self.assertIn(os.path.abspath(self.dup1_path), dup_paths)
        self.assertIn(os.path.abspath(self.dup2_path), dup_paths)
        
        # Assert trash folder detected
        self.assertEqual(len(summary["caches"]["trash"]), 1)
        self.assertEqual(summary["caches"]["trash"][0]["path"], os.path.abspath(self.trash_dir))
        
        # Assert cold directory detected
        cold_dirs = [cd["path"] for cd in summary["cold_directories"]]
        self.assertIn(os.path.abspath(self.cold_dir), cold_dirs)

    def test_policy_engine(self):
        """Verify that policy engine categorizes actions correctly."""
        s = scanner.FileSystemScanner(
            root_path=TEST_ENV_ROOT,
            exclusions=self.config_data["exclusions"],
            large_file_threshold_gb=self.config_data["large_file_threshold_gb"],
            cold_data_days=self.config_data["cold_data_days"],
            min_dir_size_gb=0.0
        )
        duplicates = s.scan()
        summary = s.get_summary()
        
        # We override disk usage report to trigger warning alert (say 85% used)
        disk_usage = {
            "total_size_gb": 100.0,
            "used_size_gb": 85.0,
            "free_size_gb": 15.0,
            "percent_used": 85.0
        }
        
        pe = policy_engine.PolicyEngine(self.config_data)
        results = pe.evaluate(summary, disk_usage)
        
        # Verify warnings/alerts
        self.assertEqual(len(results["alerts"]), 1)
        self.assertIn("WARNING", results["alerts"][0])
        
        # Because percent_used >= warning threshold, Trash empty should go to auto_actions
        auto_actions_types = [a["action_type"] for a in results["auto_actions"]]
        self.assertIn("empty_trash", auto_actions_types)
        
        # Manual actions should include compression of cold directory and duplicate removal
        manual_actions_types = [m["action_type"] for m in results["manual_actions"]]
        self.assertIn("compress", manual_actions_types)
        self.assertIn("delete", manual_actions_types)

    def test_executor_and_db_flow(self):
        """Test full system database recording, approval, and execution flow."""
        # 1. Run Scan
        disk_usage = {
            "total_size_gb": 100.0,
            "used_size_gb": 40.0,
            "free_size_gb": 60.0,
            "percent_used": 40.0
        }
        s = scanner.FileSystemScanner(
            root_path=TEST_ENV_ROOT,
            exclusions=self.config_data["exclusions"],
            large_file_threshold_gb=self.config_data["large_file_threshold_gb"],
            cold_data_days=self.config_data["cold_data_days"],
            min_dir_size_gb=0.0
        )
        duplicates = s.scan()
        summary = s.get_summary()
        
        # Record scan to SQLite DB
        scan_id = database.record_scan(
            db_path=TEST_DB_PATH,
            system_metrics=disk_usage,
            user_metrics=summary["user_metrics"],
            directory_metrics=summary["directory_metrics"],
            large_files_metrics=summary["large_files_metrics"]
        )
        self.assertIsNotNone(scan_id)
        
        # 2. Policy evaluation
        pe = policy_engine.PolicyEngine(self.config_data)
        results = pe.evaluate(summary, disk_usage)
        
        # Save to JSON
        rep = reporter.ActionReporter(TEST_JSON_PATH)
        rep.generate_report(disk_usage, summary, results, duplicates)
        
        # Sync to DB
        actions = rep.load_actions_from_json()
        sentinel.sync_actions_to_db(TEST_DB_PATH, actions)
        
        # Verify db records
        conn = database.get_connection(TEST_DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) as cnt FROM pending_actions")
        self.assertGreaterEqual(cursor.fetchone()["cnt"], 2)
        conn.close()
        
        # 3. Simulate User Approving duplicate delete and cold compress
        # Load from json, set approved = True
        for a in actions:
            if a["action_type"] in ["delete", "compress", "empty_trash"]:
                a["approved"] = True
                
        rep.save_actions_to_json(actions)
        sentinel.sync_actions_to_db(TEST_DB_PATH, actions)
        
        # 4. Clean execution
        # Verify targets exist before cleaning
        self.assertTrue(os.path.exists(self.dup2_path))
        self.assertTrue(os.path.exists(self.cold_dir))
        self.assertTrue(os.path.exists(self.old_trash_file))
        
        # Execute approved actions
        approved_actions = [a for a in actions if a["approved"]]
        exec_results = executor.execute_actions(approved_actions, TEST_DB_PATH, test_mode=False)
        
        # Verify actions executed successfully
        for res in exec_results:
            self.assertTrue(res["success"], f"Action failed: {res['log']}")
            
        # Verify file system changes
        # Duplicate file (dup1) should be deleted
        self.assertFalse(os.path.exists(self.dup1_path))
        # Original (dup2) should remain
        self.assertTrue(os.path.exists(self.dup2_path))
        
        # Cold directory should be replaced by a tar.zst archive
        self.assertFalse(os.path.exists(self.cold_dir))
        self.assertTrue(os.path.exists(self.cold_dir + ".tar.zst"))
        
        # Trash directory contents should be cleaned
        self.assertFalse(os.path.exists(self.old_trash_file))
        self.assertTrue(os.path.exists(self.trash_files_dir)) # wrapper folder should be preserved
        
        # Check database updated executed flag
        conn = database.get_connection(TEST_DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) as cnt FROM pending_actions WHERE executed = 1")
        self.assertEqual(cursor.fetchone()["cnt"], len(approved_actions))
        conn.close()

if __name__ == "__main__":
    unittest.main()
