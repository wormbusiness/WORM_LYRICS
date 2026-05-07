import os
import re
import json
import requests
import subprocess
from pathlib import Path
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from moviepy.editor import AudioFileClip, ImageClip, TextClip, CompositeVideoClip
from tiktok_uploader.upload import upload_video

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
OUTPUT_DIR     = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)

# ── Lyrics ────────────────────────────────────────────────────────────────────
def fetch_lyrics(title: str, artist: str) -> list[dict]:
    """Returns list of {timestamp, text} dicts from lrclib."""
    for attempt in range(3):
        try:
            r = requests.get("https://lrclib.net/api/search", params={"q": f"{artist} {title}"}, timeout=30)
            break
        except requests.Timeout:
            if attempt == 2:
                raise ValueError("Lyrics service timed out after 3 attempts. Try again.")
    
    r.raise_for_status()
    results = r.json()
    if not results:
        raise ValueError("No lyrics found.")

    synced = results[0].get("syncedLyrics")
    if not synced:
        raise ValueError("No synced lyrics available for this track.")

    lines = []
    for line in synced.strip().split("\n"):
        m = re.match(r"\[(\d+):(\d+\.\d+)\](.*)", line)
        if m:
            mins, secs, text = m.groups()
            ts = int(mins) * 60 + float(secs)
            if text.strip():
                lines.append({"t": ts, "text": text.strip()})
    return lines

# ── Audio ─────────────────────────────────────────────────────────────────────
def download_audio(query: str, out_path: Path) -> Path:
    """Downloads best audio via yt-dlp and converts to mp3."""
    # yt-dlp adds extension itself, so strip it from the template
    template = str(out_path).replace(".mp3", ".%(ext)s")
    cmd = [
        "yt-dlp",
        f"ytsearch1:{query}",
        "--extract-audio",
        "--audio-format", "mp3",
        "--audio-quality", "0",
        "-o", template,
        "--no-playlist",
        "--no-check-certificate",
        "--retries", "5",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise ValueError(f"yt-dlp failed: {result.stderr[-300:]}")
    return out_path

# ── Video Rendering ───────────────────────────────────────────────────────────
BG_COLOR   = (15, 15, 15)       # near-black background
TEXT_COLOR = "white"
HI_COLOR   = "#FF2D55"          # TikTok red for active line
FONT       = "DejaVu-Sans-Bold"
FONTSIZE   = 52
W, H       = 1080, 1920         # 9:16 portrait

def make_text_clip(text: str, color: str, duration: float) -> TextClip:
    return (
        TextClip(text, fontsize=FONTSIZE, color=color, font=FONT,
                 size=(W - 80, None), method="caption", align="center")
        .set_duration(duration)
    )

def render_video(audio_path: Path, lyrics: list[dict], out_path: Path) -> Path:
    audio  = AudioFileClip(str(audio_path))
    dur    = audio.duration

    # Background
    bg = ImageClip(
        __import__("numpy").full((H, W, 3), BG_COLOR, dtype="uint8"),
        duration=dur
    )

    clips = [bg]

    # Lyrics clips — show current line + dim next line
    for i, line in enumerate(lyrics):
        start = line["t"]
        end   = lyrics[i + 1]["t"] if i + 1 < len(lyrics) else dur
        line_dur = max(end - start, 0.1)

        # Active line (centered, bright)
        active = (
            make_text_clip(line["text"], HI_COLOR, line_dur)
            .set_start(start)
            .set_position(("center", H // 2 - 40))
        )
        clips.append(active)

        # Next line preview (dimmed, below)
        if i + 1 < len(lyrics):
            preview = (
                make_text_clip(lyrics[i + 1]["text"], "#888888", line_dur)
                .set_start(start)
                .set_position(("center", H // 2 + 80))
            )
            clips.append(preview)

    video = CompositeVideoClip(clips, size=(W, H)).set_audio(audio)

    # Trim to 60s max (TikTok sweet spot)
    video = video.subclip(0, min(dur, 60))

    video.write_videofile(
        str(out_path),
        fps=30,
        codec="libx264",
        audio_codec="aac",
        threads=2,
        preset="ultrafast",   # faster render on free VPS
        logger=None,
    )
    return out_path

# ── TikTok Upload ─────────────────────────────────────────────────────────────
def post_to_tiktok(video_path: Path, caption: str):
    upload_video(
        str(video_path),
        description=caption,
        cookies="cookies.json",   # full cookies file in project root
        headless=True,
    )

# ── Telegram Bot ──────────────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎵 Send me a song like:\n`Artist - Song Title`",
        parse_mode="Markdown"
    )

async def handle_song(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()

    # Expect "Artist - Title" format
    if " - " not in raw:
        await update.message.reply_text("Format: `Artist - Song Title`", parse_mode="Markdown")
        return

    artist, title = [p.strip() for p in raw.split(" - ", 1)]
    slug = re.sub(r"[^\w]", "_", f"{artist}_{title}")[:40]

    audio_path = OUTPUT_DIR / f"{slug}.mp3"
    video_path = OUTPUT_DIR / f"{slug}.mp4"

    await update.message.reply_text(f"⏳ Processing *{title}* by *{artist}*...", parse_mode="Markdown")

    try:
        # 1. Lyrics
        await update.message.reply_text("📝 Fetching lyrics...")
        lyrics = fetch_lyrics(title, artist)

        # 2. Audio
        await update.message.reply_text("🎧 Downloading audio...")
        download_audio(f"{artist} {title}", audio_path)

        # 3. Render
        await update.message.reply_text("🎬 Rendering video (this takes ~2 min)...")
        render_video(audio_path, lyrics, video_path)

        # 4. Upload
        await update.message.reply_text("🚀 Uploading to TikTok...")
        caption = f"{title} - {artist} #lyrics #fyp #{artist.replace(' ','')}".lower()
        post_to_tiktok(video_path, caption)

        await update.message.reply_text(f"✅ Posted! *{title}* is live on TikTok.", parse_mode="Markdown")

    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

    finally:
        # Cleanup
        for f in [audio_path, video_path]:
            if f.exists():
                f.unlink()

# ── Entry ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_song))
    print("Bot running...")
    app.run_polling()
