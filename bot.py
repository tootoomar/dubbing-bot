import os, re, json, time, asyncio, subprocess, requests, threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters
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

def clean_non_burmese(text):
    text = re.sub(r'[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]+', '', str(text))
    return re.sub(r'\s+', ' ', text).strip()

def download_video_all(url, output_path):
    headers = {
        'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1'
    }
    
    if "douyin.com" in url:
        try:
            r = requests.get(url, headers=headers, allow_redirects=True, timeout=10)
            item_ids = re.findall(r'/video/(\d+)', r.url) or re.findall(r'item_ids=(\d+)', r.url)
            if item_ids:
                api_url = f"https://www.iesdouyin.com/web/api/v2/aweme/iteminfo/?item_ids={item_ids[0]}"
                res = requests.get(api_url, headers=headers, timeout=10).json()
                video_url = res["item_list"][0]["video"]["play_addr"]["url_list"][0].replace("playwm", "play")
                vr = requests.get(video_url, headers=headers, stream=True, timeout=20)
                with open(output_path, 'wb') as f:
                    for chunk in vr.iter_content(chunk_size=1024*1024):
                        if chunk: f.write(chunk)
                if os.path.exists(output_path) and os.path.getsize(output_path) > 1024:
                    return output_path
        except Exception:
            pass

    ydl_opts = {
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'outtmpl': output_path,
        'quiet': True,
        'no_warnings': True,
        'user_agent': headers['User-Agent']
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])
    
    if not os.path.exists(output_path):
        raise Exception("Video download မရရှိပါ")
    return output_path

def get_video_duration(video_path):
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", video_path]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return float(res.stdout.strip())

def extract_audio(video_path, audio_path):
    subprocess.run(["ffmpeg", "-y", "-i", video_path, "-vn", "-acodec", "libmp3lame", "-q:a", "4", audio_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not os.path.exists(audio_path):
        raise Exception("Audio extraction failed")
    return True

def analyze_with_gemini(audio_path):
    url = f"https://generativelanguage.googleapis.com/upload/v1beta/files?key={GEMINI_API_KEY}"
    size = os.path.getsize(audio_path)
    headers = {
        "X-Goog-Upload-Protocol": "resumable",
        "X-Goog-Upload-Command": "start",
        "X-Goog-Upload-Header-Content-Length": str(size),
        "X-Goog-Upload-Header-Content-Type": "audio/mp3",
        "Content-Type": "application/json"
    }
    init_res = requests.post(url, headers=headers, json={"file": {"display_name": os.path.basename(audio_path)}}, timeout=30)
    upload_url = init_res.headers.get("X-Goog-Upload-URL")
    
    with open(audio_path, "rb") as f:
        upload_res = requests.put(upload_url, headers={"Content-Length": str(size), "X-Goog-Upload-Offset": "0", "X-Goog-Upload-Command": "upload, finalize"}, data=f, timeout=120)
    
    file_info = upload_res.json()
    file_uri = file_info.get("file", {}).get("uri")
    
    if not file_uri:
        raise Exception("Gemini Audio upload failed")
        
    gen_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    prompt = (
        "Transcribe and translate dialogue into natural spoken Burmese. "
        "Return STRICT JSON array with keys: 'start' (float seconds), 'end' (float seconds), 'text' (Burmese text). "
        "Remove all Chinese/English letters."
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}, {"file_data": {"mime_type": "audio/mp3", "file_uri": file_uri}}]}],
        "generationConfig": {"response_mime_type": "application/json"}
    }
    resp = requests.post(gen_url, json=payload, timeout=120)
    data = resp.json()
    raw_json = data["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(re.sub(r'```json\s*|\s*```', '', raw_json.strip()))

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
        raise Exception("အသံဖိုင် ဖန်တီး၍မရပါ")
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

async def process_video_pipeline(update: Update, context: ContextTypes.DEFAULT_TYPE, video_url):
    chat_id = update.effective_chat.id
    msg = await update.message.reply_text(build_ui(0, 10), parse_mode="HTML")
    prefix = f"job_{chat_id}_{int(time.time())}"
    raw_video = f"{prefix}_raw.mp4"
    audio_file = f"{prefix}_raw.mp3"
    final_video = f"{prefix}_final.mp4"

    try:
        download_video_all(video_url, raw_video)
        dur = get_video_duration(raw_video)
        await msg.edit_text(build_ui(1, 30), parse_mode="HTML")
        
        extract_audio(raw_video, audio_file)
        await msg.edit_text(build_ui(2, 50), parse_mode="HTML")
        
        subs = analyze_with_gemini(audio_file)
        await msg.edit_text(build_ui(3, 70), parse_mode="HTML")
        
        bm_audio = await build_burmese_track(subs, dur, prefix)
        await msg.edit_text(build_ui(4, 90), parse_mode="HTML")
        
        merge_audio_video(raw_video, bm_audio, final_video)
        
        with open(final_video, "rb") as f:
            await context.bot.send_video(chat_id=chat_id, video=f, caption="✅ မြန်မာအသံ ထည့်သွင်းပြီးပါပြီ။")
        await msg.delete()
    except Exception as e:
        await msg.edit_text(build_ui(0, 0, error=str(e)[:150]), parse_mode="HTML")
    finally:
        for f in os.listdir("."):
            if f.startswith(prefix):
                try: os.remove(f)
                except: pass

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("မင်္ဂလာပါ! TikTok / Douyin / YouTube Link ပို့ပေးပါ။")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    links = re.findall(r'https?://[^\s]+', update.message.text)
    if links:
        await process_video_pipeline(update, context, links[0])

def main():
    threading.Thread(target=start_health_server, daemon=True).start()
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    print("Bot is ready...")
    app.run_polling()

if __name__ == "__main__":
    main()
