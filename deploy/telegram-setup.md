# Enable Telegram notifications (Phase 5)

mp-relay can push terminal pipeline events (scrape success / failure, QC retry exhausted, etc.) to a Telegram chat. Disabled by default — token + chat_id missing makes `notify()` a no-op.

## 5-step setup

### 1. Create the bot

On Telegram, message **[@BotFather](https://t.me/BotFather)**:

```
/newbot
```

It will ask for a display name and a username (must end in `bot`). It then prints a token like:

```
1234567890:AAEhBP0av28...XYZ
```

Save the token.

### 2. Open a chat with your bot

In Telegram, search for the bot's username (the one ending in `bot`) and send it `/start`. The bot won't reply, but this opens the conversation so it can DM you later.

### 3. Find your chat_id

Open in browser (replace `<TOKEN>`):

```
https://api.telegram.org/bot<TOKEN>/getUpdates
```

Look for `"chat":{"id":<NUMBER>,...}`. That `<NUMBER>` is your chat_id (positive for personal chats, negative for groups).

If `getUpdates` returns an empty `result`, send another message to the bot in Telegram first, then refresh.

### 4. Configure mp-relay

SSH to the host:

```powershell
notepad C:\mp-relay\.env
```

Find the Telegram block (already scaffolded — last few lines), uncomment the two lines, and fill in:

```ini
TELEGRAM_BOT_TOKEN=1234567890:AAEhBP0av28...XYZ
TELEGRAM_CHAT_ID=987654321
# Optional CSV filter — if unset, all events fire
TELEGRAM_EVENT_FILTER=qc_failed_exhausted,scrape_failed,scraped
```

### 5. Restart the service

```powershell
Restart-Service mp-relay
```

Verify:

```powershell
Invoke-RestMethod http://localhost:5000/health | ConvertTo-Json
```

The `telegram` field should flip from `"disabled (no token / chat_id)"` to `"ok"`.

## What gets sent

| Event kind | When |
|---|---|
| `scraped` | mdcx scrape succeeded end-to-end |
| `scrape_failed` | mdcx returned non-zero or timed out |
| `qc_failed_exhausted` | 3 retries blown, manual review needed |
| `qc_failed_no_alt` | QC failed, no alternate sukebei candidate exists |
| `qc_failed_no_code` | QC failed but no JAV code parseable for retry |
| `pre_mdcx_failed` | disc remux or another pre-mdcx step blew up |

Empty `TELEGRAM_EVENT_FILTER` = all events. CSV value = allowlist.

Each message is HTML-formatted, includes task id and (truncated) error / file path. Per-message rate limit by Telegram is ~30/sec — way more than mp-relay will ever generate.

## Troubleshooting

- **`/health` shows `getMe HTTP 401`**: token is wrong. Re-check.
- **`/health` shows `getMe HTTP 404`**: token is malformed. Re-paste from BotFather.
- **No messages arrive but `/health` is `ok`**: send `/start` to your bot first; bots can't message users who haven't initiated.
- **Wrong chat_id**: `/health` will pass (`getMe` is identity-only) but `sendMessage` returns 400 "chat not found". Check service logs (`C:\mp-relay\service-stderr.log`) for the exact response.
- **Telegram unreachable from China**: set `HTTPS_PROXY` in `.env` or run mp-relay through a proxy reachable from `10.100.100.13`.
