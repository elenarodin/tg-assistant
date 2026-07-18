import asyncio
import json
import logging
import os
import tempfile
import time
from datetime import datetime
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


# Shared MCP guardrail prepended to every claude -p invocation. Keeps the model on the
# ms365 calendar tools and off Google Calendar unless explicitly asked.
SYSTEM_PREFIX = (
    "[SYSTEM CONSTRAINT - HARD RULE] You have access to the ms365 MCP server. Verify with ListMcpResourcesTool if uncertain. "
    "For any calendar operation, you MUST use mcp__ms365__* tools (create-calendar-event, list-calendar-events, "
    "get-calendar-view, update-calendar-event, delete-calendar-event). "
    "Calling mcp__claude_ai_Google_Calendar__* is FORBIDDEN unless the user message contains the literal string \"google calendar\". "
    "If you believe ms365 is unavailable, you are wrong - call ListMcpResourcesTool first.\n"
)

# Step 1 — READ ONLY. Classify intent and, for a new booking, return the requested window
# plus that day's events as ISO-8601 strings WITH UTC offset. Python (not the model) then
# decides overlap, so the timezone comparison can't be fudged. The model's only jobs here
# are natural-language time parsing and faithfully echoing what the calendar returned.
ANALYZE_PROMPT = (
    SYSTEM_PREFIX +
    "[TASK — READ ONLY] Do NOT create, update, or delete anything in this step. Only read and report.\n"
    "1. Call mcp__ms365__get-mailbox-settings and read `timeZone` — the user's local IANA timezone. "
    "Every clock time the user gives (e.g. \"3 PM\") is in THIS timezone.\n"
    "2. Classify intent: is the user asking to CREATE/SCHEDULE a NEW event at a specific date+time? "
    "If yes intent=\"create\"; for anything else (listing, canceling, moving, questions) intent=\"other\".\n"
    "3. If intent=\"create\": work out the requested start and end. If no duration is given, assume 30 minutes. "
    "Express BOTH as ISO-8601 WITH the user's local UTC offset, e.g. 2026-06-17T15:00:00-04:00. "
    "Then call mcp__ms365__get-calendar-view for that whole day, passing the `timezone` parameter set to the user's IANA "
    "timezone, and list EVERY event with subject, start, end (ISO-8601 with offset, as returned).\n"
    "Output ONLY one JSON object, no prose and no markdown fences:\n"
    "{\"intent\":\"create|other\",\"timezone\":\"<iana|null>\",\"requested_start\":\"<iso|null>\","
    "\"requested_end\":\"<iso|null>\",\"events\":[{\"subject\":\"...\",\"start\":\"<iso>\",\"end\":\"<iso>\"}]}\n"
    "[USER MESSAGE] "
)

# Step 2 — the actual write (book / list / cancel / reschedule). Only reached for non-create
# intents, or for a create that Python has already cleared as conflict-free.
ACT_PROMPT = (
    SYSTEM_PREFIX +
    "Carry out the user's request. After the operation, reply with one short line confirming what you did.\n"
    "[USER MESSAGE] "
)


def _child_env() -> dict:
    """Environment for spawned `claude` processes.

    Claude Code authenticates via an OAuth token in the macOS login Keychain (the Max
    subscription login). Resolving that keychain requires USER/LOGNAME/HOME — under launchd
    these are easily absent, and without them `claude` reports "Not logged in" / returns
    401 Invalid authentication credentials even though the session is perfectly valid. We
    inherit the parent env and guarantee those three are set so credential lookup never
    depends on how the bot happened to be launched. (Setting ANTHROPIC_API_KEY in the env
    would also flow through here automatically, if we ever switch to key-based auth.)
    """
    env = dict(os.environ)
    env.setdefault("HOME", str(HOME))
    env["USER"] = os.environ.get("USER") or HOME.name
    env["LOGNAME"] = os.environ.get("LOGNAME") or env["USER"]
    return env


async def run_claude(full_prompt: str) -> tuple[int, str, str]:
    """Run a fully-built prompt through `claude -p` and return (rc, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        str(CLAUDE_BIN),
        "-p",
        full_prompt,
        "--dangerously-skip-permissions",
        cwd=str(CLAUDE_CWD),
        env=_child_env(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return (
        proc.returncode if proc.returncode is not None else -1,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


def _extract_json(text: str) -> dict | None:
    """Pull the first {...} object out of the model's stdout and parse it.

    The analyze step is told to emit bare JSON, but we tolerate stray prose or ```json
    fences by grabbing the outermost brace span.
    """
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        obj = json.loads(text[start : end + 1])
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def _parse_dt(s: str, fallback_tz=None) -> datetime:
    """Parse an ISO-8601 string to an aware datetime. 'Z' is normalized; naive values
    inherit `fallback_tz` so we never mix aware/naive datetimes in a comparison."""
    dt = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    if dt.tzinfo is None and fallback_tz is not None:
        dt = dt.replace(tzinfo=fallback_tz)
    return dt


def _fmt_time(dt: datetime) -> str:
    # 3:00 PM — strip a leading zero from the hour without relying on platform %-I.
    return dt.strftime("%I:%M %p").lstrip("0")


def find_conflicts(req_start: str, req_end: str, events: list[dict]) -> list[tuple[str, datetime, datetime]]:
    """Deterministic overlap check. Returns (subject, start, end) for every event that
    overlaps [req_start, req_end). Overlap = existing.start < req.end AND existing.end > req.start,
    all as timezone-aware datetimes. This is the hard gate — the model does not decide here."""
    rs = _parse_dt(req_start)
    re_ = _parse_dt(req_end, rs.tzinfo)
    rs = _parse_dt(req_start, re_.tzinfo)
    out: list[tuple[str, datetime, datetime]] = []
    for ev in events:
        try:
            es = _parse_dt(str(ev["start"]), rs.tzinfo)
            ee = _parse_dt(str(ev["end"]), rs.tzinfo)
        except (KeyError, ValueError, TypeError):
            continue
        if es < re_ and ee > rs:
            out.append((str(ev.get("subject") or "(busy)"), es, ee))
    return out


async def schedule_request(text: str) -> tuple[int, str]:
    """Two-step scheduling with a deterministic conflict gate.

    Step 1 analyzes the request read-only and returns the requested window + that day's
    events. For a create, Python computes overlap; if the slot is taken we reply and NEVER
    reach the write step. Otherwise (and for all non-create intents) step 2 performs the
    action. Returns (rc, user_facing_reply).
    """
    rc, out, err = await run_claude(ANALYZE_PROMPT + text)
    data = _extract_json(out) if rc == 0 else None

    is_create = bool(
        data
        and data.get("intent") == "create"
        and data.get("requested_start")
        and data.get("requested_end")
    )

    if is_create:
        try:
            conflicts = find_conflicts(
                data["requested_start"], data["requested_end"], data.get("events") or []
            )
        except Exception:
            log.exception("overlap computation failed; falling through to act step")
            conflicts = None

        if conflicts:
            subj, es, ee = conflicts[0]
            extra = f" (+{len(conflicts) - 1} more)" if len(conflicts) > 1 else ""
            log.info("conflict gate BLOCKED booking; %d overlap(s)", len(conflicts))
            return 0, (
                f"⛔ That slot is taken — '{subj}' is already booked "
                f"{_fmt_time(es)}–{_fmt_time(ee)}{extra}. Nothing was scheduled."
            )
        if conflicts == []:
            log.info("conflict gate CLEAR; proceeding to book")
    else:
        log.info("analyze: intent=%s (no gate)", (data or {}).get("intent", "unknown"))

    # Reached for: conflict-free create, every non-create intent, or analyze failure
    # (degrade to acting rather than dropping the user's request).
    rc, out, err = await run_claude(ACT_PROMPT + text)
    if rc != 0:
        body = out.strip() or err.strip() or f"(no output, exit {rc})"
        return rc, f"⚠️ claude exited {rc}:\n{body}"
    return rc, (out.strip() or "(claude returned no output)")


def chunks(text: str, size: int = TG_MSG_LIMIT):
    if not text:
        yield ""
        return
    for i in range(0, len(text), size):
        yield text[i : i + size]


# --- Auth-failure alerting ---------------------------------------------------
# When a spawned `claude` fails with a 401 / auth error, warn the owner over Telegram
# (the bot's own send path keeps working while Claude auth is down) with recovery steps.
# Rate-limited to once per hour so a burst of failed messages can't spam the chat.
AUTH_ALERT_INTERVAL = 3600.0  # seconds
_last_auth_alert: float = 0.0

AUTH_ERROR_MARKERS = (
    "invalid authentication",
    "failed to authenticate",
    "not logged in",
    "401",
)

AUTH_ALERT_TEXT = (
    "⚠️ Claude auth expired — run `claude login` in Terminal, then restart me with: "
    "launchctl kickstart -k gui/$UID/com.lena.tgassistant"
)


def is_auth_failure(rc: int, text: str) -> bool:
    """True when a failed claude run looks like an authentication error. The rc != 0 guard
    keeps a successful booking whose text merely mentions '401' from tripping the alert."""
    if rc == 0:
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in AUTH_ERROR_MARKERS)


async def alert_auth_failure(bot, chat_id: int) -> bool:
    """Send the recovery alert directly via Telegram, at most once per hour.

    Goes through the bot's own send path (never claude), so it works even while Claude
    auth is down. Returns True if an alert was sent, False if suppressed by the rate limit.
    """
    global _last_auth_alert
    now = time.monotonic()
    if now - _last_auth_alert < AUTH_ALERT_INTERVAL:
        return False
    _last_auth_alert = now  # reserve the slot before sending so a send error can't unblock a burst
    try:
        await bot.send_message(chat_id=chat_id, text=AUTH_ALERT_TEXT)
        log.warning("sent auth-failure alert to chat_id=%s", chat_id)
    except Exception:
        log.exception("failed to send auth-failure alert")
    return True


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
        rc, reply = await schedule_request(text)
    except Exception as e:
        log.exception("claude invocation failed")
        reply = f"⚠️ failed to run claude: {e}"
        rc = -1
    finally:
        stop.set()
        for t in (typing_task, notice_task):
            t.cancel()
        for t in (typing_task, notice_task):
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

    # Auth-failure path: claude couldn't authenticate. Notify the owner directly over
    # Telegram with recovery steps (rate-limited to 1/hour) instead of echoing the raw
    # 401, and skip the normal reply so repeated failures don't spam the chat.
    if is_auth_failure(rc, reply):
        sent = await alert_auth_failure(bot, chat_id)
        log.warning("auth failure on user_id=%s request; alert_sent=%s", user_id, sent)
        return

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
            env=_child_env(),
        )
        for line in (result.stdout + result.stderr).splitlines():
            if "ms365" in line:
                log.info("mcp-health %s", line.strip())
                break
        else:
            log.warning("mcp-health ms365 not found in `claude mcp list` output")
    except Exception:
        log.exception("mcp-health probe failed")


def verify_claude_auth() -> bool:
    """Confirm `claude` can actually authenticate before the bot accepts messages.

    Runs a tiny prompt with the exact env spawned commands use. Returns True on success.
    On failure we log a loud, actionable error (the most common cause is the launchd env
    starving the Keychain lookup, or an expired login) but do NOT hard-exit — KeepAlive
    would just crash-loop, and a clear log line is more useful than a restart storm.
    """
    import subprocess

    try:
        result = subprocess.run(
            [str(CLAUDE_BIN), "-p", "ok"],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(CLAUDE_CWD),
            env=_child_env(),
        )
    except Exception:
        log.exception("auth-check probe failed to run")
        return False

    output = (result.stdout + result.stderr).strip()
    broken = (
        result.returncode != 0
        or "Not logged in" in output
        or "401" in output
        or "authenticate" in output.lower()
        or "Invalid authentication" in output
    )
    if broken:
        log.error(
            "AUTH CHECK FAILED — claude could not authenticate (rc=%s): %s\n"
            "  The bot will keep running but every scheduling request will fail until this "
            "is fixed.\n"
            "  Most likely: the launchd process is missing USER/LOGNAME/HOME so the macOS "
            "Keychain login can't be read, OR the `claude login` session expired.\n"
            "  Fix: ensure the LaunchAgent sets USER and LOGNAME (see plist), then reload it; "
            "if still failing, run `claude login` once in a terminal as this user. "
            "Alternatively set ANTHROPIC_API_KEY in ~/tg-assistant/.env for key-based auth.",
            result.returncode,
            output[:500] or "(no output)",
        )
        return False
    log.info("auth check OK — claude authenticated")
    return True


def main() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    log.info(
        "bot starting allowed_users=%s claude_cwd=%s",
        sorted(ALLOWED_USER_IDS),
        CLAUDE_CWD,
    )
    _log_startup_env()

    # Validate claude auth up front so a broken login surfaces clearly in the log at
    # startup rather than as a cryptic 401 on the user's first message.
    verify_claude_auth()

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
