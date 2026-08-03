"""
check_and_post.py — a single, self-contained script meant to be run on a
schedule by GitHub Actions (see .github/workflows/check.yml).

Each run:
    1. Reads state.json to see the last post ID we already posted.
    2. Fetches the monitored account's recent posts from a public Nitter
       RSS instance (free, no login).
    3. Posts anything new to a Discord webhook, as a rich embed.
    4. Updates state.json (committed back to the repo by the workflow),
       so restarts / new runs never repost old content.

Configuration comes entirely from environment variables (set as GitHub
repo secrets — see README.md):

    DISCORD_WEBHOOK_URL   required
    X_USERNAME            required
    INCLUDE_REPLIES       optional, "true"/"false", default "false"
    MENTION_ROLE_ID       optional, Discord role ID to @mention
    NITTER_INSTANCES      optional, comma-separated, has a sensible default
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import feedparser
import requests
from bs4 import BeautifulSoup

STATE_PATH = Path("state.json")

DEFAULT_INSTANCES = [
    "https://nitter.net",
    "https://nitter.privacyredirect.com",
    "https://xcancel.com",
]

_RT_MATCH = re.compile(r"^RT @(\w+):\s*(.*)$", re.DOTALL)
_REPLY_MATCH = re.compile(r"^R to @(\w+):\s*(.*)$", re.DOTALL)


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}", flush=True)


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"last_seen_id": None}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2))


def fetch_rss(username: str, instances: list[str]) -> tuple[bytes, str]:
    last_error = None
    for instance in instances:
        url = f"{instance.rstrip('/')}/{username}/rss"
        try:
            resp = requests.get(
                url,
                timeout=15,
                headers={"User-Agent": "Mozilla/5.0 (compatible; xbot-github-action/1.0)"},
            )
            if resp.status_code == 404:
                raise RuntimeError(f"@{username} not found on {instance} (404)")
            resp.raise_for_status()
            return resp.content, instance
        except Exception as exc:  # noqa: BLE001 — try the next instance
            log(f"Instance {instance} failed: {exc}")
            last_error = exc
            continue
    raise RuntimeError(f"All Nitter instances failed. Last error: {last_error}")


def parse_pubdate(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:  # noqa: BLE001
        return datetime.now(timezone.utc)


def to_x_url(link: str, instance: str) -> str:
    path = link
    if path.startswith(instance):
        path = path[len(instance):]
    elif path.startswith("http"):
        m = re.match(r"https?://[^/]+(/.*)", path)
        path = m.group(1) if m else path
    return f"https://x.com{path.split('#')[0]}"


def absolutize(url: str, instance: str) -> str:
    if url.startswith("http"):
        return url
    return f"{instance}{url if url.startswith('/') else '/' + url}"


def parse_entry(entry, instance: str, username: str) -> dict | None:
    link = entry.get("link", "")
    m = re.search(r"/status/(\d+)", link)
    if not m:
        return None
    post_id = m.group(1)

    title = (entry.get("title") or "").strip()
    description_html = entry.get("description") or ""
    soup = BeautifulSoup(description_html, "html.parser")

    post_type = "original"
    text = title
    quoted_text = None
    quoted_url = None

    rt = _RT_MATCH.match(title)
    reply = _REPLY_MATCH.match(title)

    if rt:
        post_type = "retweet"
        text = rt.group(2).strip()
    elif reply:
        post_type = "reply"
        text = reply.group(2).strip()
    elif soup.find("blockquote"):
        quote_link = soup.find("a", href=re.compile(r"/status/\d+"))
        if quote_link and quote_link.get("href", "") not in link:
            post_type = "quote"
            quoted_url = to_x_url(quote_link["href"], instance)
            quoted_text = quote_link.get_text(strip=True) or None

    images, video_url = [], None
    for img in soup.find_all("img"):
        src = img.get("src", "")
        if src and "emoji" not in src:
            images.append(absolutize(src, instance))
    for video in soup.find_all("video"):
        src = video.get("src") or (video.find("source") or {}).get("src")
        if src:
            video_url = absolutize(src, instance)

    return {
        "id": post_id,
        "type": post_type,
        "text": text or "(no text)",
        "url": to_x_url(link, instance),
        "created_at": parse_pubdate(entry.get("published")),
        "images": images,
        "video_url": video_url,
        "quoted_text": quoted_text,
        "quoted_url": quoted_url,
    }


_TYPE_COLOR = {"original": 0x1DA1F2, "retweet": 0x17BF63, "quote": 0x9146FF, "reply": 0xF45D22}
_TYPE_LABEL = {"original": "Post", "retweet": "Repost", "quote": "Quote Post", "reply": "Reply"}


def build_embed(post: dict, username: str) -> dict:
    label = _TYPE_LABEL[post["type"]]
    embed = {
        "title": f"{label} by @{username}",
        "description": post["text"][:4096],
        "url": post["url"],
        "color": _TYPE_COLOR[post["type"]],
        "timestamp": post["created_at"].isoformat(),
        "footer": {"text": f"X (Twitter) • {label}"},
        "author": {"name": f"@{username}", "url": post["url"]},
    }
    if post["type"] == "quote" and post["quoted_text"]:
        value = post["quoted_text"][:1000]
        if post["quoted_url"]:
            value += f"\n[View quoted post]({post['quoted_url']})"
        embed["fields"] = [{"name": "Quoting", "value": value, "inline": False}]
    if post["images"]:
        embed["image"] = {"url": post["images"][0]}
        if len(post["images"]) > 1:
            embed.setdefault("fields", []).append(
                {"name": "Media", "value": f"{len(post['images'])} images — open the post to view all", "inline": False}
            )
    return embed


def send_to_discord(webhook_url: str, post: dict, embed: dict, mention_role_id: str | None) -> None:
    content_parts = []
    if mention_role_id:
        content_parts.append(f"<@&{mention_role_id}>")
    if post["video_url"]:
        content_parts.append(post["video_url"])  # bare link -> Discord auto-embeds/plays it

    payload = {"embeds": [embed]}
    if content_parts:
        payload["content"] = "\n".join(content_parts)

    resp = requests.post(webhook_url, json=payload, timeout=15)
    if resp.status_code == 429:
        # Discord webhook rate limit — extremely unlikely at a 5-minute poll interval,
        # but handle it rather than crashing the whole run.
        retry_after = resp.json().get("retry_after", 1)
        log(f"Rate limited by Discord, waiting {retry_after}s")
        import time

        time.sleep(retry_after)
        resp = requests.post(webhook_url, json=payload, timeout=15)
    resp.raise_for_status()


def main() -> int:
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    username = os.environ.get("X_USERNAME", "").strip().lstrip("@").lower()
    include_replies = os.environ.get("INCLUDE_REPLIES", "false").strip().lower() in ("1", "true", "yes")
    mention_role_id = os.environ.get("MENTION_ROLE_ID", "").strip() or None
    instances = [
        s.strip()
        for s in os.environ.get("NITTER_INSTANCES", ",".join(DEFAULT_INSTANCES)).split(",")
        if s.strip()
    ]

    if not webhook_url or not username:
        log("ERROR: DISCORD_WEBHOOK_URL and X_USERNAME must be set (as repo secrets).")
        return 1

    state = load_state()
    last_seen_id = state.get("last_seen_id")

    log(f"Checking @{username} (last seen: {last_seen_id or 'none — first run'})")

    try:
        raw, instance = fetch_rss(username, instances)
    except Exception as exc:  # noqa: BLE001
        log(f"ERROR: {exc}")
        return 1

    feed = feedparser.parse(raw)
    posts = []
    for entry in feed.entries:
        try:
            p = parse_entry(entry, instance, username)
            if p:
                posts.append(p)
        except Exception as exc:  # noqa: BLE001
            log(f"Skipping unparsable entry: {exc}")

    posts.sort(key=lambda p: int(p["id"]))

    if last_seen_id is None:
        # First run ever: don't flood the channel with history — just record
        # the newest post as the baseline and start posting from the next run.
        if posts:
            state["last_seen_id"] = posts[-1]["id"]
            save_state(state)
            log(f"First run — baseline set to post {posts[-1]['id']}. Nothing posted this run.")
        else:
            log("First run — no posts found to baseline against.")
        return 0

    new_posts = [p for p in posts if int(p["id"]) > int(last_seen_id)]
    if not include_replies:
        new_posts = [p for p in new_posts if p["type"] != "reply"]

    if not new_posts:
        log("No new posts.")
        return 0

    posted = 0
    for post in new_posts:
        try:
            embed = build_embed(post, username)
            send_to_discord(webhook_url, post, embed, mention_role_id)
            state["last_seen_id"] = post["id"]
            save_state(state)  # save after each successful post, so a mid-run crash can't cause reposts
            posted += 1
            log(f"Posted {post['type']} {post['id']}")
        except Exception as exc:  # noqa: BLE001
            log(f"ERROR posting {post['id']}: {exc}")
            break  # stop here; next run will retry this post and any after it

    log(f"Done — posted {posted} new item(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
