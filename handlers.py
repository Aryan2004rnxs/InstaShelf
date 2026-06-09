import os
import re
import logging
import asyncio
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes
)
import sheets
import ai_client
from scraper import IG_REGEX

logger = logging.getLogger("InstaShelf.handlers")

def is_allowed_user(user_id: int) -> bool:
    """Verifies if the sender is authorized to use the bot."""
    allowed_ids_str = os.getenv("ALLOWED_USER_IDS", "")
    if not allowed_ids_str:
        logger.warning("ALLOWED_USER_IDS is empty. Denying access to all users.")
        return False
    try:
        allowed_ids = [int(x.strip()) for x in allowed_ids_str.split(",") if x.strip()]
        return user_id in allowed_ids
    except Exception as e:
        logger.error(f"Failed to parse ALLOWED_USER_IDS: {e}")
        return False

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Greets the user and explains how to use the bot."""
    user = update.effective_user
    if not is_allowed_user(user.id):
        await update.message.reply_text("⛔ Sorry, you are not authorized to use this bot.")
        return
        
    welcome_text = (
        "Welcome to *InstaShelf*! 📚🚀\n\n"
        "I will help you save valuable content (YouTube links, books, resources) "
        "directly from Instagram posts or reels to Google Sheets with zero effort.\n\n"
        "👉 *How to use*:\n"
        "1. Share any public Instagram Post or Reel link directly to me.\n"
        "2. I'll acknowledge the request immediately (< 1s).\n"
        "3. In the background, I'll scrape and extract titles/links and save them to Google Sheets.\n"
        "4. I will send you a friendly AI summary when it is saved!\n\n"
        "📚 *Commands*:\n"
        "/digest - Get a weekly curated reading/watch list summary\n"
        "/search <query> - Search your shelf using natural language"
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")

async def digest_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generates an AI-curated digest of shelf items saved in the last 7 days."""
    user = update.effective_user
    if not is_allowed_user(user.id):
        await update.message.reply_text("⛔ Unauthorized.")
        return
        
    await update.message.reply_text("🔄 Pulling your weekly shelf data and generating digest...")
    
    records = await sheets.get_all_rows_sync_fallback()
    if not records:
        await update.message.reply_text("Your shelf is currently empty! Save some links first.")
        return
        
    # Filter items saved in the last 7 days
    recent_items = []
    seven_days_ago = datetime.utcnow() - timedelta(days=7)
    
    for r in records:
        saved_at_str = r.get("saved_at")
        if not saved_at_str:
            continue
        try:
            # Parse ISO 8601 timestamp
            dt = datetime.fromisoformat(saved_at_str.replace("Z", "+00:00")).replace(tzinfo=None)
            if dt >= seven_days_ago:
                recent_items.append({
                    "title": r.get("title"),
                    "creator": r.get("creator"),
                    "type": r.get("content_type"),
                    "url": r.get("url"),
                    "tags": r.get("tags"),
                    "summary": r.get("ai_summary")
                })
        except Exception:
            # Fallback if timestamp format is unexpected
            pass
            
    sheet_id = os.getenv("GOOGLE_SHEET_ID")
    sheets_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"
    
    digest_text = await ai_client.get_weekly_digest(recent_items, sheets_url)
    await update.message.reply_text(digest_text)

async def search_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Searches the shelf items using natural language."""
    user = update.effective_user
    if not is_allowed_user(user.id):
        await update.message.reply_text("⛔ Unauthorized.")
        return
        
    query = " ".join(context.args)
    if not query:
        await update.message.reply_text("⚠️ Please provide a search query. Example: `/search machine learning`", parse_mode="Markdown")
        return
        
    await update.message.reply_text(f"🔍 Searching your shelf for: '{query}'...")
    
    records = await sheets.get_all_rows_sync_fallback()
    if not records:
        await update.message.reply_text("Your shelf is currently empty!")
        return
        
    # Simplify records list to reduce token usage
    simplified_items = []
    for r in records:
        simplified_items.append({
            "title": r.get("title"),
            "creator": r.get("creator"),
            "type": r.get("content_type"),
            "url": r.get("url"),
            "tags": r.get("tags"),
            "summary": r.get("ai_summary"),
            "context": r.get("raw_context")
        })
        
    search_results = await ai_client.search_shelf(query, simplified_items)
    await update.message.reply_text(search_results)

async def instagram_link_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Parses messages for Instagram links and queues them for processing."""
    user = update.effective_user
    if not is_allowed_user(user.id):
        await update.message.reply_text("⛔ Unauthorized.")
        return
        
    text = update.message.text
    match = re.search(IG_REGEX, text)
    
    if not match:
        await update.message.reply_text("Send me an Instagram post or reel link! 📥")
        return
        
    instagram_url = match.group(0)
    chat_id = update.effective_chat.id
    
    # Acknowledge immediately (< 1s)
    await update.message.reply_text("📥 Processing... I'll update you shortly.")
    
    # Push to asyncio queue
    queue = context.application.bot_data.get("processing_queue")
    if queue:
        await queue.put({"url": instagram_url, "chat_id": chat_id})
        logger.info(f"Queued URL: {instagram_url} for chat_id: {chat_id}")
    else:
        logger.error("Processing queue not found in bot_data.")
        await update.message.reply_text("❌ System error: processing queue is offline.")

def register_handlers(application: Application, processing_queue: asyncio.Queue):
    """Registers handlers and attaches the processing queue to application state."""
    # Store queue in bot_data to access inside handlers
    application.bot_data["processing_queue"] = processing_queue
    
    # Add command handlers
    application.add_handler(CommandHandler("start", start_handler))
    application.add_handler(CommandHandler("digest", digest_handler))
    application.add_handler(CommandHandler("search", search_handler))
    
    # Add message handler for text containing Instagram links (or generic messages)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, instagram_link_handler))
    logger.info("Telegram command and message handlers registered successfully.")
