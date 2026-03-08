import sqlite3
import datetime
import config

def get_connection():
    return sqlite3.connect(config.DB_PATH)

def init_db():
    conn = get_connection()
    cursor = conn.cursor()
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            is_premium BOOLEAN DEFAULT 0,
            free_requests INTEGER DEFAULT 3,
            total_requests INTEGER DEFAULT 0,
            join_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS usage_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            task_type TEXT,
            input_text TEXT,
            output_text TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            amount INTEGER,
            currency TEXT,
            payload TEXT UNIQUE,
            status TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')
    
    conn.commit()
    conn.close()

def get_setting(key: str, default: str) -> str:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else default

def set_setting(key: str, value: str):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()

def create_user_if_not_exists(user_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
    if not cursor.fetchone():
        default_requests = int(get_setting("free_requests_default", str(config.FREE_REQUESTS_FALLBACK)))
        cursor.execute("INSERT INTO users (user_id, free_requests) VALUES (?, ?)", (user_id, default_requests))
        conn.commit()
    conn.close()

def get_user(user_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return {
            "user_id": row[0],
            "is_premium": bool(row[1]),
            "free_requests": row[2],
            "total_requests": row[3],
            "join_date": row[4]
        }
    return None

def decrement_request(user_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET free_requests = free_requests - 1 WHERE user_id = ? AND free_requests > 0", (user_id,))
    conn.commit()
    conn.close()

def increment_total_requests(user_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET total_requests = total_requests + 1 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def set_premium(user_id: int, status: bool):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET is_premium = ? WHERE user_id = ?", (int(status), user_id))
    conn.commit()
    conn.close()

def add_requests(user_id: int, amount: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET free_requests = free_requests + ? WHERE user_id = ?", (amount, user_id))
    conn.commit()
    conn.close()

def save_usage(user_id: int, task_type: str, input_text: str, output_text: str):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO usage_history (user_id, task_type, input_text, output_text) VALUES (?, ?, ?, ?)",
                   (user_id, task_type, input_text, output_text))
    conn.commit()
    conn.close()

def save_payment(user_id: int, amount: int, currency: str, payload: str, status: str) -> bool:
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO payments (user_id, amount, currency, payload, status) VALUES (?, ?, ?, ?, ?)",
                       (user_id, amount, currency, payload, status))
        conn.commit()
        is_new = True
    except sqlite3.IntegrityError:
        is_new = False
    conn.close()
    return is_new

def get_stats():
    conn = get_connection()
    cursor = conn.cursor()
    
    cursor.execute("SELECT COUNT(*) FROM users")
    total_users = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM users WHERE is_premium = 1")
    premium_users = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM usage_history")
    total_generations = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*), SUM(amount) FROM payments WHERE status = 'success'")
    payment_data = cursor.fetchone()
    total_payments = payment_data[0]
    total_revenue = payment_data[1] if payment_data[1] else 0
    
    conn.close()
    return {
        "total_users": total_users,
        "premium_users": premium_users,
        "total_generations": total_generations,
        "total_payments": total_payments,
        "total_revenue_stars": total_revenue
    }

def get_all_users():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM users")
    rows = cursor.fetchall()
    conn.close()
    return [{"user_id": row[0]} for row in rows]
