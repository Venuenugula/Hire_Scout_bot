import os
import logging
import sqlite3
import asyncio
import pandas as pd
from datetime import datetime
from jobspy import scrape_jobs
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
)

# ====================================================================
# CONFIGURATION
# ====================================================================

# 🚨 REPLACE WITH YOUR ACTUAL TOKEN
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
# Use mounted disk path if available (for Render persistence), else local file
DB_FILE = "/app/data/bot_database.db" if os.path.exists("/app/data") else "bot_database.db"

# Logging setup
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Conversation states
AWAITING_ROLE, AWAITING_LOCATION = range(2)

# ====================================================================
# DATABASE LAYER
# ====================================================================

class Database:
    def __init__(self, db_file):
        self.db_file = db_file
        self.init_db()

    def get_connection(self):
        return sqlite3.connect(self.db_file)

    def init_db(self):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            # Table for user subscriptions
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS subscriptions (
                    chat_id INTEGER PRIMARY KEY,
                    role TEXT NOT NULL,
                    location TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # Table for processed jobs to prevent duplicates
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS processed_jobs (
                    job_hash TEXT PRIMARY KEY,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()

    def add_subscription(self, chat_id, role, location):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO subscriptions (chat_id, role, location) VALUES (?, ?, ?)",
                (chat_id, role, location)
            )
            conn.commit()

    def remove_subscription(self, chat_id):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM subscriptions WHERE chat_id = ?", (chat_id,))
            conn.commit()

    def get_all_subscriptions(self):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT chat_id, role, location FROM subscriptions")
            return cursor.fetchall()

    def is_job_processed(self, job_hash):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM processed_jobs WHERE job_hash = ?", (job_hash,))
            return cursor.fetchone() is not None

    def mark_job_processed(self, job_hash):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT OR IGNORE INTO processed_jobs (job_hash) VALUES (?)", (job_hash,))
            conn.commit()

db = Database(DB_FILE)

# ====================================================================
# JOB SCRAPING LAYER
# ====================================================================

def get_job_hash(job):
    """Generates a unique hash for a job."""
    # Using title, company, and location to create a unique signature
    raw_str = f"{job.get('title', '')}-{job.get('company_name', '')}-{job.get('location', '')}"
    return str(hash(raw_str))

def scrape_and_filter(role, location, hours_old=24):
    """Scrapes jobs and returns only new ones."""
    logger.info(f"Scraping for {role} in {location}...")
    try:
        jobs = scrape_jobs(
            site_name=["indeed", "linkedin", "zip_recruiter"],
            search_term=role,
            location=location,
            hours_old=hours_old,
            results_wanted=20, 
        )
        
        if jobs is None or jobs.empty:
            return []

        new_jobs = []
        for _, row in jobs.iterrows():
            # Basic validation
            if pd.isna(row['title']) or pd.isna(row['job_url']):
                continue
                
            job_hash = get_job_hash(row)
            if not db.is_job_processed(job_hash):
                new_jobs.append(row)
                db.mark_job_processed(job_hash)
        
        return new_jobs

    except Exception as e:
        logger.error(f"Error scraping jobs: {e}")
        return []

# ====================================================================
# TELEGRAM HANDLERS
# ====================================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "👋 Welcome! I can help you find jobs.\n\n"
        "What **job role** are you looking for? (e.g., 'Python Developer')",
        parse_mode='Markdown'
    )
    return AWAITING_ROLE

async def get_role(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['role'] = update.message.text
    await update.message.reply_text(
        f"✅ Role set to **{context.user_data['role']}**.\n\n"
        "Now, please enter the **location** (e.g., 'Remote', 'New York').",
        parse_mode='Markdown'
    )
    return AWAITING_LOCATION

async def get_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    role = context.user_data['role']
    location = update.message.text
    chat_id = update.effective_chat.id

    # Save to DB
    db.add_subscription(chat_id, role, location)

    # Schedule job immediately for this user
    context.job_queue.run_repeating(
        check_jobs_task,
        interval=3600, # 1 hour
        first=10,      # Start in 10s
        chat_id=chat_id,
        name=str(chat_id),
        data={'role': role, 'location': location}
    )

    await update.message.reply_text(
        f"🎉 **Setup Complete!**\n"
        f"Monitoring **{role}** in **{location}**.\n"
        f"I'll check every hour. Use /stop to cancel.",
        parse_mode='Markdown'
    )
    return ConversationHandler.END

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    db.remove_subscription(chat_id)
    
    # Remove from job queue
    current_jobs = context.job_queue.get_jobs_by_name(str(chat_id))
    for job in current_jobs:
        job.schedule_removal()
        
    await update.message.reply_text("🛑 Monitoring stopped. Use /start to begin again.")

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("❌ Setup cancelled.")
    return ConversationHandler.END

# ====================================================================
# SCHEDULED TASK
# ====================================================================

async def check_jobs_task(context: ContextTypes.DEFAULT_TYPE):
    job_data = context.job.data
    chat_id = context.job.chat_id
    role = job_data['role']
    location = job_data['location']

    new_jobs = await asyncio.to_thread(scrape_and_filter, role, location)

    if new_jobs:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"🚨 Found {len(new_jobs)} new job(s) for **{role}**!",
            parse_mode='Markdown'
        )
        
        # Helper to escape Markdown special characters
        def escape_md(text):
            special_chars = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
            for char in special_chars:
                text = str(text).replace(char, f"\\{char}")
            return text

        for job in new_jobs:
            # Escape fields to prevent Markdown parsing errors
            title = escape_md(job.get('title', 'N/A'))
            company = escape_md(job.get('company_name', 'N/A'))
            loc = escape_md(job.get('location', 'N/A'))
            
            msg = (
                f"💼 *{title}*\n"
                f"🏢 {company}\n"
                f"📍 {loc}\n"
                f"🔗 [Apply Here]({job['job_url']})"
            )
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=msg,
                    parse_mode='MarkdownV2', # Use MarkdownV2 for better escaping support
                    disable_web_page_preview=True
                )
                # Sleep to avoid hitting Telegram rate limits (approx 30 msgs/sec max, but safer to go slow)
                await asyncio.sleep(0.5) 
            except Exception as e:
                logger.error(f"Failed to send message to {chat_id}: {e}")
                # Fallback: Try sending without Markdown if it fails
                try:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=f"💼 {job.get('title')}\n🏢 {job.get('company_name')}\n📍 {job.get('location')}\n🔗 {job['job_url']}",
                        disable_web_page_preview=True
                    )
                except:
                    pass
    else:
        logger.info(f"No new jobs for {chat_id}")

# ====================================================================
# MAIN
# ====================================================================

def main():
    if not BOT_TOKEN:
        print("🚨 ERROR: TELEGRAM_BOT_TOKEN environment variable is not set!")
        return

    application = Application.builder().token(BOT_TOKEN).build()

    # Restore subscriptions on startup
    subscriptions = db.get_all_subscriptions()
    for chat_id, role, location in subscriptions:
        application.job_queue.run_repeating(
            check_jobs_task,
            interval=3600,
            first=30, # Stagger slightly on startup
            chat_id=chat_id,
            name=str(chat_id),
            data={'role': role, 'location': location}
        )
    print(f"Restored {len(subscriptions)} subscriptions.")

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            AWAITING_ROLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_role)],
            AWAITING_LOCATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_location)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("stop", stop))

    print("Bot is running...")
    application.run_polling()

# ====================================================================
# HEALTH CHECK SERVER (FOR RENDER)
# ====================================================================
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

def start_health_server():
    port = int(os.getenv("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    print(f"Health check server listening on port {port}")
    server.serve_forever()

if __name__ == '__main__':
    # Start health check server in a separate thread
    threading.Thread(target=start_health_server, daemon=True).start()
    main()