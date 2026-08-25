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
# API KEYS (Pre-configured)
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
        if any(domain in u for domain in ['vt.tiktok.com', 'tiktok.com', 'youtu.be', 'youtube.com', 'douyin.com', 'xhslink.com', 'xiaohongshu', 'facebook.com', 'fb.watch']):
            return u
    return all_urls[0]

def download_douyin_direct(url: str, output_path: str) -> str:
    headers = {
        'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1'
    }
    try:
        r = requests.get(url, headers=headers, allow_redirects=True, timeout=15)
        item_ids = re.findall(r'/video/(\d+)', r.url) or re.findall(r'item_ids=(\d+)', r.url)
        if item_ids:
            api_url = f"https://www.iesdouyin.com/web/api/v2/aweme/iteminfo/?item_ids={item_ids[0]}"
            res = requests.get(api_url, headers=headers, timeout=15).json()
            video_url = res["item_list"][0]["video"]["play_addr"]["url_list"][0].replace("playwm", "play")
            vr = requests.get(video_url, headers=headers, stream=True, timeout=30)
            with open(output_path, 'wb') as f:
                for chunk in vr.iter_content(chunk_size=1024*1024):
                    if chunk: f.write(chunk)
            if os.path.exists(output_path) and os.path.getsize(output_path) > 1024:
                return output_path
    except Exception:
        pass
    return None

def download_video_universal(url: str, output_path: str) -> str:
    if "douyin.com" in url:
        d_res = download_douyin_direct(url, output_path)
        if d_res:
            return d_res

    headers_ua = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'
    if "tiktok.com" in url:
        headers_ua = 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 Mobile/15E148 Safari/604.1'

    ydl_opts = {
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'outtmpl': output_path,
        'quiet': True,
        'no_warnings': True,
        'user_agent': headers_ua,
        'retries': 10,
        'fragment_retries': 10,
        'continuedl': True
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        return output_path if os.path.exists(output_path) and os.path.getsize(output_path) > 0 else None
    except Exception as e:
        print(f"Download Error: {e}")
        return None

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

async def download_telegram_large_file(video_obj, context, local_path: str) -> bool:
    try:
        tg_file = await context.bot.get_file(video_obj.file_id)
        if tg_file.file_path:
            download_url = tg_file.file_path
            curl_cmd = [
                'curl', '-L', '-C', '-', '--retry', '10', '--retry-delay', '2',
                '--connect-timeout', '30', '-m', '900', '-o', local_path, download_url
            ]
            subprocess.run(curl_cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if os.path.exists(local_path) and os.path.getsize(local_path) > 0:
                return True
        await tg_file.download_to_drive(local_path)
        return os.path.exists(local_path) and os.path.getsize(local_path) > 0
    except Exception as e:
        print(f"Telegram Download Fail: {e}")
        return False

def upload_audio_to_gemini(audio_path: str) -> str:
    if not os.path.exists(audio_path) or os.path.getsize(audio_path) == 0:
        raise Exception("Audio extraction failed (0 bytes file)")

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
Transcribe and translate EVERY single spoken sentence, narration line, and conversation into fluent natural spoken Burmese.

CRITICAL INSTRUCTIONS:
1. Cover the ENTIRE video from 0:00 to {total_duration:.2f}s continuously without stopping early.
2. Provide exact start and end timestamps matching the original spoken segments.
3. 100% Burmese translation only. NO English or foreign words.
4. Output ONLY a valid JSON array of objects without Markdown code blocks:
[
  {{"start": 0.00, "end": 4.50, "burmese_text": "မြန်မာဘာသာပြန် စာကြောင်း"}},
  ...
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

    last_err = ""
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
            elif "error" in data:
                last_err = data["error"].get("message", "")
        except Exception as e:
            last_err = str(e)
            continue

    raise Exception(f"Dialogue Extraction Error: {last_err}")

async def generate_fixed_130x_speech(text: str, voice: str, output_path: str):
    clean_txt = clean_pure_burmese_speech(text)
    if not clean_txt:
        clean_txt = "ထိုအချိန်တွင်"

    for _ in range(3):
        try:
            communicate = edge_tts.Communicate(text=clean_txt, voice=voice, rate="+30%")
            await communicate.save(output_path)
            if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                return True
        except Exception:
            await asyncio.sleep(1)

    subprocess.run([
        'ffmpeg', '-y', '-f', 'lavfi', '-i', 'anullsrc=r=44100:cl=stereo', '-t', '1.0', output_path
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True

async def render_precision_stretch_synced_video(
    orig_video_path: str,
    segments: list,
    total_duration: float,
    final_output_video: str,
    srt_output: str,
    chat_id: int
):
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

            subprocess.run([
                'ffmpeg', '-y', '-ss', f"{last_vid_point:.3f}", '-t', f"{gap_dur:.3f}",
                '-i', orig_video_path, '-c:v', 'libx264', '-preset', 'ultrafast', '-an', gap_v
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

            subprocess.run([
                'ffmpeg', '-y', '-f', 'lavfi', '-i', 'anullsrc=r=44100:cl=stereo', '-t', f"{gap_dur:.3f}", gap_a
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

            temp_files.extend([gap_v, gap_a])
            v_list.write(f"file '{os.path.abspath(gap_v)}'\n")
            a_list.write(f"file '{os.path.abspath(gap_a)}'\n")
            current_timeline_accumulated += gap_dur

        seg_audio_mp3 = f"seg_a_{chat_id}_{idx}.mp3"
        await generate_fixed_130x_speech(text, voice, seg_audio_mp3)
        temp_files.append(seg_audio_mp3)

        seg_audio_wav = f"seg_a_{chat_id}_{idx}.wav"
        subprocess.run([
            'ffmpeg', '-y', '-i', seg_audio_mp3, '-ar', '44100', '-ac', '2', seg_audio_wav
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        temp_files.append(seg_audio_wav)

        actual_audio_dur = get_media_duration(seg_audio_wav)
        orig_scene_dur = max(0.4, raw_end - raw_start)

        pts_factor = actual_audio_dur / orig_scene_dur
        retimed_v = f"v_retimed_{chat_id}_{idx}.mp4"

        subprocess.run([
            'ffmpeg', '-y', '-ss', f"{raw_start:.3f}", '-t', f"{orig_scene_dur:.3f}",
            '-i', orig_video_path,
            '-filter:v', f'setpts={pts_factor:.6f}*PTS',
            '-c:v', 'libx264', '-preset', 'ultrafast', '-an', retimed_v
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

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

        subprocess.run([
            'ffmpeg', '-y', '-ss', f"{last_vid_point:.3f}", '-t', f"{end_gap_dur:.3f}",
            '-i', orig_video_path, '-c:v', 'libx264', '-preset', 'ultrafast', '-an', end_gap_v
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

        subprocess.run([
            'ffmpeg', '-y', '-f', 'lavfi', '-i', 'anullsrc=r=44100:cl=stereo', '-t', f"{end_gap_dur:.3f}", end_gap_a
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

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

    subprocess.run([
        'ffmpeg', '-y', '-f', 'concat', '-safe', '0', '-i', video_concat_txt, '-c:v', 'libx264', '-preset', 'ultrafast', temp_full_v
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

    subprocess.run([
        'ffmpeg', '-y', '-f', 'concat', '-safe', '0', '-i', audio_concat_txt, '-c', 'copy', temp_full_a
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

    subprocess.run([
        'ffmpeg', '-y', '-i', temp_full_v, '-i', temp_full_a,
        '-map', '0:v:0', '-map', '1:a:0',
        '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k',
        final_output_video
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

    for f in temp_files:
        if os.path.exists(f):
            try:
                os.remove(f)
            except Exception:
                pass

async def safe_edit_text(message, text: str, reply_markup=None):
    for _ in range(5):
        try:
            await message.edit_text(text, reply_markup=reply_markup, parse_mode="HTML")
            return
        except Exception:
            await asyncio.sleep(1)

def compress_video_for_telegram(input_path: str, output_path: str) -> str:
    if not os.path.exists(input_path):
        raise Exception("Final video file not found.")

    input_size = os.path.getsize(input_path)
    if input_size <= TELEGRAM_MAX_UPLOAD_BYTES:
        return input_path

    duration = get_media_duration(input_path)
    if duration <= 0:
        duration = 60.0

    target_bytes = TELEGRAM_TARGET_BYTES
    total_bitrate = int((target_bytes * 8 * 0.90) / duration)
    audio_bitrate = 96000
    video_bitrate = max(180000, total_bitrate - audio_bitrate)
    last_size = input_size

    for _ in range(1, 5):
        if os.path.exists(output_path):
            try:
                os.remove(output_path)
            except Exception:
                pass

        cmd = [
            'ffmpeg', '-y', '-i', input_path,
            '-vf', "scale='min(720,iw)':-2",
            '-c:v', 'libx264', '-preset', 'veryfast',
            '-b:v', str(video_bitrate),
            '-maxrate', str(int(video_bitrate * 1.10)),
            '-bufsize', str(int(video_bitrate * 2)),
            '-pix_fmt', 'yuv420p',
            '-c:a', 'aac', '-b:a', '96k',
            '-movflags', '+faststart',
            output_path
        ]

        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        if result.returncode != 0 or not os.path.exists(output_path):
            error_text = result.stderr[-1500:] if result.stderr else "Unknown FFmpeg error"
            raise Exception("Video compression failed:\n" + error_text)

        new_size = os.path.getsize(output_path)
        if new_size <= TELEGRAM_MAX_UPLOAD_BYTES:
            return output_path

        ratio = TELEGRAM_TARGET_BYTES / max(new_size, 1)
        video_bitrate = int(video_bitrate * ratio * 0.90)
        video_bitrate = max(120000, video_bitrate)
        last_size = new_size

    raise Exception(f"Video compression error: size = {last_size / 1024 / 1024:.2f} MB")

async def upload_video_with_retry(message, video_path: str, caption: str, status_msg, chat_id: int):
    if not os.path.exists(video_path):
        raise Exception("Video file does not exist.")

    video_size = os.path.getsize(video_path)
    upload_video_path = video_path
    compressed_path = f"telegram_compressed_{chat_id}.mp4"

    if video_size > TELEGRAM_MAX_UPLOAD_BYTES:
        await safe_edit_text(
            status_msg,
            "🎬 <b>Video File ကြီးနေပါသည်</b>\n\n"
            "📦 Telegram Upload Limit အတွက် Video ကို အလိုအလျောက်ချုံ့နေပါသည်...\n\n"
            "⏳ ခဏစောင့်ပါ...",
            get_stop_button(chat_id)
        )
        upload_video_path = await asyncio.to_thread(compress_video_for_telegram, video_path, compressed_path)

    final_upload_size = os.path.getsize(upload_video_path)
    if final_upload_size > TELEGRAM_MAX_UPLOAD_BYTES:
        raise Exception(f"Telegram Upload မလုပ်နိုင်ပါ။ Video Size သည် {final_upload_size / 1024 / 1024:.2f} MB ဖြစ်နေသေးပါသည်။")

    last_error = None
    for attempt in range(1, 6):
        try:
            if attempt > 1:
                await safe_edit_text(
                    status_msg,
                    "📤 <b>Telegram Video Upload ပြန်လည်ကြိုးစားနေပါသည်...</b>\n\n"
                    f"🔄 Attempt {attempt}/5",
                    get_stop_button(chat_id)
                )
                await asyncio.sleep(min(3 * attempt, 15))

            with open(upload_video_path, "rb") as video_file:
                await message.reply_video(
                    video=video_file,
                    caption=caption,
                    parse_mode="HTML",
                    supports_streaming=True,
                    read_timeout=1800,
                    write_timeout=1800,
                    connect_timeout=180,
                    pool_timeout=180
                )

            if os.path.exists(compressed_path):
                try:
                    os.remove(compressed_path)
                except Exception:
                    pass

            return True
        except Exception as e:
            last_error = e
            error_text = str(e)
            print(f"Telegram video upload attempt {attempt}/5 failed: {error_text}")

            if "Request Entity Too Large" in error_text or "entity too large" in error_text.lower():
                if os.path.exists(compressed_path):
                    try:
                        os.remove(compressed_path)
                    except Exception:
                        pass
                raise Exception("Telegram Upload Error: File size exceeds Telegram limits.")

    if os.path.exists(compressed_path):
        try:
            os.remove(compressed_path)
        except Exception:
            pass

    raise Exception(f"Telegram Video Upload 5 ကြိမ်ကြိုးစားပြီး မအောင်မြင်ပါ။\n\nError: {last_error}")

async def upload_srt_with_retry(message, srt_path: str):
    last_error = None
    for attempt in range(1, 6):
        try:
            with open(srt_path, "rb") as srt_doc:
                await message.reply_document(
                    document=srt_doc,
                    filename="subtitle.srt",
                    caption="📄 အချိန်ကိုက် SRT Subtitle File",
                    read_timeout=600,
                    write_timeout=600,
                    connect_timeout=180,
                    pool_timeout=180
                )
            return True
        except Exception as e:
            last_error = e
            await asyncio.sleep(min(3 * attempt, 15))

    raise Exception(f"SRT Upload မအောင်မြင်ပါ: {last_error}")

async def process_recap_and_reply(update: Update, video_input_path: str, status_msg):
    chat_id = update.effective_chat.id
    temp_extracted_audio = f"extracted_{chat_id}.mp3"
    srt_path = f"dub_{chat_id}.srt"
    final_video_path = f"dub_video_{chat_id}.mp4"
    compressed_video_path = f"telegram_compressed_{chat_id}.mp4"

    if chat_id in CANCELLED_TASKS:
        CANCELLED_TASKS.remove(chat_id)

    try:
        await safe_edit_text(status_msg, build_status_ui_html(1, 20), get_stop_button(chat_id))
        if chat_id in CANCELLED_TASKS:
            return

        await safe_edit_text(status_msg, build_status_ui_html(2, 35), get_stop_button(chat_id))
        video_duration = get_media_duration(video_input_path)

        subprocess.run([
            'ffmpeg', '-y', '-i', video_input_path, '-vn', '-c:a', 'libmp3lame', '-q:a', '4', temp_extracted_audio
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

        file_uri = upload_audio_to_gemini(temp_extracted_audio)
        if chat_id in CANCELLED_TASKS:
            return

        await safe_edit_text(status_msg, build_status_ui_html(3, 55), get_stop_button(chat_id))
        segments = extract_timestamped_dubbing_json(file_uri, video_duration)
        if chat_id in CANCELLED_TASKS:
            return

        await safe_edit_text(status_msg, build_status_ui_html(4, 75), get_stop_button(chat_id))
        await render_precision_stretch_synced_video(video_input_path, segments, video_duration, final_video_path, srt_path, chat_id)
        if chat_id in CANCELLED_TASKS:
            return

        await safe_edit_text(status_msg, build_status_ui_html(5, 100), get_stop_button(chat_id))
        combined_text = "\n".join([s.get("burmese_text", "") for s in segments])
        caption = f"🎬 <b>AI Complete Burmese Dubbed Video (100% Synced)</b>\n\n{combined_text[:300]}..."

        await upload_video_with_retry(update.effective_message, final_video_path, caption, status_msg, chat_id)
        await upload_srt_with_retry(update.effective_message, srt_path)
        await status_msg.delete()

    except Exception as e:
        print(f"PROCESS ERROR: {repr(e)}")
        if chat_id not in CANCELLED_TASKS:
            await safe_edit_text(status_msg, build_status_ui_html(1, 0, str(e)))
    finally:
        for f in [video_input_path, temp_extracted_audio, srt_path, final_video_path, compressed_video_path]:
            if f and os.path.exists(f):
                try:
                    os.remove(f)
                except Exception:
                    pass

async def cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data.startswith("stop_"):
        chat_id = int(data.split("_")[1])
        CANCELLED_TASKS.add(chat_id)
        await query.edit_message_text("🛑 <b>Process ကို အောင်မြင်စွာ ရပ်တန့်လိုက်ပါပြီ။</b>", parse_mode="HTML")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "မင်္ဂလာပါ! Video File သို့မဟုတ် Link ပို့ပေးပါ။\n"
        "ဗီဒီယိုထဲက မူရင်းစကားပြောအတိုင်း မြန်မာလို အချိန်ကိုက် Dubbing ပြုလုပ်ပေးပါမည်။"
    )

async def handle_any_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw_text = update.message.text.strip()
    raw_url = extract_best_video_url(raw_text)
    if not raw_url:
        await update.message.reply_text("❌ Video Link မတွေ့ရှိပါ။ Link သီးသန့် ပို့ပေးပါခင်ဗျာ။")
        return

    chat_id = update.effective_chat.id
    status_msg = await update.message.reply_text(
        build_status_ui_html(1, 5),
        reply_markup=get_stop_button(chat_id),
        parse_mode="HTML"
    )

    video_output = f"downloaded_video_{chat_id}.mp4"
    downloaded_file = download_video_universal(raw_url, video_output)

    if not downloaded_file or not os.path.exists(downloaded_file):
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

    local_video_path = f"telegram_video_{chat_id}.mp4"

    try:
        success = await download_telegram_large_file(video, context, local_video_path)
        if not success:
            await safe_edit_text(status_msg, "❌ File is too big သို့မဟုတ် ဒေါင်းလုဒ်မအောင်မြင်ပါ။ Link ပို့ပေးပါခင်ဗျာ။")
            return

        await process_recap_and_reply(update, local_video_path, status_msg)
    except Exception as e:
        await safe_edit_text(status_msg, f"❌ Error: {str(e)}")

def main():
    threading.Thread(target=start_health_server, daemon=True).start()
    while True:
        try:
            app = (
                ApplicationBuilder()
                .token(TELEGRAM_BOT_TOKEN)
                .connect_timeout(300.0)
                .read_timeout(300.0)
                .write_timeout(300.0)
                .pool_timeout(300.0)
                .get_updates_read_timeout(60.0)
                .get_updates_connect_timeout(60.0)
                .build()
            )

            app.add_handler(CommandHandler("start", start))
            app.add_handler(CallbackQueryHandler(cancel_callback, pattern=r"^stop_"))
            app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_any_link))
            app.add_handler(MessageHandler(filters.VIDEO | (filters.Document.ALL & filters.Document.MimeType("video/mp4")), handle_video_file))

            print("====================================")
            print("Bot စတင် အလုပ်လုပ်နေပါပြီ...")
            print("====================================")

            app.run_polling(drop_pending_updates=True)

        except KeyboardInterrupt:
            print("\nBot ရပ်တန့်လိုက်ပါပြီ။")
            break
        except Exception as e:
            print(f"Connection retry in 5s: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
