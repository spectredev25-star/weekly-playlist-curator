
import json
import logging
import random
import re
import sqlite3
import time

from datetime import date, datetime, timedelta
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    filters,
    ContextTypes,
)

from dotenv import load_dotenv
import os


# ============================================================
# 1. CONFIGURATION & LOGGING
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

DATA_DIR = BASE_DIR / "data"
PLAYLIST_DIR = BASE_DIR / "playlists"
LOG_DIR = BASE_DIR / "logs"

DB_PATH = DATA_DIR / "history.db"
LOG_PATH = LOG_DIR / "curator.log"

# IMPORTANT:
# This must be the iTunes Search API endpoint.
API_URL = "https://itunes.apple.com/search"

MAX_RESULTS_PER_SEARCH = 50
MAX_SEARCH_TERMS = 5
HISTORY_DAYS = 90

# Telegram conversation states
GET_GENRE, GET_ARTIST, GET_SIZE = range(3)

# Load environment variables
load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")


# Create required folders
for folder in (DATA_DIR, PLAYLIST_DIR, LOG_DIR):
    folder.mkdir(parents=True, exist_ok=True)


# ============================================================
# 2. LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger("playlist_curator")


# ============================================================
# 3. API SESSION
# ============================================================

def create_session():
    """Create an HTTP session with automatic retries."""

    session = requests.Session()

    retry_strategy = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=1,
        status_forcelist=[
            429,
            500,
            502,
            503,
            504
        ],
        allowed_methods=["GET"],
        respect_retry_after_header=True
    )

    adapter = HTTPAdapter(
        max_retries=retry_strategy
    )

    session.mount("https://", adapter)
    session.mount("http://", adapter)

    session.headers.update({
        "User-Agent": "WeeklyPlaylistCurator/1.0"
    })

    return session


# ============================================================
# 4. DATABASE
# ============================================================

def get_connection():
    """Open a SQLite database connection."""

    return sqlite3.connect(
        DB_PATH,
        timeout=10
    )


def initialize_database():
    """Create playlist history table if it doesn't exist."""

    with get_connection() as connection:

        connection.execute("""
            CREATE TABLE IF NOT EXISTS playlist_history (
                track_id TEXT PRIMARY KEY,
                track_name TEXT NOT NULL,
                artist_name TEXT NOT NULL,
                genre TEXT,
                playlist_date TEXT NOT NULL
            )
        """)

    logger.info("Database initialized.")


def get_recent_track_ids():
    """Return track IDs selected within the history window."""

    cutoff = (
        date.today()
        - timedelta(days=HISTORY_DAYS)
    ).isoformat()

    with get_connection() as connection:

        rows = connection.execute(
            """
            SELECT track_id
            FROM playlist_history
            WHERE playlist_date >= ?
            """,
            (cutoff,)
        ).fetchall()

    return {
        row[0]
        for row in rows
    }


def save_playlist_history(playlist):
    """Save selected tracks to playlist history."""

    playlist_date = date.today().isoformat()

    with get_connection() as connection:

        connection.executemany(
            """
            INSERT OR IGNORE INTO playlist_history (
                track_id,
                track_name,
                artist_name,
                genre,
                playlist_date
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    str(song["track_id"]),
                    song["title"],
                    song["artist"],
                    song["genre"],
                    playlist_date
                )
                for song in playlist
            ]
        )

    logger.info("Playlist history updated.")


# ============================================================
# 5. API SEARCH
# ============================================================

def search_tracks(session, search_term):
    """Search the iTunes API and return normalized tracks."""

    params = {
        "term": search_term,
        "entity": "song",
        "limit": MAX_RESULTS_PER_SEARCH
    }

    try:

        response = session.get(
            API_URL,
            params=params,
            timeout=(5, 20)
        )

        response.raise_for_status()

        data = response.json()

        if not isinstance(data, dict):
            raise ValueError(
                "Unexpected API response format."
            )

        results = data.get(
            "results",
            []
        )

        if not isinstance(results, list):
            raise ValueError(
                "API results must be a list."
            )

        tracks = []

        for item in results:

            track_id = item.get("trackId")
            title = item.get("trackName")
            artist_name = item.get("artistName")

            # Ignore incomplete API records.
            if not track_id or not title or not artist_name:
                continue

            tracks.append({
                "track_id": str(track_id),
                "title": title,
                "artist": artist_name,
                "album": item.get(
                    "collectionName",
                    "Unknown"
                ),
                "genre": item.get(
                    "primaryGenreName",
                    "Unknown"
                ),
                "release_date": item.get(
                    "releaseDate"
                ),
                "preview_url": item.get(
                    "previewUrl"
                ),
                "track_url": item.get(
                    "trackViewUrl"
                )
            })

        logger.info(
            "Found %d tracks for '%s'.",
            len(tracks),
            search_term
        )

        return tracks

    except requests.exceptions.Timeout as error:

        logger.warning(
            "API timeout for '%s': %s",
            search_term,
            error
        )

        return []

    except requests.exceptions.HTTPError as error:

        logger.error(
            "API HTTP error for '%s': %s",
            search_term,
            error
        )

        return []

    except requests.exceptions.RequestException as error:

        logger.error(
            "API connection error for '%s': %s",
            search_term,
            error
        )

        return []

    except (ValueError, TypeError) as error:

        logger.error(
            "Invalid API response: %s",
            error
        )

        return []


# ============================================================
# 6. FETCH CANDIDATES
# ============================================================

def fetch_candidates(session, preferences):
    """Fetch candidate tracks using multiple search terms."""

    search_terms = [
        preferences["genre"],
        preferences["artist"],
        f'{preferences["artist"]} {preferences["genre"]}'
    ]

    # Remove duplicates while preserving order.
    search_terms = list(
        dict.fromkeys(search_terms)
    )

    search_terms = search_terms[
        :MAX_SEARCH_TERMS
    ]

    candidates = {}

    for term in search_terms:

        logger.info(
            "Searching for: %s",
            term
        )

        tracks = search_tracks(
            session,
            term
        )

        for track in tracks:

            candidates[
                track["track_id"]
            ] = track

        # Small delay between API requests.
        time.sleep(0.2)

    logger.info(
        "Retrieved %d unique candidate tracks.",
        len(candidates)
    )

    return list(
        candidates.values()
    )


# ============================================================
# 7. NORMALIZATION
# ============================================================

def normalize(value):
    """Normalize text for comparison."""

    if not value:
        return ""

    return re.sub(
        r"\s+",
        " ",
        str(value).strip().casefold()
    )


# ============================================================
# 8. RECOMMENDATION ENGINE
# ============================================================

def score_and_filter_tracks(
    candidates,
    preferences,
    history_ids
):
    """Score candidate tracks and remove recent tracks."""

    filtered_candidates = []

    target_artist = normalize(
        preferences["artist"]
    )

    target_genre = normalize(
        preferences["genre"]
    )

    for track in candidates:

        # Avoid recently selected tracks.
        if track["track_id"] in history_ids:
            continue

        score = 0

        track_artist = normalize(
            track["artist"]
        )

        track_genre = normalize(
            track["genre"]
        )

        # Artist match
        if (
            target_artist in track_artist
            or track_artist in target_artist
        ):
            score += 5

        # Genre match
        if (
            target_genre in track_genre
            or track_genre in target_genre
        ):
            score += 3

        # Small randomness so every playlist
        # isn't identical.
        score += random.uniform(0, 1)

        filtered_candidates.append(
            (score, track)
        )

    # Highest scores first.
    filtered_candidates.sort(
        key=lambda x: x[0],
        reverse=True
    )

    return [
        track
        for _, track in filtered_candidates
    ]


# ============================================================
# 9. SAVE PLAYLIST FILE
# ============================================================

def generate_playlist_file(playlist):
    """Save playlist to a JSON file."""

    timestamp = datetime.now().strftime(
        "%Y%m%d_%H%M%S"
    )

    file_path = (
        PLAYLIST_DIR
        / f"playlist_{timestamp}.json"
    )

    with file_path.open(
        "w",
        encoding="utf-8"
    ) as file:

        json.dump(
            playlist,
            file,
            indent=4,
            ensure_ascii=False
        )

    logger.info(
        "Playlist saved to %s",
        file_path
    )

    return file_path


# ============================================================
# 10. TELEGRAM /start
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Show the bot welcome message."""

    await update.message.reply_text(
        "🎵 *Welcome to Spectre Music Curator!*\n\n"
        "I create a fresh playlist based on your "
        "music preferences while avoiding recently "
        "selected tracks.\n\n"
        "🎧 Use /playlist to create a playlist.\n"
        "ℹ️ Use /help to see available commands.",
        parse_mode="Markdown"
    )

    return ConversationHandler.END


# ============================================================
# 11. TELEGRAM /help
# ============================================================

async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    """Display available commands."""

    await update.message.reply_text(
        "🎵 *Spectre Music Curator*\n\n"
        "/start — Open the bot\n"
        "/playlist — Create a playlist\n"
        "/cancel — Cancel playlist creation\n"
        "/help — Show this help message",
        parse_mode="Markdown"
    )


# ============================================================
# 12. START PLAYLIST CONVERSATION
# ============================================================

async def playlist_start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Start the playlist creation workflow."""

    context.user_data.clear()

    await update.message.reply_text(
        "🎧 *Let's build your playlist!*\n\n"
        "What genre are you feeling this week?",
        parse_mode="Markdown"
    )

    return GET_GENRE


# ============================================================
# 13. GET GENRE
# ============================================================

async def get_genre(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Store the user's genre."""

    genre = update.message.text.strip()

    if not genre:

        await update.message.reply_text(
            "❌ Please enter a genre."
        )

        return GET_GENRE

    context.user_data["genre"] = genre

    await update.message.reply_text(
        "🎤 Great!\n\n"
        "Which artist are you feeling?"
    )

    return GET_ARTIST


# ============================================================
# 14. GET ARTIST
# ============================================================

async def get_artist(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Store the user's preferred artist."""

    artist = update.message.text.strip()

    if not artist:

        await update.message.reply_text(
            "❌ Please enter an artist."
        )

        return GET_ARTIST

    context.user_data["artist"] = artist

    await update.message.reply_text(
        "🔢 How many songs would you like?\n\n"
        "Enter a number between 1 and 50."
    )

    return GET_SIZE


# ============================================================
# 15. GENERATE PLAYLIST
# ============================================================

async def generate_playlist(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Generate and send the playlist."""

    size_input = update.message.text.strip()

    # --------------------------------------------------------
    # VALIDATE PLAYLIST SIZE
    # --------------------------------------------------------

    try:

        playlist_size = int(size_input)

    except ValueError:

        await update.message.reply_text(
            "❌ Please enter a valid whole number "
            "between 1 and 50."
        )

        return GET_SIZE

    if not 1 <= playlist_size <= 50:

        await update.message.reply_text(
            "❌ Playlist size must be between "
            "1 and 50."
        )

        return GET_SIZE

    context.user_data["playlist_size"] = playlist_size

    status_message = await update.message.reply_text(
        "🔍 Searching for tracks...\n"
        "🧠 Matching your preferences...\n"
        "🎵 Building your playlist..."
    )

    session = None

    try:

        # ----------------------------------------------------
        # BUILD CLEAN PREFERENCES DICTIONARY
        # ----------------------------------------------------

        preferences = {
            "genre": context.user_data["genre"],
            "artist": context.user_data["artist"],
            "playlist_size": playlist_size
        }

        # ----------------------------------------------------
        # CREATE API SESSION
        # ----------------------------------------------------

        session = create_session()

        # ----------------------------------------------------
        # SEARCH
        # ----------------------------------------------------

        candidates = fetch_candidates(
            session,
            preferences
        )

        if not candidates:

            await status_message.edit_text(
                "❌ I couldn't find any tracks "
                "matching those preferences.\n\n"
                "Try another artist or genre."
            )

            return ConversationHandler.END

        # ----------------------------------------------------
        # HISTORY
        # ----------------------------------------------------

        history_ids = get_recent_track_ids()

        # ----------------------------------------------------
        # SCORE AND FILTER
        # ----------------------------------------------------

        ranked_tracks = score_and_filter_tracks(
            candidates,
            preferences,
            history_ids
        )

        if not ranked_tracks:

            await status_message.edit_text(
                "😕 All matching tracks were selected "
                "within the last 90 days.\n\n"
                "Try another artist or genre."
            )

            return ConversationHandler.END

        # ----------------------------------------------------
        # SELECT PLAYLIST
        # ----------------------------------------------------

        size = min(
            playlist_size,
            len(ranked_tracks)
        )

        final_playlist = ranked_tracks[:size]

        # ----------------------------------------------------
        # SAVE PLAYLIST FILE FIRST
        # ----------------------------------------------------

        file_path = generate_playlist_file(
            final_playlist
        )

        # ----------------------------------------------------
        # THEN UPDATE HISTORY
        # ----------------------------------------------------

        save_playlist_history(
            final_playlist
        )

        # ----------------------------------------------------
        # BUILD TELEGRAM RESPONSE
        # ----------------------------------------------------

        response_text = (
            f"🎉 *Your Playlist Is Ready!*\n\n"
            f"🎧 Genre: {preferences['genre']}\n"
            f"🎤 Artist: {preferences['artist']}\n"
            f"📀 Tracks: {size}\n\n"
        )

        for idx, song in enumerate(
            final_playlist[:15],
            start=1
        ):

            response_text += (
                f"{idx}. *{song['title']}*\n"
                f"   🎤 {song['artist']}\n"
            )

            if song.get("track_url"):
                response_text += (
                    f"   🔗 {song['track_url']}\n"
                )

            response_text += "\n"

        if size > 15:

            response_text += (
                f"...and {size - 15} more tracks "
                "are included in the JSON file."
            )

        # ----------------------------------------------------
        # SEND PLAYLIST SUMMARY
        # ----------------------------------------------------

        await status_message.edit_text(
            response_text,
            parse_mode="Markdown",
            disable_web_page_preview=True
        )

        # ----------------------------------------------------
        # SEND JSON FILE
        # ----------------------------------------------------

        with file_path.open(
            "rb"
        ) as playlist_file:

            await update.message.reply_document(
                document=playlist_file,
                filename=file_path.name,
                caption="🎵 Your playlist data file."
            )

        logger.info(
            "Playlist successfully generated for Telegram user %s.",
            update.effective_user.id
        )

    except Exception as error:

        logger.exception(
            "Error in playlist generation: %s",
            error
        )

        await status_message.edit_text(
            "❌ Something went wrong while creating "
            "your playlist.\n\n"
            "The error has been recorded in the log."
        )

    finally:

        # Always close the HTTP session.
        if session is not None:
            session.close()

        # Clear temporary conversation data.
        context.user_data.clear()

    return ConversationHandler.END


# ============================================================
# 16. CANCEL
# ============================================================

async def cancel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Cancel playlist creation."""

    context.user_data.clear()

    await update.message.reply_text(
        "❌ Playlist creation cancelled.\n\n"
        "Use /playlist whenever you're ready."
    )

    return ConversationHandler.END


# ============================================================
# 17. TELEGRAM ERROR HANDLER
# ============================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE
):
    """Log unexpected Telegram errors."""

    logger.error(
        "Telegram error: %s",
        context.error,
        exc_info=context.error
    )


# ============================================================
# 18. BOT APPLICATION
# ============================================================

def main():
    """Start the Telegram bot."""

    if not BOT_TOKEN:

        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is missing. "
            "Add it to your .env file."
        )

    # Initialize database before starting bot.
    initialize_database()

    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .concurrent_updates(False)
        .build()
    )

    # --------------------------------------------------------
    # PLAYLIST CONVERSATION
    # --------------------------------------------------------

    playlist_conversation = ConversationHandler(
        entry_points=[
            CommandHandler(
                "playlist",
                playlist_start
            )
        ],

        states={

            GET_GENRE: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    get_genre
                )
            ],

            GET_ARTIST: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    get_artist
                )
            ],

            GET_SIZE: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    generate_playlist
                )
            ]
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

    # --------------------------------------------------------
    # COMMAND HANDLERS
    # --------------------------------------------------------

    application.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    application.add_handler(
        CommandHandler(
            "help",
            help_command
        )
    )

    application.add_handler(
        playlist_conversation
    )

    application.add_handler(
        CommandHandler(
            "cancel",
            cancel
        )
    )

    # --------------------------------------------------------
    # ERROR HANDLER
    # --------------------------------------------------------

    application.add_error_handler(
        error_handler
    )

    # --------------------------------------------------------
    # START BOT
    # --------------------------------------------------------

    logger.info(
        "Spectre Music Curator Telegram bot is starting..."
    )

    print(
        "🎵 Spectre Music Curator is running..."
    )

    application.run_polling()


# ============================================================
# 19. APPLICATION ENTRY POINT
# ============================================================

if __name__ == "__main__":
    main()
