import unittest
from datetime import datetime, timezone

import agent

NOW = datetime(2026, 11, 26, 20, 0, tzinfo=timezone.utc)


def c(cid, user, text, stamp="2026-11-26T18:00:00+0000"):
    return {"id": cid, "username": user, "text": text, "timestamp": stamp}


class FakeIG:
    def __init__(self, comments, fail_on=(), chats=()):
        self._comments, self.fail_on = comments, set(fail_on)
        self._chats = list(chats)
        self.dms, self.public, self.direct = [], [], []

    def conversations(self, limit):
        return self._chats

    answered = ()

    def already_replied(self, comment_id, my_username):
        return comment_id in self.answered

    def send_dm(self, recipient_id, text):
        self.direct.append((recipient_id, text))

    def me(self):
        return {"username": "mypage"}

    def recent_media(self, limit):
        return [{"id": "m1", "permalink": "https://x/m1", "comments_count": len(self._comments)}]

    def comments(self, media_id):
        return self._comments

    def private_reply(self, comment_id, text):
        if comment_id in self.fail_on:
            raise agent.ApiError("blocked")
        self.dms.append((comment_id, text))

    def public_reply(self, comment_id, text):
        self.public.append(comment_id)


def chat(mid, user, text, stamp="2026-11-26T19:00:00+0000"):
    return {"id": "t" + mid, "messages": {"data": [
        {"id": mid, "created_time": stamp, "message": text,
         "from": {"username": user, "id": "igsid-" + user}}]}}


def cfg(**kw):
    base = {"dry_run": False, "media_limit": 5, "max_sends": 30, "public_reply": ""}
    base.update(kw)
    return base


REPLIES = {"AGENT": "agent text", "CV": "cv text"}


class Tests(unittest.TestCase):
    def run_once(self, ig, db=None, **kw):
        db = db or agent.open_db(":memory:")
        return agent.run_once(ig, db, REPLIES, cfg(**kw), now=NOW, out=lambda *_: None), db

    def test_keyword_is_whole_word_any_case(self):
        self.assertEqual(agent.match_keyword("agent please!", ["AGENT"]), "AGENT")
        self.assertEqual(agent.match_keyword("Agent🔥", ["AGENT"]), "AGENT")
        self.assertIsNone(agent.match_keyword("agents are cool", ["AGENT"]))
        self.assertIsNone(agent.match_keyword("nice video", ["AGENT", "CV"]))
        self.assertIsNone(agent.match_keyword(None, ["AGENT"]))

    def test_sends_matching_reply_only(self):
        ig = FakeIG([c("1", "a", "AGENT"), c("2", "b", "great reel"), c("3", "d", "cv")])
        (sent, failed, seen), _ = self.run_once(ig)
        self.assertEqual((sent, failed, seen), (2, 0, 3))
        self.assertEqual(ig.dms, [("1", "agent text"), ("3", "cv text")])

    def test_second_run_sends_nothing(self):
        ig = FakeIG([c("1", "a", "AGENT")])
        _, db = self.run_once(ig)
        (sent, _, _), _ = self.run_once(ig, db)
        self.assertEqual(sent, 0)
        self.assertEqual(len(ig.dms), 1)

    def test_same_person_same_keyword_new_comment_not_repeated(self):
        ig = FakeIG([c("1", "a", "AGENT"), c("2", "a", "agent agent")])
        self.run_once(ig)
        self.assertEqual(len(ig.dms), 1)

    def test_skips_own_and_old_comments(self):
        ig = FakeIG([c("1", "mypage", "comment AGENT"),
                     c("2", "a", "AGENT", "2026-11-18T18:00:00+0000")])
        (sent, _, _), _ = self.run_once(ig)
        self.assertEqual(sent, 0)

    def test_dry_run_sends_and_records_nothing(self):
        ig = FakeIG([c("1", "a", "AGENT")])
        (sent, _, _), db = self.run_once(ig, dry_run=True)
        self.assertEqual(sent, 1)
        self.assertEqual(ig.dms, [])
        self.assertEqual(db.execute("SELECT COUNT(*) FROM sent").fetchone()[0], 0)

    def test_failure_is_logged_and_not_retried(self):
        ig = FakeIG([c("1", "a", "AGENT"), c("2", "b", "AGENT")], fail_on=["1"])
        (sent, failed, _), db = self.run_once(ig)
        self.assertEqual((sent, failed), (1, 1))
        (sent, failed, _), _ = self.run_once(ig, db)
        self.assertEqual((sent, failed), (0, 0))

    def test_cap_per_run(self):
        ig = FakeIG([c(str(i), "u%d" % i, "AGENT") for i in range(5)])
        (sent, _, _), _ = self.run_once(ig, max_sends=2)
        self.assertEqual(sent, 2)

    def test_public_reply_when_set(self):
        ig = FakeIG([c("1", "a", "AGENT")])
        self.run_once(ig, public_reply="Sent, check your DMs")
        self.assertEqual(ig.public, ["1"])

    def test_dm_keyword_gets_reply(self):
        ig = FakeIG([], chats=[chat("1", "a", "CV"), chat("2", "b", "cv please 🙏")])
        (sent, _, _), _ = self.run_once(ig)
        self.assertEqual(sent, 2)
        self.assertEqual(ig.direct, [("igsid-a", "cv text"), ("igsid-b", "cv text")])

    def test_dm_normal_chat_is_left_alone(self):
        ig = FakeIG([], chats=[chat("1", "a", "hey can you look at my cv and tell me what is wrong"),
                               chat("2", "b", "thanks bro")])
        (sent, _, _), _ = self.run_once(ig)
        self.assertEqual(sent, 0)

    def test_dm_skipped_when_i_replied_last_or_too_old(self):
        ig = FakeIG([], chats=[chat("1", "mypage", "CV"),
                               chat("2", "a", "CV", "2026-11-25T10:00:00+0000")])
        (sent, _, _), _ = self.run_once(ig)
        self.assertEqual(sent, 0)

    def test_dm_not_repeated_and_not_after_comment_reply(self):
        ig = FakeIG([c("1", "a", "AGENT")], chats=[chat("9", "a", "agent"), chat("8", "b", "CV")])
        _, db = self.run_once(ig)
        self.assertEqual(len(ig.dms), 1)
        self.assertEqual(ig.direct, [("igsid-b", "cv text")])
        (sent, _, _), _ = self.run_once(ig, db)
        self.assertEqual(sent, 0)

    def test_dm_replies_can_be_turned_off(self):
        ig = FakeIG([], chats=[chat("1", "a", "CV")])
        (sent, _, _), _ = self.run_once(ig, dm_replies=False)
        self.assertEqual(sent, 0)

    def test_reel_default_sends_to_any_comment(self):
        ig = FakeIG([c("1", "a", "🙌"), c("2", "b", "nice"), c("3", "d", "cv please"),
                     c("4", "mypage", "thanks all")])
        ig.recent_media = lambda limit: [{"id": "m1", "permalink": "https://www.instagram.com/reel/ABC123/", "comments_count": 4}]
        (sent, _, _), db = self.run_once(ig, reel_defaults={"ABC123": "AGENT"})
        self.assertEqual(ig.dms, [("1", "agent text"), ("2", "agent text"), ("3", "cv text")])
        (sent, _, _), _ = self.run_once(ig, db, reel_defaults={"ABC123": "AGENT"})
        self.assertEqual(sent, 0)

    def test_reel_default_only_on_that_reel(self):
        ig = FakeIG([c("1", "a", "🙌")])
        (sent, _, _), _ = self.run_once(ig, reel_defaults={"OTHER": "AGENT"})
        self.assertEqual(sent, 0)

    def test_public_reply_rotates(self):
        opts = "Sent! | Check your DMs | Just sent it"
        got = {agent.pick_public_reply(opts, str(i)) for i in range(30)}
        self.assertEqual(got, {"Sent!", "Check your DMs", "Just sent it"})
        self.assertEqual(agent.pick_public_reply("One", "5"), "One")
        self.assertEqual(agent.pick_public_reply("", "5"), "")

    def test_comment_i_already_answered_is_skipped(self):
        ig = FakeIG([c("1", "a", "AGENT"), c("2", "b", "AGENT")])
        ig.answered = {"1"}
        (sent, _, _), db = self.run_once(ig)
        self.assertEqual(ig.dms, [("2", "agent text")])
        self.assertEqual(db.execute("SELECT status FROM sent WHERE comment_id='1'").fetchone()[0], "skipped")

    def test_shortcode(self):
        self.assertEqual(agent.shortcode("https://www.instagram.com/reel/DgOc3ZaBnll/"), "DgOc3ZaBnll")
        self.assertEqual(agent.shortcode("https://www.instagram.com/p/XyZ/?x=1"), "XyZ")
        self.assertEqual(agent.shortcode(None), "")

    def test_real_reply_files_fit_the_limit(self):
        self.assertIn("PACK", agent.load_replies())


if __name__ == "__main__":
    unittest.main()
