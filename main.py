import os
import shutil
import asyncio
import logging
from datetime import datetime

# Force native gRPC DNS resolution to fix macOS DNS lookup failures
os.environ["GRPC_DNS_RESOLVER"] = "native"

# Fix SSL CA Bundle paths overridden by Hugging Face Spaces (causes SSLError in containers)
for var in ["CURL_CA_BUNDLE", "REQUESTS_CA_BUNDLE", "SSL_CERT_FILE"]:
    if var in os.environ:
        del os.environ[var]
from contextlib import asynccontextmanager
from typing import Optional, List

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse
from telegram import Update, Bot
from telegram.ext import Application

import utils

import health
import sheets
import enrichment
import dedup
import ai_client
from scraper import scrape_instagram_content
from handlers import register_handlers
from models import ShelfRow
from pydantic import BaseModel
from fastapi.staticfiles import StaticFiles

import progress
# Initialize logging
logger = logging.getLogger("InstaShelf.main")

# Load configuration variables
HF_SPACE_URL = os.getenv("HF_SPACE_URL")
WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_API_BASE_URL = os.getenv("TELEGRAM_API_BASE_URL")

if not HF_SPACE_URL:
    logger.warning("HF_SPACE_URL is not set. Webhook configuration might fail.")
if not BOT_TOKEN:
    logger.error("TELEGRAM_BOT_TOKEN is not set. Bot startup will fail.")

# Initialize python-telegram-bot application
builder = Application.builder().token(BOT_TOKEN)
if TELEGRAM_API_BASE_URL:
    logger.info(f"Using custom Telegram API base URL: {TELEGRAM_API_BASE_URL}")
    builder.base_url(TELEGRAM_API_BASE_URL)
tg_app = builder.build()

def cleanup_temp_files(temp_dir: Optional[str], image_paths: List[str]):
    """Cleans up temporary files and directories created during scraping."""
    # Delete individual downloaded image files
    for path in image_paths:
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception as e:
            logger.warning(f"Failed to delete temporary image {path}: {e}")
            
    # Remove the temporary download directory
    if temp_dir and os.path.exists(temp_dir):
        try:
            shutil.rmtree(temp_dir)
            logger.info(f"Cleaned up temporary directory: {temp_dir}")
        except Exception as e:
            logger.warning(f"Failed to remove temporary directory {temp_dir}: {e}")

async def background_worker(queue: asyncio.Queue, bot: Bot):
    """
    Background worker that runs sequentially. Processes Instagram URLs:
    1. Scrapes caption, videos, carousel images, and subtitles.
    2. Runs Gemini extraction (multimodal vision for images, text for Reels).
    3. Resolves and enriches YouTube details and Open Library books.
    4. Computes deduplication hashes and runs Gemini semantic smart-dedup.
    5. Saves items to Google Sheets (batch append with offline SQLite caching on failure).
    6. Sends AI-curated summary reply to Telegram user.
    """
    logger.info("Background queue processing worker started.")
    
    while True:
        job = await queue.get()
        url = job.get("url")
        chat_id = job.get("chat_id")
        
        logger.info(f"Starting processing job for URL: {url} (chat_id: {chat_id})")
        
        temp_dir = None
        image_paths = []
        
        try:
            # Step 2: Scrape Instagram content
            source_type, caption, image_paths, temp_dir = await scrape_instagram_content(url)
            
            # Step 3: AI Extraction (passes images for vision, caption/subtitles for text)
            extracted = await ai_client.extract_content_with_ai(caption, image_paths)
            
            # Fetch existing records from Sheets to run exact/semantic deduplication
            sheet_id = os.getenv("GOOGLE_SHEET_ID")
            sheets_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"
            
            try:
                worksheet = await sheets.get_worksheet()
                existing_hashes = await sheets.get_existing_hashes(worksheet)
                recent_titles = await sheets.get_recent_titles(worksheet, limit=50)
            except Exception as e:
                logger.error(f"Failed to fetch sheet records for deduplication check: {e}")
                existing_hashes = set()
                recent_titles = []
                
            rows_to_save: List[ShelfRow] = []
            saved_items_summary = []
            
            # Process & Enrich extracted YouTube videos
            videos_to_check = []
            enriched_videos = []
            for video in extracted.youtube_videos:
                enriched = await enrichment.enrich_youtube_video(video.title, video.direct_url, video.search_query)
                video_id = enriched["video_id"]
                content_hash = dedup.compute_youtube_hash(video_id, video.search_query)
                
                # Check duplicates (Hash-based)
                if content_hash in existing_hashes:
                    logger.info(f"Duplicate YouTube video found (hash: {content_hash}). Skipping.")
                    continue
                    
                videos_to_check.append(enriched["title"])
                enriched_videos.append((video, enriched, content_hash))
                
            # Run batch smart deduplication check (reduces API calls from N to 1)
            duplicate_video_titles = await ai_client.check_smart_dedup_batch(videos_to_check, recent_titles)
            
            for video, enriched, content_hash in enriched_videos:
                if enriched["title"] in duplicate_video_titles:
                    logger.info(f"Smart duplicate YouTube video found: '{enriched['title']}'. Skipping.")
                    continue
                    
                row = ShelfRow(
                    saved_at=datetime.utcnow().isoformat() + "Z",
                    source_type=source_type,
                    content_type="YOUTUBE",
                    title=enriched["title"],
                    creator=enriched["channel"] or video.channel or "",
                    url=enriched["url"],
                    thumbnail_url=enriched["thumbnail_url"],
                    confidence=video.confidence,
                    instagram_url=url,
                    raw_context=video.context,
                    ai_summary=extracted.summary,
                    content_hash=content_hash,
                    status="UNREAD",
                    gemini_notes=f"Original Search Title: {video.title}",
                    tags=" ".join(video.tags)
                )
                rows_to_save.append(row)
                saved_items_summary.append({"title": enriched["title"], "type": "YOUTUBE"})
                recent_titles.append(enriched["title"]) # Avoid self-deduplication in the same batch
                
            # Process & Enrich extracted books
            books_to_check = []
            enriched_books = []
            for book in extracted.books:
                enriched = await enrichment.enrich_book(book.title, book.author, book.search_query)
                content_hash = dedup.compute_book_hash(None, enriched["title"], enriched["author"])
                
                # Check duplicates (Hash-based)
                if content_hash in existing_hashes:
                    logger.info(f"Duplicate Book found (hash: {content_hash}). Skipping.")
                    continue
                    
                books_to_check.append(enriched["title"])
                enriched_books.append((book, enriched, content_hash))
                
            # Run batch smart deduplication check (reduces API calls from N to 1)
            duplicate_book_titles = await ai_client.check_smart_dedup_batch(books_to_check, recent_titles)
            
            for book, enriched, content_hash in enriched_books:
                if enriched["title"] in duplicate_book_titles:
                    logger.info(f"Smart duplicate Book found: '{enriched['title']}'. Skipping.")
                    continue
                    
                publish_year_note = f" (First Published: {enriched['publish_year']})" if enriched.get('publish_year') else ""
                row = ShelfRow(
                    saved_at=datetime.utcnow().isoformat() + "Z",
                    source_type=source_type,
                    content_type="BOOK",
                    title=enriched["title"],
                    creator=enriched["author"] or book.author or "",
                    url=enriched["url"],
                    thumbnail_url=enriched["thumbnail_url"],
                    confidence=book.confidence,
                    instagram_url=url,
                    raw_context=book.context,
                    ai_summary=extracted.summary,
                    content_hash=content_hash,
                    status="UNREAD",
                    gemini_notes=f"Original Title: {book.title}{publish_year_note}",
                    tags=" ".join(book.tags)
                )
                rows_to_save.append(row)
                saved_items_summary.append({"title": enriched["title"], "type": "BOOK"})
                recent_titles.append(enriched["title"])
                
            # Process extracted general links
            for link in extracted.other_links:
                if not link.url or not link.url.strip():
                    logger.warning(f"Skipping link '{link.label}' because it has no URL.")
                    continue
                content_hash = dedup.compute_link_hash(link.url)
                
                # Check duplicates (Hash-based)
                if content_hash in existing_hashes:
                    logger.info(f"Duplicate Link found (hash: {content_hash}). Skipping.")
                    continue
                    
                link_title = link.label or "Resource Link"
                row = ShelfRow(
                    saved_at=datetime.utcnow().isoformat() + "Z",
                    source_type=source_type,
                    content_type="LINK",
                    title=link_title,
                    creator="",
                    url=link.url,
                    thumbnail_url="",
                    confidence=1.0,
                    instagram_url=url,
                    raw_context="General URL in post",
                    ai_summary=extracted.summary,
                    content_hash=content_hash,
                    status="UNREAD",
                    gemini_notes="",
                    tags=" ".join(link.tags)
                )
                rows_to_save.append(row)
                saved_items_summary.append({"title": link_title, "type": "LINK"})
                
            # Steps 5 & 6: Write to Google Sheets
            if not rows_to_save:
                if not extracted.youtube_videos and not extracted.books and not extracted.other_links:
                    # Nothing found
                    excerpt = caption[:300]
                    await bot.send_message(
                        chat_id=chat_id,
                        text=f"Nothing found in this post. Here's what I read:\n\n\"{excerpt}...\"\n\nDoes this look right?"
                    )
                else:
                    # Found, but all were duplicates
                    await bot.send_message(
                        chat_id=chat_id,
                        text="No new items saved. All items found are already on your shelf! 📚"
                    )
            else:
                new_saved_count, dup_count = await sheets.save_rows_to_shelf(rows_to_save)
                
                # Step 7: Reply to user with AI friendly summary
                reply_text = await ai_client.compose_reply_message(saved_items_summary, sheets_url)
                await bot.send_message(chat_id=chat_id, text=reply_text)
                
        except ValueError as ve:
            logger.warning(f"Validation failure: {ve}")
            await bot.send_message(chat_id=chat_id, text=f"⚠️ {str(ve)}")
        except Exception as e:
            logger.exception(f"Unexpected error processing job: {e}")
            await bot.send_message(chat_id=chat_id, text=f"❌ An error occurred: {str(e)}")
        finally:
            cleanup_temp_files(temp_dir, image_paths)
            queue.task_done()
            logger.info(f"Finished processing job for URL: {url}")

# FastAPI lifecycles
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize python-telegram-bot
    await tg_app.initialize()
    await tg_app.start()
    
    # Check if polling mode is forced (e.g. for private Hugging Face spaces)
    polling_mode = os.getenv("TELEGRAM_POLLING", "false").lower() == "true"
    
    if polling_mode:
        logger.info("Forcing POLLING mode: Deleting any active webhook and starting polling...")
        await tg_app.bot.delete_webhook(drop_pending_updates=True)
        await asyncio.sleep(2)
        await tg_app.updater.start_polling(drop_pending_updates=True)
        logger.info("Bot started in POLLING mode successfully.")
    else:
        # Configure bot webhook url dynamically on startup
        if HF_SPACE_URL:
            webhook_url = f"{HF_SPACE_URL}/webhook"
            logger.info(f"Configuring Telegram webhook to: {webhook_url}")
            await tg_app.bot.delete_webhook(drop_pending_updates=True)
            await asyncio.sleep(2)
            await tg_app.bot.set_webhook(
                url=webhook_url,
                secret_token=WEBHOOK_SECRET if WEBHOOK_SECRET else None
            )
        else:
            logger.error("HF_SPACE_URL is not set. Webhook was not configured.")
            
    # Set up async in-process queue
    processing_queue = asyncio.Queue()
    
    # Register Telegram bot handlers with the queue
    register_handlers(tg_app, processing_queue)
    
    # Run the background worker task
    worker_task = asyncio.create_task(background_worker(processing_queue, tg_app.bot))
    
    # Sync any offline cached rows from SQLite to Sheets on startup
    asyncio.create_task(sheets.sync_pending_rows())
    
    yield
    
    # Shutdown sequence
    logger.info("Shutting down background tasks and Telegram bot...")
    if polling_mode:
        await tg_app.updater.stop()
    worker_task.cancel()
    try:
        await worker_task
    except asyncio.CancelledError:
        pass
        
    await tg_app.stop()
    await tg_app.shutdown()
    logger.info("Shutdown complete.")

app = FastAPI(lifespan=lifespan)

# Add healthcheck route
app.include_router(health.router)

# Mount frontend directory for static assets
app.mount("/static", StaticFiles(directory="frontend"), name="static")

@app.get("/shelf", response_class=HTMLResponse)
async def serve_shelf():
    """Serves the interactive InstaShelf web view."""
    try:
        with open("frontend/index.html", "r") as f:
            return HTMLResponse(content=f.read(), status_code=200)
    except FileNotFoundError:
        return HTMLResponse(content="<h1>Frontend not found. Please build or create the frontend/index.html file.</h1>", status_code=404)

@app.get("/api/shelf")
async def api_get_shelf():
    """API endpoint to get all shelf items with their progress."""
    rows = await sheets.get_all_rows_sync_fallback()
    progress_data = progress.get_all_progress()
    return {"status": "success", "data": rows, "progress": progress_data}

@app.get("/api/progress")
async def api_get_progress():
    """API endpoint to get all user progress."""
    progress_data = progress.get_all_progress()
    return {"status": "success", "progress": progress_data}

class ProgressUpdate(BaseModel):
    content_hash: str
    progress_seconds: int
    is_completed: bool

@app.post("/api/progress")
async def api_update_progress(update: ProgressUpdate):
    """API endpoint to update user progress for an item."""
    success = progress.update_progress(update.content_hash, update.progress_seconds, update.is_completed)
    if success:
        return {"status": "success"}
    else:
        return {"status": "error", "message": "Failed to update progress"}, 500

class NoteCreate(BaseModel):
    content_hash: str
    timestamp_seconds: int
    note_text: str

@app.get("/api/notes/{content_hash}")
async def api_get_notes(content_hash: str):
    """API endpoint to get all notes for a specific content item."""
    notes = progress.get_notes(content_hash)
    return {"status": "success", "notes": notes}

@app.post("/api/notes")
async def api_create_note(note: NoteCreate):
    """API endpoint to create a new timestamped note."""
    new_note = progress.add_note(note.content_hash, note.timestamp_seconds, note.note_text)
    if new_note:
        return {"status": "success", "note": new_note}
    return {"status": "error", "message": "Failed to create note"}, 500

class GenerateNoteRequest(BaseModel):
    title: str

@app.post("/api/notes/{content_hash}/generate")
async def api_generate_notes_summary(content_hash: str, req: GenerateNoteRequest):
    """API endpoint to generate an AI summary from existing notes."""
    notes = progress.get_notes(content_hash)
    if not notes:
        return {"status": "error", "message": "No notes found"}, 400
    
    summary = await ai_client.generate_notes_summary(req.title, notes)
    if summary:
        return {"status": "success", "summary": summary}
    return {"status": "error", "message": "Failed to generate summary"}, 500

@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    """Serves the premium InstaShelf status & control web dashboard."""
    groq_usage = utils.get_groq_usage()
    gemini_usage = utils.get_gemini_usage()
    pending_rows = len(utils.get_pending_rows())
    
    groq_percentage = min(100.0, (groq_usage / 1000.0) * 100.0)
    gemini_percentage = min(100.0, (gemini_usage / 20.0) * 100.0)
    
    html_content = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>InstaShelf Dashboard</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;800&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-color: #0b0f19;
            --card-bg: rgba(255, 255, 255, 0.03);
            --card-border: rgba(255, 255, 255, 0.08);
            --text-primary: #ffffff;
            --text-secondary: #94a3b8;
            --primary: #8b5cf6;
            --primary-glow: rgba(139, 92, 246, 0.15);
            --success: #10b981;
            --success-glow: rgba(16, 185, 129, 0.15);
            --warning: #f59e0b;
        }

        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }

        body {
            font-family: 'Outfit', sans-serif;
            background-color: var(--bg-color);
            color: var(--text-primary);
            min-height: 100vh;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            overflow-x: hidden;
            position: relative;
        }

        /* Abstract glowing backgrounds */
        body::before {
            content: '';
            position: absolute;
            width: 400px;
            height: 400px;
            background: radial-gradient(circle, var(--primary-glow) 0%, transparent 70%);
            top: -100px;
            left: -100px;
            z-index: 0;
            pointer-events: none;
        }

        body::after {
            content: '';
            position: absolute;
            width: 450px;
            height: 450px;
            background: radial-gradient(circle, rgba(16, 185, 129, 0.08) 0%, transparent 70%);
            bottom: -100px;
            right: -100px;
            z-index: 0;
            pointer-events: none;
        }

        .container {
            width: 100%;
            max-width: 900px;
            padding: 40px 20px;
            z-index: 1;
        }

        header {
            text-align: center;
            margin-bottom: 40px;
        }

        h1 {
            font-size: 2.8rem;
            font-weight: 800;
            background: linear-gradient(135deg, #ffffff 40%, #a78bfa 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            margin-bottom: 8px;
            letter-spacing: -0.5px;
        }

        header p {
            color: var(--text-secondary);
            font-size: 1.1rem;
            font-weight: 300;
        }

        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
            gap: 24px;
            margin-bottom: 32px;
        }

        .card {
            background: var(--card-bg);
            border: 1px solid var(--card-border);
            border-radius: 20px;
            padding: 24px;
            backdrop-filter: blur(20px);
            -webkit-backdrop-filter: blur(20px);
            transition: all 0.4s cubic-bezier(0.16, 1, 0.3, 1);
            position: relative;
            overflow: hidden;
        }

        .card:hover {
            transform: translateY(-5px);
            border-color: rgba(255, 255, 255, 0.15);
            box-shadow: 0 12px 30px rgba(0, 0, 0, 0.3);
        }

        .card-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
        }

        .card-title {
            font-size: 0.95rem;
            font-weight: 600;
            color: var(--text-secondary);
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }

        .card-icon {
            font-size: 1.4rem;
            width: 40px;
            height: 40px;
            border-radius: 12px;
            display: flex;
            align-items: center;
            justify-content: center;
            background: rgba(255, 255, 255, 0.03);
            border: 1px solid var(--card-border);
        }

        .stat-value {
            font-size: 2.2rem;
            font-weight: 800;
            margin-bottom: 8px;
            display: flex;
            align-items: baseline;
            gap: 4px;
        }

        .stat-unit {
            font-size: 1rem;
            font-weight: 400;
            color: var(--text-secondary);
        }

        .stat-desc {
            font-size: 0.85rem;
            color: var(--text-secondary);
        }

        /* Status Badge */
        .status-badge {
            display: inline-flex;
            align-items: center;
            gap: 8px;
            background: var(--success-glow);
            color: var(--success);
            padding: 6px 14px;
            border-radius: 30px;
            font-weight: 600;
            font-size: 0.9rem;
            border: 1px solid rgba(16, 185, 129, 0.2);
            box-shadow: 0 0 15px rgba(16, 185, 129, 0.05);
        }

        .status-dot {
            width: 8px;
            height: 8px;
            background-color: var(--success);
            border-radius: 50%;
            display: inline-block;
            animation: pulse 2s infinite;
        }

        @keyframes pulse {
            0% { transform: scale(0.9); opacity: 0.6; }
            50% { transform: scale(1.15); opacity: 1; box-shadow: 0 0 10px var(--success); }
            100% { transform: scale(0.9); opacity: 0.6; }
        }

        /* Progress Bar */
        .progress-container {
            width: 100%;
            height: 6px;
            background: rgba(255, 255, 255, 0.05);
            border-radius: 10px;
            margin-top: 15px;
            overflow: hidden;
        }

        .progress-bar {
            height: 100%;
            background: linear-gradient(90deg, var(--primary) 0%, #a78bfa 100%);
            border-radius: 10px;
            transition: width 1s ease-in-out;
        }

        .progress-bar.gemini {
            background: linear-gradient(90deg, #3b82f6 0%, #60a5fa 100%);
        }

        /* Guide Card */
        .guide-card {
            grid-column: span 1;
        }

        @media (min-width: 768px) {
            .guide-card {
                grid-column: span 2;
            }
        }

        .guide-list {
            list-style: none;
            display: flex;
            flex-direction: column;
            gap: 12px;
        }

        .guide-item {
            display: flex;
            align-items: flex-start;
            gap: 12px;
            font-size: 0.95rem;
            color: var(--text-secondary);
        }

        .guide-num {
            background: rgba(139, 92, 246, 0.1);
            color: var(--primary);
            border: 1px solid rgba(139, 92, 246, 0.2);
            width: 24px;
            height: 24px;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 0.8rem;
            font-weight: 700;
            flex-shrink: 0;
            margin-top: 2px;
        }

        footer {
            text-align: center;
            color: var(--text-secondary);
            font-size: 0.85rem;
            margin-top: 20px;
            border-top: 1px solid rgba(255, 255, 255, 0.05);
            padding-top: 20px;
            width: 100%;
        }

        footer a {
            color: var(--primary);
            text-decoration: none;
            transition: color 0.2s;
        }

        footer a:hover {
            color: #a78bfa;
        }

        .shelf-btn {
            display: inline-block;
            margin-top: 20px;
            padding: 12px 32px;
            background: linear-gradient(135deg, var(--primary) 0%, #a78bfa 100%);
            color: white;
            text-decoration: none;
            font-weight: 600;
            border-radius: 30px;
            font-size: 1.1rem;
            transition: all 0.3s ease;
            box-shadow: 0 4px 15px rgba(139, 92, 246, 0.4);
        }

        .shelf-btn:hover {
            transform: translateY(-2px);
            box-shadow: 0 6px 20px rgba(139, 92, 246, 0.6);
        }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>InstaShelf</h1>
            <p>Telegram Intelligent Content Extractor & Book Shelf Curation</p>
            <a href="/shelf" class="shelf-btn">✨ Open Interactive Shelf</a>
        </header>

        <div class="grid">
            <!-- Bot Status Card -->
            <div class="card">
                <div class="card-header">
                    <span class="card-title">System Health</span>
                    <span class="card-icon">⚡</span>
                </div>
                <div style="margin: 15px 0;">
                    <div class="status-badge">
                        <span class="status-dot"></span>
                        Active & Running
                    </div>
                </div>
                <p class="stat-desc">Webhook configuration active, receiving updates.</p>
            </div>

            <!-- Groq API Usage Card -->
            <div class="card">
                <div class="card-header">
                    <span class="card-title">Groq API Quota (Primary)</span>
                    <span class="card-icon">⚡</span>
                </div>
                <div class="stat-value">
                    {groq_usage} <span class="stat-unit">/ 1000</span>
                </div>
                <p class="stat-desc">Daily primary AI extraction requests used.</p>
                <div class="progress-container">
                    <div class="progress-bar" style="width: {groq_percentage}%"></div>
                </div>
            </div>

            <!-- Gemini API Usage Card -->
            <div class="card">
                <div class="card-header">
                    <span class="card-title">Gemini API Quota (Fallback)</span>
                    <span class="card-icon">🤖</span>
                </div>
                <div class="stat-value">
                    {gemini_usage} <span class="stat-unit">/ 20</span>
                </div>
                <p class="stat-desc">Daily fallback AI extraction requests used.</p>
                <div class="progress-container">
                    <div class="progress-bar gemini" style="width: {gemini_percentage}%"></div>
                </div>
            </div>

            <!-- Database / Caching Card -->
            <div class="card">
                <div class="card-header">
                    <span class="card-title">Offline Cache Sync</span>
                    <span class="card-icon">💾</span>
                </div>
                <div class="stat-value">
                    {pending_rows}
                    <span class="stat-unit">rows</span>
                </div>
                <p class="stat-desc">Pending Google Sheets updates in offline queue.</p>
            </div>

            <!-- Quick Instructions Card -->
            <div class="card guide-card">
                <div class="card-header">
                    <span class="card-title">Telegram Bot Instructions</span>
                    <span class="card-icon">📖</span>
                </div>
                <ul class="guide-list">
                    <li class="guide-item">
                        <span class="guide-num">1</span>
                        <span>Send any Instagram Post or Reel URL to your Telegram bot.</span>
                    </li>
                    <li class="guide-item">
                        <span class="guide-num">2</span>
                        <span>The bot automatically scrapes the video keyframes/captions, runs Groq/Gemini Multimodal extraction, and dedups.</span>
                    </li>
                    <li class="guide-item">
                        <span class="guide-num">3</span>
                        <span>Identified books and YouTube videos are enriched and saved instantly to your Google Sheet.</span>
                    </li>
                </ul>
            </div>
        </div>

        <footer>
            <p>Made with ❤️ by <a href="https://github.com/Aryan2004rnxs/InstaShelf" target="_blank">Aryan2004rnxs</a> | Powered by Groq Llama 4 & Gemini 2.5 & FastAPI</p>
        </footer>
    </div>
</body>
</html>"""
    formatted_html = html_content.replace("{groq_usage}", str(groq_usage))\
                                 .replace("{groq_percentage}", f"{groq_percentage:.1f}")\
                                 .replace("{gemini_usage}", str(gemini_usage))\
                                 .replace("{gemini_percentage}", f"{gemini_percentage:.1f}")\
                                 .replace("{pending_rows}", str(pending_rows))
    return HTMLResponse(content=formatted_html, status_code=200)

@app.post("/webhook")
async def telegram_webhook(request: Request):
    """
    Receives updates from Telegram webhook.
    Validates security header if secret token is configured.
    """
    if WEBHOOK_SECRET:
        received_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if received_secret != WEBHOOK_SECRET:
            logger.warning("Forbidden webhook request: Secret token mismatch.")
            return Response(status_code=403)
            
    try:
        data = await request.json()
        update = Update.de_json(data, tg_app.bot)
        # Process asynchronously using PTB process_update
        await tg_app.process_update(update)
    except Exception as e:
        logger.error(f"Error processing incoming Telegram update: {e}")
        
    return Response(status_code=200)

if __name__ == "__main__":
    import uvicorn
    dev_reload = os.getenv("DEV_RELOAD", "false").lower() == "true"
    uvicorn.run("main:app", host="0.0.0.0", port=7860, reload=dev_reload)
