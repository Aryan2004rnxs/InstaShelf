import asyncio
import logging
import os
import sys
from dotenv import load_dotenv

# Force native gRPC DNS resolution to fix macOS DNS lookup failures
os.environ["GRPC_DNS_RESOLVER"] = "native"

# Load env variables
load_dotenv(override=True)

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("InstaShelf.polling")

try:
    from main import tg_app, background_worker
    from handlers import register_handlers
except ImportError as e:
    logger.error(f"Failed to import modules: {e}")
    sys.exit(1)

async def main():
    logger.info("Starting InstaShelf in local POLLING mode (bypassing webhooks)...")
    
    # Initialize the telegram app
    await tg_app.initialize()
    
    # Set up the processing queue
    processing_queue = asyncio.Queue()
    
    # Register handlers with the queue
    register_handlers(tg_app, processing_queue)
    
    # Start the background worker
    worker_task = asyncio.create_task(background_worker(processing_queue, tg_app.bot))
    
    # Sync any offline cached rows from SQLite to Sheets on startup
    import sheets
    asyncio.create_task(sheets.sync_pending_rows())
    
    # Start polling
    if tg_app.updater:
        logger.info("Deleting active webhook to force polling mode...")
        await tg_app.bot.delete_webhook(drop_pending_updates=False)
        await tg_app.start()
        await tg_app.updater.start_polling()
        logger.info("Bot is polling successfully! 🎉")
        logger.info("Open Telegram and send an Instagram link (post or reel) to your bot to test.")
        
        try:
            # Keep running
            while True:
                await asyncio.sleep(1)
        except (KeyboardInterrupt, asyncio.CancelledError):
            logger.info("Received stop signal.")
        finally:
            logger.info("Stopping polling...")
            await tg_app.updater.stop()
            await tg_app.stop()
            await tg_app.shutdown()
            worker_task.cancel()
            try:
                await worker_task
            except asyncio.CancelledError:
                pass
            logger.info("Bot stopped.")
    else:
        logger.error("Updater is not initialized in the Telegram Application.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot execution terminated by user.")
