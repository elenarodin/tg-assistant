# ms365 MCP server — Microsoft 365 auth setup

How the bot talks to Outlook Calendar/Mail: Claude Code spawns
`@softeria/ms-365-mcp-server` (registered in `~/.claude.json`, user scope), which
authenticates to Microsoft Graph. This doc reproduces the **durable** auth setup so a
future machine (or future you) can redo it in ~10 minutes instead of rediscovering it.

> Related: [README.md](README.md) covers the LaunchAgent + the Claude Code Keychain-401
> fix. This file is specifically the **Microsoft 365 / Entra** side.

---

## 1. Why this exists

The package ships with a **shared, built-in app registration**. For **personal**
Microsoft accounts (e.g. `elena.rodin@outlook.com`), Microsoft rejects the refresh
tokens issued to that shared app (a known **June 2026** issue), so the login silently
dies roughly **once a day** with:

```
Failed to authenticate ... Silent token acquisition failed ... known issue (June 2026)
where Microsoft rejects refresh tokens issued to personal accounts via the default
'common' authority.
```

The durable fix is to register **your own Entra app** and point the server at it. Then
you control the app and get proper, long-lived personal-account refresh tokens.

---

## 2. Azure app registration (one-time — already done)

### Gotcha #1 — a bare personal account can't create app registrations
Signing into <https://portal.azure.com> with only a personal Microsoft account lands you
in the restricted **"Microsoft Services"** system tenant
(`f8cdef31-a31e-4b4a-93e4-5f571e91255a`) showing **"No directories found"**. You cannot
create app registrations there and there is nothing to switch to.

**Fix:** sign up for a **free Azure account** at
<https://azure.microsoft.com/en-us/pricing/purchase-options/azure-account>. This
auto-creates a **"Default Directory"** tenant where your MSA is **Global Administrator**.
The card is for identity verification only — **app registrations are free** and survive
trial expiry.

### Create the registration
1. **App registrations** → **New registration**.
2. **Name:** `ms365-mcp-personal`
3. **Supported account types:** **"Personal Microsoft accounts only"**.
4. **Redirect URI:** leave **empty**.
5. **Register.**
6. **Authentication** → **Advanced settings** → **Allow public client flows** → **Yes**
   → **Save**. (Required for the device-code flow.)

No client secret, no platform, no pre-added API permissions — the delegated Graph scopes
(Calendars.ReadWrite, Mail.ReadWrite, etc.) are requested at login and consented once.

### Current values (client ID is public by design — safe to commit)
| Field | Value |
| --- | --- |
| App name | `ms365-mcp-personal` |
| Application (client) ID | `51b1ba28-a141-4fcf-b3d3-6541be27cea4` |
| Directory | Default Directory (elenarodinoutl…) |
| Authority / tenant | `consumers` |

---

## 3. Server wiring (`~/.claude.json`)

### Gotcha #2 — MSAL caches tokens BY CLIENT ID
If the server is spawned **without** `MS365_MCP_CLIENT_ID`, it uses the shared app ID,
**misses your cached token**, and silently reverts to the broken daily-expiry behavior.
The client ID in the config **must** match the app you logged in under.

### Gotcha #3 — tenant must be `consumers`
You authenticate under the **consumers** authority, so the server's tenant must match.
Leaving the default `common` re-triggers the personal-account refresh-token rejection.

### The config
`~/.claude.json` → `mcpServers.ms365` (user scope) — **not** in this repo (it's global
machine state and may contain other servers). Its `env` block must be:

```json
"mcpServers": {
  "ms365": {
    "type": "stdio",
    "command": "npx",
    "args": ["-y", "@softeria/ms-365-mcp-server"],
    "env": {
      "MS365_MCP_TENANT_ID": "consumers",
      "MS365_MCP_CLIENT_ID": "51b1ba28-a141-4fcf-b3d3-6541be27cea4"
    }
  }
}
```

Apply/inspect via the CLI (avoids hand-editing the large `~/.claude.json`):

```sh
claude mcp remove ms365 -s user
claude mcp add ms365 -s user \
  -e MS365_MCP_TENANT_ID=consumers \
  -e MS365_MCP_CLIENT_ID=51b1ba28-a141-4fcf-b3d3-6541be27cea4 \
  -- npx -y @softeria/ms-365-mcp-server
claude mcp get ms365          # confirm the Environment block
```

**No bot restart needed** — the bot spawns `claude` (and thus the server) fresh per
request, so it picks up config changes on the next message.

---

## 4. Re-auth procedure

A re-login is only forced by a password change, a Microsoft security event, or a token
cache wipe — not by normal daily use anymore. When it happens:

1. Run the login with **both** env vars set (so it uses your app, not the shared one):
   ```sh
   MS365_MCP_CLIENT_ID=51b1ba28-a141-4fcf-b3d3-6541be27cea4 \
   MS365_MCP_TENANT_ID=consumers \
   npx -y @softeria/ms-365-mcp-server --login
   ```
2. Open <https://microsoft.com/devicelogin>, enter the device code, pick the personal
   account (`elena.rodin@outlook.com`).
3. **Confirm the consent screen names `ms365-mcp-personal`.** If it names any other app,
   the env vars aren't being applied — **stop** and check the config before consenting.
4. Verify (should succeed with **no** device-code prompt):
   ```sh
   MS365_MCP_CLIENT_ID=51b1ba28-a141-4fcf-b3d3-6541be27cea4 \
   MS365_MCP_TENANT_ID=consumers \
   npx -y @softeria/ms-365-mcp-server --verify-login
   ```

The token cache lives in the macOS **login Keychain** entry named `ms-365-mcp-server`
(managed by MSAL — do not edit by hand). Useful commands:
`--list-accounts`, `--logout`, `--verify-login`.

---

## 5. Verification checklist

- [ ] `claude mcp get ms365` shows both `MS365_MCP_TENANT_ID=consumers` and
      `MS365_MCP_CLIENT_ID=51b1ba28-…`.
- [ ] `--verify-login` (with both env vars) returns `success: true` for your account and
      **no device-code prompt**.
- [ ] A calendar **list → create → confirm → delete → confirm** round-trip through the
      bot path succeeds (send the bot a scheduling message, or run `schedule_request`).
- [ ] It still verifies ~24–48h later **without** a re-login (proves the durable fix held).
