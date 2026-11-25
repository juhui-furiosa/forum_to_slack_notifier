import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import DEFAULT, patch

import requests

import main


def topic(topic_id, created_at, *, last_posted_at=None, highest_post_number=1):
    return {
        "id": topic_id,
        "title": f"Topic {topic_id}",
        "created_at": created_at,
        "last_posted_at": last_posted_at or created_at,
        "highest_post_number": highest_post_number,
    }


class PostingWindowTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        environment = patch.multiple(
            main,
            STATE_PATH=str(Path(directory.name) / "state.json"),
            SLACK_WEBHOOK_URL="https://example.invalid/webhook",
            now_utc_iso=DEFAULT,
            fetch_latest_pages=DEFAULT,
            post_to_slack=DEFAULT,
            find_topic_message_ts=DEFAULT,
        )
        mocks = environment.start()
        self.addCleanup(environment.stop)
        self.clock = mocks["now_utc_iso"]
        self.fetch = mocks["fetch_latest_pages"]
        self.post = mocks["post_to_slack"]
        self.lookup = mocks["find_topic_message_ts"]
        self.lookup.return_value = None
        self.seed_existing_state()

    def seed_existing_state(self):
        main.save_state({
            "initialized_at": "2026-11-20T00:00:00+00:00",
            "topic_highest_post_number": {"100": 1},
        })

    def poll(self, time, *topics):
        self.clock.return_value = datetime.fromisoformat(time).astimezone(timezone.utc).isoformat()
        self.fetch.return_value = (list(topics), {})
        return main.run_once()

    def posted(self):
        return [
            (call.kwargs["title"], call.kwargs["delta_posts"], call.kwargs.get("thread_ts"))
            for call in self.post.call_args_list
        ]

    def test_posting_window_is_09_inclusive_and_19_exclusive_even_on_weekends(self):
        for time, can_post in (
            ("08:59:59", False),
            ("09:00:00", True),
            ("18:59:59", True),
            ("19:00:00", False),
        ):
            with self.subTest(time=time):
                self.seed_existing_state()
                self.post.reset_mock()
                self.poll(
                    f"2026-11-28T{time}+09:00",
                    topic(200, "2026-11-28T08:00:00+09:00"),
                )
                self.assertEqual(self.post.call_count, int(can_post))
                self.assertEqual(len(main.load_state()["pending_notifications"]), int(not can_post))

    def test_next_morning_posts_persisted_queue_in_event_order(self):
        created_at = "2026-11-27T20:00:00+09:00"
        self.poll(
            "2026-11-27T20:05:00+09:00",
            topic(201, created_at),
            topic(202, "2026-11-27T19:40:00+09:00"),
        )
        self.poll(
            "2026-11-27T21:00:00+09:00",
            topic(201, created_at, last_posted_at="2026-11-27T20:15:00+09:00", highest_post_number=2),
            topic(203, "2026-11-27T10:50:00Z"),
        )
        self.post.assert_not_called()
        self.lookup.assert_not_called()
        self.assertEqual(len(main.load_state()["pending_notifications"]), 4)

        def find_parent(topic_id):
            self.assertEqual(topic_id, 201)
            self.assertIn(("Topic 201", 0, None), self.posted())
            return "1234.567890"

        self.lookup.side_effect = find_parent
        self.poll("2026-11-28T09:00:00+09:00")
        self.assertEqual(self.posted(), [
            ("Topic 202", 0, None),
            ("Topic 203", 0, None),
            ("Topic 201", 0, None),
            ("Topic 201", 1, "1234.567890"),
        ])
        self.assertEqual(main.load_state()["pending_notifications"], [])

    def test_failed_post_leaves_pending_items_for_retry_without_replaying_success(self):
        topics = [
            topic(301, "2026-11-27T09:01:00+09:00"),
            topic(302, "2026-11-27T09:02:00+09:00"),
            topic(303, "2026-11-27T09:03:00+09:00"),
        ]
        self.post.side_effect = [None, requests.HTTPError("temporary failure")]
        with self.assertRaises(requests.HTTPError):
            self.poll("2026-11-27T09:04:00+09:00", *topics)
        self.assertEqual(self.posted(), [("Topic 301", 0, None), ("Topic 302", 0, None)])
        self.assertEqual(len(main.load_state()["pending_notifications"]), 2)

        self.post.reset_mock(side_effect=True)
        self.poll("2026-11-27T09:05:00+09:00", *topics)
        self.assertEqual(self.posted(), [("Topic 302", 0, None), ("Topic 303", 0, None)])
        self.assertEqual(main.load_state()["pending_notifications"], [])

    def test_drain_stops_if_window_closes_between_posts(self):
        def reach_closing_time(*args, **kwargs):
            self.clock.return_value = "2026-11-27T10:00:00+00:00"

        self.post.side_effect = reach_closing_time
        self.poll(
            "2026-11-27T18:59:59+09:00",
            topic(401, "2026-11-27T18:58:00+09:00"),
            topic(402, "2026-11-27T18:59:00+09:00"),
        )
        self.assertEqual(self.posted(), [("Topic 401", 0, None)])
        self.assertEqual(len(main.load_state()["pending_notifications"]), 1)

    def test_reply_is_held_if_parent_lookup_crosses_closing_time(self):
        def find_parent(topic_id):
            self.clock.return_value = "2026-11-27T10:00:00+00:00"
            return "1234.567890"

        self.lookup.side_effect = find_parent
        self.poll(
            "2026-11-27T18:59:59+09:00",
            topic(100, "2026-11-20T08:00:00+09:00",
                  last_posted_at="2026-11-27T18:59:00+09:00", highest_post_number=2),
        )
        self.post.assert_not_called()
        self.assertEqual(len(main.load_state()["pending_notifications"]), 1)

    def test_reply_waits_if_new_parent_is_temporarily_missing_from_slack_history(self):
        created_at = "2026-11-27T19:05:00+09:00"
        self.poll("2026-11-27T19:06:00+09:00", topic(501, created_at))
        self.poll(
            "2026-11-27T19:08:00+09:00",
            topic(501, created_at, last_posted_at="2026-11-27T19:07:00+09:00", highest_post_number=2),
        )
        self.lookup.side_effect = [None, None, "1234.567890"]
        with patch.multiple(main, SLACK_BOT_TOKEN="fake-token", FORUM_CHANNEL_ID="C0123"):
            self.poll("2026-11-28T09:00:00+09:00")
            self.assertEqual(self.posted(), [("Topic 501", 0, None)])
            self.assertEqual(len(main.load_state()["pending_notifications"]), 1)

            self.poll("2026-11-28T09:01:00+09:00")
            self.assertEqual(self.posted(), [("Topic 501", 0, None)])
            self.assertEqual(len(main.load_state()["pending_notifications"]), 1)

            self.poll("2026-11-28T09:02:00+09:00")
        self.assertEqual(self.posted(), [("Topic 501", 0, None), ("Topic 501", 1, "1234.567890")])
        self.assertEqual(main.load_state()["pending_notifications"], [])

    def test_existing_queue_can_drain_even_if_forum_fetch_fails(self):
        self.poll("2026-11-27T19:06:00+09:00", topic(601, "2026-11-27T19:05:00+09:00"))
        self.fetch.side_effect = requests.ConnectionError("forum temporarily unreachable")
        with self.assertRaises(requests.ConnectionError):
            self.poll("2026-11-28T09:00:00+09:00")
        self.assertEqual(self.posted(), [("Topic 601", 0, None)])
        self.assertEqual(main.load_state()["pending_notifications"], [])
