# Deploying tg-assistant as a macOS LaunchAgent

The bot runs as a per-user `launchd` agent so it starts at login and restarts on crash.
This directory holds the LaunchAgent template — the **root-cause fix for the Keychain
401 lives here**, so keep it in version control.

## The one thing that will bite you: USER / LOGNAME

Claude Code authenticates with an OAuth token in the **macOS login Keychain**. Reading
that Keychain requires `USER`, `LOGNAME`, and `HOME` in the process environment.
**launchd does not set `USER`/`LOGNAME` by default.** Without them the bot spawns
`claude`, which can't read its own token and fails with:

```
Failed to authenticate. API Error: 401 Invalid authentication credentials
```

…even though `claude -p "ok"` works fine in your interactive terminal. The fix is the
`USER` and `LOGNAME` keys in `EnvironmentVariables` (the bot also hardens this in code via
`_child_env()`, but the plist is the primary fix). If you ever see that 401 on a new
machine, this is why.

`PATH` must include the NVM node bin dir too, or the ms365 MCP calendar server (`npx -y
@softeria/ms-365-mcp-server`) won't launch and the bot reports "no calendar tools".

## Install

1. Copy the app into place and create the venv:
   ```sh
   mkdir -p ~/tg-assistant
   cp bot.py requirements.txt ~/tg-assistant/
   python3 -m venv ~/tg-assistant/.venv
   ~/tg-assistant/.venv/bin/pip install -r ~/tg-assistant/requirements.txt
   ```

2. Create `~/tg-assistant/.env` from `.env.example` and fill in `BOT_TOKEN` and
   `ALLOWED_USER_IDS`. (Never commit `.env`.)

3. Log in to Claude Code once so the Keychain token exists:
   ```sh
   claude login
   ```

4. Render the plist template, substituting your account's values:
   ```sh
   NODE_BIN="$(dirname "$(command -v node)")"
   sed -e "s#__HOME__#$HOME#g" \
       -e "s#__USER__#$USER#g" \
       -e "s#__NODE_BIN__#$NODE_BIN#g" \
       deploy/com.lena.tgassistant.plist \
       > ~/Library/LaunchAgents/com.lena.tgassistant.plist
   plutil -lint ~/Library/LaunchAgents/com.lena.tgassistant.plist
   ```

5. Load it:
   ```sh
   launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.lena.tgassistant.plist
   ```

## Operate

```sh
# restart after editing bot.py
launchctl kickstart -k gui/$UID/com.lena.tgassistant

# reload after editing the plist (bootout + bootstrap re-reads EnvironmentVariables)
launchctl bootout    gui/$UID/com.lena.tgassistant
launchctl bootstrap  gui/$UID ~/Library/LaunchAgents/com.lena.tgassistant.plist

# logs
tail -f ~/tg-assistant/bot.log            # app log (startup auth check lives here)
tail -f ~/tg-assistant/launchd.err.log    # stderr
```

## Auth expiry (expected, ~monthly)

The OAuth refresh token expires roughly every 29 days. When it does, the startup auth
check logs `AUTH CHECK FAILED` and the bot DMs you `Claude auth expired …`. Recover with:

```sh
claude login
launchctl kickstart -k gui/$UID/com.lena.tgassistant
```

To avoid this entirely, set `ANTHROPIC_API_KEY` in `~/tg-assistant/.env` (pay-as-you-go
API billing, separate from a Claude subscription); `_child_env()` forwards it to `claude`
automatically.
