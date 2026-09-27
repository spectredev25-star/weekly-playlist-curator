import os
import logging

from dotenv import load_dotenv

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from curator import generate_playlist


# ============================================================
# CONFIGURATION
# ============================================================

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

if not BOT_TOKEN:
    raise RuntimeError(
        "TELEGRAM_BOT_TOKEN is not configured."
    )


# ============================================================
# LOGGING
# ============================================================

logger = logging.getLogger("telegram_bot")


# ============================================================
# CONVERSATION STATES
# ============================================================

GENRE, ARTIST, SIZE = range(3)


# ============================================================
# START
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start."""

    await update.message.reply_text(
        "🎵 *Welcome to Spectre Music Curator!*\n\n"
        "I build a fresh playlist based on your taste "
        "while avoiding recently selected tracks.\n\n"
        "🎧 Use /playlist to create your playlist.\n"
        "ℹ️ Use /help to see available commands.",
        parse_mode="Markdown"
    )


# ============================================================
# HELP
# ============================================================

async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    """Handle /help."""

    await update.message.reply_text(
        "🎵 *Spectre Music Curator*\n\n"
        "/playlist — Create a new playlist\n"
        "/cancel — Cancel the current playlist setup\n"
        "/help — Show this message",
        parse_mode="Markdown"
    )


# ============================================================
# PLAYLIST CONVERSATION
# ============================================================

async def playlist_start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    """Start playlist creation."""

    context.user_data.clear()

    await update.message.reply_text(
        "🎧 Let's build your playlist.\n\n"
        "What genre are you feeling this week?"
    )

    return GENRE


async def receive_genre(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    """Receive the user's preferred genre."""

    genre = update.message.text.strip()

    if not genre:
        await update.message.reply_text(
            "❌ Please enter a genre."
        )
        return GENRE

    context.user_data["genre"] = genre

    await update.message.reply_text(
        "🎤 Nice.\n\n"
        "Which artist would you like me to use as your main artist preference?"
    )

    return ARTIST


async def receive_artist(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    """Receive the user's preferred artist."""

    artist = update.message.text.strip()

    if not artist:
        await update.message.reply_text(
            "❌ Please enter an artist."
        )
        return ARTIST

    context.user_data["artist"] = artist

    await update.message.reply_text(
        "🔢 How many songs should I generate?\n\n"
        "Choose a number from 1 to 50."
    )

    return SIZE


async def receive_size(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    """Receive playlist size and generate playlist."""

    text = update.message.text.strip()

    try:
        playlist_size = int(text)

    except ValueError:
        await update.message.reply_text(
            "❌ Please enter a whole number between 1 and 50."
        )
        return SIZE

    if not 1 <= playlist_size <= 50:
        await update.message.reply_text(
            "❌ Choose a number between 1 and 50."
        )
        return SIZE

    preferences = {
        "genre": context.user_data["genre"],
        "artist": context.user_data["artist"],
        "playlist_size": playlist_size
    }

    await update.message.reply_text(
        "🔎 Searching for tracks...\n"
        "🧠 Scoring your matches...\n"
        "🎵 Building your playlist..."
    )

    try:
        playlist, output_path = generate_playlist(
            preferences
        )

    except Exception:
        logger.exception(
            "Playlist generation failed."
        )

        await update.message.reply_text(
            "❌ Something went wrong while creating "
            "your playlist.\n\n"
            "Check the curator logs for details."
        )

        return ConversationHandler.END

    if not playlist:
        await update.message.reply_text(
            "😕 I couldn't find enough suitable tracks.\n\n"
            "Try another artist or genre."
        )

        return ConversationHandler.END

    # --------------------------------------------------------
    # SEND PLAYLIST
    # --------------------------------------------------------

    message = (
        "🎵 *YOUR WEEKLY PLAYLIST*\n\n"
        f"🎧 Genre: {preferences['genre']}\n"
        f"🎤 Artist: {preferences['artist']}\n"
        f"📀 Tracks: {len(playlist)}\n\n"
    )

    for number, song in enumerate(
        playlist,
        start=1
    ):
        message += (
            f"*{number}. {song['title']}*\n"
            f"   🎤 {song['artist']}\n"
            f"   💿 {song['album']}\n"
            f"   🎼 {song['genre']}\n"
        )

        if song.get("track_url"):
            message += (
                f"   🔗 {song['track_url']}\n"
            )

        message += "\n"

    await update.message.reply_text(
        message,
        parse_mode="Markdown",
        disable_web_page_preview=True
    )

    await update.message.reply_text(
        "✨ Playlist created successfully!\n\n"
        f"💾 Saved locally as:\n"
        f"`{output_path}`\n\n"
        "Use /playlist whenever you want a new one.",
        parse_mode="Markdown"
    )

    return ConversationHandler.END


# ============================================================
# CANCEL
# ============================================================

async def cancel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    """Cancel playlist creation."""

    context.user_data.clear()

    await update.message.reply_text(
        "❌ Playlist creation cancelled.\n\n"
        "Use /playlist whenever you're ready."
    )

    return ConversationHandler.END


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE
):
    """Log Telegram errors."""

    logger.exception(
        "Telegram error:",
        exc_info=context.error
    )


# ============================================================
# BOT SETUP
# ============================================================

def main():

    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .concurrent_updates(False)
        .build()
    )

    playlist_conversation = ConversationHandler(
        entry_points=[
            CommandHandler(
                "playlist",
                playlist_start
            )
        ],

        states={
            GENRE: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    receive_genre
                )
            ],

            ARTIST: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    receive_artist
                )
            ],

            SIZE: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    receive_size
                )
            ],
        },

        fallbacks=[
            CommandHandler(
                "cancel",
                cancel
            )
        ],

        per_user=True,
        per_chat=True
    )

    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        CommandHandler("help", help_command)
    )

    application.add_handler(
        playlist_conversation
    )

    application.add_handler(
        CommandHandler("cancel", cancel)
    )

    application.add_error_handler(
        error_handler
    )

    print("🎵 Spectre Music Curator is running...")

    application.run_polling()


if __name__ == "__main__":
    main()