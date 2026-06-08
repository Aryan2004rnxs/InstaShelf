import os
import shutil
import asyncio
import logging
from datetime import datetime

# Force native gRPC DNS resolution to fix macOS DNS lookup failures
os.environ["GRPC_DNS_RESOLVER"] = "native"
from contextlib import asynccontextmanager
from typing import Optional, List

from fastapi import FastAPI, Request, Response
from telegram import Update, Bot
from telegram.ext import Application

import health
import sheets
import enrichment
import dedup
import gemini_client
from scraper import scrape_instagram_content
from handlers import register_handlers
from models import ShelfRow

# Initialize logging
logger = logging.getLogger("InstaShelf.main")

# Load configuration variables
HF_SPACE_URL = os.getenv("HF_SPACE_URL")
WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

if not HF_SPACE_URL:
    logger.warning("HF_SPACE_URL is not set. Webhook configuration might fail.")
if not BOT_TOKEN:
    logger.error("TELEGRAM_BOT_TOKEN is not set. Bot startup will fail.")

# Initialize python-telegram-bot application
tg_app = Application.builder().token(BOT_TOKEN).build()

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
            
            # Step 3: Gemini AI Extraction (passes images for vision, caption/subtitles for text)
            extracted = await gemini_client.extract_content_with_gemini(caption, image_paths)
            
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
            for video in extracted.youtube_videos:
                enriched = await enrichment.enrich_youtube_video(video.title, video.direct_url, video.search_query)
                video_id = enriched["video_id"]
                content_hash = dedup.compute_youtube_hash(video_id, video.search_query)
                
                # Check duplicates (Hash-based)
                if content_hash in existing_hashes:
                    logger.info(f"Duplicate YouTube video found (hash: {content_hash}). Skipping.")
                    continue
                    
                # Check duplicates (Semantic-based)
                is_smart_dup = await gemini_client.check_smart_dedup(enriched["title"], recent_titles)
                if is_smart_dup:
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
            for book in extracted.books:
                enriched = await enrichment.enrich_book(book.title, book.author, book.search_query)
                content_hash = dedup.compute_book_hash(None, enriched["title"], enriched["author"])
                
                # Check duplicates (Hash-based)
                if content_hash in existing_hashes:
                    logger.info(f"Duplicate Book found (hash: {content_hash}). Skipping.")
                    continue
                    
                # Check duplicates (Semantic-based)
                is_smart_dup = await gemini_client.check_smart_dedup(enriched["title"], recent_titles)
                if is_smart_dup:
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
                content_hash = dedup.compute_link_hash(link.url)
                
                # Check duplicates (Hash-based)
                if content_hash in existing_hashes:
                    logger.info(f"Duplicate Link found (hash: {content_hash}). Skipping.")
                    continue
                    
                row = ShelfRow(
                    saved_at=datetime.utcnow().isoformat() + "Z",
                    source_type=source_type,
                    content_type="LINK",
                    title=link.label,
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
                saved_items_summary.append({"title": link.label, "type": "LINK"})
                
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
                reply_text = await gemini_client.compose_reply_message(saved_items_summary, sheets_url)
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
    
    # Configure bot webhook url dynamically on startup
    if HF_SPACE_URL:
        webhook_url = f"{HF_SPACE_URL}/webhook"
        logger.info(f"Configuring Telegram webhook to: {webhook_url}")
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
    uvicorn.run("main:app", host="0.0.0.0", port=7860, reload=True)
