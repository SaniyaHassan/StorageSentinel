import sqlite3
import os
from datetime import datetime

DEFAULT_DB_PATH = "sentinel.db"

def get_connection(db_path=DEFAULT_DB_PATH):
    """Establish a connection to the SQLite database."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn

def init_db(db_path=DEFAULT_DB_PATH):
    """Initialize database tables if they do not exist and apply schema migrations."""
    conn = get_connection(db_path)
    cursor = conn.cursor()
    
    # 1. System scans overall metrics
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS system_scans (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
        total_size_gb REAL,
        used_size_gb REAL,
        free_size_gb REAL,
        percent_used REAL
    )
    """)
    
    # 2. User directory usage
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS user_usage (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scan_id INTEGER,
        username TEXT,
        used_size_gb REAL,
        FOREIGN KEY(scan_id) REFERENCES system_scans(id) ON DELETE CASCADE
    )
    """)
    
    # 3. Top level or critical directory usage
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS directory_usage (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scan_id INTEGER,
        path TEXT,
        size_gb REAL,
        last_modified TEXT,
        FOREIGN KEY(scan_id) REFERENCES system_scans(id) ON DELETE CASCADE
    )
    """)
    
    # 4. Large files log
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS large_files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scan_id INTEGER,
        path TEXT,
        size_gb REAL,
        owner TEXT,
        last_accessed TEXT,
        FOREIGN KEY(scan_id) REFERENCES system_scans(id) ON DELETE CASCADE
    )
    """)
    
    # 5. Pending and executed actions
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS pending_actions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
        action_type TEXT, -- 'delete', 'compress', 'conda_clean', 'pip_clean', 'journald_clean', 'delayed_delete'
        target_path TEXT,
        size_gb REAL,
        description TEXT,
        approved INTEGER DEFAULT 0, -- 0 = Pending, 1 = Approved, -1 = Rejected
        executed INTEGER DEFAULT 0, -- 0 = No, 1 = Yes
        execution_timestamp TEXT,
        risk TEXT DEFAULT 'Medium'
    )
    """)
    
    # 6. File type analytics
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS file_type_analytics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scan_id INTEGER,
        category TEXT,
        size_gb REAL,
        FOREIGN KEY(scan_id) REFERENCES system_scans(id) ON DELETE CASCADE
    )
    """)
    
    conn.commit()
    
    # Run migration to add 'risk' column if database already exists without it
    try:
        cursor.execute("ALTER TABLE pending_actions ADD COLUMN risk TEXT DEFAULT 'Medium'")
        conn.commit()
    except sqlite3.OperationalError:
        # Column already exists
        pass
        
    conn.close()

def record_scan(db_path, system_metrics, user_metrics, directory_metrics, large_files_metrics, file_type_metrics=None):
    """
    Record all metrics from a scan session in a single database transaction.
    system_metrics: dict with keys (total_size_gb, used_size_gb, free_size_gb, percent_used)
    user_metrics: list of dicts/tuples (username, used_size_gb)
    directory_metrics: list of dicts/tuples (path, size_gb, last_modified)
    large_files_metrics: list of dicts/tuples (path, size_gb, owner, last_accessed)
    file_type_metrics: dict mapping category -> size_gb
    """
    conn = get_connection(db_path)
    cursor = conn.cursor()
    
    try:
        # Insert system scan header
        cursor.execute("""
        INSERT INTO system_scans (total_size_gb, used_size_gb, free_size_gb, percent_used)
        VALUES (?, ?, ?, ?)
        """, (
            system_metrics['total_size_gb'],
            system_metrics['used_size_gb'],
            system_metrics['free_size_gb'],
            system_metrics['percent_used']
        ))
        scan_id = cursor.lastrowid
        
        # Insert user usage
        for username, size_gb in user_metrics:
            cursor.execute("""
            INSERT INTO user_usage (scan_id, username, used_size_gb)
            VALUES (?, ?, ?)
            """, (scan_id, username, size_gb))
            
        # Insert directory usage
        for path, size_gb, last_modified in directory_metrics:
            cursor.execute("""
            INSERT INTO directory_usage (scan_id, path, size_gb, last_modified)
            VALUES (?, ?, ?, ?)
            """, (scan_id, path, size_gb, last_modified))
            
        # Insert large files
        for path, size_gb, owner, last_accessed in large_files_metrics:
            cursor.execute("""
            INSERT INTO large_files (scan_id, path, size_gb, owner, last_accessed)
            VALUES (?, ?, ?, ?, ?)
            """, (scan_id, path, size_gb, owner, last_accessed))
            
        # Insert file type analytics
        if file_type_metrics:
            for category, size_gb in file_type_metrics.items():
                cursor.execute("""
                INSERT INTO file_type_analytics (scan_id, category, size_gb)
                VALUES (?, ?, ?)
                """, (scan_id, category, size_gb))
            
        conn.commit()
        return scan_id
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

def add_pending_action(db_path, action_type, target_path, size_gb, description="", risk="Medium"):
    """Add an action to the pending queue if it does not already exist as pending or executed."""
    conn = get_connection(db_path)
    cursor = conn.cursor()
    
    # Check if this target has an active pending/approved action already
    cursor.execute("""
    SELECT id FROM pending_actions 
    WHERE target_path = ? AND executed = 0 AND approved >= 0
    """, (target_path,))
    
    row = cursor.fetchone()
    if row:
        conn.close()
        return row['id']
        
    cursor.execute("""
    INSERT INTO pending_actions (action_type, target_path, size_gb, description, approved, executed, risk)
    VALUES (?, ?, ?, ?, 0, 0, ?)
    """, (action_type, target_path, size_gb, description, risk))
    
    action_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return action_id

def get_pending_actions(db_path):
    """Retrieve all pending actions (approved=0 or approved=1, executed=0)."""
    conn = get_connection(db_path)
    cursor = conn.cursor()
    cursor.execute("""
    SELECT id, timestamp, action_type, target_path, size_gb, approved, risk, description
    FROM pending_actions 
    WHERE executed = 0 AND approved >= 0
    ORDER BY size_gb DESC
    """)
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows

def update_action_approval(db_path, action_id, approved):
    """Update approval status (1 = Approved, -1 = Rejected/Ignored)."""
    conn = get_connection(db_path)
    cursor = conn.cursor()
    cursor.execute("""
    UPDATE pending_actions 
    SET approved = ? 
    WHERE id = ?
    """, (approved, action_id))
    conn.commit()
    conn.close()

def mark_action_executed(db_path, action_id):
    """Mark action as executed."""
    conn = get_connection(db_path)
    cursor = conn.cursor()
    now_str = datetime.now().isoformat()
    cursor.execute("""
    UPDATE pending_actions 
    SET executed = 1, execution_timestamp = ? 
    WHERE id = ?
    """, (now_str, action_id))
    conn.commit()
    conn.close()

def get_historical_usage(db_path, limit=10):
    """Get history of overall disk usage scans."""
    conn = get_connection(db_path)
    cursor = conn.cursor()
    cursor.execute("""
    SELECT timestamp, total_size_gb, used_size_gb, free_size_gb, percent_used 
    FROM system_scans 
    ORDER BY timestamp DESC 
    LIMIT ?
    """, (limit,))
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows

def get_growth_report(db_path):
    """
    Compare the latest scan with the previous scan to determine directory and user growth.
    """
    conn = get_connection(db_path)
    cursor = conn.cursor()
    
    # Get the latest two scan IDs
    cursor.execute("SELECT id, timestamp FROM system_scans ORDER BY id DESC LIMIT 2")
    scans = cursor.fetchall()
    
    if len(scans) < 2:
        conn.close()
        return None
        
    latest_scan_id, latest_time = scans[0]['id'], scans[0]['timestamp']
    prev_scan_id, prev_time = scans[1]['id'], scans[1]['timestamp']
    
    # User growth
    cursor.execute("""
    SELECT 
        l.username, 
        l.used_size_gb as latest_gb, 
        p.used_size_gb as prev_gb, 
        (l.used_size_gb - COALESCE(p.used_size_gb, 0)) as growth_gb
    FROM (SELECT username, used_size_gb FROM user_usage WHERE scan_id = ?) l
    LEFT JOIN (SELECT username, used_size_gb FROM user_usage WHERE scan_id = ?) p
    ON l.username = p.username
    ORDER BY growth_gb DESC
    """, (latest_scan_id, prev_scan_id))
    user_growth = [dict(row) for row in cursor.fetchall()]
    
    # Directory growth
    cursor.execute("""
    SELECT 
        l.path, 
        l.size_gb as latest_gb, 
        p.size_gb as prev_gb, 
        (l.size_gb - COALESCE(p.size_gb, 0)) as growth_gb
    FROM (SELECT path, size_gb FROM directory_usage WHERE scan_id = ?) l
    LEFT JOIN (SELECT path, size_gb FROM directory_usage WHERE scan_id = ?) p
    ON l.path = p.path
    ORDER BY growth_gb DESC
    LIMIT 20
    """, (latest_scan_id, prev_scan_id))
    dir_growth = [dict(row) for row in cursor.fetchall()]
    
    conn.close()
    return {
        "latest_time": latest_time,
        "prev_time": prev_time,
        "user_growth": user_growth,
        "dir_growth": dir_growth
    }
