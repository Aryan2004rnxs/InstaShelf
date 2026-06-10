import os
import logging
import asyncio
import sqlite3
import json

# Force native gRPC DNS resolution to fix macOS DNS lookup failures
os.environ["GRPC_DNS_RESOLVER"] = "native"

# Fix SSL CA Bundle paths overridden by Hugging Face Spaces (causes SSLError in containers)
for var in ["CURL_CA_BUNDLE", "REQUESTS_CA_BUNDLE", "SSL_CERT_FILE"]:
    if var in os.environ:
        del os.environ[var]
from datetime import datetime
from functools import wraps
from typing import Callable, Any, Type, Tuple, List
from dotenv import load_dotenv

# Load environment variables
load_dotenv(override=True)

# Configure logging
LOG_LEVEL_STR = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_LEVEL = getattr(logging, LOG_LEVEL_STR, logging.INFO)

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler()]
)

logger = logging.getLogger("InstaShelf")

# DB Configuration path
DB_DIR = os.getenv("DB_DIR", "/app/data" if os.path.exists("/app/data") else "./data")
os.makedirs(DB_DIR, exist_ok=True)
DB_PATH = os.path.join(DB_DIR, "instashelf.db")

def init_db():
    """Initializes the local SQLite database for quota tracking and offline fallback cache."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # Table for tracking Gemini daily requests quota
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS gemini_quota (
                date TEXT PRIMARY KEY,
                count INTEGER NOT NULL DEFAULT 0
            )
        """)
        
        # Table for tracking Groq daily requests quota
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS groq_quota (
                date TEXT PRIMARY KEY,
                count INTEGER NOT NULL DEFAULT 0
            )
        """)
        
        # Table for storing offline google sheets cache rows when writes fail
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS pending_rows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                row_data TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        
        # Table for storing user interaction progress (YouTube watch time, read status)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_progress (
                content_hash TEXT PRIMARY KEY,
                progress_seconds INTEGER NOT NULL DEFAULT 0,
                is_completed BOOLEAN NOT NULL DEFAULT 0,
                last_updated TEXT NOT NULL
            )
        """)
        
        # Table for storing user video timestamp notes
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS video_notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content_hash TEXT NOT NULL,
                timestamp_seconds INTEGER NOT NULL,
                note_text TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        
        conn.commit()
        conn.close()
        logger.info(f"Local SQLite database initialized at {DB_PATH}")
    except Exception as e:
        logger.error(f"Failed to initialize SQLite database: {e}")

# Call init_db on import
init_db()

def get_current_date() -> str:
    """Returns today's date formatted as YYYY-MM-DD."""
    return datetime.utcnow().strftime("%Y-%m-%d")

# Gemini Quota Tracking Functions
def get_gemini_usage(date_str: str = None) -> int:
    """Gets the Gemini request count for a given date."""
    if date_str is None:
        date_str = get_current_date()
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT count FROM gemini_quota WHERE date = ?", (date_str,))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else 0
    except Exception as e:
        logger.error(f"Error reading Gemini usage from database: {e}")
        return 0

def increment_gemini_usage(date_str: str = None) -> int:
    """Increments and returns the Gemini request count for a given date."""
    if date_str is None:
        date_str = get_current_date()
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        # Insert or update
        cursor.execute("""
            INSERT INTO gemini_quota (date, count) 
            VALUES (?, 1)
            ON CONFLICT(date) DO UPDATE SET count = count + 1
        """, (date_str,))
        conn.commit()
        
        # Retrieve updated count
        cursor.execute("SELECT count FROM gemini_quota WHERE date = ?", (date_str,))
        row = cursor.fetchone()
        conn.close()
        new_count = row[0] if row else 1
        logger.info(f"Gemini daily quota usage: {new_count}/20 for {date_str}")
        return new_count
    except Exception as e:
        logger.error(f"Error incrementing Gemini usage in database: {e}")
        return 0

# Groq Quota Tracking Functions
def get_groq_usage(date_str: str = None) -> int:
    """Gets the Groq request count for a given date."""
    if date_str is None:
        date_str = get_current_date()
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT count FROM groq_quota WHERE date = ?", (date_str,))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else 0
    except Exception as e:
        logger.error(f"Error reading Groq usage from database: {e}")
        return 0

def increment_groq_usage(date_str: str = None) -> int:
    """Increments and returns the Groq request count for a given date."""
    if date_str is None:
        date_str = get_current_date()
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        # Insert or update
        cursor.execute("""
            INSERT INTO groq_quota (date, count) 
            VALUES (?, 1)
            ON CONFLICT(date) DO UPDATE SET count = count + 1
        """, (date_str,))
        conn.commit()
        
        # Retrieve updated count
        cursor.execute("SELECT count FROM groq_quota WHERE date = ?", (date_str,))
        row = cursor.fetchone()
        conn.close()
        new_count = row[0] if row else 1
        logger.info(f"Groq daily quota usage: {new_count}/1000 for {date_str}")
        return new_count
    except Exception as e:
        logger.error(f"Error incrementing Groq usage in database: {e}")
        return 0

# Sheets Caching Functions
def cache_pending_row(row_dict: dict) -> bool:
    """Saves a row to the offline SQLite queue to retry writing to Google Sheets later."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        data_str = json.dumps(row_dict)
        now_str = datetime.utcnow().isoformat()
        cursor.execute(
            "INSERT INTO pending_rows (row_data, created_at) VALUES (?, ?)",
            (data_str, now_str)
        )
        conn.commit()
        conn.close()
        logger.warning("Google Sheet write failed. Saved row to local SQLite cache.")
        return True
    except Exception as e:
        logger.critical(f"Failed to cache pending row to SQLite: {e}")
        return False

def get_pending_rows() -> List[Tuple[int, dict]]:
    """Retrieves all pending rows from the offline SQLite queue."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT id, row_data FROM pending_rows ORDER BY id ASC")
        rows = cursor.fetchall()
        conn.close()
        
        parsed_rows = []
        for row_id, data_str in rows:
            try:
                parsed_rows.append((row_id, json.loads(data_str)))
            except Exception as pe:
                logger.error(f"Failed to parse offline row {row_id}: {pe}")
        return parsed_rows
    except Exception as e:
        logger.error(f"Failed to get pending rows from SQLite: {e}")
        return []

def delete_pending_row(row_id: int) -> bool:
    """Deletes a successfully synced row from the offline SQLite queue."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM pending_rows WHERE id = ?", (row_id,))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"Failed to delete pending row {row_id} from SQLite: {e}")
        return False

# Async Retry Decorator
def retry_async(
    retries: int = 3,
    delay: float = 1.0,
    backoff: float = 2.0,
    exceptions: Tuple[Type[BaseException], ...] = (Exception,)
):
    """Decorator to retry asynchronous functions with exponential backoff."""
    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            current_delay = delay
            for attempt in range(1, retries + 1):
                try:
                    return await func(*args, **kwargs)
                except exceptions as e:
                    if attempt == retries:
                        logger.error(f"Function {func.__name__} failed after {retries} attempts. Exception: {e}")
                        raise
                    logger.warning(
                        f"Attempt {attempt}/{retries} for {func.__name__} failed: {e}. "
                        f"Retrying in {current_delay:.2f} seconds..."
                    )
                    await asyncio.sleep(current_delay)
                    current_delay *= backoff
        return wrapper
    return decorator
