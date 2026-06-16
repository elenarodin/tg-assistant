# tg-assistant

A Telegram bot that forwards messages to `claude -p` and pipes the output back. Runs under `launchd` so it auto-starts on login and restarts on crash.

## Layout

```
~/tg-assistant/
├── .env                  # BOT_TOKEN, ALLOWED_USER_IDS  (gitignored)
├── .venv/                # python venv with deps        (gitignored)
├── bot.py                # the bot
├── bot.log               # app log (incoming/outgoing)  (gitignored)
├── launchd.out.log       # subprocess stdout            (gitignored)
└── launchd.err.log       # subprocess stderr / tracebacks (gitignored)

~/Library/LaunchAgents/com.lena.tgassistant.plist
```

`claude` is invoked from `~/sched` so it picks up that directory's `CLAUDE.md` and configured MCPs.

## Setup on a new machine

```sh
git clone <this-repo> tg-assistant && cd tg-assistant
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env        # then edit .env with your BOT_TOKEN and ALLOWED_USER_IDS
```

The voice handler downloads the `small.en` faster-whisper model on first load
(cached under `~/.cache/huggingface`). `.env`, `.venv/`, and the logs are
gitignored, so recreate them per the steps above. The launchd plist
(`~/Library/LaunchAgents/com.lena.tgassistant.plist`) lives outside this repo —
set it up separately if you want the bot to auto-start.

## Run as a launchd service

Load (and start) the agent:

```sh
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.lena.tgassistant.plist
```

Stop and unload:

```sh
launchctl bootout gui/$(id -u)/com.lena.tgassistant
```

Check status (PID, last exit, restart count):

```sh
launchctl print gui/$(id -u)/com.lena.tgassistant | head -40
```

Force a restart (e.g. after editing `.env` or `bot.py`):

```sh
launchctl kickstart -k gui/$(id -u)/com.lena.tgassistant
```

## Run manually (for debugging)

If the launchd job is loaded, unload it first so they don't both poll Telegram (you'll get conflicts).

```sh
launchctl bootout gui/$(id -u)/com.lena.tgassistant
cd ~/tg-assistant
.venv/bin/python bot.py
```

Ctrl-C to stop.

## Logs

App log — every incoming message and outgoing reply, with timestamps:

```sh
tail -f ~/tg-assistant/bot.log
```

launchd-captured stdout / stderr (Python tracebacks land here):

```sh
tail -f ~/tg-assistant/launchd.err.log
tail -f ~/tg-assistant/launchd.out.log
```

## Rotate the bot token

1. In Telegram, message [@BotFather](https://t.me/BotFather): `/revoke` → pick the bot → copy the new token. The old token is dead the moment you do this.
2. Edit `~/tg-assistant/.env`, replace `BOT_TOKEN=...` with the new value.
3. Restart the bot:

   ```sh
   launchctl kickstart -k gui/$(id -u)/com.lena.tgassistant
   ```

## Add (or remove) allowed users

`ALLOWED_USER_IDS` in `.env` is a comma-separated list of Telegram user IDs. Anyone not on the list is silently ignored.

To get someone's user ID: have them message [@userinfobot](https://t.me/userinfobot), it replies with their numeric ID.

```sh
# .env
ALLOWED_USER_IDS=514969470,123456789
```

Then restart:

```sh
launchctl kickstart -k gui/$(id -u)/com.lena.tgassistant
```
