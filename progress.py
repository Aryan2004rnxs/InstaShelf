import sqlite3
import logging
from datetime import datetime
from typing import Dict, Any, List

from utils import DB_PATH

logger = logging.getLogger("InstaShelf.progress")

def get_all_progress() -> Dict[str, Dict[str, Any]]:
    """Retrieves all user progress from the local SQLite database."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT content_hash, progress_seconds, is_completed, last_updated FROM user_progress")
        rows = cursor.fetchall()
        conn.close()
        
        progress_dict = {}
        for row in rows:
            progress_dict[row["content_hash"]] = {
                "progress_seconds": row["progress_seconds"],
                "is_completed": bool(row["is_completed"]),
                "last_updated": row["last_updated"]
            }
        return progress_dict
    except Exception as e:
        logger.error(f"Failed to get user progress from SQLite: {e}")
        return {}

def update_progress(content_hash: str, progress_seconds: int, is_completed: bool) -> bool:
    """Updates the progress for a specific item in the SQLite database."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        now_str = datetime.utcnow().isoformat() + "Z"
        
        # SQLite doesn't have true booleans, so we use integers 0 and 1
        is_completed_int = 1 if is_completed else 0
        
        cursor.execute("""
            INSERT INTO user_progress (content_hash, progress_seconds, is_completed, last_updated)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(content_hash) DO UPDATE SET
                progress_seconds = excluded.progress_seconds,
                is_completed = excluded.is_completed,
                last_updated = excluded.last_updated
        """, (content_hash, progress_seconds, is_completed_int, now_str))
        
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"Failed to update progress for {content_hash}: {e}")
        return False

def get_notes(content_hash: str) -> List[Dict[str, Any]]:
    """Retrieves all notes for a specific content item ordered by timestamp."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, timestamp_seconds, note_text, created_at FROM video_notes WHERE content_hash = ? ORDER BY timestamp_seconds ASC",
            (content_hash,)
        )
        rows = cursor.fetchall()
        conn.close()
        
        notes = []
        for row in rows:
            notes.append({
                "id": row["id"],
                "timestamp_seconds": row["timestamp_seconds"],
                "note_text": row["note_text"],
                "created_at": row["created_at"]
            })
        return notes
    except Exception as e:
        logger.error(f"Failed to get notes for {content_hash}: {e}")
        return []

def add_note(content_hash: str, timestamp_seconds: int, note_text: str) -> Dict[str, Any]:
    """Adds a new note for a specific content item at a timestamp."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        now_str = datetime.utcnow().isoformat() + "Z"
        
        cursor.execute("""
            INSERT INTO video_notes (content_hash, timestamp_seconds, note_text, created_at)
            VALUES (?, ?, ?, ?)
        """, (content_hash, timestamp_seconds, note_text, now_str))
        
        note_id = cursor.lastrowid
        conn.commit()
        conn.close()
        
        return {
            "id": note_id,
            "timestamp_seconds": timestamp_seconds,
            "note_text": note_text,
            "created_at": now_str
        }
    except Exception as e:
        logger.error(f"Failed to add note for {content_hash}: {e}")
        return None

