import os, re, json, time, asyncio, subprocess, requests, threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, CallbackQueryHandler, filters
import edge_tts, yt_dlp

TELEGRAM_BOT_TOKEN = "7752878545:AAFMBSnhvLEHh7Z9jdHkGoZyDTsaG-gbZj8"
GEMINI_API_KEY = "AQ.Ab8RN6J3SI7Hcw4jHJQU4-b4IvkYYprAGPp6Wcn0PVvYvkk2BQ"

CANCELLED_TASKS = set()
TELEGRAM_MAX_UPLOAD_BYTES = 47 * 1024 * 1024
TELEGRAM_TARGET_BYTES = 45 * 1024 * 1024

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is Running 24/7!")

def start_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

def build_ui(step_idx, pct, error=None):
    steps = [
        "1. Video ရယူနေသည်",
        "2. Audio ခွဲထုတ်နေသည်",
        "3. AI နားထောင် & ဘာသာပြန်နေသည်",
        "4. မြန်မာ AI အသံဖန်တီးနေသည်",
        "5. အသံသွင်း & ပေါင်းစပ်နေသည်"
    ]
    lines = ["🎬 <b>AI Burmese Dubbing Bot</b>\n"]
    for i, s in enumerate(steps):
        if error and i == step_idx:
            lines.append(f"❌ <b>{s} (မအောင်မြင်ပါ)</b>")
        elif i < step_idx:
            lines.append(f"✅ <b>{s}</b>")
        elif i == step_idx:
            lines.append(f"⏳ <b>{s}</b> ({pct}%)")
        else:
            lines.append(f"⚪ {s}")
    if error:
        lines.append(f"\n⚠️ <i>{error}</i>")
    return "\n".join(lines)

def get_stop_markup(chat_id):
    return InlineKeyboardMarkup([[InlineKeyboardButton("🛑 Stop", callback_data=f"stop_{chat_id}")]])

def clean_non_burmese(text):
    text = re.sub(r'[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]+', '', str(text))
    return re.sub(r'\s+', ' ', text).strip()

def download_video(url, output_path):
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    real_url = url
    try:
        res = requests.get(url, headers=headers, allow_redirects=True, timeout=10)
        real_url = res.url
    except Exception:
        pass

    ydl_opts = {
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'outtmpl': output_path,
        'quiet': True,
        'no_warnings': True,
        'http_headers': headers
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([real_url])
    return output_path if os.path.exists(output_path) else None

def get_video_duration(video_path):
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", video_path]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return float(res.stdout.strip())

def extract_audio(video_path, audio_path):
    subprocess.run(["ffmpeg", "-y", "-i", video_path, "-vn", "-acodec", "libmp3lame", "-q:a", "4", audio_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return os.path.exists(audio_path)

def upload_to_gemini(path, mime_type="audio/mp3"):
    url = f"https://generativelanguage.googleapis.com/upload/v1beta/files?key={GEMINI_API_KEY}"
    size = os.path.getsize(path)
    h = {"X-Goog-Upload-Protocol": "resumable", "X-Goog-Upload-Command": "start", "X-Goog-Upload-Header-Content-Length": str(size), "X-Goog-Upload-Header-Content-Type": mime_type, "Content-Type": "application/json"}
    init = requests.post(url, headers=h, json={"file": {"display_name": os.path.basename(path)}}, timeout=30)
    upload_url = init.headers.get("X-Goog-Upload-URL")
    with open(path, "rb") as f:
        resp = requests.put(upload_url, headers={"Content-Length": str(size), "X-Goog-Upload-Offset": "0", "X-Goog-Upload-Command": "upload, finalize"}, data=f, timeout=120)
    return resp.json()["file"]["uri"]

def analyze_and_translate(audio_uri):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    prompt = (
        "Listen to the audio, transcribe dialogue and translate into natural spoken Burmese. "
        "Return strictly a JSON array of objects with keys: 'start' (float seconds), 'end' (float seconds), 'text' (Burmese text). "
        "Remove all Chinese characters."
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}, {"file_data": {"mime_type": "audio/mp3", "file_uri": audio_uri}}]}],
        "generationConfig": {"response_mime_type": "application/json"}
    }
    resp = requests.post(url, json=payload, timeout=120)
    txt = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(re.sub(r'```json\s*|\s*```', '', txt.strip()))

async def generate_speech(text, out_file):
    tts = edge_tts.Communicate(clean_non_burmese(text), "my-MM-ThihaNeural")
    await tts.save(out_file)

async def build_burmese_track(subs, total_dur, prefix):
    inputs, filter_complex = [], []
    valid = 0
    for i, s in enumerate(subs):
        t_file = f"{prefix}_t_{i}.mp3"
        await generate_speech(s["text"], t_file)
        if os.path.exists(t_file) and os.path.getsize(t_file) > 100:
            inputs.extend(["-i", t_file])
            delay_ms = int(float(s["start"]) * 1000)
            filter_complex.append(f"[{valid}]adelay={delay_ms}|{delay_ms}[a{valid}];")
            valid += 1
    if valid == 0:
        return None
    mix_src = "".join([f"[a{k}]" for k in range(valid)])
    filter_complex.append(f"{mix_src}amix=inputs={valid}:dropout_transition=0:normalize=0[out]")
    out_audio = f"{prefix}_burmese.mp3"
    subprocess.run(["ffmpeg", "-y"] + inputs + ["-filter_complex", "".join(filter_complex), "-map", "[out]", "-t", str(total_dur), out_audio], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return out_audio

def merge_audio_video(video, burmese_audio, output):
    cmd = [
        "ffmpeg", "-y", "-i", video, "-i", burmese_audio,
        "-filter_complex", "[0:a]volume=0.15[bg];[1:a]volume=1.8[fg];[bg][fg]amix=inputs=2:duration=first[aout]",
        "-map", "0:v", "-map", "[aout]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", output
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return os.path.exists(output)

async def process_video_pipeline(update: Update, context: ContextTypes.DEFAULT_TYPE, video_source, is_file=False):
    chat_id = update.effective_chat.id
    CANCELLED_TASKS.discard(chat_id)
    msg = await update.message.reply_text(build_ui(0, 10), parse_mode="HTML", reply_markup=get_stop_markup(chat_id))
    prefix = f"job_{chat_id}_{int(time.time())}"
    raw_video = f"{prefix}_raw.mp4"
    audio_file = f"{prefix}_raw.mp3"
    final_video = f"{prefix}_final.mp4"

    try:
        if not is_file:
            download_video(video_source, raw_video)
        else:
            tf = await context.bot.get_file(video_source)
            await tf.download_to_drive(raw_video)
            
        dur = get_video_duration(raw_video)
        await msg.edit_text(build_ui(1, 30), parse_mode="HTML", reply_markup=get_stop_markup(chat_id))
        
        extract_audio(raw_video, audio_file)
        await msg.edit_text(build_ui(2, 50), parse_mode="HTML", reply_markup=get_stop_markup(chat_id))
        
        uri = upload_to_gemini(audio_file)
        subs = analyze_and_translate(uri)
        await msg.edit_text(build_ui(3, 70), parse_mode="HTML", reply_markup=get_stop_markup(chat_id))
        
        bm_audio = await build_burmese_track(subs, dur, prefix)
        await msg.edit_text(build_ui(4, 90), parse_mode="HTML", reply_markup=get_stop_markup(chat_id))
        
        merge_audio_video(raw_video, bm_audio, final_video)
        
        with open(final_video, "rb") as f:
            await context.bot.send_video(chat_id=chat_id, video=f, caption="✅ မြန်မာအသံထည့်သွင်းပြီးပါပြီ။")
        await msg.delete()
    except Exception as e:
        await msg.edit_text(build_ui(0, 0, error=str(e)[:150]), parse_mode="HTML")
    finally:
        for f in os.listdir("."):
            if f.startswith(prefix):
                try: os.remove(f)
                except: pass

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("မင်္ဂလာပါ! Video Link သို့မဟုတ် File ပို့ပေးပါ။")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    links = re.findall(r'https?://[^\s]+', update.message.text)
    if links:
        await process_video_pipeline(update, context, links[0], is_file=False)

async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    v = update.message.video or update.message.document
    if v:
        await process_video_pipeline(update, context, v.file_id, is_file=True)

def main():
    threading.Thread(target=start_health_server, daemon=True).start()
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video))
    print("Full Dubbing Bot စတင် အလုပ်လုပ်နေပါပြီ...")
    app.run_polling()

if __name__ == "__main__":
    main()
