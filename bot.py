import os
import re
import json
import time
import asyncio
import subprocess
import requests
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    CallbackQueryHandler,
    filters
)

import edge_tts
import yt_dlp

# ============================================================
# API KEYS
# ============================================================
TELEGRAM_BOT_TOKEN = "7752878545:AAFMBSnhvLEHh7Z9jdHkGoZyDTsaG-gbZj8"
GEMINI_API_KEY = "AQ.Ab8RN6J3SI7Hcw4jHJQU4-b4IvkYYprAGPp6Wcn0PVvYvkk2BQ"

CANCELLED_TASKS = set()
EN_TO_MY_DIGITS = str.maketrans("0123456789", "၀၁၂၃၄၅၆၇၈၉")

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

def convert_digits_to_burmese(text: str) -> str:
    return text.translate(EN_TO_MY_DIGITS)

def create_progress_bar(percent: int) -> str:
    percent = max(0, min(100, int(percent)))
    filled_blocks = int(percent / 10)
    empty_blocks = 10 - filled_blocks
    bar = "█" * filled_blocks + "▒" * empty_blocks
    return f"{bar} {percent}%"

def build_status_ui_html(step: int, percent: int, error_text: str = None) -> str:
    steps_info = [
        ("Video Downloaded", 20),
        ("Extracting Audio Context (Gemini Processing)", 35),
        ("Complete Video Dialogue Translation", 55),
        ("1.30x Speech Generation & Precision Stretch Sync", 75),
        ("Final High-Quality Multiplexing", 95)
    ]

    text = "🎬 <b>AI Video Complete Burmese Dubbing (100% Synced)</b>\n\n"
    text += f"<code>{create_progress_bar(percent)}</code>\n\n"

    for idx, (name, _) in enumerate(steps_info, 1):
        if error_text and step == idx:
            text += f"❌ <b>{name}</b> (မအောင်မြင်ပါ)\n"
        elif step > idx:
            text += f"✅ <b>{name}</b> (Confirmed)\n"
        elif step == idx:
            text += f"⏳ <b>{name}</b>...\n"
        else:
            text += f"⚪ {name}\n"

    if error_text:
        safe_error = str(error_text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        text += f"\n❌ <b>အမှားဖြစ်ပေါ်ပါသည်:</b> {safe_error}"

    return text

def get_stop_button(chat_id: int) -> InlineKeyboardMarkup:
    keyboard = [[InlineKeyboardButton("🛑 Stop Process", callback_data=f"stop_{chat_id}")]]
    return InlineKeyboardMarkup(keyboard)

def format_srt_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    td = timedelta(seconds=seconds)
    total_seconds = int(td.total_seconds())
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds_int = total_seconds % 60
    milliseconds = td.microseconds // 1000
    return f"{hours:02}:{minutes:02}:{seconds_int:02},{milliseconds:03}"

def clean_pure_burmese_speech(raw_text: str) -> str:
    text = str(raw_text)
    text = re.sub(r'\[\s*\d{1,2}:\d{2}(?:\s*-\s*\d{1,2}:\d{2})?\s*\]', '', text)
    text = re.sub(r'\(\s*\d{1,2}:\d{2}(?:\s*-\s*\d{1,2}:\d{2})?\s*\)', '', text)
    text = re.sub(r'\b\d{1,2}:\d{2}(?::\d{2})?\b', '', text)
    text = re.sub(r'^(Narrator|Voiceover|အသံနောက်ခံ|ဇာတ်ကောင်|Scene|Timeline)\s*[:\-]', '', text, flags=re.IGNORECASE | re.MULTILINE)
    text = re.sub(r'(အသံနောက်ခံ|Narrator|ဇာတ်ကောင်များ|ဒိုင်ယာလော့ခ်)\s*[:\-]', '', text, flags=re.IGNORECASE)
    text = re.sub(r'(\*{1,3}|_{1,3}|#{1,6}|`|~|>|\$)', '', text)
    text = re.sub(r'[\u4e00-\u9fff]+', '', text)
    text = convert_digits_to_burmese(text)
    text = re.sub(r'[\"\']', '', text)
    return text.strip()

def extract_best_video_url(raw_text: str) -> str:
    all_urls = re.findall(r'https?://[^\s]+', raw_text)
    if not all_urls:
        return None
    for u in all_urls:
        if any(d in u for d in ['vt.tiktok.com', 'tiktok.com', 'youtu.be', 'youtube.com', 'douyin.com', 'xhslink.com', 'xiaohongshu', 'facebook.com', 'fb.watch']):
            return u
    return all_urls[0]

def download_video_universal(url: str, output_path: str) -> str:
    headers = {
        'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1'
    }

    if "douyin.com" in url:
        try:
            r = requests.get(url, headers=headers, allow_redirects=True, timeout=15)
            final_url = r.url
            video_ids = re.findall(r'/video/(\d+)', final_url) or re.findall(r'modal_id=(\d+)', final_url) or re.findall(r'item_ids=(\d+)', final_url)
            if video_ids:
                api_url = f"https://www.iesdouyin.com/web/api/v2/aweme/iteminfo/?item_ids={video_ids[0]}"
                res = requests.get(api_url, headers=headers, timeout=15).json()
                video_url = res["item_list"][0]["video"]["play_addr"]["url_list"][0].replace("playwm", "play")
                vr = requests.get(video_url, headers=headers, stream=True, timeout=30)
                if vr.status_code == 200:
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
        'user_agent': headers['User-Agent'],
        'retries': 10,
        'fragment_retries': 10,
        'continuedl': True
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            return output_path
    except Exception:
        pass

    return None

async def download_telegram_large_file_force(file_obj, context, local_path: str) -> bool:
    try:
        tg_file = await context.bot.get_file(file_obj.file_id)
        if tg_file.file_path:
            download_url = tg_file.file_path
            res = requests.get(download_url, stream=True, timeout=300)
            if res.status_code == 200:
                with open(local_path, "wb") as f:
                    for chunk in res.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            f.write(chunk)
                if os.path.exists(local_path) and os.path.getsize(local_path) > 1024:
                    return True
        await tg_file.download_to_drive(local_path)
        return os.path.exists(local_path) and os.path.getsize(local_path) > 1024
    except Exception as e:
        print(f"Telegram file download error: {e}")
        return False

def get_media_duration(file_path: str) -> float:
    cmd = [
        'ffprobe', '-v', 'error', '-show_entries', 'format=duration',
        '-of', 'default=noprint_wrappers=1:nokey=1', file_path
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        return float(result.stdout.strip())
    except Exception:
        return 60.0

def upload_audio_to_gemini(audio_path: str) -> str:
    if not os.path.exists(audio_path) or os.path.getsize(audio_path) == 0:
        raise Exception("Audio extraction failed")

    file_size = os.path.getsize(audio_path)
    url = f"https://generativelanguage.googleapis.com/upload/v1beta/files?key={GEMINI_API_KEY}"

    headers = {
        "X-Goog-Upload-Protocol": "resumable",
        "X-Goog-Upload-Command": "start",
        "X-Goog-Upload-Header-Content-Length": str(file_size),
        "X-Goog-Upload-Header-Content-Type": "audio/mp3",
        "Content-Type": "application/json"
    }

    last_error = ""
    for _ in range(3):
        try:
            init_res = requests.post(url, headers=headers, json={"file": {"display_name": os.path.basename(audio_path)}}, timeout=30)
            upload_url = init_res.headers.get("X-Goog-Upload-URL")
            if not upload_url:
                continue

            with open(audio_path, "rb") as f:
                res = requests.put(upload_url, headers={"Content-Length": str(file_size), "X-Goog-Upload-Offset": "0", "X-Goog-Upload-Command": "upload, finalize"}, data=f, timeout=180)

            data = res.json()
            file_uri = data.get("file", {}).get("uri")
            file_name = data.get("file", {}).get("name")

            check_url = f"https://generativelanguage.googleapis.com/v1beta/{file_name}?key={GEMINI_API_KEY}"
            for _ in range(30):
                try:
                    check_res = requests.get(check_url, timeout=20)
                    if check_res.status_code == 200:
                        cdata = check_res.json()
                        if cdata.get("state") == "ACTIVE":
                            return file_uri
                        elif cdata.get("state") == "FAILED":
                            raise Exception("Audio Processing Failed")
                except Exception:
                    pass
                time.sleep(2)

            return file_uri
        except Exception as e:
            last_error = str(e)
            time.sleep(3)

    raise Exception(f"Audio Upload Error: {last_error}")

def extract_timestamped_dubbing_json(file_uri: str, total_duration: float):
    prompt = f"""
You are an expert movie dubbing AI.
Listen to this audio track thoroughly from start (0:00) to the very end ({total_duration:.2f}s).
Transcribe and translate EVERY single spoken sentence into natural spoken Burmese.

CRITICAL INSTRUCTIONS:
1. Cover from 0:00 to {total_duration:.2f}s continuously.
2. Provide exact start and end timestamps.
3. 100% Burmese translation only.
4. Output ONLY valid JSON array:
[
  {{"start": 0.00, "end": 4.50, "burmese_text": "မြန်မာဘာသာပြန် စာကြောင်း"}}
]
"""

    models = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"]
    headers = {"Content-Type": "application/json"}
    payload = {
        "contents": [{
            "parts": [
                {"file_data": {"mime_type": "audio/mp3", "file_uri": file_uri}},
                {"text": prompt}
            ]
        }],
        "generationConfig": {
            "response_mime_type": "application/json"
        }
    }

    for model_name in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={GEMINI_API_KEY}"
        try:
            res = requests.post(url, headers=headers, json=payload, timeout=120)
            data = res.json()
            if "candidates" in data and data["candidates"]:
                raw_text = data["candidates"][0]["content"]["parts"][0]["text"]
                parsed = json.loads(re.sub(r'```json\s*|\s*```', '', raw_text.strip()))
                clean_segments = []
                for item in parsed:
                    raw_speech = item.get("burmese_text", "")
                    clean_speech = clean_pure_burmese_speech(raw_speech)
                    if clean_speech:
                        item["burmese_text"] = clean_speech
                        clean_segments.append(item)
                if clean_segments:
                    return clean_segments
        except Exception:
            continue

    raise Exception("Dialogue Extraction Failed")

async def generate_fixed_130x_speech(text: str, voice: str, output_path: str):
    clean_txt = clean_pure_burmese_speech(text) or "ထိုအချိန်တွင်"
    for _ in range(3):
        try:
            communicate = edge_tts.Communicate(text=clean_txt, voice=voice, rate="+30%")
            await communicate.save(output_path)
            if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                return True
        except Exception:
            await asyncio.sleep(1)

    subprocess.run(['ffmpeg', '-y', '-f', 'lavfi', '-i', 'anullsrc=r=44100:cl=stereo', '-t', '1.0', output_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True

async def render_precision_stretch_synced_video(orig_video_path, segments, total_duration, final_output_video, srt_output, chat_id):
    voice = "my-MM-ThihaNeural"
    temp_files = []

    video_concat_txt = f"vid_list_{chat_id}.txt"
    audio_concat_txt = f"aud_list_{chat_id}.txt"
    v_list = open(video_concat_txt, "w", encoding="utf-8")
    a_list = open(audio_concat_txt, "w", encoding="utf-8")
    temp_files.extend([video_concat_txt, audio_concat_txt])

    last_vid_point = 0.0
    current_timeline_accumulated = 0.0
    srt_entries = []

    for idx, seg in enumerate(segments):
        text = seg.get("burmese_text", "").strip()
        if not text:
            continue

        raw_start = float(seg.get("start", last_vid_point))
        raw_end = float(seg.get("end", raw_start + 2.0))

        if raw_start > last_vid_point + 0.05:
            gap_dur = raw_start - last_vid_point
            gap_v = f"v_gap_{chat_id}_{idx}.mp4"
            gap_a = f"a_gap_{chat_id}_{idx}.wav"

            subprocess.run(['ffmpeg', '-y', '-ss', f"{last_vid_point:.3f}", '-t', f"{gap_dur:.3f}", '-i', orig_video_path, '-c:v', 'libx264', '-preset', 'ultrafast', '-an', gap_v], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            subprocess.run(['ffmpeg', '-y', '-f', 'lavfi', '-i', 'anullsrc=r=44100:cl=stereo', '-t', f"{gap_dur:.3f}", gap_a], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

            temp_files.extend([gap_v, gap_a])
            v_list.write(f"file '{os.path.abspath(gap_v)}'\n")
            a_list.write(f"file '{os.path.abspath(gap_a)}'\n")
            current_timeline_accumulated += gap_dur

        seg_audio_mp3 = f"seg_a_{chat_id}_{idx}.mp3"
        await generate_fixed_130x_speech(text, voice, seg_audio_mp3)
        temp_files.append(seg_audio_mp3)

        seg_audio_wav = f"seg_a_{chat_id}_{idx}.wav"
        subprocess.run(['ffmpeg', '-y', '-i', seg_audio_mp3, '-ar', '44100', '-ac', '2', seg_audio_wav], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        temp_files.append(seg_audio_wav)

        actual_audio_dur = get_media_duration(seg_audio_wav)
        orig_scene_dur = max(0.4, raw_end - raw_start)
        pts_factor = actual_audio_dur / orig_scene_dur
        retimed_v = f"v_retimed_{chat_id}_{idx}.mp4"

        subprocess.run(['ffmpeg', '-y', '-ss', f"{raw_start:.3f}", '-t', f"{orig_scene_dur:.3f}", '-i', orig_video_path, '-filter:v', f'setpts={pts_factor:.6f}*PTS', '-c:v', 'libx264', '-preset', 'ultrafast', '-an', retimed_v], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

        temp_files.append(retimed_v)
        v_list.write(f"file '{os.path.abspath(retimed_v)}'\n")
        a_list.write(f"file '{os.path.abspath(seg_audio_wav)}'\n")

        st_time = current_timeline_accumulated
        en_time = current_timeline_accumulated + actual_audio_dur
        srt_entries.append((st_time, en_time, text))
        current_timeline_accumulated += actual_audio_dur
        last_vid_point = raw_end

    if total_duration > last_vid_point + 0.1:
        end_gap_dur = total_duration - last_vid_point
        end_gap_v = f"v_end_gap_{chat_id}.mp4"
        end_gap_a = f"a_end_gap_{chat_id}.wav"

        subprocess.run(['ffmpeg', '-y', '-ss', f"{last_vid_point:.3f}", '-t', f"{end_gap_dur:.3f}", '-i', orig_video_path, '-c:v', 'libx264', '-preset', 'ultrafast', '-an', end_gap_v], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        subprocess.run(['ffmpeg', '-y', '-f', 'lavfi', '-i', 'anullsrc=r=44100:cl=stereo', '-t', f"{end_gap_dur:.3f}", end_gap_a], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

        temp_files.extend([end_gap_v, end_gap_a])
        v_list.write(f"file '{os.path.abspath(end_gap_v)}'\n")
        a_list.write(f"file '{os.path.abspath(end_gap_a)}'\n")

    v_list.close()
    a_list.close()

    with open(srt_output, "w", encoding="utf-8") as srt_out:
        for idx, (st, en, txt) in enumerate(srt_entries, 1):
            srt_out.write(f"{idx}\n{format_srt_time(st)} --> {format_srt_time(en)}\n{txt}\n\n")

    temp_full_v = f"full_v_{chat_id}.mp4"
    temp_full_a = f"full_a_{chat_id}.wav"
    temp_files.extend([temp_full_v, temp_full_a])

    subprocess.run(['ffmpeg', '-y', '-f', 'concat', '-safe', '0', '-i', video_concat_txt, '-c:v', 'libx264', '-preset', 'ultrafast', temp_full_v], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    subprocess.run(['ffmpeg', '-y', '-f', 'concat', '-safe', '0', '-i', audio_concat_txt, '-c', 'copy', temp_full_a], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    subprocess.run(['ffmpeg', '-y', '-i', temp_full_v, '-i', temp_full_a, '-map', '0:v:0', '-map', '1:a:0', '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k', final_output_video], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

    for f in temp_files:
        if os.path.exists(f):
            try: os.remove(f)
            except Exception: pass

async def safe_edit_text(message, text: str, reply_markup=None):
    for _ in range(5):
        try:
            await message.edit_text(text, reply_markup=reply_markup, parse_mode="HTML")
            return
        except Exception:
            await asyncio.sleep(1)

async def process_recap_and_reply(update: Update, video_input_path: str, status_msg):
    chat_id = update.effective_chat.id
    temp_extracted_audio = f"extracted_{chat_id}.mp3"
    srt_path = f"dub_{chat_id}.srt"
    final_video_path = f"dub_video_{chat_id}.mp4"

    if chat_id in CANCELLED_TASKS:
        CANCELLED_TASKS.remove(chat_id)

    try:
        await safe_edit_text(status_msg, build_status_ui_html(1, 20), get_stop_button(chat_id))
        video_duration = get_media_duration(video_input_path)

        subprocess.run(['ffmpeg', '-y', '-i', video_input_path, '-vn', '-c:a', 'libmp3lame', '-q:a', '4', temp_extracted_audio], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        await safe_edit_text(status_msg, build_status_ui_html(2, 35), get_stop_button(chat_id))

        file_uri = upload_audio_to_gemini(temp_extracted_audio)
        await safe_edit_text(status_msg, build_status_ui_html(3, 55), get_stop_button(chat_id))

        segments = extract_timestamped_dubbing_json(file_uri, video_duration)
        await safe_edit_text(status_msg, build_status_ui_html(4, 75), get_stop_button(chat_id))

        await render_precision_stretch_synced_video(video_input_path, segments, video_duration, final_video_path, srt_path, chat_id)
        await safe_edit_text(status_msg, build_status_ui_html(5, 100), get_stop_button(chat_id))

        combined_text = "\n".join([s.get("burmese_text", "") for s in segments])
        caption = f"🎬 <b>AI Complete Burmese Dubbed Video</b>\n\n{combined_text[:300]}..."

        with open(final_video_path, "rb") as vf:
            await update.effective_message.reply_video(video=vf, caption=caption, parse_mode="HTML", supports_streaming=True)
        with open(srt_path, "rb") as sf:
            await update.effective_message.reply_document(document=sf, filename="subtitle.srt", caption="📄 SRT Subtitle File")

        await status_msg.delete()
    except Exception as e:
        if chat_id not in CANCELLED_TASKS:
            await safe_edit_text(status_msg, build_status_ui_html(1, 0, str(e)))
    finally:
        for f in [video_input_path, temp_extracted_audio, srt_path, final_video_path]:
            if f and os.path.exists(f):
                try: os.remove(f)
                except Exception: pass

async def cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data.startswith("stop_"):
        chat_id = int(query.data.split("_")[1])
        CANCELLED_TASKS.add(chat_id)
        await query.edit_message_text("🛑 <b>Process ကို ရပ်တန့်လိုက်ပါပြီ။</b>", parse_mode="HTML")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("မင်္ဂလာပါ! Video File သို့မဟုတ် Douyin/TikTok/YouTube Link ပို့ပေးပါ။")

async def handle_any_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw_text = update.message.text.strip()
    raw_url = extract_best_video_url(raw_text)
    if not raw_url:
        await update.message.reply_text("❌ Video Link မတွေ့ရှိပါ။ Link သီးသန့် ပို့ပေးပါခင်ဗျာ။")
        return

    chat_id = update.effective_chat.id
    status_msg = await update.message.reply_text(build_status_ui_html(1, 5), reply_markup=get_stop_button(chat_id), parse_mode="HTML")
    video_output = f"downloaded_{chat_id}_{int(time.time())}.mp4"
    downloaded_file = download_video_universal(raw_url, video_output)

    if not downloaded_file:
        await status_msg.edit_text("❌ Video ဒေါင်းလုဒ်ဆွဲ၍ မရပါ။ Link မှန်ကန်မှုရှိမရှိ ပြန်စစ်ပေးပါ။")
        return

    await process_recap_and_reply(update, downloaded_file, status_msg)

async def handle_video_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    video = update.message.video or update.message.document
    chat_id = update.effective_chat.id

    status_msg = await update.message.reply_text(
        build_status_ui_html(1, 5),
        reply_markup=get_stop_button(chat_id),
        parse_mode="HTML"
    )

    local_video_path = f"tg_video_{chat_id}_{int(time.time())}.mp4"
    success = await download_telegram_large_file_force(video, context, local_video_path)

    if not success:
        await safe_edit_text(status_msg, "❌ Video File ဒေါင်းလုဒ် မအောင်မြင်ပါ။ Telegram ကန့်သတ်ချက်ကြောင့် Video Link (Douyin/TikTok/YouTube) ပို့ပေးပါခင်ဗျာ။")
        return

    await process_recap_and_reply(update, local_video_path, status_msg)

def main():
    threading.Thread(target=start_health_server, daemon=True).start()
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(cancel_callback, pattern=r"^stop_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_any_link))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video_file))
    print("Bot is ready...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
