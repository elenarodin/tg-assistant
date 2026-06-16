import asyncio
import logging
import os
import tempfile
import time
from pathlib import Path

from dotenv import load_dotenv
from faster_whisper import WhisperModel
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, ContextTypes, MessageHandler, filters

HOME = Path.home()
PROJECT_DIR = HOME / "tg-assistant"
LOG_PATH = PROJECT_DIR / "bot.log"
CLAUDE_BIN = HOME / ".local" / "bin" / "claude"
CLAUDE_CWD = HOME / "sched"

TG_MSG_LIMIT = 4000
STILL_WORKING_AFTER = 90
TYPING_REFRESH = 4

# Local speech-to-text for Telegram voice notes. Loaded once at startup (see main()).
# small.en handles accented English well and is fast enough for short commands on this
# Mac. Switch to "small" if non-English (Italian/Ukrainian) commands are needed later.
WHISPER_MODEL_NAME = "small.en"
WHISPER_MODEL: "WhisperModel | None" = None

load_dotenv(PROJECT_DIR / ".env")
BOT_TOKEN = os.environ["BOT_TOKEN"]
ALLOWED_USER_IDS = {
    int(uid.strip())
    for uid in os.environ["ALLOWED_USER_IDS"].split(",")
    if uid.strip()
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("tg-assistant")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.INFO)


async def keep_typing(bot, chat_id: int, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except Exception:
            log.exception("send_chat_action failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=TYPING_REFRESH)
        except asyncio.TimeoutError:
            pass


async def still_working_notice(bot, chat_id: int, stop: asyncio.Event) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=STILL_WORKING_AFTER)
    except asyncio.TimeoutError:
        try:
            await bot.send_message(chat_id=chat_id, text="still working…")
        except Exception:
            log.exception("still-working notice failed")


async def run_claude(prompt: str) -> tuple[int, str, str]:
    prompt = (
        "[SYSTEM CONSTRAINT - HARD RULE] You have access to the ms365 MCP server. Verify with ListMcpResourcesTool if uncertain. "
        "For any calendar operation, you MUST call mcp__ms365__create-calendar-event, mcp__ms365__list-calendar-events, "
        "mcp__ms365__get-calendar-view, mcp__ms365__update-calendar-event, or mcp__ms365__delete-calendar-event. "
        "Calling mcp__claude_ai_Google_Calendar__* is FORBIDDEN unless the user message contains the literal string \"google calendar\". "
        "If you believe ms365 is unavailable, you are wrong - call ListMcpResourcesTool first. "
        "After the operation, reply with one short line confirming what you did.\n\n"
        "[USER MESSAGE] " + prompt
    )
    proc = await asyncio.create_subprocess_exec(
        str(CLAUDE_BIN),
        "-p",
        prompt,
        "--dangerously-skip-permissions",
        cwd=str(CLAUDE_CWD),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return (
        proc.returncode if proc.returncode is not None else -1,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


def chunks(text: str, size: int = TG_MSG_LIMIT):
    if not text:
        yield ""
        return
    for i in range(0, len(text), size):
        yield text[i : i + size]


async def process_text(bot, chat_id: int, user_id: int, text: str, heard: str | None = None) -> None:
    """Run a text command through the claude -p scheduling flow and reply.

    Shared by the text handler and the voice handler — voice notes are transcribed
    one step upstream and then flow through this identical path. When `heard` is set
    (voice), the recognized transcript is prepended to the reply so a mis-hear is
    visible rather than silently booking the wrong thing.
    """
    stop = asyncio.Event()
    typing_task = asyncio.create_task(keep_typing(bot, chat_id, stop))
    notice_task = asyncio.create_task(still_working_notice(bot, chat_id, stop))

    try:
        rc, out, err = await run_claude(text)
    except Exception as e:
        log.exception("claude invocation failed")
        reply = f"⚠️ failed to run claude: {e}"
        rc = -1
    else:
        if rc != 0:
            body = (out.strip() or err.strip() or f"(no output, exit {rc})")
            reply = f"⚠️ claude exited {rc}:\n{body}"
        else:
            reply = out.strip() or "(claude returned no output)"
    finally:
        stop.set()
        for t in (typing_task, notice_task):
            t.cancel()
        for t in (typing_task, notice_task):
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

    if heard is not None:
        reply = f'Heard: "{heard}"\n\n{reply}'

    log.info("outgoing user_id=%s rc=%s len=%d", user_id, rc, len(reply))

    for piece in chunks(reply):
        try:
            await bot.send_message(chat_id=chat_id, text=piece)
        except Exception:
            log.exception("send_message failed")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    if msg is None or user is None or msg.text is None:
        return

    if user.id not in ALLOWED_USER_IDS:
        log.info("ignored unauthorized user_id=%s text=%r", user.id, msg.text)
        return

    text = msg.text
    chat_id = msg.chat_id
    log.info("incoming user_id=%s chat_id=%s text=%r", user.id, chat_id, text)

    await process_text(context.bot, chat_id, user.id, text)


def transcribe_sync(path: str) -> str:
    """Blocking, CPU-bound transcription. Call via asyncio.to_thread so the bot
    event loop stays responsive. Concatenates all segments into one transcript."""
    assert WHISPER_MODEL is not None, "whisper model not loaded"
    segments, _info = WHISPER_MODEL.transcribe(path)
    return "".join(seg.text for seg in segments).strip()


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    if msg is None or user is None or msg.voice is None:
        return

    if user.id not in ALLOWED_USER_IDS:
        log.info("ignored unauthorized voice user_id=%s", user.id)
        return

    chat_id = msg.chat_id
    log.info(
        "incoming voice user_id=%s chat_id=%s duration=%ss",
        user.id, chat_id, msg.voice.duration,
    )

    # Download the OGG/Opus (.oga) voice note to a temp file. faster-whisper decodes
    # it directly via PyAV, so no manual ffmpeg step is needed.
    tmp_path = None
    try:
        tg_file = await context.bot.get_file(msg.voice.file_id)
        fd, tmp_path = tempfile.mkstemp(suffix=".oga")
        os.close(fd)
        await tg_file.download_to_drive(tmp_path)
        transcript = await asyncio.to_thread(transcribe_sync, tmp_path)
    except Exception as e:
        log.exception("voice transcription failed")
        try:
            await context.bot.send_message(
                chat_id=chat_id, text=f"⚠️ couldn't transcribe voice: {e}"
            )
        except Exception:
            log.exception("send_message failed")
        return
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                log.exception("temp audio cleanup failed")

    transcript = (transcript or "").strip()
    log.info("transcript user_id=%s text=%r", user.id, transcript)

    if not transcript:
        try:
            await context.bot.send_message(
                chat_id=chat_id, text="⚠️ Heard nothing — please try again."
            )
        except Exception:
            log.exception("send_message failed")
        return

    # Feed the transcript into the identical text-scheduling path.
    await process_text(context.bot, chat_id, user.id, transcript, heard=transcript)


def _log_startup_env() -> None:
    """Log environment essentials so PATH-related MCP failures show up clearly in bot.log.

    The original LaunchAgent bug was: PATH lacked the NVM bin dir, so Claude Code could
    not spawn `npx -y @softeria/ms-365-mcp-server` and the model reported the ms365
    tools as missing. With this log, that class of issue is visible at every restart.
    """
    import shutil
    import subprocess

    log.info("env HOME=%s USER=%s", os.environ.get("HOME"), os.environ.get("USER"))
    log.info("env PATH=%s", os.environ.get("PATH"))
    for tool in ("claude", "node", "npm", "npx"):
        path = shutil.which(tool)
        log.info("which %s -> %s", tool, path or "(not found)")

    # Quick health-check on the ms365 MCP server. We DON'T fail startup on this — it's
    # purely diagnostic. `claude mcp list` exits 0 even when individual servers fail,
    # so we just grep the output.
    try:
        result = subprocess.run(
            [str(CLAUDE_BIN), "mcp", "list"],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(CLAUDE_CWD),
        )
        for line in (result.stdout + result.stderr).splitlines():
            if "ms365" in line:
                log.info("mcp-health %s", line.strip())
                break
        else:
            log.warning("mcp-health ms365 not found in `claude mcp list` output")
    except Exception:
        log.exception("mcp-health probe failed")


def main() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    log.info(
        "bot starting allowed_users=%s claude_cwd=%s",
        sorted(ALLOWED_USER_IDS),
        CLAUDE_CWD,
    )
    _log_startup_env()

    # Load the speech-to-text model once, before polling, so the first voice note
    # doesn't pay the load cost (or fail under launchd). Model files are cached under
    # ~/.cache/huggingface from the setup download.
    global WHISPER_MODEL
    log.info("loading whisper model %s ...", WHISPER_MODEL_NAME)
    t0 = time.time()
    WHISPER_MODEL = WhisperModel(WHISPER_MODEL_NAME, device="cpu", compute_type="int8")
    log.info("whisper model loaded in %.1fs", time.time() - t0)

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT, handle_message))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
