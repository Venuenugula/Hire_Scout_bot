import os
import logging
import sqlite3
import asyncio
import threading
import pandas as pd
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
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
            # Table for user subscriptions (Updated for multiple subs)
            # We drop the old table if it exists to migrate to the new schema
            # In a production app with real users, we would migrate data, but here we reset.
            try:
                cursor.execute("SELECT id FROM subscriptions LIMIT 1")
            except sqlite3.OperationalError:
                # Table doesn't exist or old schema, let's recreate safely
                pass
            
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    location TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(chat_id, role, location)
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
            try:
                cursor.execute(
                    "INSERT INTO subscriptions (chat_id, role, location) VALUES (?, ?, ?)",
                    (chat_id, role, location)
                )
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False # Already exists

    def remove_subscription(self, sub_id, chat_id):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM subscriptions WHERE id = ? AND chat_id = ?", (sub_id, chat_id))
            conn.commit()
            return cursor.rowcount > 0

    def get_user_subscriptions(self, chat_id):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, role, location FROM subscriptions WHERE chat_id = ?", (chat_id,))
            return cursor.fetchall()

    def get_all_subscriptions(self):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, chat_id, role, location FROM subscriptions")
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
    raw_str = f"{job.get('title', '')}-{job.get('company_name', '')}-{job.get('location', '')}"
    return str(hash(raw_str))

def scrape_and_filter(role, location, hours_old=24, limit=10):
    """Scrapes jobs and returns only new ones."""
    logger.info(f"Scraping for {role} in {location}...")
    try:
        jobs = scrape_jobs(
            site_name=["indeed", "linkedin", "zip_recruiter"],
            search_term=role,
            location=location,
            hours_old=hours_old,
            results_wanted=limit, 
        )
        
        if jobs is None or jobs.empty:
            return []

        # Convert to list of dicts
        job_list = []
        for _, row in jobs.iterrows():
            if pd.isna(row['title']) or pd.isna(row['job_url']):
                continue
            job_list.append(row)
            
        return job_list

    except Exception as e:
        logger.error(f"Error scraping jobs: {e}")
        return []

# ====================================================================
# TELEGRAM HANDLERS
# ====================================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "👋 **Welcome to Hire Scout Bot!** 🚀\n\n"
        "I can help you find jobs in two ways:\n"
        "1. **Live Monitoring**: I'll check for new jobs every hour and notify you.\n"
        "2. **Instant Search**: You can search for jobs anytime.\n\n"
        "**Commands:**\n"
        "🔍 `/search <role> in <location>` - Instant search\n"
        "➕ `/add` - Add a new job alert subscription\n"
        "📋 `/list` - View your active subscriptions\n"
        "❌ `/delete <id>` - Remove a subscription\n\n"
        "Try `/search Python in Remote` to see how it works!",
        parse_mode='Markdown'
    )

# --- Conversation for Adding Subscriptions ---

async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    logger.info(f"User {update.effective_user.id} started /add")
    try:
        await update.message.reply_text(
            "🔔 **New Job Alert**\n\n"
            "What **job role** do you want to monitor? (e.g., 'React Developer')",
            parse_mode='Markdown'
        )
        return AWAITING_ROLE
    except Exception as e:
        logger.error(f"Error in add_start: {e}")
        await update.message.reply_text("⚠️ An error occurred starting the setup.")
        return ConversationHandler.END

async def get_role(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    logger.info(f"User {update.effective_user.id} provided role: {update.message.text}")
    try:
        context.user_data['role'] = update.message.text
        await update.message.reply_text(
            f"✅ Role: **{context.user_data['role']}**\n\n"
            "Now, please enter the **location** (e.g., 'London', 'Remote').",
            parse_mode='Markdown'
        )
        return AWAITING_LOCATION
    except Exception as e:
        logger.error(f"Error in get_role: {e}")
        await update.message.reply_text("⚠️ An error occurred. Please try /add again.")
        return ConversationHandler.END

async def get_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    logger.info(f"User {update.effective_user.id} provided location: {update.message.text}")
    try:
        role = context.user_data.get('role')
        if not role:
            await update.message.reply_text("⚠️ Session expired. Please start over with /add.")
            return ConversationHandler.END
            
        location = update.message.text
        chat_id = update.effective_chat.id

        if db.add_subscription(chat_id, role, location):
            await update.message.reply_text(
                f"🎉 **Alert Added!**\n"
                f"I will notify you about new **{role}** jobs in **{location}**.",
                parse_mode='Markdown'
            )
        else:
            await update.message.reply_text("⚠️ You are already monitoring this role and location.")

        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Error in get_location: {e}")
        await update.message.reply_text("⚠️ An error occurred saving your alert.")
        return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("❌ Operation cancelled.")
    return ConversationHandler.END

# --- Management Commands ---

async def list_subs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    subs = db.get_user_subscriptions(chat_id)
    
    if not subs:
        await update.message.reply_text("📭 You have no active subscriptions. Use `/add` to create one!", parse_mode='Markdown')
        return

    msg = "📋 **Your Active Alerts:**\n\n"
    for sub_id, role, loc in subs:
        msg += f"🆔 `{sub_id}`: **{role}** in **{loc}**\n"
    
    msg += "\nTo delete one, use `/delete <id>` (e.g., `/delete 1`)"
    await update.message.reply_text(msg, parse_mode='Markdown')

async def delete_sub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    try:
        sub_id = int(context.args[0])
        if db.remove_subscription(sub_id, chat_id):
            await update.message.reply_text(f"✅ Subscription `{sub_id}` deleted.", parse_mode='Markdown')
        else:
            await update.message.reply_text("❌ Subscription not found or not yours.")
    except (IndexError, ValueError):
        await update.message.reply_text("⚠️ Usage: `/delete <id>`\nUse `/list` to find IDs.")

# --- Dynamic Search ---

async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Expected format: /search Role in Location
    # Example: /search Python Developer in Remote
    
    args = " ".join(context.args)
    if " in " not in args:
        await update.message.reply_text(
            "⚠️ Invalid format.\n"
            "Usage: `/search <role> in <location>`\n"
            "Example: `/search Python in Remote`",
            parse_mode='Markdown'
        )
        return

    role, location = args.split(" in ", 1)
    await update.message.reply_text(f"🔍 Searching for **{role}** in **{location}**... Please wait.", parse_mode='Markdown')

    # Run scraping in a separate thread to not block the bot
    jobs = await asyncio.to_thread(scrape_and_filter, role, location, hours_old=72, limit=10)

    if not jobs:
        await update.message.reply_text("😔 No recent jobs found for this search.")
        return

    await send_job_batch(context, update.effective_chat.id, jobs, header=f"🔎 Search Results for **{role}**:")

# --- Helper: Message Sending ---

async def send_job_batch(context, chat_id, jobs, header=""):
    if header:
        await context.bot.send_message(chat_id=chat_id, text=header, parse_mode='Markdown')

    # Send in chunks
    chunk_size = 5
    for i in range(0, len(jobs), chunk_size):
        chunk = jobs[i:i + chunk_size]
        message_text = ""
        
        for job in chunk:
            title = job.get('title', 'N/A')
            company = job.get('company_name', 'N/A')
            loc = job.get('location', 'N/A')
            url = job.get('job_url', 'N/A')
            
            message_text += (
                f"💼 {title}\n"
                f"🏢 {company}\n"
                f"📍 {loc}\n"
                f"🔗 {url}\n"
                "-------------------\n"
            )
        
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=message_text,
                disable_web_page_preview=True
            )
            await asyncio.sleep(1.0)
        except Exception as e:
            logger.error(f"Failed to send batch to {chat_id}: {e}")

# ====================================================================
# SCHEDULED TASK
# ====================================================================

async def check_jobs_task(context: ContextTypes.DEFAULT_TYPE):
    """Iterates through all subscriptions and checks for new jobs."""
    subscriptions = db.get_all_subscriptions()
    
    # Group by (role, location) to avoid scraping the same thing multiple times
    # if multiple users want the same thing.
    unique_searches = {}
    for sub_id, chat_id, role, location in subscriptions:
        key = (role.lower(), location.lower())
        if key not in unique_searches:
            unique_searches[key] = {'role': role, 'location': location, 'subscribers': []}
        unique_searches[key]['subscribers'].append(chat_id)

    for key, data in unique_searches.items():
        role = data['role']
        location = data['location']
        subscribers = data['subscribers']
        
        # Scrape
        all_jobs = await asyncio.to_thread(scrape_and_filter, role, location, hours_old=24)
        
        if not all_jobs:
            continue

        # For each subscriber, filter jobs they haven't seen
        # Note: In this simple version, we deduplicate globally based on job hash.
        # If User A and User B both sub to "Python", and we find Job X,
        # we mark Job X as processed. If we mark it processed after sending to User A,
        # User B might miss it if we are not careful.
        # FIX: We should check if the job is processed *for this run*, but we only store global processed state.
        # For simplicity in this "Master Level" update, we will just send new jobs to all subscribers
        # and THEN mark them as processed.
        
        new_jobs_for_batch = []
        for job in all_jobs:
            job_hash = get_job_hash(job)
            if not db.is_job_processed(job_hash):
                new_jobs_for_batch.append(job)
        
        if new_jobs_for_batch:
            # Notify all subscribers
            for chat_id in subscribers:
                await send_job_batch(context, chat_id, new_jobs_for_batch, header=f"🚨 New **{role}** jobs in **{location}**:")
            
            # Mark as processed after notifying everyone
            for job in new_jobs_for_batch:
                db.mark_job_processed(get_job_hash(job))

# ====================================================================
# HEALTH CHECK SERVER (FOR RENDER)
# ====================================================================

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

# ====================================================================
# MAIN
# ====================================================================

def main():
    if not BOT_TOKEN:
        print("🚨 ERROR: TELEGRAM_BOT_TOKEN environment variable is not set!")
        return

    application = Application.builder().token(BOT_TOKEN).build()

    # Conversation Handler for /add
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("add", add_start)],
        states={
            AWAITING_ROLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_role)],
            AWAITING_LOCATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_location)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    # Add ConversationHandler FIRST to ensure it captures input during the flow
    application.add_handler(conv_handler)
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("search", search_command))
    application.add_handler(CommandHandler("list", list_subs))
    application.add_handler(CommandHandler("delete", delete_sub))

    # Debug Handler: Catch any text that wasn't handled by the conversation or commands
    async def debug_catch_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
        logger.info(f"UNHANDLED MESSAGE from {update.effective_user.id}: {update.message.text}")
        await update.message.reply_text(
            "⚠️ I received your message, but I wasn't expecting it.\n"
            "If you were trying to use `/add`, the session might have timed out or reset.\n"
            "Please try `/add` again."
        )

    async def debug_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
        import socket
        import os
        
        hostname = socket.gethostname()
        pid = os.getpid()
        
        msg = (
            f"🤖 **Bot Instance Info**\n"
            f"🏠 Host: `{hostname}`\n"
            f"🔢 PID: `{pid}`\n"
            f"🕒 Server Time: `{datetime.now()}`"
        )
        await update.message.reply_text(msg, parse_mode='Markdown')

    application.add_handler(CommandHandler("debug", debug_info))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, debug_catch_all))

    # Global Job Scheduler
    # We run one global job that checks ALL subscriptions
    if application.job_queue:
        application.job_queue.run_repeating(check_jobs_task, interval=3600, first=10)
    else:
        print("⚠️ JobQueue not available. Automatic monitoring will not work.")

    print("Bot is running...")
    application.run_polling()

if __name__ == "__main__":
    # Start health check server in a separate thread
    threading.Thread(target=start_health_server, daemon=True).start()
    main()