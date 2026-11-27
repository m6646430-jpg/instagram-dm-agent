# instagram-dm-agent

Sends an automatic Instagram DM when someone comments a keyword on your posts,
or sends you a keyword as a DM. No monthly fee: one Python file, standard
library only, running on your own Mac or a small Linux server.

Someone comments `PACK` on a reel → the agent sends them the text in
`replies/PACK.txt` as a private reply → it is logged in `sent.db`, so nobody
gets the same reply twice.

It uses the official Instagram API. You need an Instagram professional
account (Creator or Business) and a free Meta developer app.

## How to use it

Each keyword is one text file in `replies/`. The file name is the keyword.
To add `CV`, create `replies/CV.txt` and write the message (max 1,000 characters).
`replies/PACK.txt` is a working example.

```bash
python3 agent.py check     # is the token working? which keywords are loaded?
python3 agent.py run       # one pass
python3 agent.py loop      # keep running, one pass every 5 minutes
python3 agent.py log       # last 30 sends
python3 agent.py refresh   # extend a 60-day Instagram-login token
python3 -m unittest        # tests
```

It starts in **dry run**: it prints who it would message and sends nothing.
Set `DRY_RUN=0` in `.env` when the output looks right.

## Setup

1. Create an app at developers.facebook.com and add the Instagram API use case.
2. Connect your Instagram professional account to the app. While the app is
   unpublished, the account must have a role on it (for example Instagram Tester).
3. Generate an access token for that account on the app's Instagram API setup
   page. Share only the Instagram account, nothing else.
4. Copy `.env.example` to `.env` and fill in `IG_ACCESS_TOKEN` and `IG_USER_ID`.
   Keep `IG_API_BASE` as it is for apps that use Unified login
   (`graph.facebook.com`); use `https://graph.instagram.com/v23.0` for the older
   Instagram login.
5. Run `python3 agent.py check`.

Meta changes these screens often. If a step looks different, follow Meta's own
Instagram API documentation.

**Never share or commit `.env`.** It holds your token. `.gitignore` already
keeps `.env`, `sent.db` and `agent.log` out of git.

## Background job (macOS)

Runs one pass every 5 minutes while the Mac is on and you are logged in.
Edit `deploy/local.dm-agent.plist` (replace `/PATH/TO/dm-agent`), copy it to
`~/Library/LaunchAgents/`, then:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.dm-agent.plist   # start
launchctl bootout gui/$(id -u)/local.dm-agent                                  # stop
tail -20 agent.log                                                             # what it did
```

## Cloud server

`deploy/deploy.sh` moves the agent to a small Linux server (Ubuntu or Debian)
so it runs with the Mac off. Run it with no arguments to see the commands.
Order: `push` (copies files, runs a dry run, starts nothing), then `golive`
(stops the Mac job first, then starts the server). Only one copy may be live
at a time, or both would answer the same people.

## Instagram's rules

- One private reply per comment, within 7 days of the comment.
- The DM includes a link to the post they commented on (Instagram adds it).
- It lands in the Inbox if they follow you, otherwise in Requests.
- You can message them again only if they reply, and within 24 hours of that.

## Any comment on one reel

`reel_defaults.json` maps a reel to a keyword: `{"AbC123xyz": "PACK"}`.
On that reel every comment gets that keyword's reply, even "🙌" or "nice".
A real keyword in the comment still wins. The code is the part of the reel
link after `/reel/`.

## DM keywords

It answers a DM only when the newest message in the chat is from the other
person, is 4 words or fewer, contains a keyword, and is under 24 hours old
(Instagram's limit). Longer messages are left for you. Set `DM_REPLIES=0` to
turn this off.

## What the agent will not do

- Message the same person twice for the same keyword.
- Reply to your own comments or comments older than 7 days.
- Send more than `MAX_SENDS_PER_RUN` (30) messages in one pass.
- Retry a comment that failed. Failures are in `python3 agent.py log`.

## Not built yet

- Instant replies. This version checks every 5 minutes. Instant needs a Meta
  webhook and a public server.

Made by [@heymahesh.ai](https://www.instagram.com/heymahesh.ai/). Free to use and change.
