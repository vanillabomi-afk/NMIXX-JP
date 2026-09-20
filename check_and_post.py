"""
check_and_post.py — a single, self-contained script meant to be run on a
schedule by GitHub Actions (see .github/workflows/check.yml).

Each run:
    1. Reads state.json to see which posts we've already sent to Discord.
    2. Fetches the monitored account's recent posts from a public Nitter
       RSS instance to detect NEW posts and classify their type.
    3. Posts the tweet's link — rewritten to fixupx.com — to a Discord
       webhook. Discord handles the visual embed.
    4. Updates state.json so restarts/new runs never repost old content.

Deduplication is done using a set of post IDs rather than a "newer than
last ID" threshold. X post IDs are global, so reposts can have IDs that
are numerically lower than the account's own recent posts.

Configuration:

    DISCORD_WEBHOOK_URL   required
    X_USERNAME            required
    INCLUDE_REPLIES       optional, "true"/"false", default "false"
    MENTION_ROLE_ID       optional, Discord role ID to @mention
    NITTER_INSTANCES      optional, comma-separated list of RSS instances

Default Nitter instances:

    https://nitter.perennialte.ch

"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import feedparser
import requests


STATE_PATH = Path("state.json")

# Primary Nitter instance followed by a fallback.
DEFAULT_INSTANCES = [
    "https://nitter.perennialte.ch"
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
    print(
        f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}",
        flush=True,
    )


def load_state() -> dict:
    if STATE_PATH.exists():
        data = json.loads(STATE_PATH.read_text())

        # Migrate from the old "last_seen_id" format if necessary.
        if "seen_ids" not in data:
            data["seen_ids"] = []

        return data

    return {
        "seen_ids": [],
    }


def save_state(state: dict) -> None:
    # Keep state.json small by retaining only the most recent 500 IDs.
    state["seen_ids"] = state["seen_ids"][-500:]

    STATE_PATH.write_text(
        json.dumps(state, indent=2),
        encoding="utf-8",
    )


def fetch_rss(
    username: str,
    instances: list[str],
) -> tuple[bytes, str]:

    last_error = None

    for instance in instances:
        instance = instance.rstrip("/")
        url = f"{instance}/{username}/rss"

        try:
            log(f"Trying RSS instance: {instance}")
            log(f"RSS URL: {url}")

            response = requests.get(
                url,
                timeout=15,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 "
                        "(compatible; xbot-github-action/1.0)"
                    ),
                    "Accept": (
                        "application/rss+xml, "
                        "application/atom+xml, "
                        "application/xml, "
                        "text/xml"
                    ),
                },
            )

            log(f"HTTP status: {response.status_code}")
            log(
                f"Content-Type: "
                f"{response.headers.get('content-type', 'unknown')}"
            )

            response.raise_for_status()

            # Parse immediately to make sure this is actually a feed.
            test_feed = feedparser.parse(response.content)

            log(
                f"Feed parser found "
                f"{len(test_feed.entries)} entr{'y' if len(test_feed.entries) == 1 else 'ies'}"
            )

            if test_feed.bozo and not test_feed.entries:
                raise RuntimeError(
                    "Response was not a valid RSS/Atom feed"
                )

            if not test_feed.entries:
                raise RuntimeError(
                    "RSS request succeeded but returned zero entries"
                )

            log(f"RSS fetch successful: {instance}")

            return response.content, instance

        except Exception as exc:
            log(f"Instance {instance} failed: {exc}")
            last_error = exc

    raise RuntimeError(
        f"All Nitter instances failed. Last error: {last_error}"
    )


def parse_pubdate(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)

    try:
        dt = parsedate_to_datetime(value)

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt

    except Exception:
        return datetime.now(timezone.utc)


def to_fixupx_url(link: str, instance: str) -> str:
    """
    Convert a Nitter status URL into a FixupX URL.

    Example:

        https://nitter.perennialte.ch/user/status/123
        ->
        https://fixupx.com/user/status/123
    """

    match = re.match(r"https?://[^/]+(/.*)", link)

    if not match:
        raise ValueError(
            f"Could not extract path from Nitter URL: {link}"
        )

    path = match.group(1).split("#")[0]

    return f"https://fixupx.com{path}"


def detect_quote(
    description_html: str,
    own_link: str,
) -> bool:
    """
    A quote-tweet's RSS description contains a link to the quoted status,
    with an ID different from the post's own ID.
    """

    own_match = re.search(r"/status/(\d+)", own_link)

    if not own_match:
        return False

    own_id = own_match.group(1)

    for _, status_id in _STATUS_HREF.findall(description_html):
        if status_id != own_id:
            return True

    return False


def parse_entry(
    entry,
    instance: str,
) -> dict | None:

    link = entry.get("link", "")

    match = re.search(r"/status/(\d+)", link)

    if not match:
        return None

    post_id = match.group(1)

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
        "created_at": parse_pubdate(
            entry.get("published")
        ),
    }


def build_message(
    post: dict,
    username: str,
    mention_role_id: str | None,
) -> str:

    lines = []

    if mention_role_id:
        lines.append(f"<@&{mention_role_id}>")

    prefix = _TYPE_PREFIX.get(post["type"])

    if prefix:
        lines.append(f"**@{username}** {prefix}")

    # Keep the FixupX URL on its own line so Discord unfurls it.
    lines.append(post["url"])

    return "\n".join(lines)


def send_to_discord(
    webhook_url: str,
    content: str,
) -> None:

    response = requests.post(
        webhook_url,
        json={"content": content},
        timeout=15,
    )

    if response.status_code == 429:

        try:
            retry_after = response.json().get(
                "retry_after",
                1,
            )
        except Exception:
            retry_after = 1

        log(
            f"Rate limited by Discord, "
            f"waiting {retry_after}s"
        )

        time.sleep(retry_after)

        response = requests.post(
            webhook_url,
            json={"content": content},
            timeout=15,
        )

    response.raise_for_status()


def main() -> int:

    webhook_url = os.environ.get(
        "DISCORD_WEBHOOK_URL",
        "",
    ).strip()

    username = (
        os.environ.get(
            "X_USERNAME",
            "",
        )
        .strip()
        .lstrip("@")
        .lower()
    )

    include_replies = (
        os.environ.get(
            "INCLUDE_REPLIES",
            "false",
        )
        .strip()
        .lower()
        in ("1", "true", "yes")
    )

    mention_role_id = (
        os.environ.get(
            "MENTION_ROLE_ID",
            "",
        ).strip()
        or None
    )

    instances = [
        instance.strip()
        for instance in os.environ.get(
            "NITTER_INSTANCES",
            ",".join(DEFAULT_INSTANCES),
        ).split(",")
        if instance.strip()
    ]

    if not webhook_url or not username:
        log(
            "ERROR: DISCORD_WEBHOOK_URL and X_USERNAME "
            "must be set as repository secrets."
        )
        return 1

    state = load_state()

    seen_ids = set(
        state.get("seen_ids", [])
    )

    initialized = bool(
        state.get("initialized")
    )

    if initialized:
        log(
            f"Checking @{username} "
            f"({len(seen_ids)} post(s) already seen)"
        )
    else:
        log(
            f"Checking @{username} "
            "(first run)"
        )

    # ------------------------------------------------------------------
    # Fetch RSS
    # ------------------------------------------------------------------

    try:
        raw, instance = fetch_rss(
            username,
            instances,
        )

    except Exception as exc:
        log(f"ERROR: {exc}")
        return 1

    # ------------------------------------------------------------------
    # Parse RSS
    # ------------------------------------------------------------------

    feed = feedparser.parse(raw)

    posts = []

    for entry in feed.entries:

        try:
            post = parse_entry(
                entry,
                instance,
            )

            if post:
                posts.append(post)

        except Exception as exc:
            log(
                f"Skipping unparsable entry: {exc}"
            )

    posts.sort(
        key=lambda post: post["created_at"]
    )

    log(
        f"Found {len(posts)} post(s) in RSS feed"
    )

    # ------------------------------------------------------------------
    # First run — establish baseline
    # ------------------------------------------------------------------

    if not initialized:

        state["seen_ids"] = [
            post["id"]
            for post in posts
        ]

        state["initialized"] = True

        save_state(state)

        log(
            f"First run — baselined "
            f"{len(posts)} post(s). "
            "Nothing posted this run."
        )

        return 0

    # ------------------------------------------------------------------
    # Find new posts
    # ------------------------------------------------------------------

    new_posts = [
        post
        for post in posts
        if post["id"] not in seen_ids
    ]

    if not include_replies:
        new_posts = [
            post
            for post in new_posts
            if post["type"] != "reply"
        ]

    if not new_posts:
        log("No new posts.")
        return 0

    log(
        f"Found {len(new_posts)} new post(s)"
    )

    # ------------------------------------------------------------------
    # Post to Discord
    # ------------------------------------------------------------------

    posted = 0

    for post in new_posts:

        try:

            content = build_message(
                post,
                username,
                mention_role_id,
            )

            send_to_discord(
                webhook_url,
                content,
            )

            state["seen_ids"].append(
                post["id"]
            )

            save_state(state)

            posted += 1

            log(
                f"Posted {post['type']} "
                f"{post['id']}"
            )

        except Exception as exc:

            log(
                f"ERROR posting "
                f"{post['id']}: {exc}"
            )

            # Stop here so the failed item can be retried
            # on the next GitHub Actions run.
            break

    log(
        f"Done — posted {posted} "
        f"new item(s)."
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
