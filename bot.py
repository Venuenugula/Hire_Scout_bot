import os
import logging
import logging
import psycopg2
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
from pypdf import PdfReader
import io

# ====================================================================
# CONFIGURATION
# ====================================================================

# 🚨 REPLACE WITH YOUR ACTUAL TOKEN
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
# Use mounted disk path if available (for Render persistence), else local file
# Database URL from Render environment
DATABASE_URL = os.getenv("DATABASE_URL")

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
    def __init__(self, db_url):
        self.db_url = db_url
        self.init_db()

    def get_connection(self):
        return psycopg2.connect(self.db_url)

    def init_db(self):
        if not self.db_url:
            logger.warning("DATABASE_URL not set. Database features will fail.")
            return

        try:
            with self.get_connection() as conn:
                with conn.cursor() as cursor:
                    # Table for user subscriptions
                    cursor.execute("""
                        CREATE TABLE IF NOT EXISTS subscriptions (
                            id SERIAL PRIMARY KEY,
                            chat_id BIGINT NOT NULL,
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
                    # Table for users (Resume Storage)
                    cursor.execute("""
                        CREATE TABLE IF NOT EXISTS users (
                            chat_id BIGINT PRIMARY KEY,
                            resume_text TEXT,
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        )
                    """)
                conn.commit()
        except Exception as e:
            logger.error(f"Database init error: {e}")

    def add_subscription(self, chat_id, role, location):
        try:
            with self.get_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "INSERT INTO subscriptions (chat_id, role, location) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                        (chat_id, role, location)
                    )
                    # Check if a row was actually inserted (psycopg2 returns rowcount)
                    # But ON CONFLICT DO NOTHING returns rowcount 0 if conflict.
                    # We need to know if it existed. 
                    # Actually, for the user UX, if it exists, we return False.
                    # rowcount is reliable here.
                    conn.commit()
                    return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Add sub error: {e}")
            return False

    def remove_subscription(self, sub_id, chat_id):
        try:
            with self.get_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("DELETE FROM subscriptions WHERE id = %s AND chat_id = %s", (sub_id, chat_id))
                    conn.commit()
                    return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Remove sub error: {e}")
            return False

    def get_user_subscriptions(self, chat_id):
        try:
            with self.get_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("SELECT id, role, location FROM subscriptions WHERE chat_id = %s", (chat_id,))
                    return cursor.fetchall()
        except Exception as e:
            logger.error(f"Get user subs error: {e}")
            return []

    def get_all_subscriptions(self):
        try:
            with self.get_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("SELECT id, chat_id, role, location FROM subscriptions")
                    return cursor.fetchall()
        except Exception as e:
            logger.error(f"Get all subs error: {e}")
            return []

    def is_job_processed(self, job_hash):
        try:
            with self.get_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("SELECT 1 FROM processed_jobs WHERE job_hash = %s", (job_hash,))
                    return cursor.fetchone() is not None
        except Exception as e:
            logger.error(f"Check processed error: {e}")
            return False

    def mark_job_processed(self, job_hash):
        try:
            with self.get_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("INSERT INTO processed_jobs (job_hash) VALUES (%s) ON CONFLICT DO NOTHING", (job_hash,))
                conn.commit()
        except Exception as e:
            logger.error(f"Mark processed error: {e}")

    def add_user_resume(self, chat_id, resume_text):
        try:
            with self.get_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO users (chat_id, resume_text) 
                        VALUES (%s, %s) 
                        ON CONFLICT (chat_id) 
                        DO UPDATE SET resume_text = EXCLUDED.resume_text
                        """,
                        (chat_id, resume_text)
                    )
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"Add resume error: {e}")
            return False

    def get_user_resume(self, chat_id):
        try:
            with self.get_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("SELECT resume_text FROM users WHERE chat_id = %s", (chat_id,))
                    result = cursor.fetchone()
                    return result[0] if result else None
        except Exception as e:
            logger.error(f"Get resume error: {e}")
            return None

# Initialize with DATABASE_URL
db = Database(DATABASE_URL)

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
        raw_jobs = []
        for _, row in jobs.iterrows():
            if pd.isna(row['title']) or pd.isna(row['job_url']):
                continue
            raw_jobs.append(row.to_dict())

        # AI Filtering in Batches
        user_query = f"{role} in {location}"
        job_list = []
        
        chunk_size = 5
        for i in range(0, len(raw_jobs), chunk_size):
            chunk = raw_jobs[i:i + chunk_size]
            relevant_indices = analyze_jobs_batch(chunk, user_query)
            
            for j, job in enumerate(chunk):
                if j in relevant_indices:
                    job_list.append(job)
                else:
                    logger.info(f"Skipping irrelevant job: {job.get('title')}")
            
        return job_list

    except Exception as e:
        logger.error(f"Error scraping jobs: {e}")
        return []

# ====================================================================
# AI FILTERING LAYER (Gemini)
# ====================================================================
import google.generativeai as genai

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
else:
    logger.warning("⚠️ GEMINI_API_KEY not found. AI filtering will be disabled.")

import time
import json

def call_gemini_with_backoff(prompt, model_names, retries=3):
    """
    Calls Gemini with exponential backoff for rate limits.
    Tries multiple models if one fails.
    """
    delay = 2
    for attempt in range(retries):
        for model_name in model_names:
            try:
                model = genai.GenerativeModel(model_name)
                response = model.generate_content(prompt)
                return response.text
            except Exception as e:
                if "429" in str(e) or "Resource has been exhausted" in str(e):
                    logger.warning(f"Rate limit hit on {model_name}. Waiting {delay}s...")
                    time.sleep(delay)
                    delay *= 2 # Exponential backoff
                    break # Break inner loop to retry with backoff
                else:
                    logger.warning(f"Model {model_name} failed: {e}")
                    continue # Try next model immediately
        
    raise Exception("All models and retries failed.")

def analyze_jobs_batch(jobs, user_query):
    """
    Analyzes a batch of jobs for relevance using Gemini.
    Returns a set of indices of relevant jobs.
    """
    if not GEMINI_API_KEY:
        return set(range(len(jobs))) # Fail open

    # Prepare batch prompt
    job_descriptions = ""
    for i, job in enumerate(jobs):
        title = job.get('title', 'N/A')
        company = job.get('company_name', 'N/A')
        desc = str(job.get('description', ''))[:500] # Truncate heavily for batch
        if not desc or desc.lower() == 'nan':
            desc = "No description"
        
        job_descriptions += f"JOB_ID {i}:\nTitle: {title}\nCompany: {company}\nSnippet: {desc}\n\n"

    prompt = (
        f"You are a technical recruiter. User is looking for: '{user_query}'.\n"
        f"Below are {len(jobs)} job summaries. Identify which ones are RELEVANT matches.\n"
        f"Ignore 'Senior'/'Lead' if not asked. Ignore unrelated tech.\n\n"
        f"{job_descriptions}\n"
        f"Return ONLY a JSON list of integers representing the JOB_IDs of relevant jobs.\n"
        f"Example: [0, 2, 5]\n"
        f"If none are relevant, return []"
    )

    model_names = ['gemini-2.0-flash', 'gemini-flash-latest', 'gemini-pro-latest', 'gemini-1.5-flash']

    try:
        response_text = call_gemini_with_backoff(prompt, model_names)
        # Clean response to ensure it's valid JSON
        response_text = response_text.replace("```json", "").replace("```", "").strip()
        relevant_indices = set(json.loads(response_text))
        return relevant_indices
    except Exception as e:
        logger.error(f"Batch analysis failed: {e}")
        return set(range(len(jobs))) # Fail open

def score_jobs_batch(jobs, resume_text):
    """
    Scores a batch of jobs against a resume.
    Returns a dict: {job_index: {'score': int, 'reason': str}}
    """
    if not GEMINI_API_KEY or not resume_text:
        return {}

    # Prepare batch prompt
    job_descriptions = ""
    for i, job in enumerate(jobs):
        title = job.get('title', 'N/A')
        desc = str(job.get('description', ''))[:500]
        if not desc or desc.lower() == 'nan':
            desc = "No description"
        job_descriptions += f"JOB_ID {i}:\nTitle: {title}\nSnippet: {desc}\n\n"

    prompt = (
        f"You are a career coach. Rate the match between the candidate and these jobs (0-100).\n"
        f"Candidate Resume: '{resume_text[:2000]}'\n\n"
        f"{job_descriptions}\n"
        f"Return ONLY a JSON object where keys are JOB_IDs and values are objects with 'score' and 'reason'.\n"
        f"Example: {{ \"0\": {{\"score\": 85, \"reason\": \"Good match\"}}, \"2\": {{\"score\": 40, \"reason\": \"Missing skills\"}} }}\n"
    )

    model_names = ['gemini-2.0-flash', 'gemini-flash-latest', 'gemini-pro-latest', 'gemini-1.5-flash']

    try:
        response_text = call_gemini_with_backoff(prompt, model_names)
        response_text = response_text.replace("```json", "").replace("```", "").strip()
        scores = json.loads(response_text)
        # Convert keys to int
        return {int(k): v for k, v in scores.items()}
    except Exception as e:
        logger.error(f"Batch scoring failed: {e}")
        return {}

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

async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Simple health check command."""
    await update.message.reply_text("Pong! 🏓 I am alive and running.")

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

# --- Resume Upload Handler ---

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    document = update.message.document
    
    # Check if PDF
    if document.mime_type != 'application/pdf':
        await update.message.reply_text("⚠️ Please upload a **PDF** file.")
        return

    try:
        file = await document.get_file()
        file_bytes = await file.download_as_bytearray()
        
        # Extract text
        pdf_reader = PdfReader(io.BytesIO(file_bytes))
        text = ""
        for page in pdf_reader.pages:
            text += page.extract_text() + "\n"
            
        if not text.strip():
            await update.message.reply_text("⚠️ Could not read text from this PDF. Is it scanned?")
            return
            
        # Save to DB
        chat_id = update.effective_chat.id
        if db.add_user_resume(chat_id, text):
            await update.message.reply_text(
                "📄 **Resume Saved!**\n\n"
                "I will now use your resume to score job matches.\n"
                "You'll see a 'Match Score' on new job alerts!",
                parse_mode='Markdown'
            )
        else:
            await update.message.reply_text("⚠️ Error saving resume.")
            
    except Exception as e:
        logger.error(f"Resume upload error: {e}")
        await update.message.reply_text("⚠️ An error occurred processing your file.")

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
            
            message_text += f"💼 {title}\n"
            message_text += f"🏢 {company}\n"
            message_text += f"📍 {loc}\n"
            message_text += f"🔗 {url}\n"
            
            if 'match_score' in job:
                message_text += f"⭐ **Match: {job['match_score']}%** - {job['match_reason']}\n"
                
            message_text += "-------------------\n"
        
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
                # Personalize with Resume Score
                resume_text = db.get_user_resume(chat_id)
                
                personalized_jobs = []
                
                if resume_text:
                    # Batch Score
                    chunk_size = 5
                    for i in range(0, len(new_jobs_for_batch), chunk_size):
                        chunk = new_jobs_for_batch[i:i + chunk_size]
                        scores = score_jobs_batch(chunk, resume_text)
                        
                        for j, job in enumerate(chunk):
                            if j in scores:
                                score_data = scores[j]
                                if score_data['score'] >= 50:
                                    # Create a copy to not affect other subscribers
                                    job_copy = job.copy()
                                    job_copy['match_score'] = score_data['score']
                                    job_copy['match_reason'] = score_data['reason']
                                    personalized_jobs.append(job_copy)
                            else:
                                # AI failed or skipped, include without score?
                                # Let's include it to be safe, or skip?
                                # If AI fails, we probably want to show it anyway.
                                personalized_jobs.append(job)
                else:
                    personalized_jobs = new_jobs_for_batch

                if personalized_jobs:
                    await send_job_batch(context, chat_id, personalized_jobs, header=f"🚨 New **{role}** jobs in **{location}**:")
            
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

import time
import httpx

def keep_alive():
    """Pings the bot's own URL every 10 minutes to prevent Render sleep."""
    url = os.getenv("RENDER_EXTERNAL_URL")
    if not url:
        logger.warning("RENDER_EXTERNAL_URL not set. Keep-alive disabled.")
        return

    logger.info(f"Starting keep-alive for {url}")
    while True:
        time.sleep(600) # 10 minutes
        try:
            response = httpx.get(url)
            logger.info(f"Keep-alive ping: {response.status_code}")
        except Exception as e:
            logger.error(f"Keep-alive failed: {e}")

# ====================================================================
# MAIN
# ====================================================================

def main():
    if not BOT_TOKEN:
        print("🚨 ERROR: TELEGRAM_BOT_TOKEN environment variable is not set!")
        return

    # List available Gemini models for debugging
    if GEMINI_API_KEY:
        try:
            logger.info("🔍 Checking available Gemini models...")
            for m in genai.list_models():
                if 'generateContent' in m.supported_generation_methods:
                    logger.info(f"   - {m.name}")
        except Exception as e:
            logger.error(f"⚠️ Error listing models: {e}")

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
    application.add_handler(CommandHandler("ping", ping))
    application.add_handler(CommandHandler("search", search_command))
    application.add_handler(CommandHandler("list", list_subs))
    application.add_handler(CommandHandler("delete", delete_sub))
    
    # Resume Handler
    application.add_handler(MessageHandler(filters.Document.PDF, handle_document))

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
    
    # Start keep-alive pinger in a separate thread
    threading.Thread(target=keep_alive, daemon=True).start()
    
    main()
