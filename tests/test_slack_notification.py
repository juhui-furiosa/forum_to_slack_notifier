import unittest
from unittest.mock import patch

from main import post_to_slack


class SlackNotificationTest(unittest.TestCase):
    def test_link_label_escapes_slack_control_characters(self):
        url = "https://forums.furiosa.ai/t/example/516"
        for title, label in (
            ("일반 포럼 제목", "일반 포럼 제목"),
            ('TP<8 & TP>2 "번들" 문의', 'TP&lt;8 &amp; TP&gt;2 "번들" 문의'),
        ):
            for delta_posts, thread_ts in ((0, None), (1, "12.345678"), (1, None)):
                with self.subTest(title=title, delta_posts=delta_posts, thread_ts=thread_ts):
                    with patch("main.requests.post") as post:
                        post_to_slack(
                            "https://example.invalid/webhook",
                            title=title,
                            url=url,
                            event_time="2026-11-24 14:00:25 KST",
                            author_username="author",
                            author_name="Author",
                            tags=["rngd"],
                            delta_posts=delta_posts,
                            thread_ts=thread_ts,
                        )

                    payload = post.call_args.kwargs["json"]
                    section = next(block for block in payload["blocks"] if block["type"] == "section")
                    prefix = "" if thread_ts else "@channel "
                    self.assertEqual(section["text"]["text"].splitlines()[0], f"{prefix}*<{url}|{label}>*")
                    if thread_ts:
                        self.assertEqual(payload["thread_ts"], thread_ts)
                        self.assertEqual(payload["text"], f"New replies: +1 - {label}")


if __name__ == "__main__":
    unittest.main()
