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
        job_list = []
        for _, row in jobs.iterrows():
            if pd.isna(row['title']) or pd.isna(row['job_url']):
                continue
            
            # AI Filtering
            # We pass the job and the original role/location query to the AI
            user_query = f"{role} in {location}"
            
            # Fetch user resume if available (Assuming we have a way to know WHICH user this job is for)
            # In the current architecture, scrape_and_filter is generic for a role/location.
            # However, check_jobs_task iterates through subscribers.
            # We need to pass the resume_text to analyze_job_relevance if we want personalization.
            # BUT, scrape_and_filter is called ONCE per role/location for ALL users.
            # This is a conflict: Personalization vs Efficiency.
            # For "Master Level", we should prioritize Personalization.
            # We will return ALL valid jobs here, and let the AI filter per-user in the loop?
            # OR, we pass a list of resumes?
            # Let's keep it simple: scrape_and_filter does generic relevance (Is this a Python job?).
            # Then, in check_jobs_task, we do a second pass for "Resume Match".
            
            # Actually, the user asked for "Resume Matcher".
            # Let's update analyze_job_relevance to accept an optional resume.
            # If called from scrape_and_filter (generic), it uses just the query.
            # We will add a NEW function `score_job_match(job, resume)` for the personalized check.
            
            if analyze_job_relevance(row, user_query):
                job_list.append(row)
            else:
                logger.info(f"Skipping irrelevant job: {row.get('title')}")
            
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

def analyze_job_relevance(job, user_query):
    """
    Uses Gemini to check if a job matches the user's query.
    Returns True (Relevant) or False (Not Relevant).
    """
    if not GEMINI_API_KEY:
        return True # Fail open if no key

    try:
        title = job.get('title', 'N/A')
        company = job.get('company_name', 'N/A')
        description = str(job.get('description', ''))  # Ensure string
        if not description or description.lower() == 'nan':
            description = "No description available"
        
        # Truncate description to save tokens and avoid limits
        description = description[:1000] 

        prompt = (
            f"You are a strict technical recruiter. "
            f"User is looking for: '{user_query}'. "
            f"Job Title: '{title}'. "
            f"Company: '{company}'. "
            f"Job Description Snippet: '{description}'. "
            f"Is this job a good match for the user's query? "
            f"Ignore 'Senior' or 'Lead' roles if the user didn't ask for them. "
            f"Ignore unrelated technologies. "
            f"Reply ONLY with 'YES' or 'NO'."
        )

        model = genai.GenerativeModel('gemini-1.5-flash-001')
        response = model.generate_content(prompt)
        
        answer = response.text.strip().upper()
        # logger.info(f"AI Decision for '{title}': {answer}")
        
        return "YES" in answer

    except Exception as e:
        logger.error(f"AI Error: {e}")
        return True # Fail open on error to not miss jobs

def score_job_match(job, resume_text):
    """
    Compares a job against a user's resume.
    Returns a score (0-100) and a short reason.
    """
    if not GEMINI_API_KEY or not resume_text:
        return 100, "No resume to match."

    try:
        title = job.get('title', 'N/A')
        description = str(job.get('description', '')) # Ensure string
        if not description or description.lower() == 'nan':
            description = "No description available"
            
        description = description[:1000]
        
        prompt = (
            f"You are a career coach. "
            f"Job Title: '{title}'. "
            f"Job Description: '{description}'. "
            f"Candidate Resume: '{resume_text[:2000]}'. " # Truncate resume too
            f"Rate the match between the candidate and this job on a scale of 0-100. "
            f"Provide a 1-sentence reason. "
            f"Format: SCORE | REASON"
        )
        
        model = genai.GenerativeModel('gemini-1.5-flash-001')
        response = model.generate_content(prompt)
        text = response.text.strip()
        
        if "|" in text:
            score_str, reason = text.split("|", 1)
            return int(score_str.strip()), reason.strip()
        else:
            return 50, "AI could not score."
            
    except Exception as e:
        logger.error(f"AI Scoring Error: {e}")
        return 100, "Error in scoring."

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
                for job in new_jobs_for_batch:
                    if resume_text:
                        score, reason = score_job_match(job, resume_text)
                        # Filter out low scores? Let's keep them but show score.
                        if score >= 50: # Only show good matches
                            job['match_score'] = score
                            job['match_reason'] = reason
                            personalized_jobs.append(job)
                    else:
                        personalized_jobs.append(job)

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
    main()
