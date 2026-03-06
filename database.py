import sqlite3
import logging
from datetime import datetime
import config

logger = logging.getLogger(__name__)

def get_connection():
    # check_same_thread=False позволяет использовать sqlite3 в асинхронном aiogram
    return sqlite3.connect(config.DB_PATH, check_same_thread=False)

def init_db():
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            
            # Таблица пользователей (Исправлено: используем f-строку для DEFAULT)
            cursor.execute(f'''
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    free_requests INTEGER NOT NULL DEFAULT {config.FREE_REQUESTS_DEFAULT},
                    is_premium INTEGER NOT NULL DEFAULT 0,
                    total_requests INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT,
                    updated_at TEXT
                )
            ''')
            
            # Таблица платежей
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    amount INTEGER NOT NULL,
                    currency TEXT NOT NULL,
                    payload TEXT UNIQUE,
                    status TEXT,
                    created_at TEXT
                )
            ''')
            
            # Таблица истории использования
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS usage_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    tone TEXT,
                    source_text TEXT,
                    result_text TEXT,
                    created_at TEXT
                )
            ''')
            conn.commit()
            logger.info("Database initialized successfully.")
    except Exception as e:
        logger.error(f"Database initialization error: {e}")

def create_user_if_not_exists(user_id: int):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
        if not cursor.fetchone():
            now = datetime.utcnow().isoformat()
            cursor.execute(
                "INSERT INTO users (user_id, free_requests, is_premium, created_at, updated_at) VALUES (?, ?, 0, ?, ?)",
                (user_id, config.FREE_REQUESTS_DEFAULT, now, now)
            )
            conn.commit()

def get_user(user_id: int) -> dict:
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

def decrement_request(user_id: int):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET free_requests = free_requests - 1, updated_at = ? WHERE user_id = ? AND free_requests > 0",
            (datetime.utcnow().isoformat(), user_id)
        )
        conn.commit()

def increment_total_requests(user_id: int):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET total_requests = total_requests + 1, updated_at = ? WHERE user_id = ?",
            (datetime.utcnow().isoformat(), user_id)
        )
        conn.commit()

def set_premium(user_id: int, value: bool = True):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET is_premium = ?, updated_at = ? WHERE user_id = ?",
            (1 if value else 0, datetime.utcnow().isoformat(), user_id)
        )
        conn.commit()

def save_payment(user_id: int, amount: int, currency: str, payload: str, status: str):
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO payments (user_id, amount, currency, payload, status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, amount, currency, payload, status, datetime.utcnow().isoformat())
            )
            conn.commit()
            return True
    except sqlite3.IntegrityError:
        # Защита от повторной обработки одного и того же payload
        return False

def save_usage(user_id: int, tone: str, source_text: str, result_text: str):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO usage_history (user_id, tone, source_text, result_text, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, tone, source_text, result_text, datetime.utcnow().isoformat())
        )
        conn.commit()

def get_stats() -> dict:
    with get_connection() as conn:
        cursor = conn.cursor()
        stats = {}
        cursor.execute("SELECT COUNT(*) FROM users")
        stats['total_users'] = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM users WHERE is_premium = 1")
        stats['premium_users'] = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM usage_history")
        stats['total_generations'] = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*), SUM(amount) FROM payments WHERE status = 'success'")
        row = cursor.fetchone()
        stats['total_payments'] = row[0]
        stats['total_revenue_stars'] = row[1] or 0
        
        return stats
