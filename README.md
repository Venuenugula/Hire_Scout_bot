# 🤖 Hire Scout Bot - AI-Powered Job Recruiter

**Hire Scout Bot** is an intelligent Telegram bot that automates your job search. It scrapes job listings from major platforms (LinkedIn, Indeed, ZipRecruiter), filters them using **Google Gemini AI** based on your preferences, and even scores jobs against your uploaded resume!

![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)
![Telegram](https://img.shields.io/badge/Telegram-Bot-blue.svg)
![Gemini AI](https://img.shields.io/badge/AI-Gemini_1.5-orange.svg)
![PostgreSQL](https://img.shields.io/badge/Database-PostgreSQL-336791.svg)
![Render](https://img.shields.io/badge/Deploy-Render-black.svg)

## ✨ Key Features

*   **🔍 Instant Search**: Search for jobs on demand using `/search <role> in <location>`.
*   **🔔 Live Monitoring**: Subscribe to job alerts. The bot checks for new jobs every hour.
*   **🧠 AI Filtering**: Uses **Gemini AI** to analyze job descriptions and filter out irrelevant listings (e.g., ignoring "Senior" roles if you want "Junior").
*   **📄 Resume Matcher**: Upload your PDF resume. The bot scores every new job (0-100%) based on how well it matches your profile!
*   **⚡ Smart Batching**: Optimized AI processing to handle high volumes without hitting API rate limits.
*   **☁️ Cloud Ready**: Designed for easy deployment on Render with persistent PostgreSQL storage.

## 🛠️ Tech Stack

*   **Language**: Python 3.12
*   **Framework**: `python-telegram-bot` (Async)
*   **Scraper**: `python-jobspy` (LinkedIn, Indeed, ZipRecruiter)
*   **AI Engine**: Google Gemini API (`google-generativeai`)
*   **Database**: PostgreSQL (`psycopg2`)
*   **PDF Parsing**: `pypdf`
*   **Scheduler**: `APScheduler`

## 🚀 Setup & Installation

### Prerequisites
*   Python 3.9+
*   A Telegram Bot Token (from [@BotFather](https://t.me/BotFather))
*   A Google Gemini API Key (from [Google AI Studio](https://aistudio.google.com/))
*   PostgreSQL Database (Local or Cloud)

### Local Development

1.  **Clone the repository**:
    ```bash
    git clone https://github.com/Venuenugula/Hire_Scout_bot.git
    cd Hire_Scout_bot
    ```

2.  **Install dependencies**:
    ```bash
    pip install -r requirements.txt
    ```

3.  **Set up environment variables**:
    Create a `.env` file (or export variables):
    ```bash
    export TELEGRAM_BOT_TOKEN="your_bot_token"
    export GEMINI_API_KEY="your_gemini_key"
    export DATABASE_URL="postgresql://user:pass@localhost:5432/dbname"
    ```

4.  **Run the bot**:
    ```bash
    python bot.py
    ```

## ☁️ Deployment (Render)

This project is configured for **Render**.

1.  **New Web Service**: Connect your GitHub repo.
2.  **Runtime**: Python 3.
3.  **Build Command**: `pip install -r requirements.txt`
4.  **Start Command**: `python bot.py`
5.  **Environment Variables**: Add `TELEGRAM_BOT_TOKEN`, `GEMINI_API_KEY`, and `DATABASE_URL` (from a Render PostgreSQL instance).

## 🤖 Usage

| Command | Description |
| :--- | :--- |
| `/start` | Start the bot and see the welcome message. |
| `/search <role> in <loc>` | Instant search (e.g., `/search Python in Remote`). |
| `/add` | Subscribe to a new job alert (Interactive). |
| `/list` | View your active subscriptions. |
| `/delete <id>` | Delete a subscription. |
| `/ping` | Check if the bot is alive. |
| **Upload PDF** | Send a PDF file to save your resume for AI scoring. |

## 🛡️ License

This project is open-source and available under the MIT License.

---
*Built with ❤️ by Venu Enugula*
