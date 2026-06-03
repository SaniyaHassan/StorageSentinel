import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta

import server_health_report
import server_health_agent

TEST_DB = "./test_server_health.db"

class TestServerHealth(unittest.TestCase):
    def setUp(self):
        if os.path.exists(TEST_DB):
            os.remove(TEST_DB)
        server_health_agent.init_db(TEST_DB)
        now = datetime.utcnow()
        conn = sqlite3.connect(TEST_DB)
        cursor = conn.cursor()

        for i in range(3):
            sample_time = now - timedelta(hours=6 * i)
            cursor.execute(
                "INSERT INTO server_samples (timestamp, root_path, cpu_percent, load_1, load_5, load_15, ram_total_gb, ram_used_gb, ram_free_gb, swap_total_gb, swap_used_gb, disk_total_gb, disk_used_gb, disk_free_gb, disk_percent, network_rx_bytes, network_tx_bytes, uptime_seconds) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    sample_time.isoformat(),
                    "/",
                    10.0 + i * 5.0,
                    0.5 + i * 0.1,
                    0.7 + i * 0.1,
                    0.9 + i * 0.1,
                    16.0,
                    6.0 + i * 0.5,
                    10.0 - i * 0.5,
                    4.0,
                    0.5 + i * 0.2,
                    250.0,
                    140.0 + i * 2.0,
                    110.0 - i * 2.0,
                    56.0 + i * 1.0,
                    1000000 + i * 1000,
                    300000 + i * 500,
                    3600 * 24
                )
            )
        conn.commit()
        conn.close()

    def tearDown(self):
        if os.path.exists(TEST_DB):
            os.remove(TEST_DB)

    def test_get_samples(self):
        since = datetime.utcnow() - timedelta(days=1)
        samples = server_health_report.get_samples(TEST_DB, since)
        self.assertEqual(len(samples), 3)

    def test_aggregate_series(self):
        samples = server_health_report.get_samples(TEST_DB, datetime.utcnow() - timedelta(days=1))
        result = server_health_report.aggregate_series(samples, "cpu_percent")
        self.assertEqual(result["average"], 15.0)
        self.assertEqual(result["peak"], 20.0)
        self.assertEqual(result["lowest"], 10.0)

    def test_make_text_report(self):
        samples = server_health_report.get_samples(TEST_DB, datetime.utcnow() - timedelta(days=1))
        report_text = server_health_report.make_text_report("Daily", samples, {})
        self.assertIn("Server Health Report (Daily)", report_text)
        self.assertIn("Average CPU usage: 15.0%", report_text)

    def test_send_email_report_disabled(self):
        config = {"email_alerts": {"enabled": False}}
        with self.assertRaises(RuntimeError):
            server_health_report.send_email_report(config, "Subject", "Body")

    def test_collect_and_store(self):
        # This test validates that collect_and_store writes a row to the database.
        sample_id = server_health_agent.collect_and_store(TEST_DB, "/")
        self.assertIsInstance(sample_id, int)
        conn = sqlite3.connect(TEST_DB)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM server_samples")
        count = cursor.fetchone()[0]
        conn.close()
        self.assertGreaterEqual(count, 1)

if __name__ == "__main__":
    unittest.main()
