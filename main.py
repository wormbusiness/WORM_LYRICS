import os
import re
import subprocess
import requests
import numpy as np
from pathlib import Path
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from moviepy.editor import AudioFileClip, ImageClip, TextClip, CompositeVideoClip

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
OUTPUT_DIR     = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)

W, H       = 1080, 1920
BG_COLOR   = (15, 15, 15)
HI_COLOR   = "#FF2D55"
DIM_COLOR  = "#888888"
FONT       = "DejaVu-Sans-Bold"
FONTSIZE   = 52

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
def fetch_deezer_preview(query: str) -> tuple[str, str]:
    """Returns (preview_url, track_title) from Deezer."""
    r = requests.get(
        "https://api.deezer.com/search",
        params={"q": query, "limit": 1},
        timeout=15
    )
    r.raise_for_status()
    data = r.json()
    if not data.get("data"):
        raise ValueError("No audio found on Deezer.")
    track = data["data"][0]
    if not track.get("preview"):
        raise ValueError("Deezer track has no preview available.")
    return track["preview"], track["title"]

def download_preview(url: str, out_path: Path) -> Path:
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    out_path.write_bytes(r.content)
    return out_path

# ── Video Rendering ────────────────────────────────────────────────────────────
def make_text_clip(text: str, color: str, duration: float) -> TextClip:
    return (
        TextClip(text, fontsize=FONTSIZE, color=color, font=FONT,
                 size=(W - 80, None), method="caption", align="center")
        .set_duration(duration)
    )

def render_video(audio_path: Path, lyrics: list[dict], out_path: Path) -> Path:
    audio = AudioFileClip(str(audio_path))
    dur   = audio.duration  # ~30s from Deezer

    # Only use lyrics that fall within the preview duration
    visible = [l for l in lyrics if l["t"] < dur]

    bg = ImageClip(np.full((H, W, 3), BG_COLOR, dtype="uint8"), duration=dur)
    clips = [bg]

    for i, line in enumerate(visible):
        start    = line["t"]
        end      = visible[i + 1]["t"] if i + 1 < len(visible) else dur
        line_dur = max(end - start, 0.1)

        active = (
            make_text_clip(line["text"], HI_COLOR, line_dur)
            .set_start(start)
            .set_position(("center", H // 2 - 40))
        )
        clips.append(active)

        if i + 1 < len(visible):
            preview = (
                make_text_clip(visible[i + 1]["text"], DIM_COLOR, line_dur)
                .set_start(start)
                .set_position(("center", H // 2 + 80))
            )
            clips.append(preview)

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

        title    = track.get("trackName", "Unknown")
        artist   = track.get("artistName", "Unknown")
        duration = track.get("duration", 0)
        mins, secs = divmod(int(duration), 60)

        preview = "\n".join(
            f"`[{int(l['t']//60)}:{l['t']%60:05.2f}]` {l['text']}"
            for l in lyrics[:5]
        )

        ctx.user_data["track"]  = track
        ctx.user_data["lyrics"] = lyrics
        ctx.user_data["query"]  = query

        await update.message.reply_text(
            f"✅ *{title}* — {artist}\n"
            f"⏱ {mins}:{secs:02d} | {len(lyrics)} synced lines\n\n"
            f"{preview}\n{'...' if len(lyrics) > 5 else ''}\n\n"
            f"Send /confirm to render a preview video 🎬",
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
        preview_url, _ = fetch_deezer_preview(query)
        download_preview(preview_url, audio_path)

        await update.message.reply_text("🎬 Rendering video (~1 min)...")
        render_video(audio_path, lyrics, video_path)

        await update.message.reply_text("📤 Sending preview...")
        caption = f"{title} — {artist}\n\nSend /post to upload to TikTok or /cancel to discard."
        with open(video_path, "rb") as vf:
            await update.message.reply_video(video=vf, caption=caption, supports_streaming=True)

        ctx.user_data["video_path"] = str(video_path)
        ctx.user_data["caption"]    = f"{title} - {artist} #lyrics #fyp #{artist.replace(' ', '').lower()}"

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
    # TikTok upload will plug in here
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
