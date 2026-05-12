import asyncio
import logging
import os
import re
import sys
from pathlib import Path

from telegram import BotCommand, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from telegram.request import HTTPXRequest

# ---------------------------------------------------------------------------
# Bootstrap: add sibling directory to sys.path so local modules are importable
# ---------------------------------------------------------------------------
BRIDGE_DIR = Path(__file__).parent
sys.path.insert(0, str(BRIDGE_DIR))

from block_patterns import check_command          # noqa: E402
from formatter import format_and_chunk            # noqa: E402
from opencode_client import OpenCodeClient, extract_text_response  # noqa: E402
from session_store import SessionStore            # noqa: E402

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("opencode-bridge")

# ---------------------------------------------------------------------------
# Config from .env (simple parser — no external deps needed)
# ---------------------------------------------------------------------------
def _load_env(path: Path) -> None:
    if not path.exists():
        return
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env(BRIDGE_DIR / ".env")

TELEGRAM_BOT_TOKEN: str = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_USERS: set[int] = {
    int(uid.strip())
    for uid in os.environ.get("TELEGRAM_ALLOWED_USERS", "").split(",")
    if uid.strip()
}
OPENCODE_HOST: str = os.environ.get("OPENCODE_HOST", "127.0.0.1")
OPENCODE_PORT: int = int(os.environ.get("OPENCODE_PORT", "4096"))
OPENCODE_PASSWORD: str | None = os.environ.get("OPENCODE_PASSWORD")
DEFAULT_MODEL: str = os.environ.get("OPENCODE_DEFAULT_MODEL", "minimax-m2.5-free")

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------
store = SessionStore(BRIDGE_DIR / "sessions.db")
client = OpenCodeClient(
    host=OPENCODE_HOST,
    port=OPENCODE_PORT,
    password=OPENCODE_PASSWORD,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _is_allowed(user_id: int) -> bool:
    return user_id in ALLOWED_USERS


async def _ensure_session(telegram_user_id: int) -> str:
    """Return existing opencode session_id or create a new one."""
    session = await store.get_session(str(telegram_user_id))
    if session:
        return session.opencode_session_id

    oc_session = await client.create_session()
    session_id = oc_session.get("id") or oc_session.get("sessionId") or oc_session["session"]["id"]
    await store.create_session(str(telegram_user_id), session_id, DEFAULT_MODEL)
    await client.update_session(session_id, model=DEFAULT_MODEL)
    return session_id


async def _typing_loop(chat_id: int, context: ContextTypes.DEFAULT_TYPE, stop_event: asyncio.Event) -> None:
    """Send typing action every 5 s until stop_event is set."""
    try:
        while not stop_event.is_set():
            if chat_id:
                await context.application.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            await asyncio.sleep(5)
    except Exception:
        pass  # bot may have been stopped; harmless


async def _send_chunks(update: Update, text: str) -> None:
    if not update.message:
        return
    chunks = format_and_chunk(text)
    for chunk in chunks:
        for attempt in range(3):
            try:
                await update.message.reply_text(chunk, parse_mode=ParseMode.MARKDOWN_V2)
                break
            except Exception:
                if attempt < 2:
                    import asyncio
                    await asyncio.sleep(2 ** attempt)
                    continue
                await update.message.reply_text(chunk)


# ---------------------------------------------------------------------------
# Auth guard decorator
# ---------------------------------------------------------------------------
def authorized(handler):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if user is None or not _is_allowed(user.id):
            if update.message:
                await update.message.reply_text("Unauthorized.")
            return
        if user.is_bot:
            return
        await handler(update, context)
    return wrapper


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------
@authorized
async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start a fresh session."""
    user_id = str(update.effective_user.id)
    try:
        existing = await store.get_session(user_id)
        if existing:
            try:
                await client.abort_session(existing.opencode_session_id)
            except Exception:
                pass
            await store.delete_session(user_id)

        oc_session = await client.create_session()
        session_id = (
            oc_session.get("id")
            or oc_session.get("sessionId")
            or oc_session["session"]["id"]
        )
        await store.create_session(user_id, session_id, DEFAULT_MODEL)
        await client.update_session(session_id, model=DEFAULT_MODEL)
        await update.message.reply_text("Started new session.")
    except Exception as exc:
        log.error("cmd_new error: %s", exc, exc_info=True)
        await update.message.reply_text(f"Error starting session: {exc}")


@authorized
async def cmd_abort(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Abort the current session."""
    user_id = str(update.effective_user.id)
    try:
        session = await store.get_session(user_id)
        if not session:
            await update.message.reply_text("No active session.")
            return
        await client.abort_session(session.opencode_session_id)
        await update.message.reply_text("Session aborted.")
    except Exception as exc:
        log.error("cmd_abort error: %s", exc, exc_info=True)
        await update.message.reply_text(f"Error aborting session: {exc}")


@authorized
async def cmd_share(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Share the current session."""
    user_id = str(update.effective_user.id)
    try:
        session = await store.get_session(user_id)
        if not session:
            await update.message.reply_text("No active session.")
            return
        url = await client.share_session(session.opencode_session_id)
        if url:
            await update.message.reply_text(url)
        else:
            await update.message.reply_text("Share failed.")
    except Exception as exc:
        log.error("cmd_share error: %s", exc, exc_info=True)
        await update.message.reply_text("Share failed.")


@authorized
async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List or switch models."""
    user_id = str(update.effective_user.id)
    args = context.args or []

    try:
        if not args:
            providers = await client.list_providers()
            lines = ["*Available models:*"]
            for provider in providers if isinstance(providers, list) else []:
                pname = provider.get("id") or provider.get("name", "?")
                models = provider.get("models") or []
                for m in models:
                    mid = m.get("id") or m.get("name") or str(m)
                    lines.append(f"  - `{pname}/{mid}`")
            if len(lines) == 1:
                lines.append("_(none found)_")
            text = "\n".join(lines)
            if update.message:
                await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)
        else:
            model_arg = args[0]
            session_id = await _ensure_session(user_id)
            await client.update_session(session_id, model=model_arg)
            await store.update_session(user_id, model=model_arg)
            if update.message:
                await update.message.reply_text(f"Model set to {model_arg}")
    except Exception as exc:
        log.error("cmd_model error: %s", exc, exc_info=True)
        if update.message:
            await update.message.reply_text(f"Error: {exc}")


@authorized
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show current session info."""
    user_id = str(update.effective_user.id)
    try:
        session = await store.get_session(user_id)
        if not session:
            if update.message:
                await update.message.reply_text("No active session.")
            return

        session_id = session.opencode_session_id
        model = session.model or DEFAULT_MODEL
        title = session.title or "(untitled)"

        lines = [
            "*Session Status*",
            f"ID: `{session_id}`",
            f"Model: `{model}`",
            f"Title: {title}",
        ]
        if update.message:
            await update.message.reply_text(
                "\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2
            )
    except Exception as exc:
        log.error("cmd_status error: %s", exc, exc_info=True)
        if update.message:
            await update.message.reply_text(f"Error: {exc}")


@authorized
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show help."""
    if not update.message:
        return
    text = (
        "OpenCode Telegram Bridge\n\n"
        "Commands:\n"
        "/new, /reset — Start a fresh session\n"
        "/abort, /stop — Stop the current session\n"
        "/share — Get a shareable link\n"
        "/model — List available models\n"
        "/model name — Switch model\n"
        "/status — Show session info\n"
        "/help — Show this message"
    )
    await update.message.reply_text(text)


# ---------------------------------------------------------------------------
# Regular message handler
# ---------------------------------------------------------------------------
@authorized
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    user_id = str(update.effective_user.id)
    text = update.message.text
    if not text.strip():
        return

    chat_id = update.effective_chat.id if update.effective_chat else None
    if chat_id:
        try:
            await update.message.reply_chat_action(ChatAction.TYPING)
        except Exception:
            pass

    try:
        session_id = await _ensure_session(user_id)

        stop_typing = asyncio.Event()
        typing_task = asyncio.create_task(_typing_loop(chat_id, context, stop_typing))

        try:
            response = await client.send_prompt(session_id, text)
        finally:
            stop_typing.set()
            typing_task.cancel()
            try:
                await typing_task
            except asyncio.CancelledError:
                pass

        reply_text = extract_text_response(response)

        if not reply_text:
            if update.message:
                await update.message.reply_text("(No response received.)")
            return

        description, is_hardline, is_dangerous = check_command(reply_text)

        if is_hardline:
            if update.message:
                await update.message.reply_text(f"Blocked: {description}")
            return

        if is_dangerous:
            if update.message:
                await update.message.reply_text(
                    f"This command requires approval: {description}. "
                    "Do you want to proceed? (not yet implemented -- blocked for safety)"
                )
            return

        await _send_chunks(update, reply_text)

    except Exception as exc:
        log.error("handle_message error: %s", exc, exc_info=True)
        if update.message:
            await update.message.reply_text(f"Error: {exc}")


# ---------------------------------------------------------------------------
# Bot startup
# ---------------------------------------------------------------------------
async def post_init(app: Application) -> None:
    # Check OpenCode server health (non-fatal)
    try:
        await client.health_check()
        log.info("OpenCode server is reachable.")
    except Exception as exc:
        log.warning(
            "OpenCode server not reachable at startup: %s — "
            "send /new once 'opencode serve' is running.",
            exc,
        )

    # Register commands with BotFather
    commands = [
        BotCommand("new", "Start a fresh session"),
        BotCommand("reset", "Alias for /new"),
        BotCommand("abort", "Abort the current session"),
        BotCommand("stop", "Alias for /abort"),
        BotCommand("share", "Get a shareable link for the current session"),
        BotCommand("model", "List or switch models"),
        BotCommand("status", "Show current session info"),
        BotCommand("help", "Show help"),
    ]
    await app.bot.set_my_commands(commands)
    log.info("Bot commands registered.")


async def main() -> None:
    # Generous timeouts for congested networks (Python 3.14 no longer creates
    # an implicit event loop, so run_polling() breaks; use asyncio.run() instead).
    request = HTTPXRequest(
        connect_timeout=60.0,
        read_timeout=120.0,
        write_timeout=60.0,
        pool_timeout=60.0,
        http_version="1.1",
    )

    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .request(request)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("reset", cmd_new))
    app.add_handler(CommandHandler("abort", cmd_abort))
    app.add_handler(CommandHandler("stop", cmd_abort))
    app.add_handler(CommandHandler("share", cmd_share))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    import signal

    await store.init()
    log.info("Session store initialized.")

    stop_event = asyncio.Event()

    def _request_stop(*_):
        stop_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _request_stop)

    # app.initialize() calls getMe() which can time out on a cold/flaky network.
    # Retry with backoff so a single timeout doesn't kill the bot on startup.
    log.info("Connecting to Telegram…")
    for attempt in range(1, 11):
        try:
            await app.initialize()
            break
        except Exception as exc:
            wait = min(attempt * 5, 60)
            log.warning(
                "Telegram connect attempt %d/10 failed (%s). Retrying in %ds…",
                attempt, exc, wait,
            )
            if attempt == 10:
                log.error("Could not connect to Telegram after 10 attempts. Exiting.")
                raise
            await asyncio.sleep(wait)

    await app.start()
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    log.info("Bot is polling. Press Ctrl-C to stop.")
    await stop_event.wait()          # block until SIGINT / SIGTERM
    await app.updater.stop()
    await app.stop()
    await app.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
