#!/usr/bin/env python3
"""Instagram comment-keyword DM agent.

Checks recent posts for new comments and the inbox for new DMs. When a
comment contains a keyword, or a short DM is a keyword (one file per keyword
in replies/), it sends that person the matching message and logs it so nobody
is messaged twice.

Standard library only. Python 3.9+.

    python3 agent.py check          # verify the token and list keywords
    python3 agent.py run            # one pass (dry run unless DRY_RUN=0)
    python3 agent.py loop           # run forever, every POLL_SECONDS
    python3 agent.py refresh        # extend the 60-day token
    python3 agent.py log            # show the last 30 sends
"""
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPLY_WINDOW = timedelta(days=7)      # Instagram's limit for private replies
DM_WINDOW = timedelta(hours=24)       # Instagram's limit for answering a DM
DM_MAX_WORDS = 4                      # "CV", "CV please" - not a real conversation
MAX_DM_CHARS = 1000                   # Instagram's limit for a text message


# ---------- config ----------

def load_env(path=None):
    """Read KEY=VALUE lines from .env; real environment variables win."""
    path = path or ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def config():
    load_env()
    return {
        "token": os.environ.get("IG_ACCESS_TOKEN", ""),
        "api": os.environ.get("IG_API_BASE", "https://graph.facebook.com/v23.0"),
        "user_id": os.environ.get("IG_USER_ID", ""),
        "dry_run": os.environ.get("DRY_RUN", "1") != "0",
        "media_limit": int(os.environ.get("MEDIA_LIMIT", "15")),
        "max_sends": int(os.environ.get("MAX_SENDS_PER_RUN", "30")),
        "poll_seconds": int(os.environ.get("POLL_SECONDS", "300")),
        "public_reply": os.environ.get("PUBLIC_REPLY", ""),
        "dm_replies": os.environ.get("DM_REPLIES", "1") != "0",
        "reel_defaults": load_reel_defaults(),
        "conversation_limit": int(os.environ.get("CONVERSATION_LIMIT", "25")),
    }


def load_replies(folder=None):
    """replies/AGENT.txt -> {"AGENT": "text"}. Empty files are ignored."""
    folder = folder or ROOT / "replies"
    replies = {}
    for f in sorted(folder.glob("*.txt")):
        text = f.read_text(encoding="utf-8").strip()
        if not text:
            continue
        if len(text) > MAX_DM_CHARS:
            raise SystemExit("%s is %d characters; Instagram allows %d."
                             % (f.name, len(text), MAX_DM_CHARS))
        replies[f.stem.upper()] = text
    return replies


def load_reel_defaults(path=None):
    """reel_defaults.json: {"<reel shortcode>": "KEYWORD"}.

    On those posts ANY comment gets that keyword's reply, not only comments
    that contain a keyword. The shortcode is the part of the reel link after
    /reel/ or /p/.
    """
    path = path or ROOT / "reel_defaults.json"
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    return {k: v.upper() for k, v in data.items() if not k.startswith("_")}


def shortcode(permalink):
    m = re.search(r"/(?:reel|reels|p)/([^/?#]+)", permalink or "")
    return m.group(1) if m else ""


def pick_public_reply(setting, comment_id):
    """PUBLIC_REPLY may hold several replies separated by |. One is chosen per
    comment so the same sentence is not posted under every comment."""
    options = [s.strip() for s in setting.split("|") if s.strip()]
    if not options:
        return ""
    return options[sum(ord(ch) for ch in str(comment_id)) % len(options)]


# ---------- matching ----------

def match_keyword(text, keywords):
    """First keyword that appears as a whole word, ignoring case."""
    words = set(re.findall(r"[A-Za-z0-9]+", (text or "").upper()))
    for kw in keywords:
        if kw in words:
            return kw
    return None


def match_dm_keyword(text, keywords):
    """Like match_keyword, but only for short messages, so a normal chat that
    happens to contain the word is left for a human."""
    if len((text or "").split()) > DM_MAX_WORDS:
        return None
    return match_keyword(text, keywords)


def parse_time(stamp):
    # Instagram sends 2026-11-26T18:03:34+0000
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S%z")


# ---------- Instagram API ----------

class ApiError(Exception):
    pass


class Instagram:
    def __init__(self, token, base, user_id=""):
        self.token = token
        self.base = base.rstrip("/")
        # Unified login (graph.facebook.com) has no /me for the Instagram
        # account, so the account ID is given. Instagram login can use /me.
        self.user_id = user_id or "me"

    def _call(self, method, path, params=None, body=None):
        url = path if path.startswith("http") else self.base + path
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", "Bearer " + self.token)
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            try:
                detail = json.loads(detail)["error"]["message"]
            except Exception:
                pass
            raise ApiError("%s %s -> %s: %s" % (method, path.split("?")[0], e.code, detail))
        except urllib.error.URLError as e:
            raise ApiError("network: %s" % e.reason)

    def _pages(self, path, params, max_items):
        items, page = [], self._call("GET", path, params)
        while True:
            items.extend(page.get("data", []))
            nxt = page.get("paging", {}).get("next")
            if not nxt or len(items) >= max_items:
                return items[:max_items]
            page = self._call("GET", nxt)

    def me(self):
        if self.user_id != "me":
            return self._call("GET", "/%s" % self.user_id, {"fields": "username"})
        me = self._call("GET", "/me", {"fields": "user_id,username"})
        self.user_id = me.get("user_id") or "me"
        return me

    def recent_media(self, limit):
        return self._pages("/%s/media" % self.user_id,
                           {"fields": "id,permalink,timestamp,comments_count", "limit": limit},
                           limit)

    def comments(self, media_id):
        return self._pages("/%s/comments" % media_id,
                           {"fields": "id,text,username,timestamp", "limit": 50}, 500)

    def private_reply(self, comment_id, text):
        return self._call("POST", "/%s/messages" % self.user_id, body={
            "recipient": {"comment_id": comment_id},
            "message": {"text": text},
        })

    def already_replied(self, comment_id, my_username):
        """True if this account has already replied under the comment."""
        r = self._call("GET", "/%s/replies" % comment_id, {"fields": "username", "limit": 50})
        return any(x.get("username") == my_username for x in r.get("data", []))

    def conversations(self, limit):
        return self._pages("/%s/conversations" % self.user_id, {
            "platform": "instagram", "limit": min(limit, 25),
            "fields": "id,updated_time,messages.limit(1){id,created_time,from,message}",
        }, limit)

    def send_dm(self, recipient_id, text):
        return self._call("POST", "/%s/messages" % self.user_id, body={
            "recipient": {"id": recipient_id},
            "message": {"text": text},
        })

    def public_reply(self, comment_id, text):
        return self._call("POST", "/%s/replies" % comment_id, params={"message": text})

    def refresh_token(self):
        return self._call("GET", "/refresh_access_token",
                          {"grant_type": "ig_refresh_token", "access_token": self.token})


# ---------- log of what was sent ----------

def open_db(path=None):
    db = sqlite3.connect(str(path or ROOT / "sent.db"))
    db.execute("""CREATE TABLE IF NOT EXISTS sent (
        comment_id TEXT PRIMARY KEY, username TEXT, keyword TEXT,
        media_id TEXT, status TEXT, detail TEXT, at TEXT)""")
    return db


def already_handled(db, comment_id, username, keyword):
    row = db.execute(
        "SELECT 1 FROM sent WHERE comment_id=? OR (username=? AND keyword=? AND status='sent')",
        (comment_id, username, keyword)).fetchone()
    return row is not None


def record(db, comment, keyword, media_id, status, detail=""):
    db.execute("INSERT OR REPLACE INTO sent VALUES (?,?,?,?,?,?,?)",
               (comment["id"], comment.get("username", ""), keyword, media_id, status,
                detail[:300], datetime.now(timezone.utc).isoformat(timespec="seconds")))
    db.commit()


# ---------- one pass ----------

def run_once(ig, db, replies, cfg, now=None, out=print):
    now = now or datetime.now(timezone.utc)
    me = ig.me().get("username", "")
    keywords = list(replies)
    sent = failed = seen = 0

    defaults = cfg.get("reel_defaults") or {}
    for media in ig.recent_media(cfg["media_limit"]):
        if not media.get("comments_count", 1):
            continue
        default_kw = defaults.get(shortcode(media.get("permalink")))
        if default_kw not in replies:
            default_kw = None
        for c in ig.comments(media["id"]):
            seen += 1
            user = c.get("username", "")
            if user == me:
                continue
            # A keyword in the comment wins; otherwise the reel's default, if set.
            kw = match_keyword(c.get("text"), keywords) or default_kw
            if not kw:
                continue
            if now - parse_time(c["timestamp"]) > REPLY_WINDOW:
                continue
            if already_handled(db, c["id"], user, kw):
                continue
            if sent >= cfg["max_sends"]:
                out("Reached MAX_SENDS_PER_RUN (%d); the rest go next run." % cfg["max_sends"])
                return sent, failed, seen
            # If the creator already answered this comment by hand, leave it alone.
            try:
                if ig.already_replied(c["id"], me):
                    if not cfg["dry_run"]:
                        record(db, c, kw, media["id"], "skipped", "already answered in the comments")
                    continue
            except ApiError:
                pass
            if cfg["dry_run"]:
                out("[dry run] would DM @%s for %s  (%s)" % (user, kw, media.get("permalink", "")))
                sent += 1
                continue
            try:
                ig.private_reply(c["id"], replies[kw])
                record(db, c, kw, media["id"], "sent")
                sent += 1
                out("sent %s to @%s" % (kw, user))
                if cfg["public_reply"]:
                    try:
                        ig.public_reply(c["id"], pick_public_reply(cfg["public_reply"], c["id"]))
                    except ApiError as e:
                        out("  public reply failed: %s" % e)
            except ApiError as e:
                # Logged as failed so one broken comment is not retried forever.
                record(db, c, kw, media["id"], "failed", str(e))
                failed += 1
                out("FAILED %s to @%s: %s" % (kw, user, e))
    if not cfg.get("dm_replies", True):
        return sent, failed, seen

    # DMs: answer only when the newest message in a chat is a short keyword
    # from the other person. If you already replied, the chat is skipped.
    for conv in ig.conversations(cfg.get("conversation_limit", 25)):
        msgs = conv.get("messages", {}).get("data", [])
        if not msgs:
            continue
        m = msgs[0]
        seen += 1
        sender = m.get("from", {})
        user = sender.get("username", "")
        if user == me or not sender.get("id"):
            continue
        kw = match_dm_keyword(m.get("message"), keywords)
        if not kw:
            continue
        if now - parse_time(m["created_time"]) > DM_WINDOW:
            continue
        entry = {"id": "dm:" + m["id"], "username": user}
        if already_handled(db, entry["id"], user, kw):
            continue
        if sent >= cfg["max_sends"]:
            out("Reached MAX_SENDS_PER_RUN (%d); the rest go next run." % cfg["max_sends"])
            break
        if cfg["dry_run"]:
            out("[dry run] would answer @%s's DM for %s" % (user, kw))
            sent += 1
            continue
        try:
            ig.send_dm(sender["id"], replies[kw])
            record(db, entry, kw, "dm", "sent")
            sent += 1
            out("sent %s to @%s (DM)" % (kw, user))
        except ApiError as e:
            record(db, entry, kw, "dm", "failed", str(e))
            failed += 1
            out("FAILED %s to @%s (DM): %s" % (kw, user, e))
    return sent, failed, seen


# ---------- commands ----------

def main(argv):
    cmd = argv[1] if len(argv) > 1 else "run"
    cfg = config()

    if cmd == "log":
        for row in open_db().execute(
                "SELECT at, status, keyword, username, detail FROM sent ORDER BY at DESC LIMIT 30"):
            print("  ".join(str(x) for x in row))
        return 0

    if not cfg["token"]:
        print("IG_ACCESS_TOKEN is missing. Copy .env.example to .env and add it (see README).")
        return 2
    ig = Instagram(cfg["token"], cfg["api"], cfg["user_id"])
    replies = load_replies()

    try:
        if cmd == "check":
            print("Connected as @%s" % ig.me().get("username"))
            print("Keywords: %s" % (", ".join(replies) or "none (add files to replies/)"))
            print("Mode: %s" % ("DRY RUN (nothing is sent)" if cfg["dry_run"] else "LIVE"))
            for code, kw in cfg["reel_defaults"].items():
                print("Any comment on %s gets: %s" % (code, kw))
        elif cmd == "refresh":
            r = ig.refresh_token()
            print("New token, valid about %d days. Put this in .env as IG_ACCESS_TOKEN:"
                  % (r.get("expires_in", 0) // 86400))
            print(r.get("access_token", ""))
        elif cmd in ("run", "loop"):
            if not replies:
                print("No reply files in replies/. Nothing to do.")
                return 2
            db = open_db()
            while True:
                try:
                    sent, failed, seen = run_once(ig, db, replies, cfg)
                    print("%s  comments + chats read: %d, %s: %d, failed: %d" % (
                        datetime.now().strftime("%H:%M"), seen,
                        "would send" if cfg["dry_run"] else "sent", sent, failed))
                except ApiError as e:
                    print("API error: %s" % e)
                    if cmd == "run":
                        return 1
                if cmd == "run":
                    return 1 if failed else 0
                time.sleep(cfg["poll_seconds"])
        else:
            print(__doc__)
            return 2
    except ApiError as e:
        print("API error: %s" % e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
