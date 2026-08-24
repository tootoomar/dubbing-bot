import os, re, json, time, asyncio, subprocess, requests, threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, CallbackQueryHandler, filters
import edge_tts, yt_dlp

TELEGRAM_BOT_TOKEN = "7752878545:AAFMBSnhvLEHh7Z9jdHkGoZyDTsaG-gbZj8"
GEMINI_API_KEY = "AQ.Ab8RN6J3SI7Hcw4jHJQU4-b4IvkYYprAGPp6Wcn0PVvYvkk2BQ"

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is Running 24/7!")

def start_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

def download_video(url, out):
    try:
        ydl_opts = {'format': 'best', 'outtmpl': out, 'quiet': True}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl: ydl.download([url])
        return out if os.path.exists(out) else None
    except: return None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("မင်္ဂလာပါ! Video Link သို့မဟုတ် File ပို့ပေးပါ။")

async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    msg = await update.message.reply_text("⏳ ဒေါင်းလုဒ်ဆွဲနေပါပြီ...")
    out = download_video(url, f"vid_{update.effective_chat.id}.mp4")
    if out: 
        await msg.edit_text("✅ Video ရပါပြီ။ ဆက်လက်ဆောင်ရွက်နေပါသည်...")
    else: 
        await msg.edit_text("❌ ဒေါင်းလုဒ်မအောင်မြင်ပါ။")

def main():
    threading.Thread(target=start_health_server, daemon=True).start()
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    print("Bot စတင် အလုပ်လုပ်နေပါပြီ...")
    app.run_polling()

if __name__ == "__main__":
    main()
