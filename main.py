#!/usr/bin/env python3

import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple, Optional
from zoneinfo import ZoneInfo
import argparse
import time

import requests
"""
맨처음 실행 시, forumn_notifier_state.json 을 세팅해서 지금 포럼 글의 상태를 저장합니다. 이후 실행 시에는 그 상태와 비교하여 새 글이나 새 댓글이 있으면 슬랙으로 알림을 보냅니다.
새 글: topic_posts_count 에 topic_id 추가, 알림
새 댓글: /latest.json의 하나의 post에 대해 posts_count 가 증가했으면 있던 topic_id 의 posts_count 증가, 알림
"""

FORUM_BASE = "https://forums.furiosa.ai"
LATEST_JSON_URL = f"{FORUM_BASE}/latest.json"

STATE_PATH = os.environ["STATE_PATH"]
SLACK_WEBHOOK_URL = os.environ["SLACK_WEBHOOK_URL"]

TIMEOUT_SEC = 15
MAX_PAGES = 3  # latest.json은 페이지당 고정 개수(30개)라, 누락 방지하려면 pages를 늘릴 수 있음
KST = ZoneInfo("Asia/Seoul")

def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def iso_utc_to_kst_str(ts: str) -> str:
    dt_utc = parse_iso(ts)  # aware datetime in UTC
    dt_kst = dt_utc.astimezone(KST)
    return dt_kst.strftime("%Y-%m-%d %H:%M:%S KST")


def parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def load_state() -> Dict[str, Any]:
    if not os.path.exists(STATE_PATH):
        return {"initialized_at": None, "topic_posts_count": {}}

    with open(STATE_PATH, "r", encoding="utf-8") as f:
        state = json.load(f)

    # 구버전 호환
    if "initialized_at" not in state:
        state["initialized_at"] = None
    if "topic_posts_count" not in state:
        state["topic_posts_count"] = {}

    return state


def save_state(state: Dict[str, Any]) -> None:
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)


def fetch_latest_pages(max_pages: int = 200) -> Tuple[List[Dict[str, Any]], Dict[int, Dict[str, Any]]]:
    headers = {
        "User-Agent": "furiosa-forum-slack-notifier/1.0",
        "Accept": "application/json",
    }

    topics_by_id: Dict[int, Dict[str, Any]] = {}
    users_by_id: Dict[int, Dict[str, Any]] = {}

    next_url = f"{FORUM_BASE}/latest.json?no_definitions=true"

    visited = set()
    pages = 0

    while next_url and pages < max_pages:
        if next_url in visited:
            # 방어: 같은 URL 반복이면 중단
            break
        visited.add(next_url)

        r = requests.get(next_url, headers=headers, timeout=TIMEOUT_SEC)
        r.raise_for_status()
        data = r.json()

        for u in data.get("users", []) or []:
            uid = int(u.get("id"))
            users_by_id[uid] = u

        topic_list = (data.get("topic_list") or {})
        topics = (topic_list.get("topics") or [])
        for t in topics:
            tid = int(t.get("id", 0))
            if tid:
                topics_by_id[tid] = t

        more = topic_list.get("more_topics_url")
        if not more:
            break

        if more.startswith("/latest?"):
            query = more.split("?", 1)[1]
            next_url = f"{FORUM_BASE}/latest.json?{query}"
        elif more.startswith("/"):
            next_url = f"{FORUM_BASE}{more}"
            # 혹시 html 경로면 json으로 바꿔주기
            if next_url.startswith(f"{FORUM_BASE}/latest?"):
                query = next_url.split("?", 1)[1]
                next_url = f"{FORUM_BASE}/latest.json?{query}"
        else:
            next_url = more
            if next_url.startswith(f"{FORUM_BASE}/latest?"):
                query = next_url.split("?", 1)[1]
                next_url = f"{FORUM_BASE}/latest.json?{query}"

        pages += 1

    return list(topics_by_id.values()), users_by_id


def topic_url(topic: Dict[str, Any]) -> str:
    slug = topic.get("slug") or "topic"
    tid = topic["id"]
    return f"{FORUM_BASE}/t/{slug}/{tid}"


def get_original_poster(topic: Dict[str, Any], users_by_id: Dict[int, Dict[str, Any]]) -> Tuple[str, str]:
    posters = topic.get("posters") or []
    op_user_id: Optional[int] = None

    for p in posters:
        desc = (p.get("description") or "").lower()
        if "original poster" in desc:
            try:
                op_user_id = int(p.get("user_id"))
                break
            except Exception:
                pass

    if op_user_id is None:
        return ("unknown", "Unknown")

    u = users_by_id.get(op_user_id) or {}
    username = u.get("username") or "unknown"
    name = u.get("name") or username
    return (username, name)


def post_to_slack(
    webhook_url: str,
    title: str,
    url: str,
    event_time: str,
    author_username: str,
    author_name: str,
    tags: List[str],
    delta_posts: int,
) -> None:
    mention = "@channel"  # @here 원하면 "@here"로 바꾸기

    tags_text = ", ".join(tags) if tags else "-"
    activity_line = "New topic" if delta_posts <= 0 else f"New replies: `+{delta_posts}`"

    payload = {
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": "📢 New activity on FuriosaAI Forum"}},
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"{mention} *<{url}|{title}>*\n"
                        f"• Author: `{author_name}`\n"
                        f"• Tags: `{tags_text}`\n"
                        f"• Type: {activity_line}\n"
                        f"• Time: `{event_time}`"
                    ),
                },
            },
            {"type": "context", "elements": [{"type": "mrkdwn", "text": f"{FORUM_BASE}"}]},
        ]
    }

    resp = requests.post(webhook_url, json=payload, timeout=TIMEOUT_SEC)
    resp.raise_for_status()


def run_once() -> int:
    if not SLACK_WEBHOOK_URL:
        print("ERROR: SLACK_WEBHOOK_URL env var is required.", file=sys.stderr)
        return 2

    state = load_state()

    topics, users_by_id = fetch_latest_pages()
    if not topics:
        print("No topics found.")
        return 0

    # 최초 실행: 기준만 세팅하고 알림은 보내지 않음
    if not state.get("initialized_at"):
        state["initialized_at"] = now_utc_iso()
        topic_posts_count: Dict[str, int] = state.get("topic_posts_count", {})
        for t in topics:
            tid = int(t.get("id", 0))
            pc = int(t.get("posts_count", 0) or 0)
            if tid:
                topic_posts_count[str(tid)] = pc
        state["topic_posts_count"] = topic_posts_count
        save_state(state)
        print(f"Initialized state (no notifications). initialized_at={state['initialized_at']}")
        return 0

    initialized_at = parse_iso(state["initialized_at"])
    topic_posts_count: Dict[str, int] = state.get("topic_posts_count", {}) or {}

    def activity_key(t: Dict[str, Any]) -> str:
        return t.get("last_posted_at") or t.get("bumped_at") or t.get("created_at") or ""

    topics.sort(key=activity_key)

    notified = 0

    for t in topics:
        tid = int(t.get("id", 0))
        if not tid:
            continue

        title = t.get("title", "(no title)")
        url = topic_url(t)

        tags = t.get("tags") or []
        if not isinstance(tags, list):
            tags = []

        author_username, author_name = get_original_poster(t, users_by_id)

        current_pc = int(t.get("posts_count", 0) or 0)
        prev_pc = topic_posts_count.get(str(tid))

        if prev_pc is None:
            topic_posts_count[str(tid)] = current_pc

            created_at_raw = t.get("created_at")
            if created_at_raw:
                created_at = parse_iso(created_at_raw)
                if created_at > initialized_at:
                    post_to_slack(
                        SLACK_WEBHOOK_URL,
                        title=title,
                        url=url,
                        event_time=iso_utc_to_kst_str(created_at_raw),
                        author_username=author_username,
                        author_name=author_name,
                        tags=tags,
                        delta_posts=0,
                    )
                    print(f"Notified new topic: {tid} {title}")
                    notified += 1
            continue

        if current_pc > prev_pc:
            delta = current_pc - prev_pc

            topic_posts_count[str(tid)] = current_pc

            event_time = t.get("last_posted_at") or t.get("bumped_at") or now_utc_iso()

            post_to_slack(
                SLACK_WEBHOOK_URL,
                title=title,
                url=url,
                event_time=iso_utc_to_kst_str(event_time),
                author_username=author_username,
                author_name=author_name,
                tags=tags,
                delta_posts=delta,
            )
            print(f"Notified replies: {tid} {title} (+{delta})")
            notified += 1
        else:
            topic_posts_count[str(tid)] = max(prev_pc, current_pc)

    state["topic_posts_count"] = topic_posts_count
    save_state(state)

    print(f"Done. notified={notified}")
    return 0


def cli_main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", action="store_true", help="Run forever (daemon mode)")
    parser.add_argument("--interval-sec", type=int, default=60, help="Polling interval in seconds")
    args = parser.parse_args()

    if not args.loop:
        return run_once()

    # k8s 종료(SIGTERM) 들어오면 while 탈출하도록 처리
    keep_running = True

    def _handle_sigterm(signum, frame):
        nonlocal keep_running
        keep_running = False

    try:
        import signal
        signal.signal(signal.SIGTERM, _handle_sigterm)
        signal.signal(signal.SIGINT, _handle_sigterm)
    except Exception:
        pass

    while keep_running:
        try:
            rc = run_once()
            # run_once가 2(설정오류) 같은 치명적 오류면 계속 돌 의미가 없으니 종료
            if rc != 0 and rc != 0:
                pass
        except Exception as e:
            print(f"ERROR: run_once failed: {e}", file=sys.stderr)

        # sleep 중에도 SIGTERM 받으면 빨리 종료되도록 1초 단위로 쪼갬
        for _ in range(max(1, args.interval_sec)):
            if not keep_running:
                break
            time.sleep(1)

    print("Exiting daemon loop.")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main())
