# Connecting Slack

Your workspace `Jarvis-Ops-Testing`, channel `#all-jarvis-ops-testing`.

You need TWO things, from the same Slack app. Takes about five minutes.

## Create the app
1. Go to https://api.slack.com/apps -> **Create New App** -> **From scratch**
2. Name: `Jarvis Ops`. Workspace: `Jarvis-Ops-Testing`. -> Create App

## A. Incoming webhook  ->  SLACK_WEBHOOK_URL   (write-only alerts)
3. Left nav -> **Incoming Webhooks** -> toggle **Activate Incoming Webhooks** to On
4. **Add New Webhook to Workspace** -> pick `#all-jarvis-ops-testing` -> Allow
5. Copy the URL: `https://hooks.slack.com/services/T.../B.../...`

This is all you need for pipeline alerts. Simplest thing that works.

## B. Bot token  ->  SLACK_BOT_TOKEN   (only if you want a conversational ops agent)
6. Left nav -> **OAuth & Permissions** -> **Scopes** -> **Bot Token Scopes** -> add:
   - `chat:write`          post messages
   - `channels:history`    read the channel (needed to answer questions)
   - `channels:read`       resolve channel IDs
   - `files:write`         attach a CSV of failed rows to an alert
7. Scroll up -> **Install to Workspace** -> Allow
8. Copy the **Bot User OAuth Token**: starts `xoxb-`
9. In Slack, invite the bot into the channel: `/invite @Jarvis Ops`

## Test it
    curl -X POST -H 'Content-type: application/json' \
      --data '{"text":"Jarvis ops channel wired up."}' \
      "$SLACK_WEBHOOK_URL"

## Security
Both values go in `.env` only. `.env` is git-ignored. If a token ever lands in a commit
or a chat window, revoke it in the app settings and issue a new one — rotating is cheap,
leaking is not.
