import os, re, json, time, asyncio, subprocess, requests
from datetime import timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, CallbackQueryHandler, filters
import edge_tts, yt_dlp

TELEGRAM_BOT_TOKEN = "7752878545:AAFMBSnhvLEHh7Z9jdHkGoZyDTsaG-gbZj8"
GEMINI_API_KEY = "AQ.Ab8RN6J3SI7Hcw4jHJQU4-b4IvkYYprAGPp6Wcn0PVvYvkk2BQ"
CANCELLED_TASKS = set()
TELEGRAM_MAX_UPLOAD_BYTES = 47 * 1024 * 1024
TELEGRAM_TARGET_BYTES = 45 * 1024 * 1024

def build_ui(step, pct, err=None):
    return f"🎬 <b>AI Burmese Dubbing Bot</b>\n\nProgress: {pct}%\nStep {step}/5 Done."

def get_stop_btn(chat_id):
    return InlineKeyboardMarkup([[InlineKeyboardButton("🛑 Stop", callback_data=f"stop_{chat_id}")]])

def clean_txt(t):
    return re.sub(r'[\u4e00-\u9fff]+', '', str(t)).strip()

def download_video(url, out):
    try:
        ydl_opts = {'format': 'best', 'outtmpl': out, 'quiet': True}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl: ydl.download([url])
        return out if os.path.exists(out) else None
    except: return None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("မင်္ဂလာပါ! Video Link သို့မဟုတ် File ပို့ပါ။")

async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    msg = await update.message.reply_text("⏳ ဒေါင်းလုဒ်ဆွဲနေပါပြီ...")
    out = download_video(url, f"vid_{update.effective_chat.id}.mp4")
    if out: await msg.edit_text("✅ Video ရပါပြီ။ ဆက်လက်ဆောင်ရွက်နေပါသည်...")
    else: await msg.edit_text("❌ ဒေါင်းလုဒ်မအောင်မြင်ပါ။")

def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    print("Bot Started...")
    app.run_polling()

if __name__ == "__main__":
    main()
