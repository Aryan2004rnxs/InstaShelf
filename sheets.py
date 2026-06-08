import os
import json
import base64
import logging
import asyncio
from typing import List, Set, Dict, Tuple, Any
import gspread
from models import ShelfRow
from utils import retry_async, cache_pending_row, get_pending_rows, delete_pending_row

logger = logging.getLogger("InstaShelf.sheets")

# Columns layout matching the specification:
# A:saved_at, B:source_type, C:content_type, D:title, E:creator, F:url, G:thumbnail_url,
# H:confidence, I:instagram_url, J:raw_context, K:ai_summary, L:content_hash, M:status,
# N:gemini_notes, O:tags
HEADERS = [
    "saved_at", "source_type", "content_type", "title", "creator", "url", "thumbnail_url",
    "confidence", "instagram_url", "raw_context", "ai_summary", "content_hash", "status",
    "gemini_notes", "tags"
]

def _get_worksheet_sync() -> gspread.Worksheet:
    """Helper to authenticate and retrieve the 'Shelf' worksheet."""
    creds_b64 = os.getenv("GOOGLE_CREDS_JSON")
    sheet_id = os.getenv("GOOGLE_SHEET_ID")
    
    if not creds_b64 or not sheet_id:
        raise ValueError("Missing GOOGLE_CREDS_JSON or GOOGLE_SHEET_ID in environment secrets.")
        
    creds_json = base64.b64decode(creds_b64).decode("utf-8")
    creds_dict = json.loads(creds_json)
    
    gc = gspread.service_account_from_dict(creds_dict)
    sh = gc.open_by_key(sheet_id)
    
    try:
        worksheet = sh.worksheet("Shelf")
    except gspread.exceptions.WorksheetNotFound:
        logger.info("Worksheet 'Shelf' not found. Creating a new one...")
        worksheet = sh.add_worksheet(title="Shelf", rows="1000", cols="20")
        # Add headers
        worksheet.append_row(HEADERS)
        
    return worksheet

def _read_hashes_sync(worksheet: gspread.Worksheet) -> Set[str]:
    """Helper to read column L (content_hash) from the sheet."""
    try:
        # Column 12 is 'L' (content_hash)
        col_values = worksheet.col_values(12)
        if len(col_values) > 1:
            return set(col_values[1:]) # Skip header
    except Exception as e:
        logger.error(f"Failed to read hashes from Google Sheet: {e}")
    return set()

def _read_recent_titles_sync(worksheet: gspread.Worksheet, limit: int = 50) -> List[str]:
    """Helper to read the last 'limit' titles from Column 4 (D)."""
    try:
        col_values = worksheet.col_values(4)
        if len(col_values) > 1:
            # Get only recent values (excluding header)
            titles = col_values[1:]
            return titles[-limit:]
    except Exception as e:
        logger.error(f"Failed to read titles from Google Sheet: {e}")
    return []

def _append_rows_sync(worksheet: gspread.Worksheet, rows_data: List[List[Any]]):
    """Helper to append list of rows to the sheet."""
    worksheet.append_rows(rows_data, value_input_option="USER_ENTERED")

# Async Wrappers

async def get_worksheet() -> gspread.Worksheet:
    return await asyncio.to_thread(_get_worksheet_sync)

async def get_existing_hashes(worksheet: gspread.Worksheet) -> Set[str]:
    return await asyncio.to_thread(_read_hashes_sync, worksheet)

async def get_recent_titles(worksheet: gspread.Worksheet, limit: int = 50) -> List[str]:
    return await asyncio.to_thread(_read_recent_titles_sync, worksheet, limit)

@retry_async(retries=3, delay=2.0, backoff=2.0, exceptions=(Exception,))
async def append_rows_with_retry(worksheet: gspread.Worksheet, rows_data: List[List[Any]]):
    """Retries appending rows to Google Sheet 3 times with exponential backoff."""
    await asyncio.to_thread(_append_rows_sync, worksheet, rows_data)

# Main sheet write logic

async def save_rows_to_shelf(rows: List[ShelfRow]) -> Tuple[int, int]:
    """
    Saves new ShelfRows to Google Sheets.
    Checks for duplicates based on content_hash against existing column L.
    Returns: (new_items_saved_count, duplicate_items_skipped_count)
    """
    if not rows:
        return 0, 0
        
    try:
        worksheet = await get_worksheet()
        existing_hashes = await get_existing_hashes(worksheet)
    except Exception as e:
        logger.error(f"Error accessing Google Sheet during initialization: {e}. Falling back to SQLite cache.")
        # Cache all rows to SQLite
        for row in rows:
            cache_pending_row(row.model_dump())
        return 0, len(rows)

    new_rows_data = []
    new_items_count = 0
    duplicate_count = 0
    
    for row in rows:
        if row.content_hash in existing_hashes:
            logger.info(f"Duplicate found by hash: {row.title} ({row.content_type}). Skipping.")
            duplicate_count += 1
            continue
            
        # Convert ShelfRow model to flat list
        flat_row = [
            row.saved_at,
            row.source_type,
            row.content_type,
            row.title,
            row.creator or "",
            row.url,
            row.thumbnail_url or "",
            row.confidence,
            row.instagram_url,
            row.raw_context,
            row.ai_summary,
            row.content_hash,
            row.status,
            row.gemini_notes or "",
            row.tags or ""
        ]
        new_rows_data.append(flat_row)
        new_items_count += 1
        
    if new_rows_data:
        try:
            logger.info(f"Appending {len(new_rows_data)} rows to Google Sheets...")
            await append_rows_with_retry(worksheet, new_rows_data)
            logger.info(f"Successfully wrote {len(new_rows_data)} rows to Google Sheets.")
            
            # Sync offline cache in the background
            asyncio.create_task(sync_pending_rows(worksheet))
            
        except Exception as e:
            logger.error(f"Failed writing rows to Google Sheets after retries: {e}. Caching locally.")
            # Cache successfully parsed rows that failed to upload
            for row in rows:
                # Recheck if it was skipped
                if row.content_hash not in existing_hashes:
                    cache_pending_row(row.model_dump())
            return 0, len(rows)
            
    return new_items_count, duplicate_count

async def sync_pending_rows(worksheet: gspread.Worksheet = None):
    """
    Looks for locally cached pending rows in SQLite and writes them to Google Sheets.
    Called automatically after a successful write, or can be triggered periodically.
    """
    pending = get_pending_rows()
    if not pending:
        return
        
    logger.info(f"Syncing {len(pending)} pending rows from SQLite cache to Google Sheets...")
    try:
        if worksheet is None:
            worksheet = await get_worksheet()
            
        existing_hashes = await get_existing_hashes(worksheet)
        
        rows_to_sync = []
        ids_to_delete = []
        
        for local_id, row_dict in pending:
            content_hash = row_dict.get("content_hash")
            
            if content_hash in existing_hashes:
                # Already exists in sheet, just delete from cache
                ids_to_delete.append(local_id)
                continue
                
            flat_row = [
                row_dict.get("saved_at"),
                row_dict.get("source_type"),
                row_dict.get("content_type"),
                row_dict.get("title"),
                row_dict.get("creator", ""),
                row_dict.get("url"),
                row_dict.get("thumbnail_url", ""),
                row_dict.get("confidence", 1.0),
                row_dict.get("instagram_url"),
                row_dict.get("raw_context"),
                row_dict.get("ai_summary"),
                content_hash,
                row_dict.get("status", "UNREAD"),
                row_dict.get("gemini_notes", ""),
                row_dict.get("tags", "")
            ]
            rows_to_sync.append(flat_row)
            ids_to_delete.append(local_id)
            
        if rows_to_sync:
            await append_rows_with_retry(worksheet, rows_to_sync)
            logger.info(f"Synced {len(rows_to_sync)} cached rows to Google Sheets.")
            
        # Delete from SQLite cache
        for db_id in ids_to_delete:
            delete_pending_row(db_id)
            
    except Exception as e:
        logger.error(f"Failed to sync pending rows from SQLite cache: {e}")

async def get_all_rows_sync_fallback() -> List[Dict[str, Any]]:
    """
    Helper to pull all rows as dictionaries from the Google Sheet.
    Used by /digest and /search.
    """
    try:
        worksheet = await get_worksheet()
        all_records = await asyncio.to_thread(worksheet.get_all_records)
        return all_records
    except Exception as e:
        logger.error(f"Failed to pull records from Google Sheets: {e}")
        return []
