import os
import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]

# ── Lyrics ────────────────────────────────────────────────────────────────────
def fetch_lyrics(query: str) -> dict:
    """Search lrclib for synced lyrics. Returns best match."""
    r = requests.get(
        "https://lrclib.net/api/search",
        params={"q": query},
        timeout=30
    )
    r.raise_for_status()
    results = r.json()

    if not results:
        raise ValueError("No results found on lrclib.")

    # Prefer results with synced lyrics
    for track in results:
        if track.get("syncedLyrics"):
            return track

    raise ValueError("Found the track but no synced lyrics available.")

def parse_lrc(synced: str) -> list[dict]:
    """Parse .lrc format into list of {t, text} dicts."""
    import re
    lines = []
    for line in synced.strip().split("\n"):
        m = re.match(r"\[(\d+):(\d+\.\d+)\](.*)", line)
        if m:
            mins, secs, text = m.groups()
            ts = int(mins) * 60 + float(secs)
            if text.strip():
                lines.append({"t": round(ts, 2), "text": text.strip()})
    return lines

# ── Telegram Handlers ─────────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎵 Send me a song query, e.g.:\n"
        "`Sabrina Carpenter Espresso`\n"
        "or `Artist - Title`",
        parse_mode="Markdown"
    )

async def handle_query(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.message.text.strip()
    await update.message.reply_text(f"🔍 Searching for: *{query}*...", parse_mode="Markdown")

    try:
        track = fetch_lyrics(query)
        lines = parse_lrc(track["syncedLyrics"])

        title    = track.get("trackName", "Unknown")
        artist   = track.get("artistName", "Unknown")
        duration = track.get("duration", 0)
        mins, secs = divmod(int(duration), 60)

        # Preview first 5 lyric lines
        preview = "\n".join(f"`[{int(l['t']//60)}:{l['t']%60:05.2f}]` {l['text']}" for l in lines[:5])

        msg = (
            f"✅ *{title}* — {artist}\n"
            f"⏱ {mins}:{secs:02d} | {len(lines)} lyric lines\n\n"
            f"{preview}\n"
            f"{'...' if len(lines) > 5 else ''}\n\n"
            f"Ready to render. Reply /confirm to proceed or send a new query."
        )
        # Store match in user context for later
        ctx.user_data["track"]  = track
        ctx.user_data["lyrics"] = lines

        await update.message.reply_text(msg, parse_mode="Markdown")

    except Exception as e:
        await update.message.reply_text(f"❌ {e}")

async def confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    track  = ctx.user_data.get("track")
    lyrics = ctx.user_data.get("lyrics")

    if not track or not lyrics:
        await update.message.reply_text("No song queued. Send a song name first.")
        return

    await update.message.reply_text(
        f"✅ Confirmed: *{track['trackName']}* by *{track['artistName']}*\n"
        f"{len(lyrics)} synced lines ready.\n\n"
        f"_(Rendering pipeline coming next)_",
        parse_mode="Markdown"
    )

# ── Entry ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("confirm", confirm))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_query))
    print("Bot running...")
    app.run_polling()
