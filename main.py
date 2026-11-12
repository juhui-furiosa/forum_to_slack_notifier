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

감지 기준은 topic 별 highest_post_number 입니다. (posts_count 는 글 삭제 시 감소해서,
'중간 글 삭제 후 재작성' 같은 경우 알림을 놓칠 수 있어 사용하지 않습니다.)
새 글: 처음 보는 topic 이고 created_at 이 초기화 시점 이후면 알림 (저자 = 원글 작성자)
새 댓글: highest_post_number 가 이전보다 커지면 알림 (저자 = 최근 작성자)
저장값은 증가/감소 무관하게 항상 현재 highest_post_number 로 동기화합니다.
"""

FORUM_BASE = "https://forums.furiosa.ai"
LATEST_JSON_URL = f"{FORUM_BASE}/latest.json"

STATE_PATH = os.environ.get("STATE_PATH", "")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")

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
    # 신규: 삭제에 강건한 highest_post_number 기반 추적
    if "topic_highest_post_number" not in state:
        state["topic_highest_post_number"] = {}

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
 
def normalize_tags(raw: Any) -> List[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        out = []
        for x in raw:
            if isinstance(x, str):
                out.append(x)
            elif isinstance(x, dict):
                # dict면 name/slug 같은 키를 우선 사용
                out.append(str(x.get("name") or x.get("slug") or x))
            else:
                out.append(str(x))
        return out
    # tags가 list가 아닌 경우도 방어
    if isinstance(raw, str):
        return [raw]
    return [str(raw)]

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


def get_latest_poster(topic: Dict[str, Any], users_by_id: Dict[int, Dict[str, Any]]) -> Tuple[str, str]:
    """가장 최근에 글(답글)을 쓴 작성자를 반환. 답글 알림의 실제 저자 표기용."""
    posters = topic.get("posters") or []
    latest_user_id: Optional[int] = None

    # 1순위: extras 에 "latest" 표시가 있는 poster
    for p in posters:
        extras = (p.get("extras") or "").lower()
        if "latest" in extras:
            try:
                latest_user_id = int(p.get("user_id"))
                break
            except Exception:
                pass

    # 2순위: description 에 "most recent" / "latest" 가 있는 poster
    if latest_user_id is None:
        for p in posters:
            desc = (p.get("description") or "").lower()
            if "most recent" in desc or "latest" in desc:
                try:
                    latest_user_id = int(p.get("user_id"))
                    break
                except Exception:
                    pass

    # 3순위: 마지막 poster (관례상 가장 최근 작성자)
    if latest_user_id is None and posters:
        try:
            latest_user_id = int(posters[-1].get("user_id"))
        except Exception:
            latest_user_id = None

    if latest_user_id is None:
        return ("unknown", "Unknown")

    u = users_by_id.get(latest_user_id) or {}
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
    dry_run: bool = False,
) -> None:
    mention = "@channel"  # @here 원하면 "@here"로 바꾸기

    tags_text = ", ".join(map(str, tags)) if tags else "-"
    activity_line = "New topic" if delta_posts <= 0 else f"New replies: `+{delta_posts}`"

    if dry_run:
        print(
            "[DRY-RUN] Slack 미전송. 실제로 보냈을 내용:\n"
            f"  {mention} {title}\n"
            f"  URL: {url}\n"
            f"  Author: {author_name} (@{author_username})\n"
            f"  Tags: {tags_text}\n"
            f"  Type: {activity_line}\n"
            f"  Time: {event_time}"
        )
        return

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


def run_once(dry_run: bool = False) -> int:
    if not STATE_PATH:
        print("ERROR: STATE_PATH env var is required.", file=sys.stderr)
        return 2
    if not dry_run and not SLACK_WEBHOOK_URL:
        print("ERROR: SLACK_WEBHOOK_URL env var is required.", file=sys.stderr)
        return 2

    if dry_run:
        print("=== DRY-RUN 모드: Slack 미전송, 상태 저장 안 함 ===")

    state = load_state()

    topics, users_by_id = fetch_latest_pages()
    if not topics:
        print("No topics found.")
        return 0

    def highest_post_number(t: Dict[str, Any]) -> int:
        # highest_post_number 는 중간 글이 삭제돼도 유지되어 posts_count 보다 강건함
        hpn = t.get("highest_post_number")
        if hpn is None:
            hpn = t.get("posts_count", 0)
        return int(hpn or 0)

    # 최초 실행 또는 기존 상태 마이그레이션(topic_highest_post_number 미존재):
    # 알림 없이 현재 상태를 baseline 으로만 저장한다.
    # (구버전 topic_posts_count 만 있는 상태에서 재배포 시 과거 글이 전부
    #  '새 글'로 오인되어 대량 알림이 나가는 것을 방지)
    if not state.get("initialized_at") or not state.get("topic_highest_post_number"):
        first_run = not state.get("initialized_at")
        if first_run:
            state["initialized_at"] = now_utc_iso()
        topic_highest: Dict[str, int] = state.get("topic_highest_post_number", {}) or {}
        for t in topics:
            tid = int(t.get("id", 0))
            if tid:
                topic_highest[str(tid)] = highest_post_number(t)
        state["topic_highest_post_number"] = topic_highest
        if not dry_run:
            save_state(state)
        if first_run:
            print(f"Initialized state (no notifications). initialized_at={state['initialized_at']}")
        else:
            print("Migrated to topic_highest_post_number (no notifications).")
        return 0

    initialized_at = parse_iso(state["initialized_at"])
    topic_highest: Dict[str, int] = state.get("topic_highest_post_number", {}) or {}

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

        tags = normalize_tags(t.get("tags"))

        current_hpn = highest_post_number(t)
        prev_hpn = topic_highest.get(str(tid))

        if prev_hpn is None:
            # 처음 보는 topic: baseline 세팅. init 이후 생성된 글이면 새 글 알림.
            topic_highest[str(tid)] = current_hpn

            created_at_raw = t.get("created_at")
            if created_at_raw:
                created_at = parse_iso(created_at_raw)
                if created_at > initialized_at:
                    author_username, author_name = get_original_poster(t, users_by_id)
                    post_to_slack(
                        SLACK_WEBHOOK_URL,
                        title=title,
                        url=url,
                        event_time=iso_utc_to_kst_str(created_at_raw),
                        author_username=author_username,
                        author_name=author_name,
                        tags=tags,
                        delta_posts=0,
                        dry_run=dry_run,
                    )
                    print(f"Notified new topic: {tid} {title}")
                    notified += 1
            continue

        if current_hpn > prev_hpn:
            delta = current_hpn - prev_hpn

            # 답글 알림은 원글 작성자가 아니라 실제 최근 작성자를 저자로 표기
            author_username, author_name = get_latest_poster(t, users_by_id)

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
                dry_run=dry_run,
            )
            print(f"Notified replies: {tid} {title} (+{delta})")
            notified += 1

        # 증가/감소(글 삭제) 무관하게 항상 최신값으로 동기화.
        # max() 로 감추지 않으므로, 삭제 후 재작성된 글도 정상 감지됨.
        topic_highest[str(tid)] = current_hpn

    state["topic_highest_post_number"] = topic_highest
    if not dry_run:
        save_state(state)

    print(f"Done. notified={notified}" + (" (dry-run, 상태 미저장)" if dry_run else ""))
    return 0


def cli_main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", action="store_true", help="Run forever (daemon mode)")
    parser.add_argument("--interval-sec", type=int, default=60, help="Polling interval in seconds")
    parser.add_argument("--dry-run", action="store_true", help="Slack 미전송·상태 미저장. 무엇을 보낼지 출력만 함")
    args = parser.parse_args()

    if not args.loop:
        return run_once(dry_run=args.dry_run)

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
            rc = run_once(dry_run=args.dry_run)
            # run_once가 2(설정오류) 같은 치명적 오류면 계속 돌 의미가 없으니 종료
            if rc == 2:
                print("Fatal config error. Exiting.", file=sys.stderr)
                break
        except Exception:
            import traceback
            print("ERROR: run_once failed.", file=sys.stderr)
            traceback.print_exc()

        # sleep 중에도 SIGTERM 받으면 빨리 종료되도록 1초 단위로 쪼갬
        for _ in range(max(1, args.interval_sec)):
            if not keep_running:
                break
            time.sleep(1)

    print("Exiting daemon loop.")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main())
