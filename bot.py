import os
import re
import json
import time
import html
import asyncio
import subprocess
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import timedelta
from urllib.parse import unquote, urlparse

import requests
import edge_tts
import yt_dlp

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

# ============================================================
# IMPORTANT: put these in your hosting Environment Variables.
# Do NOT hard-code Telegram/Gemini keys in this file.
# ============================================================
TELEGRAM_BOT_TOKEN = "7752878545:AAFMBSnhvLEHh7Z9jdHkGoZyDTsaG-gbZj8"
GEMINI_API_KEY = "AQ.Ab8RN6J3SI7Hcw4jHJQU4-b4IvkYYprAGPp6Wcn0PVvYvkk2BQ"


CANCELLED_TASKS = set()
EN_TO_MY_DIGITS = str.maketrans("0123456789", "၀၁၂၃၄၅၆၇၈၉")

# Telegram Bot API uploads are commonly limited around 50 MB.
# Keep a safety margin.
TELEGRAM_MAX_UPLOAD_BYTES = 49 * 1024 * 1024
TELEGRAM_TARGET_BYTES = 45 * 1024 * 1024

COMMON_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 "
        "Mobile/15E148 Safari/604.1"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://www.douyin.com/",
}

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Bot is Running 24/7!")

    def log_message(self, format, *args):
        return

def start_health_server():
    port = int(os.environ.get("PORT", "8080"))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

def convert_digits_to_burmese(text: str) -> str:
    return text.translate(EN_TO_MY_DIGITS)

def create_progress_bar(percent: int) -> str:
    percent = max(0, min(100, int(percent)))
    filled_blocks = int(percent / 10)
    return "█" * filled_blocks + "▒" * (10 - filled_blocks) + f" {percent}%"

def build_status_ui_html(step: int, percent: int, error_text: str = None) -> str:
    steps_info = [
        ("Video Downloaded", 20),
        ("Extracting Audio Context (Gemini Processing)", 35),
        ("Complete Video Dialogue Translation", 55),
        ("1.30x Speech Generation & Precision Stretch Sync", 75),
        ("Final High-Quality Multiplexing", 95),
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
        safe_error = (
            str(error_text)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        text += f"\n❌ <b>အမှားဖြစ်ပေါ်ပါသည်:</b> {safe_error[:1000]}"
    return text

def get_stop_button(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🛑 Stop Process", callback_data=f"stop_{chat_id}")]]
    )

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
    text = re.sub(r"\[\s*\d{1,2}:\d{2}(?:\s*-\s*\d{1,2}:\d{2})?\s*\]", "", text)
    text = re.sub(r"\(\s*\d{1,2}:\d{2}(?:\s*-\s*\d{1,2}:\d{2})?\s*\)", "", text)
    text = re.sub(r"\b\d{1,2}:\d{2}(?::\d{2})?\b", "", text)
    text = re.sub(
        r"^(Narrator|Voiceover|အသံနောက်ခံ|ဇာတ်ကောင်|Scene|Timeline)\s*[:\-]",
        "",
        text,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    text = re.sub(
        r"(အသံနောက်ခံ|Narrator|ဇာတ်ကောင်များ|ဒိုင်ယာလော့ခ်)\s*[:\-]",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"(\*{1,3}|_{1,3}|#{1,6}|`|~|>|\$)", "", text)
    # Remove Chinese characters only; keep Burmese and useful punctuation.
    text = re.sub(r"[\u4e00-\u9fff]+", "", text)
    text = convert_digits_to_burmese(text)
    text = re.sub(r'["\']', "", text)
    return re.sub(r"\s+", " ", text).strip()

def extract_best_video_url(raw_text: str) -> str | None:
    """
    Handles:
      - clean URLs
      - Douyin copied share text
      - punctuation immediately after a URL
    """
    if not raw_text:
        return None

    # Telegram can receive the entire Douyin share text.
    pattern = re.compile(
        r"https?://[^\s<>\u200b]+",
        re.IGNORECASE,
    )
    candidates = pattern.findall(raw_text)

    cleaned = []
    for u in candidates:
        u = u.strip().rstrip(".,!?;:)]}>'\"")
        cleaned.append(u)

    preferred_domains = (
        "v.douyin.com",
        "douyin.com",
        "iesdouyin.com",
        "tiktok.com",
        "vt.tiktok.com",
        "youtu.be",
        "youtube.com",
        "xhslink.com",
        "xiaohongshu.com",
        "facebook.com",
        "fb.watch",
    )

    for u in cleaned:
        try:
            host = (urlparse(u).hostname or "").lower()
        except Exception:
            host = ""
        if any(host == d or host.endswith("." + d) for d in preferred_domains):
            return u

    return cleaned[0] if cleaned else None

def _extract_aweme_id(url: str) -> str | None:
    patterns = [
        r"/video/(\d{10,25})",
        r"/share/video/(\d{10,25})",
        r"/note/(\d{10,25})",
        r"[?&]modal_id=(\d{10,25})",
        r"[?&]aweme_id=(\d{10,25})",
        r"[?&]item_ids?=(\d{10,25})",
    ]
    for p in patterns:
        m = re.search(p, url)
        if m:
            return m.group(1)
    return None

def _walk_for_play_urls(obj):
    """Recursively find play_addr.url_list / download_addr.url_list."""
    found = []

    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in ("play_addr", "download_addr", "bit_rate"):
                if isinstance(value, dict):
                    urls = value.get("url_list")
                    if isinstance(urls, list):
                        for u in urls:
                            if isinstance(u, str) and u.startswith("http"):
                                found.append(u)
                    # Some structures use URL directly.
                    for k in ("url", "url_list"):
                        v = value.get(k)
                        if isinstance(v, str) and v.startswith("http"):
                            found.append(v)
            found.extend(_walk_for_play_urls(value))

    elif isinstance(obj, list):
        for item in obj:
            found.extend(_walk_for_play_urls(item))

    return found

def _extract_router_json(page: str):
    """
    Extracts window._ROUTER_DATA JSON from the current mobile share page.
    """
    marker = "window._ROUTER_DATA"
    pos = page.find(marker)
    if pos < 0:
        return None

    start = page.find("=", pos)
    if start < 0:
        return None

    start += 1
    while start < len(page) and page[start].isspace():
        start += 1

    # Usually JSON ends before </script>.
    end = page.find("</script>", start)
    if end < 0:
        end = len(page)

    candidate = page[start:end].strip().rstrip(";").strip()
    candidate = html.unescape(candidate)

    try:
        return json.loads(candidate)
    except Exception:
        # Fallback: locate the first balanced JSON object.
        first = candidate.find("{")
        if first < 0:
            return None

        depth = 0
        in_string = False
        escape = False

        for i in range(first, len(candidate)):
            ch = candidate[i]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue

            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(candidate[first:i + 1])
                    except Exception:
                        return None
    return None

def _extract_video_urls_from_html(page: str) -> list[str]:
    urls = []

    router = _extract_router_json(page)
    if router is not None:
        urls.extend(_walk_for_play_urls(router))

    # Generic fallback for JSON-embedded play_addr.
    for match in re.finditer(r'"url_list"\s*:\s*\[(.*?)\]', page, re.S):
        block = match.group(1)
        for u in re.findall(r'"(https?://[^"]+)"', block):
            urls.append(bytes(u, "utf-8").decode("unicode_escape", errors="ignore"))

    # Deduplicate while preserving order.
    result = []
    seen = set()
    for u in urls:
        u = u.replace("\\u002F", "/").replace("\\/", "/")
        u = html.unescape(u)
        if u.startswith("http") and u not in seen:
            seen.add(u)
            result.append(u)
    return result

def _download_direct_stream(video_url: str, output_path: str, referer: str) -> bool:
    headers = dict(COMMON_HEADERS)
    headers["Referer"] = referer

    with requests.get(
        video_url,
        headers=headers,
        stream=True,
        timeout=(20, 180),
        allow_redirects=True,
    ) as r:
        if r.status_code != 200:
            return False

        content_type = (r.headers.get("Content-Type") or "").lower()
        if "text/html" in content_type:
            return False

        tmp = output_path + ".part"
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)

        if os.path.exists(tmp) and os.path.getsize(tmp) > 1024:
            os.replace(tmp, output_path)
            return True

        try:
            os.remove(tmp)
        except OSError:
            pass
        return False

def download_douyin_public(url: str, output_path: str) -> str | None:
    """
    Current Douyin path:
      1) follow v.douyin.com short URL
      2) get aweme_id
      3) request iesdouyin mobile share page
      4) parse _ROUTER_DATA
      5) use play URL, with playwm fallback
    """
    session = requests.Session()
    session.headers.update(COMMON_HEADERS)

    # First resolve the short link.
    try:
        r = session.get(url, allow_redirects=True, timeout=25)
        final_url = r.url
        page = r.text or ""
    except Exception as e:
        raise RuntimeError(f"Douyin short-link ဖွင့်မရပါ: {e}")

    aweme_id = _extract_aweme_id(final_url) or _extract_aweme_id(url)

    # If the redirect page contains an ID, use it.
    if not aweme_id:
        m = re.search(r"(?:video|note|modal_id)[^\d]{0,20}(\d{10,25})", page)
        if m:
            aweme_id = m.group(1)

    if not aweme_id:
        raise RuntimeError(
            "Douyin Video ID မတွေ့ပါ။ Video က public ဖြစ်/မဖြစ် စစ်ပေးပါ။"
        )

    share_url = f"https://www.iesdouyin.com/share/video/{aweme_id}/"

    last_error = ""
    for attempt in range(3):
        try:
            res = session.get(
                share_url,
                headers={
                    **COMMON_HEADERS,
                    "Referer": final_url if final_url else "https://www.douyin.com/",
                },
                timeout=(20, 30),
            )
            if res.status_code != 200:
                last_error = f"share page HTTP {res.status_code}"
                time.sleep(1.5)
                continue

            html_text = res.text or ""
            video_urls = _extract_video_urls_from_html(html_text)

            # Prefer no-watermark /play/ URLs.
            preferred = []
            fallback = []
            for u in video_urls:
                if "/playwm/" in u:
                    fallback.append(u.replace("/playwm/", "/play/"))
                    fallback.append(u)
                else:
                    preferred.append(u)

            candidates = preferred + fallback

            for video_url in candidates:
                for retry in range(2):
                    try:
                        if _download_direct_stream(
                            video_url, output_path, share_url
                        ):
                            return output_path
                    except Exception as e:
                        last_error = str(e)
                        time.sleep(1)

        except Exception as e:
            last_error = str(e)
            time.sleep(1.5)

    raise RuntimeError(
        "Douyin Video ကို မဒေါင်းနိုင်ပါ။ "
        f"အကြောင်းရင်း: {last_error or '解析 failed'}"
    )

def download_video_with_ytdlp(url: str, output_path: str) -> str | None:
    """
    Fallback for TikTok / YouTube / Facebook / XHS etc.
    """
    base, ext = os.path.splitext(output_path)
    ydl_opts = {
        "format": "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b",
        "outtmpl": base + ".%(ext)s",
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "retries": 5,
        "fragment_retries": 5,
        "continuedl": True,
        "noplaylist": True,
        "http_headers": COMMON_HEADERS,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        # Find the generated file.
        for candidate in (
            output_path,
            base + ".mp4",
            base + ".mkv",
            base + ".webm",
        ):
            if os.path.exists(candidate) and os.path.getsize(candidate) > 1024:
                if candidate != output_path:
                    os.replace(candidate, output_path)
                return output_path
    except Exception as e:
        print("yt-dlp fallback error:", e)

    return None

def download_video_universal(url: str, output_path: str) -> str | None:
    url = extract_best_video_url(url) or url

    host = (urlparse(url).hostname or "").lower()

    # IMPORTANT: do NOT use the old iesdouyin /web/api/v2/aweme/iteminfo
    # endpoint as the primary path. The share-page route is much more
    # reliable for current public Douyin links.
    if "douyin.com" in host:
        try:
            return download_douyin_public(url, output_path)
        except Exception as e:
            print("Douyin primary downloader failed:", e)

            # Last-resort yt-dlp fallback.
            fallback = download_video_with_ytdlp(url, output_path)
            if fallback:
                return fallback
            return None

    return download_video_with_ytdlp(url, output_path)

async def download_telegram_large_file_force(file_obj, context, local_path: str) -> bool:
    try:
        tg_file = await context.bot.get_file(file_obj.file_id)

        # Prefer Telegram's file URL if available.
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
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        file_path,
    ]
    result = subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    try:
        return float(result.stdout.strip())
    except Exception:
        return 60.0

def upload_audio_to_gemini(audio_path: str) -> str:
    if not os.path.exists(audio_path) or os.path.getsize(audio_path) == 0:
        raise Exception("Audio extraction failed")

    file_size = os.path.getsize(audio_path)
    url = (
        "https://generativelanguage.googleapis.com/upload/v1beta/files"
        f"?key={GEMINI_API_KEY}"
    )

    headers = {
        "X-Goog-Upload-Protocol": "resumable",
        "X-Goog-Upload-Command": "start",
        "X-Goog-Upload-Header-Content-Length": str(file_size),
        "X-Goog-Upload-Header-Content-Type": "audio/mpeg",
        "Content-Type": "application/json",
    }

    last_error = ""
    for _ in range(3):
        try:
            init_res = requests.post(
                url,
                headers=headers,
                json={"file": {"display_name": os.path.basename(audio_path)}},
                timeout=30,
            )
            if init_res.status_code >= 400:
                last_error = init_res.text[:500]
                time.sleep(2)
                continue

            upload_url = init_res.headers.get("X-Goog-Upload-URL")
            if not upload_url:
                last_error = "Gemini upload URL မရပါ"
                continue

            with open(audio_path, "rb") as f:
                res = requests.put(
                    upload_url,
                    headers={
                        "Content-Length": str(file_size),
                        "X-Goog-Upload-Offset": "0",
                        "X-Goog-Upload-Command": "upload, finalize",
                    },
                    data=f,
                    timeout=300,
                )

            if res.status_code >= 400:
                last_error = res.text[:500]
                continue

            data = res.json()
            file_uri = data.get("file", {}).get("uri")
            file_name = data.get("file", {}).get("name")

            if not file_uri or not file_name:
                last_error = "Gemini file URI/name မရပါ"
                continue

            check_url = (
                f"https://generativelanguage.googleapis.com/v1beta/"
                f"{file_name}?key={GEMINI_API_KEY}"
            )

            for _ in range(30):
                check_res = requests.get(check_url, timeout=20)
                if check_res.status_code == 200:
                    cdata = check_res.json()
                    state = cdata.get("state")
                    if state == "ACTIVE":
                        return file_uri
                    if state == "FAILED":
                        raise Exception("Audio Processing Failed")
                time.sleep(2)

            return file_uri

        except Exception as e:
            last_error = str(e)
            time.sleep(2)

    raise Exception(f"Audio Upload Error: {last_error}")

def extract_timestamped_dubbing_json(file_uri: str, total_duration: float):
    prompt = f"""
You are an expert movie dubbing AI.

Listen to the entire audio from 0:00 to {total_duration:.2f} seconds.
Transcribe and translate every spoken sentence into natural spoken Burmese.

CRITICAL:
1. Cover the whole audio from 0:00 to {total_duration:.2f}s.
2. Give accurate start/end timestamps.
3. Burmese speech only.
4. Do not omit spoken dialogue.
5. Return ONLY a valid JSON array.

[
  {{"start": 0.00, "end": 4.50, "burmese_text": "မြန်မာဘာသာပြန် စာကြောင်း"}}
]
"""

    models = [
        "gemini-2.5-flash",
        "gemini-2.0-flash",
        "gemini-1.5-flash",
    ]

    payload = {
        "contents": [{
            "parts": [
                {
                    "file_data": {
                        "mime_type": "audio/mpeg",
                        "file_uri": file_uri,
                    }
                },
                {"text": prompt},
            ]
        }],
        "generationConfig": {
            "response_mime_type": "application/json",
        },
    }

    headers = {"Content-Type": "application/json"}

    for model_name in models:
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model_name}:generateContent?key={GEMINI_API_KEY}"
        )
        try:
            res = requests.post(url, headers=headers, json=payload, timeout=180)
            data = res.json()

            if "candidates" not in data or not data["candidates"]:
                continue

            raw_text = data["candidates"][0]["content"]["parts"][0]["text"]
            raw_text = re.sub(r"```json\s*|\s*```", "", raw_text.strip())
            parsed = json.loads(raw_text)

            clean_segments = []
            for item in parsed:
                try:
                    start = float(item.get("start", 0))
                    end = float(item.get("end", start + 1))
                except Exception:
                    continue

                speech = clean_pure_burmese_speech(
                    item.get("burmese_text", "")
                )

                if speech and end > start:
                    clean_segments.append({
                        "start": max(0.0, start),
                        "end": max(start + 0.05, end),
                        "burmese_text": speech,
                    })

            if clean_segments:
                clean_segments.sort(key=lambda x: x["start"])
                return clean_segments

        except Exception as e:
            print(f"Gemini {model_name} error:", e)
            continue

    raise Exception("Dialogue Extraction Failed")

async def generate_fixed_130x_speech(text: str, voice: str, output_path: str):
    clean_txt = clean_pure_burmese_speech(text) or "ထိုအချိန်တွင်"

    for _ in range(3):
        try:
            communicate = edge_tts.Communicate(
                text=clean_txt,
                voice=voice,
                rate="+30%",
            )
            await communicate.save(output_path)

            if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                return True

        except Exception as e:
            print("Edge TTS error:", e)
            await asyncio.sleep(1)

    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi",
            "-i", "anullsrc=r=44100:cl=stereo",
            "-t", "1.0",
            output_path,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return True

async def render_precision_stretch_synced_video(
    orig_video_path,
    segments,
    total_duration,
    final_output_video,
    srt_output,
    chat_id,
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
        if chat_id in CANCELLED_TASKS:
            raise RuntimeError("Process stopped by user")

        text = seg.get("burmese_text", "").strip()
        if not text:
            continue

        raw_start = float(seg.get("start", last_vid_point))
        raw_end = float(seg.get("end", raw_start + 2.0))

        raw_start = max(last_vid_point, raw_start)
        raw_end = max(raw_start + 0.05, raw_end)

        if raw_start > last_vid_point + 0.05:
            gap_dur = raw_start - last_vid_point
            gap_v = f"v_gap_{chat_id}_{idx}.mp4"
            gap_a = f"a_gap_{chat_id}_{idx}.wav"

            subprocess.run(
                [
                    "ffmpeg", "-y",
                    "-ss", f"{last_vid_point:.3f}",
                    "-t", f"{gap_dur:.3f}",
                    "-i", orig_video_path,
                    "-c:v", "libx264",
                    "-preset", "ultrafast",
                    "-an", gap_v,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
            )

            subprocess.run(
                [
                    "ffmpeg", "-y",
                    "-f", "lavfi",
                    "-i", "anullsrc=r=44100:cl=stereo",
                    "-t", f"{gap_dur:.3f}",
                    gap_a,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
            )

            temp_files.extend([gap_v, gap_a])
            v_list.write(f"file '{os.path.abspath(gap_v)}'\n")
            a_list.write(f"file '{os.path.abspath(gap_a)}'\n")
            current_timeline_accumulated += gap_dur

        seg_audio_mp3 = f"seg_a_{chat_id}_{idx}.mp3"
        await generate_fixed_130x_speech(text, voice, seg_audio_mp3)
        temp_files.append(seg_audio_mp3)

        seg_audio_wav = f"seg_a_{chat_id}_{idx}.wav"
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", seg_audio_mp3,
                "-ar", "44100",
                "-ac", "2",
                seg_audio_wav,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        temp_files.append(seg_audio_wav)

        actual_audio_dur = get_media_duration(seg_audio_wav)
        orig_scene_dur = max(0.4, raw_end - raw_start)

        pts_factor = actual_audio_dur / orig_scene_dur
        retimed_v = f"v_retimed_{chat_id}_{idx}.mp4"

        subprocess.run(
            [
                "ffmpeg", "-y",
                "-ss", f"{raw_start:.3f}",
                "-t", f"{orig_scene_dur:.3f}",
                "-i", orig_video_path,
                "-filter:v", f"setpts={pts_factor:.6f}*PTS",
                "-c:v", "libx264",
                "-preset", "ultrafast",
                "-an",
                retimed_v,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )

        temp_files.append(retimed_v)
        v_list.write(f"file '{os.path.abspath(retimed_v)}'\n")
        a_list.write(f"file '{os.path.abspath(seg_audio_wav)}'\n")

        st_time = current_timeline_accumulated
        en_time = current_timeline_accumulated + actual_audio_dur

        srt_entries.append((st_time, en_time, text))
        current_timeline_accumulated = en_time
        last_vid_point = raw_end

    if total_duration > last_vid_point + 0.1:
        end_gap_dur = total_duration - last_vid_point
        end_gap_v = f"v_end_gap_{chat_id}.mp4"
        end_gap_a = f"a_end_gap_{chat_id}.wav"

        subprocess.run(
            [
                "ffmpeg", "-y",
                "-ss", f"{last_vid_point:.3f}",
                "-t", f"{end_gap_dur:.3f}",
                "-i", orig_video_path,
                "-c:v", "libx264",
                "-preset", "ultrafast",
                "-an", end_gap_v,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )

        subprocess.run(
            [
                "ffmpeg", "-y",
                "-f", "lavfi",
                "-i", "anullsrc=r=44100:cl=stereo",
                "-t", f"{end_gap_dur:.3f}",
                end_gap_a,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )

        temp_files.extend([end_gap_v, end_gap_a])
        v_list.write(f"file '{os.path.abspath(end_gap_v)}'\n")
        a_list.write(f"file '{os.path.abspath(end_gap_a)}'\n")

    v_list.close()
    a_list.close()

    with open(srt_output, "w", encoding="utf-8") as srt_out:
        for idx, (st, en, txt) in enumerate(srt_entries, 1):
            srt_out.write(
                f"{idx}\n"
                f"{format_srt_time(st)} --> {format_srt_time(en)}\n"
                f"{txt}\n\n"
            )

    temp_full_v = f"full_v_{chat_id}.mp4"
    temp_full_a = f"full_a_{chat_id}.wav"
    temp_files.extend([temp_full_v, temp_full_a])

    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", video_concat_txt,
            "-c:v", "libx264",
            "-preset", "ultrafast",
            temp_full_v,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )

    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", audio_concat_txt,
            "-c", "copy",
            temp_full_a,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )

    # First mux.
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-i", temp_full_v,
            "-i", temp_full_a,
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            final_output_video,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )

    # If Telegram upload would be too large, compress automatically.
    if os.path.getsize(final_output_video) > TELEGRAM_TARGET_BYTES:
        compressed = final_output_video + ".compressed.mp4"

        # CRF 28 is a practical fallback for Telegram.
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", final_output_video,
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-crf", "28",
                "-c:a", "aac",
                "-b:a", "96k",
                "-movflags", "+faststart",
                compressed,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )

        if (
            os.path.exists(compressed)
            and os.path.getsize(compressed) < os.path.getsize(final_output_video)
        ):
            os.replace(compressed, final_output_video)
        elif os.path.exists(compressed):
            os.remove(compressed)

    for f in temp_files:
        if os.path.exists(f):
            try:
                os.remove(f)
            except OSError:
                pass

async def safe_edit_text(message, text: str, reply_markup=None):
    for _ in range(5):
        try:
            await message.edit_text(
                text,
                reply_markup=reply_markup,
                parse_mode="HTML",
            )
            return
        except Exception:
            await asyncio.sleep(1)

async def process_recap_and_reply(update: Update, video_input_path: str, status_msg):
    chat_id = update.effective_chat.id

    temp_extracted_audio = f"extracted_{chat_id}.mp3"
    srt_path = f"dub_{chat_id}.srt"
    final_video_path = f"dub_video_{chat_id}.mp4"

    CANCELLED_TASKS.discard(chat_id)

    try:
        await safe_edit_text(
            status_msg,
            build_status_ui_html(1, 20),
            get_stop_button(chat_id),
        )

        video_duration = get_media_duration(video_input_path)

        subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", video_input_path,
                "-vn",
                "-c:a", "libmp3lame",
                "-q:a", "4",
                temp_extracted_audio,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )

        await safe_edit_text(
            status_msg,
            build_status_ui_html(2, 35),
            get_stop_button(chat_id),
        )

        # Requests/Gemini are blocking, so run them off the bot event loop.
        file_uri = await asyncio.to_thread(
            upload_audio_to_gemini,
            temp_extracted_audio,
        )

        await safe_edit_text(
            status_msg,
            build_status_ui_html(3, 55),
            get_stop_button(chat_id),
        )

        segments = await asyncio.to_thread(
            extract_timestamped_dubbing_json,
            file_uri,
            video_duration,
        )

        await safe_edit_text(
            status_msg,
            build_status_ui_html(4, 75),
            get_stop_button(chat_id),
        )

        await render_precision_stretch_synced_video(
            video_input_path,
            segments,
            video_duration,
            final_video_path,
            srt_path,
            chat_id,
        )

        if chat_id in CANCELLED_TASKS:
            raise RuntimeError("Process stopped by user")

        await safe_edit_text(
            status_msg,
            build_status_ui_html(5, 100),
            get_stop_button(chat_id),
        )

        combined_text = "\n".join(
            s.get("burmese_text", "") for s in segments
        )
        caption = (
            "🎬 <b>AI Complete Burmese Dubbed Video</b>\n\n"
            f"{combined_text[:300]}..."
        )

        file_size = os.path.getsize(final_video_path)
        if file_size > TELEGRAM_MAX_UPLOAD_BYTES:
            raise RuntimeError(
                "Final video size is still above Telegram upload limit."
            )

        with open(final_video_path, "rb") as vf:
            await update.effective_message.reply_video(
                video=vf,
                caption=caption,
                parse_mode="HTML",
                supports_streaming=True,
            )

        with open(srt_path, "rb") as sf:
            await update.effective_message.reply_document(
                document=sf,
                filename="subtitle.srt",
                caption="📄 SRT Subtitle File",
            )

        await status_msg.delete()

    except Exception as e:
        print("PROCESS ERROR:", repr(e))
        if chat_id not in CANCELLED_TASKS:
            await safe_edit_text(
                status_msg,
                build_status_ui_html(1, 0, str(e)),
            )

    finally:
        for f in [
            video_input_path,
            temp_extracted_audio,
            srt_path,
            final_video_path,
        ]:
            if f and os.path.exists(f):
                try:
                    os.remove(f)
                except OSError:
                    pass

async def cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data.startswith("stop_"):
        chat_id = int(query.data.split("_", 1)[1])
        CANCELLED_TASKS.add(chat_id)
        await query.edit_message_text(
            "🛑 <b>Process ကို ရပ်တန့်လိုက်ပါပြီ။</b>",
            parse_mode="HTML",
        )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "မင်္ဂလာပါ! Video File သို့မဟုတ် "
        "Douyin/TikTok/YouTube Link ပို့ပေးပါ။"
    )

async def handle_any_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw_text = (update.message.text or "").strip()
    raw_url = extract_best_video_url(raw_text)

    if not raw_url:
        await update.message.reply_text(
            "❌ Video Link မတွေ့ရှိပါ။ Link သီးသန့် သို့မဟုတ် "
            "Douyin share text အပြည့်အစုံ ပို့ပေးပါ။"
        )
        return

    chat_id = update.effective_chat.id

    status_msg = await update.message.reply_text(
        build_status_ui_html(1, 5),
        reply_markup=get_stop_button(chat_id),
        parse_mode="HTML",
    )

    video_output = f"downloaded_{chat_id}_{int(time.time())}.mp4"

    # Downloader is blocking; do not freeze Telegram polling.
    downloaded_file = await asyncio.to_thread(
        download_video_universal,
        raw_url,
        video_output,
    )

    if not downloaded_file:
        await safe_edit_text(
            status_msg,
            "❌ Video ဒေါင်းလုဒ်မရပါ။\n\n"
            "Douyin link ဖြစ်ပါက public video ဖြစ်ရပါမယ်။ "
            "Deleted/private/region-blocked video များ မရနိုင်ပါ။",
        )
        return

    await process_recap_and_reply(
        update,
        downloaded_file,
        status_msg,
    )

async def handle_video_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    video = update.message.video or update.message.document
    chat_id = update.effective_chat.id

    status_msg = await update.message.reply_text(
        build_status_ui_html(1, 5),
        reply_markup=get_stop_button(chat_id),
        parse_mode="HTML",
    )

    local_video_path = (
        f"tg_video_{chat_id}_{int(time.time())}.mp4"
    )

    success = await download_telegram_large_file_force(
        video,
        context,
        local_video_path,
    )

    if not success:
        await safe_edit_text(
            status_msg,
            "❌ Video File ဒေါင်းလုဒ် မအောင်မြင်ပါ။ "
            "Video Link (Douyin/TikTok/YouTube) ပို့ပေးပါ။",
        )
        return

    await process_recap_and_reply(
        update,
        local_video_path,
        status_msg,
    )

def main():
    threading.Thread(
        target=start_health_server,
        daemon=True,
    ).start()

    app = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .concurrent_updates(True)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(
        CallbackQueryHandler(
            cancel_callback,
            pattern=r"^stop_",
        )
    )
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_any_link,
        )
    )
    app.add_handler(
        MessageHandler(
            filters.VIDEO | filters.Document.VIDEO,
            handle_video_file,
        )
    )

    print("FLOW RECAP BOT is ready...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
