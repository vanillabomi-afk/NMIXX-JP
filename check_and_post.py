"""
check_and_post.py — a single, self-contained script meant to be run on a
schedule by GitHub Actions (see .github/workflows/check.yml).

Each run:
    1. Reads state.json to see which posts we've already sent to Discord.
    2. Fetches the monitored account's recent posts from a public Nitter
       RSS instance (free, no login) — just to detect NEW posts and
       classify their type (original / repost / quote / reply).
    3. Posts the tweet's link — rewritten to fixupx.com — to a Discord
       webhook. Discord's own link-unfurling then does all the visual
       work (image, video, author, text), using fixupx.com's embed data,
       which is more complete and more reliably formatted than anything
       we could hand-build ourselves from RSS content.
    4. Updates state.json (committed back to the repo by the workflow),
       so restarts / new runs never repost old content.

Dedup is done by remembering the *set* of post IDs already posted, not by
"anything newer than the last ID" — because X's tweet IDs increase
globally over time, not per-account, a repost of an older tweet can have a
numerically lower ID than the account's own recent posts. A simple
threshold would silently skip those reposts forever; tracking membership
in a seen-IDs set handles it correctly.

Why fixupx.com: x.com/twitter.com links don't unfurl properly in Discord
on their own (X's own embed metadata is broken for bots). fixupx.com
(https://github.com/FixTweet/FxTwitter) is a free, actively maintained
service built specifically to fix this — swap the domain, get a proper
Discord embed with images, video, and quote-tweet content all handled for
you.

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

STATE_PATH = Path("state.json")

DEFAULT_INSTANCES = [
    "https://nitter.net",
    "https://nitter.privacyredirect.com",
    "https://xcancel.com",
]

_RT_MATCH = re.compile(r"^RT(?:\s+by)?\s+@(\w+):", re.IGNORECASE)
_REPLY_MATCH = re.compile(r"^R\s+to\s+@(\w+):", re.IGNORECASE)
_STATUS_HREF = re.compile(r'href="([^"]*?/status/(\d+)[^"]*)"')

_TYPE_PREFIX = {
    "retweet": "reposted:",
    "quote": "quoted:",
    "reply": "replied:",
}


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}", flush=True)


def load_state() -> dict:
    if STATE_PATH.exists():
        data = json.loads(STATE_PATH.read_text())
        # Migrate from the old "last_seen_id" single-threshold format, if present.
        if "seen_ids" not in data:
            data["seen_ids"] = []
        return data
    return {"seen_ids": []}


def save_state(state: dict) -> None:
    # Keep the file small — retain only the most recent 500 IDs.
    state["seen_ids"] = state["seen_ids"][-500:]
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


def to_fixupx_url(link: str, instance: str) -> str:
    """Rewrite a Nitter link into a fixupx.com link, so Discord unfurls it properly."""
    path = link
    if path.startswith(instance):
        path = path[len(instance):]
    elif path.startswith("http"):
        m = re.match(r"https?://[^/]+(/.*)", path)
        path = m.group(1) if m else path
    return f"https://fixupx.com{path.split('#')[0]}"


def detect_quote(description_html: str, own_link: str) -> bool:
    """A quote-tweet's RSS description contains a link to the quoted status,
    with an ID different from the post's own — that's the one reliable
    signal available without needing to parse full HTML."""
    for href, status_id in _STATUS_HREF.findall(description_html):
        if status_id not in own_link:
            return True
    return False


def parse_entry(entry, instance: str) -> dict | None:
    link = entry.get("link", "")
    m = re.search(r"/status/(\d+)", link)
    if not m:
        return None
    post_id = m.group(1)

    title = (entry.get("title") or "").strip()
    description_html = entry.get("description") or ""

    post_type = "original"
    if _RT_MATCH.match(title):
        post_type = "retweet"
    elif _REPLY_MATCH.match(title):
        post_type = "reply"
    elif detect_quote(description_html, link):
        post_type = "quote"

    return {
        "id": post_id,
        "type": post_type,
        "url": to_fixupx_url(link, instance),
        "created_at": parse_pubdate(entry.get("published")),
    }


def build_message(post: dict, username: str, mention_role_id: str | None) -> str:
    lines = []
    if mention_role_id:
        lines.append(f"<@&{mention_role_id}>")
    prefix = _TYPE_PREFIX.get(post["type"])
    if prefix:
        lines.append(f"**@{username}** {prefix}")
    lines.append(post["url"])  # on its own line — Discord unfurls a bare link reliably
    return "\n".join(lines)


def send_to_discord(webhook_url: str, content: str) -> None:
    resp = requests.post(webhook_url, json={"content": content}, timeout=15)
    if resp.status_code == 429:
        retry_after = resp.json().get("retry_after", 1)
        log(f"Rate limited by Discord, waiting {retry_after}s")
        import time

        time.sleep(retry_after)
        resp = requests.post(webhook_url, json={"content": content}, timeout=15)
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
    seen_ids = set(state["seen_ids"])
    initialized = bool(state.get("initialized"))

    log(f"Checking @{username} ({len(seen_ids)} post(s) already seen)" if initialized else f"Checking @{username} (first run)")

    try:
        raw, instance = fetch_rss(username, instances)
    except Exception as exc:  # noqa: BLE001
        log(f"ERROR: {exc}")
        return 1

    feed = feedparser.parse(raw)
    posts = []
    for entry in feed.entries:
        try:
            p = parse_entry(entry, instance)
            if p:
                posts.append(p)
        except Exception as exc:  # noqa: BLE001
            log(f"Skipping unparsable entry: {exc}")

    posts.sort(key=lambda p: p["created_at"])

    if not initialized:
        # First run ever: record everything currently in the feed as "already
        # seen" without posting it, so we don't flood the channel with history.
        # From the next run on, anything not in this set — including a repost
        # of an old tweet whose ID is numerically lower than others — counts
        # as new.
        state["seen_ids"] = [p["id"] for p in posts]
        state["initialized"] = True
        save_state(state)
        log(f"First run — baselined {len(posts)} post(s). Nothing posted this run.")
        return 0

    new_posts = [p for p in posts if p["id"] not in seen_ids]
    if not include_replies:
        new_posts = [p for p in new_posts if p["type"] != "reply"]

    if not new_posts:
        log("No new posts.")
        return 0

    posted = 0
    for post in new_posts:
        try:
            content = build_message(post, username, mention_role_id)
            send_to_discord(webhook_url, content)
            state["seen_ids"].append(post["id"])
            save_state(state)
            posted += 1
            log(f"Posted {post['type']} {post['id']}")
        except Exception as exc:  # noqa: BLE001
            log(f"ERROR posting {post['id']}: {exc}")
            break

    log(f"Done — posted {posted} new item(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
