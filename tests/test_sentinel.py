import os
import shutil
import unittest
import time
import json
import yaml
from datetime import datetime, timedelta

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
            try:
                shutil.rmtree(TEST_ENV_ROOT)
            except Exception:
                pass
                
        os.makedirs(TEST_ENV_ROOT, exist_ok=True)
        
        # Create user folders
        self.user1_path = os.path.join(TEST_ENV_ROOT, "user1")
        self.user2_path = os.path.join(TEST_ENV_ROOT, "user2")
        os.makedirs(self.user1_path, exist_ok=True)
        os.makedirs(self.user2_path, exist_ok=True)
        
        # 1. Create a dummy large file (6 MB)
        self.large_file_path = os.path.join(self.user1_path, "large_file.dat")
        with open(self.large_file_path, "wb") as f:
            f.write(b"\0" * (6 * 1024 * 1024))
            
        # 2. Create duplicate files (51 MB sparse files to trigger duplicate check)
        self.dup1_path = os.path.join(self.user1_path, "dup1.bin")
        self.dup2_path = os.path.join(self.user2_path, "dup2.bin")
        
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
        os.makedirs(self.trash_files_dir, exist_ok=True)
        self.old_trash_file = os.path.join(self.trash_files_dir, "old_cricket.mp4")
        with open(self.old_trash_file, "wb") as f:
            f.write(b"cricket match video")
            f.seek(5 * 1024 * 1024 - 1)
            f.write(b"\0")
            
        # Set modification/access time of old trash file to 40 days ago
        past_time = time.time() - (40 * 24 * 3600)
        os.utime(self.old_trash_file, (past_time, past_time))
        os.utime(self.trash_files_dir, (past_time, past_time))
        os.utime(self.trash_dir, (past_time, past_time))
        
        # Create an active video file in user1 (outside of Trash) to test Videos category
        self.video_file = os.path.join(self.user1_path, "tutorial.mp4")
        with open(self.video_file, "wb") as f:
            f.write(b"video content")
            f.seek(5 * 1024 * 1024 - 1)
            f.write(b"\0")
        
        # 4. Create a cold directory in user2 containing a dataset (5MB to exceed rounding threshold)
        self.cold_dir = os.path.join(self.user2_path, "cold_research_project")
        os.makedirs(self.cold_dir, exist_ok=True)
        self.cold_file = os.path.join(self.cold_dir, "dataset.csv")
        with open(self.cold_file, "wb") as f:
            f.write(b"dataset content")
            f.seek(5 * 1024 * 1024 - 1)
            f.write(b"\0")
            
        # Set mtime/atime of cold dir and its files to 200 days ago
        cold_time = time.time() - (200 * 24 * 3600)
        os.utime(self.cold_file, (cold_time, cold_time))
        os.utime(self.cold_dir, (cold_time, cold_time))
        
        # 5. Create a fake conda pkg cache path
        self.conda_cache_path = os.path.join(self.user1_path, "miniconda3/pkgs")
        os.makedirs(self.conda_cache_path, exist_ok=True)
        with open(os.path.join(self.conda_cache_path, "package-1.0.tar.bz2"), "wb") as f:
            f.write(b"mock pkg tarball")
            f.seek(5 * 1024 * 1024 - 1)
            f.write(b"\0")
            
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
            "exclusions": [".git", "test_sentinel.db"],
            "user_classes": {
                "user1": "student",
                "user2": "phd"
            },
            "quotas": {
                "default": 0.02, # 20 MB (so our 51MB duplicate files exceed it)
                "student": 0.02,
                "phd": 0.1
            },
            "email_alerts": {
                "enabled": False # disable for unit testing
            }
        }
        
        with open(TEST_CONFIG_PATH, "w") as f:
            yaml.dump(self.config_data, f)
            
        # Init SQLite Database
        database.init_db(TEST_DB_PATH)

    def tearDown(self):
        """Clean up dummy environment."""
        if os.path.exists(TEST_ENV_ROOT):
            try:
                shutil.rmtree(TEST_ENV_ROOT)
            except Exception:
                pass

    def test_scanner(self):
        """Verify that scanner correctly maps directories, large files, file types, and duplicates."""
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
        
        # Assert duplicate files detected using SHA256 (51MB files)
        self.assertEqual(len(duplicates), 1)
        dup_paths = duplicates[0]["paths"]
        self.assertIn(os.path.abspath(self.dup1_path), dup_paths)
        self.assertIn(os.path.abspath(self.dup2_path), dup_paths)
        
        # Assert duplicate owner mapping was resolved
        self.assertIn("owners", duplicates[0])
        self.assertEqual(len(duplicates[0]["owners"]), 2)
        
        # Assert trash folder detected
        self.assertEqual(len(summary["caches"]["trash"]), 1)
        self.assertEqual(summary["caches"]["trash"][0]["path"], os.path.abspath(self.trash_dir))
        
        # Assert conda folder detected
        self.assertEqual(len(summary["caches"]["conda"]), 1)
        self.assertEqual(summary["caches"]["conda"][0]["path"], os.path.abspath(self.conda_cache_path))
        
        # Assert cold directory detected
        cold_dirs = [cd["path"] for cd in summary["cold_directories"]]
        self.assertIn(os.path.abspath(self.cold_dir), cold_dirs)
        
        # Assert file type analytics categorized correctly
        file_type_gb = summary["file_type_gb"]
        self.assertGreater(file_type_gb["AI Models"], 0.0) # dup1.bin/dup2.bin classified as AI Models
        self.assertGreater(file_type_gb["Datasets"], 0.0) # dataset.csv classified as Datasets
        self.assertGreater(file_type_gb["Videos"], 0.0) # tutorial.mp4 classified as Videos
        self.assertGreater(file_type_gb["Trash"], 0.0) # old_cricket.mp4 in trash classified as Trash
        self.assertGreater(file_type_gb["Caches"], 0.0) # miniconda3/pkgs files classified as Caches

    def test_policy_engine_and_risk_scoring(self):
        """Verify policy engine categorizes actions, risks, and user quotas correctly."""
        s = scanner.FileSystemScanner(
            root_path=TEST_ENV_ROOT,
            exclusions=self.config_data["exclusions"],
            large_file_threshold_gb=self.config_data["large_file_threshold_gb"],
            cold_data_days=self.config_data["cold_data_days"],
            min_dir_size_gb=0.0
        )
        duplicates = s.scan()
        summary = s.get_summary()
        
        disk_usage = {
            "total_size_gb": 10.0,
            "used_size_gb": 8.5,
            "free_size_gb": 1.5,
            "percent_used": 85.0
        }
        
        pe = policy_engine.PolicyEngine(self.config_data)
        results = pe.evaluate(summary, disk_usage)
        
        # Check alerts
        self.assertEqual(len(results["alerts"]), 1)
        self.assertIn("WARNING", results["alerts"][0])
        
        # empty_trash should be in auto_actions since space is > warning
        auto_actions_types = [a["action_type"] for a in results["auto_actions"]]
        self.assertIn("empty_trash", auto_actions_types)
        
        # Duplicate deletion, cold compression, and conda cleaning must be manual actions
        manual_actions_types = [m["action_type"] for m in results["manual_actions"]]
        self.assertIn("delete", manual_actions_types)
        self.assertIn("compress", manual_actions_types)
        self.assertIn("conda_clean", manual_actions_types)
        self.assertNotIn("conda_clean", auto_actions_types)
        
        # Verify Risk scoring
        for m in results["manual_actions"]:
            if m["action_type"] == "delete":
                # Duplicate delete on .bin files should be High Risk (AI Model)
                self.assertEqual(m["risk"], "High")
            elif m["action_type"] == "compress":
                # Compression is Medium Risk
                self.assertEqual(m["risk"], "Medium")
            elif m["action_type"] == "conda_clean":
                # Conda cleanup is Low Risk
                self.assertEqual(m["risk"], "Low")
                
        # Critical risk check
        critical_risk = policy_engine.determine_risk_score("delete", "/var/lib/postgresql/data")
        self.assertEqual(critical_risk, "Critical")
        
        # Verify User Quota alerts (user1 has >50MB, student quota is 20MB, so exceeded)
        quota_warnings = results["quota_warnings"]
        user1_warnings = [qw for qw in quota_warnings if qw["username"] == "user1"]
        self.assertEqual(len(user1_warnings), 1)
        self.assertEqual(user1_warnings[0]["status"], "Exceeded")

    def test_executor_and_db_flow(self):
        """Test database storage, delayed compression, and delayed_delete execution."""
        disk_usage = {
            "total_size_gb": 10.0,
            "used_size_gb": 4.0,
            "free_size_gb": 6.0,
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
        
        # Record scan to SQLite DB (including file type analytics)
        scan_id = database.record_scan(
            db_path=TEST_DB_PATH,
            system_metrics=disk_usage,
            user_metrics=summary["user_metrics"],
            directory_metrics=summary["directory_metrics"],
            large_files_metrics=summary["large_files_metrics"],
            file_type_metrics=summary["file_type_gb"]
        )
        self.assertIsNotNone(scan_id)
        
        # Verify DB file type logging
        conn = database.get_connection(TEST_DB_PATH)
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT size_gb FROM file_type_analytics WHERE scan_id = ? AND category = 'Videos'", (scan_id,))
            row = cursor.fetchone()
            self.assertIsNotNone(row)
            self.assertGreater(row["size_gb"], 0)
        finally:
            conn.close()
        
        # Policy evaluation
        pe = policy_engine.PolicyEngine(self.config_data)
        results = pe.evaluate(summary, disk_usage)
        
        # Save to JSON
        rep = reporter.ActionReporter(TEST_JSON_PATH)
        rep.generate_report(disk_usage, summary, results, duplicates, self.config_data)
        
        # Sync to DB
        actions = rep.load_actions_from_json()
        sentinel.sync_actions_to_db(TEST_DB_PATH, actions)
        
        # Verify db records include risk column
        conn = database.get_connection(TEST_DB_PATH)
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT risk FROM pending_actions WHERE action_type = 'delete'")
            row = cursor.fetchone()
            self.assertEqual(row["risk"], "High")
        finally:
            conn.close()
        
        # Verify target exists before compression
        self.assertTrue(os.path.exists(self.cold_dir))
        
        # Approve compression action
        compress_action = None
        for a in actions:
            if a["action_type"] == "compress":
                a["approved"] = True
                compress_action = a
                break
                
        self.assertIsNotNone(compress_action)
        rep.save_actions_to_json(actions)
        sentinel.sync_actions_to_db(TEST_DB_PATH, actions)
        
        # Execute approved compression
        exec_results = executor.execute_actions([compress_action], TEST_DB_PATH, test_mode=False)
        self.assertTrue(exec_results[0]["success"])
        
        # Verify archive created
        self.assertTrue(os.path.exists(self.cold_dir + ".tar.zst"))
        # Verify original dir renamed to .deletable.YYYY-MM-DD instead of immediately deleted
        self.assertFalse(os.path.exists(self.cold_dir))
        
        date_str = datetime.now().date().isoformat()
        expected_deletable_path = f"{self.cold_dir}.deletable.{date_str}"
        self.assertTrue(os.path.exists(expected_deletable_path))
        
        # Check database registered the pre-approved delayed_delete action
        conn = database.get_connection(TEST_DB_PATH)
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT id, target_path, approved, executed FROM pending_actions WHERE action_type = 'delayed_delete'")
            delayed_row = cursor.fetchone()
            self.assertIsNotNone(delayed_row)
            self.assertEqual(delayed_row["approved"], 1)
            self.assertEqual(delayed_row["executed"], 0)
            delayed_id = delayed_row["id"]
        finally:
            conn.close()
        
        # Test Delayed Delete execution:
        # Running the delayed delete action now should SKIP deletion because it is not 7 days old yet
        delayed_action = {
            "id": delayed_id,
            "action_type": "delayed_delete",
            "target_path": expected_deletable_path,
            "size_gb": 0.001,
            "approved": True
        }
        
        del_results = executor.execute_actions([delayed_action], TEST_DB_PATH, test_mode=False)
        self.assertFalse(del_results[0]["success"]) # Should fail/skip since age is 0 days (< 7)
        self.assertTrue(os.path.exists(expected_deletable_path)) # Original remains
        
        # Now mock the path & database to simulate 10 days elapsed
        past_date = (datetime.now() - timedelta(days=10)).date().isoformat()
        mocked_deletable_path = f"{self.cold_dir}.deletable.{past_date}"
        
        # Rename physical folder
        shutil.move(expected_deletable_path, mocked_deletable_path)
        
        # Update database target path
        conn = database.get_connection(TEST_DB_PATH)
        try:
            cursor = conn.cursor()
            cursor.execute("UPDATE pending_actions SET target_path = ? WHERE id = ?", (mocked_deletable_path, delayed_id))
            conn.commit()
        finally:
            conn.close()
        
        # Run delayed delete action again with the mocked 10-day-old path
        delayed_action["target_path"] = mocked_deletable_path
        del_results_2 = executor.execute_actions([delayed_action], TEST_DB_PATH, test_mode=False)
        self.assertTrue(del_results_2[0]["success"]) # Should succeed now
        self.assertFalse(os.path.exists(mocked_deletable_path)) # Should be physically deleted
        
        # Verify db marked executed = 1
        conn = database.get_connection(TEST_DB_PATH)
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT executed FROM pending_actions WHERE id = ?", (delayed_id,))
            self.assertEqual(cursor.fetchone()["executed"], 1)
        finally:
            conn.close()

if __name__ == "__main__":
    unittest.main()
