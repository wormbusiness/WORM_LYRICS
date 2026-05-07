import os
import re
import subprocess
import requests
import numpy as np
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
from moviepy.editor import AudioFileClip, ImageClip, CompositeVideoClip
import imageio_ffmpeg
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ConversationHandler, filters, ContextTypes
)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
OUTPUT_DIR     = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)

FFMPEG_BIN = imageio_ffmpeg.get_ffmpeg_exe()

def find_bin(name: str) -> str:
    result = subprocess.run(["which", name], capture_output=True, text=True)
    if result.returncode == 0:
        return result.stdout.strip()
    for path in [
        f"/nix/var/nix/profiles/default/bin/{name}",
        f"/run/current-system/sw/bin/{name}",
        f"/usr/bin/{name}",
        f"/usr/local/bin/{name}",
    ]:
        if Path(path).exists():
            return path
    # Search nix store directly
    nix = subprocess.run(["find", "/nix/store", "-name", name, "-type", "f"],
                         capture_output=True, text=True)
    hits = [l for l in nix.stdout.splitlines() if "/bin/" in l]
    if hits:
        return hits[0]
    return name

W, H      = 1080, 1920
BG_COLOR  = (15, 15, 15)
HI_COLOR  = (255, 45, 85)
DIM_COLOR = (136, 136, 136)
FONTSIZE  = 52
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

# Conversation states
WAITING_RANGE = 1

# ── Helpers ────────────────────────────────────────────────────────────────────
def parse_timestamp(ts: str) -> float:
    """Convert m:ss or mm:ss to seconds."""
    parts = ts.strip().split(":")
    return int(parts[0]) * 60 + float(parts[1])

def parse_range(text: str) -> tuple[float, float]:
    """Parse '0:45-1:15' into (45.0, 75.0)."""
    m = re.match(r"(\d+:\d+(?:\.\d+)?)\s*[-–]\s*(\d+:\d+(?:\.\d+)?)", text.strip())
    if not m:
        raise ValueError("Format must be `m:ss-m:ss`, e.g. `0:45-1:15`")
    start = parse_timestamp(m.group(1))
    end   = parse_timestamp(m.group(2))
    if end <= start:
        raise ValueError("End time must be after start time.")
    if end - start > 60:
        raise ValueError("Max clip length is 60 seconds.")
    return start, end

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

# ── YouTube Audio ──────────────────────────────────────────────────────────────
def download_youtube_slice(query: str, start: float, end: float, out_path: Path) -> Path:
    """Download only the needed slice from YouTube using yt-dlp + system ffmpeg."""
    tmp    = out_path.with_suffix(".raw.mp3")
    ffmpeg = find_bin("ffmpeg")
    deno   = find_bin("deno")

    cmd = [
        "python", "-m", "yt_dlp",
        f"ytsearch1:{query}",
        "--extract-audio",
        "--audio-format", "mp3",
        "--audio-quality", "0",
        "--download-sections", f"*{start}-{end}",
        "--force-keyframes-at-cuts",
        "--no-playlist",
        "--ffmpeg-location", ffmpeg,
        "--cookies", "yt_cookies.txt",
        "--js-runtimes", f"deno:{deno}",
        "-o", str(tmp),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise ValueError(f"yt-dlp error: {result.stderr[-400:]}")

    duration = end - start
    trim_cmd = [ffmpeg, "-y", "-i", str(tmp), "-t", str(duration), "-acodec", "copy", str(out_path)]
    subprocess.run(trim_cmd, capture_output=True)
    if tmp.exists():
        tmp.unlink()
    return out_path

# ── Text Rendering ─────────────────────────────────────────────────────────────
def load_font(size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(FONT_PATH, size)
    except Exception:
        return ImageFont.load_default()

def wrap_text(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    words = text.split()
    lines, current = [], ""
    for word in words:
        test = f"{current} {word}".strip()
        if font.getbbox(test)[2] <= max_width:
            current = test
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines

def make_frame(active: str, upcoming: str | None) -> np.ndarray:
    img  = Image.new("RGB", (W, H), BG_COLOR)
    draw = ImageDraw.Draw(img)
    font = load_font(FONTSIZE)
    dim  = load_font(int(FONTSIZE * 0.85))
    margin, max_w = 60, W - 120

    a_lines = wrap_text(active, font, max_w)
    line_h  = FONTSIZE + 10
    y = H // 2 - (len(a_lines) * line_h) // 2 - 60
    for ln in a_lines:
        x = (W - font.getbbox(ln)[2]) // 2
        draw.text((x, y), ln, font=font, fill=HI_COLOR)
        y += line_h

    if upcoming:
        y += 30
        for ln in wrap_text(upcoming, dim, max_w):
            x = (W - dim.getbbox(ln)[2]) // 2
            draw.text((x, y), ln, font=dim, fill=DIM_COLOR)
            y += int(FONTSIZE * 0.85) + 8

    return np.array(img)

# ── Video Rendering ────────────────────────────────────────────────────────────
def render_video(audio_path: Path, lyrics: list[dict], start: float, out_path: Path) -> Path:
    audio   = AudioFileClip(str(audio_path))
    dur     = audio.duration

    # Shift lyrics to be relative to clip start
    visible = [
        {"t": max(l["t"] - start, 0), "text": l["text"]}
        for l in lyrics
        if start <= l["t"] < start + dur + 2
    ]

    clips = []
    for i, line in enumerate(visible):
        ls    = line["t"]
        le    = visible[i + 1]["t"] if i + 1 < len(visible) else dur
        ld    = max(le - ls, 0.1)
        if ls >= dur:
            continue
        upcoming = visible[i + 1]["text"] if i + 1 < len(visible) else None
        frame = make_frame(line["text"], upcoming)
        clips.append(ImageClip(frame).set_start(ls).set_duration(ld))

    if not clips:
        raise ValueError("No lyrics found in the selected time range.")

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
            f"`[{int(l['t']//60)}:{int(l['t']%60):02d}]` {l['text']}"
            for l in lyrics[:8]
        )
        ctx.user_data.update({"track": track, "lyrics": lyrics, "query": query})
        await update.message.reply_text(
            f"✅ *{title}* — {artist}\n⏱ Full song: {mins}:{secs:02d}\n\n"
            f"*First lines:*\n{preview}\n{'...' if len(lyrics) > 8 else ''}\n\n"
            f"Reply with the time range you want, e.g. `0:45-1:15`",
            parse_mode="Markdown"
        )
        return WAITING_RANGE
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")
        return ConversationHandler.END

async def handle_range(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    track  = ctx.user_data.get("track")
    lyrics = ctx.user_data.get("lyrics")
    query  = ctx.user_data.get("query", "")

    try:
        start, end = parse_range(update.message.text)
    except ValueError as e:
        await update.message.reply_text(f"❌ {e}\nTry again, e.g. `0:45-1:15`", parse_mode="Markdown")
        return WAITING_RANGE

    title  = track.get("trackName", "unknown")
    artist = track.get("artistName", "unknown")
    slug   = re.sub(r"[^\w]", "_", f"{artist}_{title}")[:40]
    audio_path = OUTPUT_DIR / f"{slug}.mp3"
    video_path = OUTPUT_DIR / f"{slug}.mp4"

    sm, ss = divmod(int(start), 60)
    em, es = divmod(int(end), 60)
    await update.message.reply_text(
        f"⏱ Clipping `{sm}:{ss:02d}` → `{em}:{es:02d}` ({int(end-start)}s)\n🎧 Downloading audio...",
        parse_mode="Markdown"
    )

    try:
        download_youtube_slice(query, start, end, audio_path)
        await update.message.reply_text("🎬 Rendering video...")
        render_video(audio_path, lyrics, start, video_path)

        await update.message.reply_text("📤 Sending preview...")
        with open(video_path, "rb") as vf:
            await update.message.reply_video(
                video=vf,
                caption=f"*{title}* — {artist}\n\n/post to upload • /cancel to discard",
                supports_streaming=True,
                parse_mode="Markdown"
            )
        ctx.user_data["video_path"]      = str(video_path)
        ctx.user_data["tiktok_caption"]  = f"{title} - {artist} #lyrics #fyp #{artist.replace(' ', '').lower()}"
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")
    finally:
        if audio_path.exists():
            audio_path.unlink()

    return ConversationHandler.END

async def post(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    video_path = ctx.user_data.get("video_path")
    if not video_path or not Path(video_path).exists():
        await update.message.reply_text("No video ready. Send a song first.")
        return
    await update.message.reply_text("🚀 TikTok upload coming next!")

async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    vp = ctx.user_data.get("video_path")
    if vp and Path(vp).exists():
        Path(vp).unlink()
    ctx.user_data.clear()
    await update.message.reply_text("🗑 Discarded. Send a new song anytime.")
    return ConversationHandler.END

# ── Entry ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & ~filters.COMMAND, handle_query)],
        states={WAITING_RANGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_range)]},
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start",  start))
    app.add_handler(CommandHandler("post",   post))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(conv)
    print("Bot running...")
    app.run_polling()
