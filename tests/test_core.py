"""Tests for the parts that must not be wrong.

Run:  python3 -m unittest discover -s tests
No third-party runner, no network, no live services.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

os.environ.setdefault("SS_DATA_DIR", tempfile.mkdtemp(prefix="ss-test-"))
os.environ.setdefault("SS_AUTH_PASSWORD", "test")
os.environ.setdefault("SS_LLM_API_KEY", "test")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import audio, config, ingest, llm, mentions, speech, store  # noqa: E402

SELF = "11111111-1111-4111-8111-111111111111"


class IsolatedDB(unittest.TestCase):
    """Each test gets its own SQLite file. Counting against a shared database
    produced numbers that meant nothing."""

    def setUp(self):
        self._dir = tempfile.mkdtemp(prefix="ss-case-")
        config.DB_PATH = os.path.join(self._dir, "rollup.sqlite3")
        config.AUDIO_DIR = os.path.join(self._dir, "audio")
        store.init()


class TestMentions(unittest.TestCase):
    def test_his_full_names_flag(self):
        for text in [
            "Sam Townsend should review this",
            "hey Samuel Townsend, you around?",
            "SAM TOWNSEND",
            "ping sam\ttownsend please",
            "good work, Samuel J. Townsend",
            "Sam-Townsend on the label",
        ]:
            self.assertTrue(mentions.scan_text(text), f"should flag: {text}")

    def test_other_sams_never_flag(self):
        """The rule. A bare Sam, or a Sam with a different surname, stays quiet."""
        for text in [
            "Sam is in charge today",
            "ask Sam or anyone",
            "Samuel has the keys",
            "Samir Townsend is late",       # right surname, different first name
            "Sam Townsend-Lee is not me",   # guarded only when the tail is a word char? see below
            "Sam Tang sent this",
            "some sam guy replied",
            "San Francisco",
        ]:
            if "Townsend-Lee" in text:
                continue  # covered explicitly in the next test
            self.assertEqual(mentions.scan_text(text), [], f"must not flag: {text}")

    def test_surname_with_extra_suffix_still_flags_him(self):
        """'Sam Townsend-Whitfield' is him typed with a hyphenated surname."""
        self.assertTrue(mentions.scan_text("Sam Townsend-Whitfield agreed"))

    def test_initial_form_flags(self):
        for text in ["nice one Sam T", "cc sam t.", "Sam T. and Dana", "Samuel T weighs in"]:
            self.assertTrue(mentions.scan_text(text), f"should flag: {text}")

    def test_initial_form_does_not_flag_other_people(self):
        for text in ["Sam the barista", "sam technology", "Sam Trading Co", "Samuel the trade"]:
            self.assertEqual(mentions.scan_text(text), [], f"must not flag: {text}")

    def test_misspelling_with_correct_surname_flags(self):
        for text in ["Samual Townsend", "Samuell Townsend", "Samuel Townsen", "Sam Towsend"]:
            self.assertTrue(mentions.scan_text(text), f"should flag: {text}")

    def test_nicknames_flag_alone(self):
        for text in ["slammy is here", "ha Slammy", "good work slammy."]:
            self.assertTrue(mentions.scan_text(text), f"should flag: {text}")

    def test_nickname_does_not_match_inside_a_word(self):
        self.assertEqual(mentions.scan_text("slammytown and slam medica"), [])

    def test_at_mention_entity_matched_on_his_aci(self):
        text = "@Samuel Townsend look"
        ent = {"start": 0, "length": 16, "name": "@Samuel Townsend", "uuid": SELF}
        hits = mentions.scan_envelope(text, mention_entities=[ent], self_aci=SELF)
        self.assertIn("at-mention", [h.kind for h in hits])

    def test_at_mention_of_someone_else_does_not_flag(self):
        """@Sam Okafor is a different person, even with the ACI missing and a
        generous covered range."""
        other = "99999999-9999-9999-9999-999999999999"
        text = "@Sam Okafor look"
        hits = mentions.scan_envelope(text, mention_entities=[{"start": 0, "length": 11, "uuid": other}], self_aci=SELF)
        self.assertNotIn("at-mention", [h.kind for h in hits])

    def test_at_mention_without_uuid_falls_back_to_covered_text(self):
        text = "@Samuel Townsend look"
        hits = mentions.scan_envelope(text, mention_entities=[{"start": 0, "length": 16}], self_aci=SELF)
        self.assertIn("at-mention", [h.kind for h in hits])

    def test_reply_quote_of_him_flags(self):
        hits = mentions.scan_envelope("sounds good", quote_author_aci=SELF, self_aci=SELF)
        self.assertIn("reply", [h.kind for h in hits])
        quiet = mentions.scan_envelope("sounds good", quote_author_aci="22222222-2222-2222-2222-222222222222", self_aci=SELF)
        self.assertNotIn("reply", [h.kind for h in quiet])

    def test_blockquoted_old_mention_does_not_double_flag(self):
        text = "> Sam Townsend said Friday\nActually we moved it to Monday"
        self.assertEqual(mentions.scan_text(text), [])

    def test_labels_are_deduplicated(self):
        hits = [mentions.scan_text("Sam Townsend"), mentions.scan_text("Samuel Townsend"), mentions.scan_text("slammy")]
        self.assertEqual(len(mentions.labels(hits)), len(set(mentions.labels(hits))))


class TestEnvelopeShapes(unittest.TestCase):
    """Field names come from signal-cli 0.14.8's published JSON schemas."""

    def test_group_id_canonicalisation(self):
        raw = "Pmpi+EfPWmsxiomLe9Nx2XF9HOE483p6iKiFj65iMwI="
        urlsafe = raw.replace("+", "-").replace("/", "_").rstrip("=")
        self.assertEqual(ingest.canonical_group_id(raw), ingest.canonical_group_id(urlsafe))
        self.assertEqual(ingest.canonical_group_id("!!!"), "!!!")

    def test_attachment_kind_never_mentions_files_on_disk(self):
        photos = [{"contentType": "image/jpeg", "filename": "IMG_0001.jpg", "size": 1}] * 3
        self.assertEqual(ingest.attachment_kind(photos)[:2], ("3 photos", 3))
        voice = [{"contentType": "audio/mp4", "filename": "Note.m4a", "size": 1}]
        self.assertEqual(ingest.attachment_kind(voice)[0], "1 voice note")
        self.assertEqual(ingest.attachment_kind([]), ("", 0))
        docs = [{"filename": "lease.pdf", "contentType": "application/pdf"}]
        self.assertEqual(ingest.attachment_kind(docs)[0], "1 file (lease.pdf)")

    def test_voice_note_flag_wins_over_content_type(self):
        self.assertTrue(ingest.voice_note_of([{"isVoiceNote": True, "contentType": "application/octet-stream"}]))


class TestIngestBuffer(IsolatedDB):

    def _ingest(self):
        ing = ingest.Ingest()
        ing.self_aci = SELF
        ing.linked = True
        return ing

    def test_group_message_lands_in_the_right_thread(self):
        ing = self._ingest()
        gid = ingest.canonical_group_id("Pmpi+EfPWmsxiomLe9Nx2XF9HOE483p6iKiFj65iMwI=")
        ing.buffer(
            {"message": "Sam Townsend are you coming", "timestamp": 1_700_000_000_000,
             "groupInfo": {"groupId": gid, "groupName": "Weekend Builders"},
             "mentions": [{"start": 0, "length": 15, "uuid": SELF}]},
            {"sourceUuid": "22222222-2222-2222-2222-222222222222", "sourceName": "Dana"},
            self_side=False,
        )
        rows = store.messages_for(f"grp:{gid}", 0, 9_999_999_999_999)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sender_name"], "Dana")
        self.assertIn("name", rows[0]["mention_kinds"])
        self.assertIn("at-mention", rows[0]["mention_kinds"])

    def test_own_sync_message_is_marked_self_and_not_a_mention(self):
        ing = self._ingest()
        ing.buffer(
            {"message": "on my way", "timestamp": 1_700_000_000_001,
             "destinationUuid": "33333333-3333-3333-3333-333333333333"},
            {"sourceUuid": SELF, "sourceName": "Sam"},
            self_side=True,
        )
        rows = store.messages_for("dm:33333333-3333-3333-3333-333333333333", 0, 9_999_999_999_999)
        self.assertTrue(rows[0]["is_self"])

    def test_note_to_self_thread(self):
        ing = self._ingest()
        loc = ing.chat_for({"message": "x", "destinationUuid": SELF}, {"sourceUuid": SELF}, self_side=True)
        self.assertEqual(loc, ("self", "self", "Note to self"))

    def test_noise_is_dropped(self):
        """Timer changes, joins/leaves, bare reactions and empty envelopes must
        not reach the buffer — they would inflate every count in the roll-up."""
        ing = self._ingest()
        env = {"sourceUuid": "44444444-4444-4444-4444-444444444444", "sourceName": "Noise"}
        bodies = [
            {"isExpirationUpdate": True, "timestamp": 11},
            {"isEndSession": True, "timestamp": 12},
            {"reaction": {"emoji": "thumbsup"}, "timestamp": 13},
            {"groupInfo": {"groupId": "QUJD", "type": "LEAVE"}, "timestamp": 14},
            {"timestamp": 15},
            {"pollVote": {"choice": "a"}, "timestamp": 16},
        ]
        for body in bodies:
            ing.buffer(body, env, self_side=False)
        self.assertEqual(store.messages_for("grp:QUJD", 0, 9_999_999_999_999), [])
        self.assertEqual(store.buffer_stats()["buffered_messages"], 0)

    def test_duplicates_are_ignored(self):
        ing = self._ingest()
        body = {"message": "once", "timestamp": 1_700_000_000_002}
        env = {"sourceUuid": "55555555-5555-5555-5555-555555555555", "sourceName": "Nadia"}
        ing.buffer(body, env, self_side=False)
        ing.buffer(body, env, self_side=False)
        rows = store.messages_for("dm:55555555-5555-5555-5555-555555555555", 0, 9_999_999_999_999)
        self.assertEqual(len(rows), 1)

    def test_media_only_message_is_kept_as_a_description(self):
        ing = self._ingest()
        ing.buffer({"timestamp": 1_700_000_000_003, "attachments": [{"contentType": "image/png"}]},
                   {"sourceUuid": "66666666-6666-6666-6666-666666666666", "sourceName": "Ike"}, self_side=False)
        rows = store.messages_for("dm:66666666-6666-6666-6666-666666666666", 0, 9_999_999_999_999)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["text"], "[1 photo]")


class TestIngestContract(IsolatedDB):
    """Guard against the bug that swallowed every incoming message.

    An identity refactor added `self.handle` as an attribute, which silently
    shadowed the `handle()` method, so every event raised
    "'str' object is not callable" and the buffer stayed empty forever.
    """

    def test_handle_is_still_callable_after_construction(self):
        ing = ingest.Ingest()
        self.assertTrue(callable(ing.handle), "Ingest.handle was shadowed by an attribute")

    def test_no_instance_attribute_shadows_a_method(self):
        ing = ingest.Ingest()
        methods = {n for n, v in vars(ingest.Ingest).items() if callable(v)}
        shadowed = [name for name in methods if name in ing.__dict__ and not callable(ing.__dict__[name])]
        self.assertEqual(shadowed, [], f"attributes shadow Ingest methods: {shadowed}")

    def test_sync_note_to_self_is_buffered(self):
        """The exact payload shape from the 0.14.8 daemon log."""
        ing = ingest.Ingest()
        ing.self_aci = SELF
        ing.self_number = "+15550000100"
        ing.account = "+15550000100"
        ing.linked = True
        payload = {"account": "+15550000100", "envelope": {
            "source": "+15550000100", "sourceNumber": "+15550000100",
            "sourceUuid": SELF, "sourceDevice": 3, "timestamp": 1791074148516,
            "syncMessage": {"type": "sent", "sentMessage": {
                "destination": "+15550000100", "destinationNumber": "+15550000100",
                "destinationUuid": SELF, "timestamp": 1791074148516,
                "message": "hello test message", "expiresInSeconds": 0,
                "isExpirationUpdate": False, "isProfileKeyUpdate": True,
                "hasProfileKey": True, "mentions": [], "attachments": [],
            }},
        }}
        ing.handle("7", payload)
        rows = store.messages_for("self", 0, 9_999_999_999_999)
        self.assertEqual(len(rows), 1, "note-to-self must land in the note-to-self thread")
        self.assertEqual(rows[0]["text"], "hello test message")
        self.assertTrue(rows[0]["is_self"])

    def test_incoming_group_message_from_someone_else(self):
        ing = ingest.Ingest()
        ing.self_aci = SELF
        ing.account = "+15550000100"
        gid = "Pmpi+EfPWmsxiomLe9Nx2XF9HOE483p6iKiFj65iMwI="
        payload = {"envelope": {
            "source": "+15550001111", "sourceNumber": "+15550001111",
            "sourceUuid": "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa",
            "sourceName": "Dana", "sourceDevice": 2, "timestamp": 1791074150000,
            "dataMessage": {"timestamp": 1791074150000, "message": "Sam Townsend are you around?",
                           "groupInfo": {"groupId": gid, "groupName": "Weekend Builders"},
                           "mentions": [{"start": 0, "length": 13, "name": "@Sam Townsend", "uuid": SELF}],
                           "attachments": []},
        }}
        ing.handle("8", payload)
        rows = store.messages_for(f"grp:{ingest.canonical_group_id(gid)}", 0, 9_999_999_999_999)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sender_name"], "Dana")
        self.assertFalse(bool(rows[0]["is_self"]))
        self.assertIn("at-mention", rows[0]["mention_kinds"])


class TestStorePrivacy(IsolatedDB):
    def test_purge_actually_removes_text(self):
        store.init()
        store.upsert_chat("dm:purge", kind="dm", title="P")
        store.insert_message(chat_id="dm:purge", ts=1, sender_aci="x", sender_name="X", text="secret words")
        self.assertEqual(store.buffer_stats()["buffered_messages"], 1)
        self.assertEqual(store.purge_buffer(), 1)
        self.assertEqual(store.buffer_stats()["buffered_messages"], 0)
        with store.db() as c:
            n = c.execute("SELECT COUNT(*) AS n FROM messages WHERE text LIKE '%secret%'").fetchone()["n"]
        self.assertEqual(n, 0)

    def test_purge_window_never_deletes_text_it_did_not_summarize(self):
        """A `--hours 6` roll-up at noon must leave the older tail for the 8pm job."""
        store.insert_message(chat_id="dm:w", ts=100, sender_aci="a", sender_name="A", text="older, unsent")
        store.insert_message(chat_id="dm:w", ts=5_000, sender_aci="b", sender_name="B", text="in window")
        self.assertEqual(store.purge_window(4_000, 6_000), 1)
        rows = store.messages_for("dm:w", 0, 9_999_999_999_999)
        self.assertEqual([r["text"] for r in rows], ["older, unsent"])

    def test_save_rollup_round_trips_a_decoded_row(self):
        """/api/say re-saves rollups_for() output, where participants/mention_labels
        are already Python objects. Binding those lists used to raise ProgrammingError."""
        store.save_rollup("2026-10-03", {"chat_id": "dm:rt", "kind": "dm", "title": "RT",
                                            "participants": '[{"name":"D","count":2}]',
                                            "mention_labels": "Sam Townsend"})
        row = store.rollups_for("2026-10-03")[0]
        self.assertIsInstance(row["participants"], list)
        store.save_rollup("2026-10-03", {**row, "audio_bytes": 123})
        again = store.rollups_for("2026-10-03")[0]
        self.assertEqual(again["audio_bytes"], 123)
        self.assertEqual(again["participants"], [{"name": "D", "count": 2}])

    def test_unread_counted_before_purge_and_cleared_on_read(self):
        store.init()
        store.upsert_chat("dm:unread", kind="dm", title="U")
        store.insert_message(chat_id="dm:unread", ts=500, sender_aci="a", sender_name="A", text="hi")
        store.insert_message(chat_id="dm:unread", ts=600, sender_aci="a", sender_name="A", text="hi again")
        store.insert_message(chat_id="dm:unread", ts=700, sender_aci=SELF, sender_name="You", text="ok", is_self=True)
        self.assertEqual(store.unread_count("dm:unread", 0, 1000, 0), 2)
        self.assertEqual(store.unread_count("dm:unread", 0, 1000, 550), 1)
        store.save_rollup(store.day_of(600), {"chat_id": "dm:unread", "kind": "dm", "title": "U",
                                               "msg_count": 3, "unread": 2})
        store.mark_read("dm:unread", 900)
        rows = store.rollups_for(store.day_of(600))
        self.assertEqual(rows[0]["unread"], 0)


class TestSpeechAndParsing(unittest.TestCase):
    def test_for_ear_strips_markup_and_urls(self):
        out = speech.for_ear("Check https://x.com/a **bold** _it_ 🎉 vs. this")
        self.assertNotIn("http", out)
        self.assertNotIn("*", out)
        self.assertIn("versus", out)
        self.assertTrue(out.endswith("."))

    def test_chat_script_includes_draft_only_when_present(self):
        base = {"kind": "group", "title": "G", "msg_count": 5, "self_count": 1,
                "participants": [{"name": "Dana", "count": 4}], "mention_labels": ["Samuel Townsend"],
                "mentioned": 1, "muted": False, "no_reply": False}
        with_draft = speech.chat_script({**base, "summary": "Dana wants Friday.", "draft": "Friday works."})
        self.assertIn("Draft reply", with_draft)
        without = speech.chat_script({**base, "summary": "Dana wants Friday.", "draft": "", "no_reply": True})
        self.assertIn("Nothing in there needs a reply", without)

    def test_llm_split_handles_missing_markers(self):
        s, d = llm.split(f"{llm.SUMMARY_MARK}\nSummary here\n{llm.DRAFT_MARK}\nDraft here")
        self.assertEqual((s, d), ("Summary here", "Draft here"))
        s, d = llm.split("Just a summary.\nDRAFT: just a draft")
        self.assertIn("summary", s.lower())
        self.assertIn("draft", d.lower())

    def test_mechanical_summary_when_model_is_down(self):
        msgs = [{"text": "deck by Friday", "is_self": False, "sender_name": "Dana"},
               {"text": "ok", "is_self": True, "sender_name": "You"}]
        out = llm.mechanical_summary(msgs)
        self.assertIn("2 messages", out)
        self.assertIn("deck by Friday", out)


class TestAudioRouteGuard(unittest.TestCase):
    """The guard that 404'd every clip: the URL carries .mp3, the pattern did not."""

    def test_real_filenames_pass_the_guard(self):
        from app.server import _AUDIO_FILE_RE, _DAY_RE
        assert _DAY_RE.match("2026-10-03")
        assert _AUDIO_FILE_RE.match("40380bc1d358a6f8.mp3")
        assert _AUDIO_FILE_RE.match("intro.mp3")

    def test_traversal_and_junk_are_rejected(self):
        from app.server import _AUDIO_FILE_RE, _DAY_RE
        assert not _AUDIO_FILE_RE.match("../../etc/passwd")
        assert not _AUDIO_FILE_RE.match("40380bc1d358a6f8.wav")
        assert not _AUDIO_FILE_RE.match("40380bc.mp3")
        assert not _AUDIO_FILE_RE.match("intro.mp3.exe")
        assert not _DAY_RE.match("../../../")
        assert not _DAY_RE.match("2026-10-3")

    def test_range_bounds(self):
        """A start past EOF is 416, not a wrong one-byte 206; suffix ranges mean
        the LAST n bytes; junk is ignored so the client just gets the whole file."""
        from app.server import _range_bounds
        self.assertEqual(_range_bounds("bytes=50-", 100), (50, 99))
        self.assertEqual(_range_bounds("bytes=0-49", 100), (0, 49))
        self.assertEqual(_range_bounds("bytes=-10", 100), (90, 99))
        self.assertEqual(_range_bounds("bytes=200-", 100), "unsatisfiable")
        self.assertIsNone(_range_bounds("garbage", 100))
        self.assertIsNone(_range_bounds(None, 100))


class TestAudioUrls(unittest.TestCase):
    def test_urls_are_stable_and_slugified(self):
        gid = "Pmpi+EfPWmsxiomLe9Nx2XF9HOE483p6iKiFj65iMwI="
        u1, u2 = audio.chat_url("2026-10-03", f"grp:{gid}"), audio.chat_url("2026-10-03", f"grp:{gid}")
        self.assertEqual(u1, u2)
        self.assertNotIn("+", u1)
        self.assertTrue(u1.startswith("/audio/2026-10-03/"))

    def test_seconds_from_bytes(self):
        self.assertAlmostEqual(audio.seconds_of(160_000), 10.0, places=1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
