import os
import re
import requests
import numpy as np
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
from moviepy.editor import AudioFileClip, ImageClip, CompositeVideoClip
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
OUTPUT_DIR     = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)

W, H       = 1080, 1920
BG_COLOR   = (15, 15, 15)
HI_COLOR   = (255, 45, 85)    # TikTok red
DIM_COLOR  = (136, 136, 136)
FONTSIZE   = 52
FONT_PATH  = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

# ── Lyrics ─────────────────────────────────────────────────────────────────────
def fetch_lyrics(query: str) -> dict:
    r = requests.get("https://lrclib.net/api/search", params={"q": query}, timeout=30)
    r.raise_for_status()
    results = r.json()
    if not results:
        raise ValueError("No results found on lrclib.")
    for track in results:
        if track.get("syncedLyrics"):
            return track
    raise ValueError("Found the track but no synced lyrics available.")

def parse_lrc(synced: str) -> list[dict]:
    lines = []
    for line in synced.strip().split("\n"):
        m = re.match(r"\[(\d+):(\d+\.\d+)\](.*)", line)
        if m:
            mins, secs, text = m.groups()
            ts = int(mins) * 60 + float(secs)
            if text.strip():
                lines.append({"t": round(ts, 2), "text": text.strip()})
    return lines

# ── Deezer Audio ───────────────────────────────────────────────────────────────
def fetch_deezer_preview(query: str) -> str:
    r = requests.get("https://api.deezer.com/search", params={"q": query, "limit": 1}, timeout=15)
    r.raise_for_status()
    data = r.json()
    if not data.get("data"):
        raise ValueError("No audio found on Deezer.")
    track = data["data"][0]
    if not track.get("preview"):
        raise ValueError("Deezer track has no preview available.")
    return track["preview"]

def download_preview(url: str, out_path: Path) -> Path:
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    out_path.write_bytes(r.content)
    return out_path

# ── Text Rendering with Pillow ─────────────────────────────────────────────────
def load_font(size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(FONT_PATH, size)
    except Exception:
        return ImageFont.load_default()

def wrap_text(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    """Word-wrap text to fit within max_width pixels."""
    words = text.split()
    lines, current = [], ""
    for word in words:
        test = f"{current} {word}".strip()
        bbox = font.getbbox(test)
        if bbox[2] <= max_width:
            current = test
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines

def make_frame(active: str, upcoming: str | None) -> np.ndarray:
    """Render a single video frame as numpy array."""
    img  = Image.new("RGB", (W, H), BG_COLOR)
    draw = ImageDraw.Draw(img)
    font = load_font(FONTSIZE)
    dim  = load_font(int(FONTSIZE * 0.85))

    margin = 60
    max_w  = W - margin * 2

    # Active lyric line (center)
    a_lines = wrap_text(active, font, max_w)
    line_h  = FONTSIZE + 10
    total_h = len(a_lines) * line_h
    y = H // 2 - total_h // 2 - 60
    for ln in a_lines:
        bbox = font.getbbox(ln)
        x = (W - (bbox[2] - bbox[0])) // 2
        draw.text((x, y), ln, font=font, fill=HI_COLOR)
        y += line_h

    # Upcoming line (dimmed, below)
    if upcoming:
        y += 30
        for ln in wrap_text(upcoming, dim, max_w):
            bbox = dim.getbbox(ln)
            x = (W - (bbox[2] - bbox[0])) // 2
            draw.text((x, y), ln, font=dim, fill=DIM_COLOR)
            y += int(FONTSIZE * 0.85) + 8

    return np.array(img)

# ── Video Rendering ────────────────────────────────────────────────────────────
def render_video(audio_path: Path, lyrics: list[dict], out_path: Path) -> Path:
    audio   = AudioFileClip(str(audio_path))
    dur     = audio.duration
    visible = [l for l in lyrics if l["t"] < dur]

    clips = []
    for i, line in enumerate(visible):
        start    = line["t"]
        end      = visible[i + 1]["t"] if i + 1 < len(visible) else dur
        line_dur = max(end - start, 0.1)
        upcoming = visible[i + 1]["text"] if i + 1 < len(visible) else None

        frame = make_frame(line["text"], upcoming)
        clip  = ImageClip(frame).set_start(start).set_duration(line_dur)
        clips.append(clip)

    video = CompositeVideoClip(clips, size=(W, H)).set_audio(audio)
    video.write_videofile(
        str(out_path), fps=30, codec="libx264",
        audio_codec="aac", threads=2, preset="ultrafast", logger=None,
    )
    return out_path

# ── Telegram Handlers ──────────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎵 Send me a song, e.g.:\n`Sabrina Carpenter Espresso`",
        parse_mode="Markdown"
    )

async def handle_query(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.message.text.strip()
    await update.message.reply_text(f"🔍 Searching: *{query}*...", parse_mode="Markdown")
    try:
        track  = fetch_lyrics(query)
        lyrics = parse_lrc(track["syncedLyrics"])
        title  = track.get("trackName", "Unknown")
        artist = track.get("artistName", "Unknown")
        dur    = track.get("duration", 0)
        mins, secs = divmod(int(dur), 60)
        preview = "\n".join(
            f"`[{int(l['t']//60)}:{l['t']%60:05.2f}]` {l['text']}"
            for l in lyrics[:5]
        )
        ctx.user_data.update({"track": track, "lyrics": lyrics, "query": query})
        await update.message.reply_text(
            f"✅ *{title}* — {artist}\n⏱ {mins}:{secs:02d} | {len(lyrics)} synced lines\n\n"
            f"{preview}\n{'...' if len(lyrics) > 5 else ''}\n\n"
            f"Send /confirm to render a preview 🎬",
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")

async def confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    track  = ctx.user_data.get("track")
    lyrics = ctx.user_data.get("lyrics")
    query  = ctx.user_data.get("query", "")
    if not track or not lyrics:
        await update.message.reply_text("No song queued. Send a song name first.")
        return

    title  = track.get("trackName", "unknown")
    artist = track.get("artistName", "unknown")
    slug   = re.sub(r"[^\w]", "_", f"{artist}_{title}")[:40]
    audio_path = OUTPUT_DIR / f"{slug}.mp3"
    video_path = OUTPUT_DIR / f"{slug}.mp4"

    try:
        await update.message.reply_text("🎧 Fetching Deezer audio...")
        url = fetch_deezer_preview(query)
        download_preview(url, audio_path)

        await update.message.reply_text("🎬 Rendering video (~1 min)...")
        render_video(audio_path, lyrics, video_path)

        await update.message.reply_text("📤 Sending preview...")
        with open(video_path, "rb") as vf:
            await update.message.reply_video(
                video=vf,
                caption=f"*{title}* — {artist}\n\n/post to upload • /cancel to discard",
                supports_streaming=True,
                parse_mode="Markdown"
            )
        ctx.user_data["video_path"] = str(video_path)
        ctx.user_data["tiktok_caption"] = f"{title} - {artist} #lyrics #fyp #{artist.replace(' ', '').lower()}"
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")
    finally:
        if audio_path.exists():
            audio_path.unlink()

async def post(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    video_path = ctx.user_data.get("video_path")
    if not video_path or not Path(video_path).exists():
        await update.message.reply_text("No video ready. Run /confirm first.")
        return
    await update.message.reply_text("🚀 TikTok upload coming in next step!")

async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    vp = ctx.user_data.get("video_path")
    if vp and Path(vp).exists():
        Path(vp).unlink()
    ctx.user_data.clear()
    await update.message.reply_text("🗑 Discarded. Send a new song anytime.")

# ── Entry ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",   start))
    app.add_handler(CommandHandler("confirm", confirm))
    app.add_handler(CommandHandler("post",    post))
    app.add_handler(CommandHandler("cancel",  cancel))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_query))
    print("Bot running...")
    app.run_polling()
