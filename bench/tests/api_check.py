#!/usr/bin/env python3
"""Black-box contract checks. Standard library only; works against any base URL."""
import argparse
import concurrent.futures
import json
import re
import secrets
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from http_client import request_json

TIMESTAMP = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z\Z")


class Contract:
    def __init__(self, url):
        self.url = url.rstrip("/")
        self.count = 0

    def request(self, method, path, body=None, status=200, raw=None, content_type="application/json"):
        actual, value = request_json(self.url + path, method, body, raw, content_type)
        assert actual == status, f"{method} {path}: expected {status}, got {actual}"
        assert isinstance(value, dict), f"{path}: expected object"
        if status >= 400:
            assert isinstance(value.get("error"), str) and value["error"], "Missing error description"
        if path == "/like" and status == 200:
            assert type(value.get("post_id")) is int and value["post_id"] == body["post_id"]
            assert type(value.get("likes")) is int and value["likes"] >= 1
            assert value.get("liked") is True, "liked must be the JSON boolean true"
        self.count += 1
        return value

    def feed(self):
        value = self.request("GET", "/feed")
        assert isinstance(value.get("posts"), list) and len(value["posts"]) <= 20
        posts = value["posts"]
        for post in posts:
            validate_post(post)
        ids = [post["id"] for post in posts]
        assert ids == sorted(set(ids), reverse=True), "Feed is not strictly descending"
        return posts


def validate_post(post):
    assert isinstance(post, dict)
    assert type(post.get("id")) is int and post["id"] > 0
    assert isinstance(post.get("author"), str) and re.fullmatch(r"[a-zA-Z0-9_]{1,32}", post["author"])
    assert isinstance(post.get("content"), str) and 1 <= len(post["content"]) <= 280 and post["content"].strip()
    assert isinstance(post.get("created_at"), str) and TIMESTAMP.fullmatch(post["created_at"])
    assert type(post.get("likes")) is int and post["likes"] >= 0


def prepare(api, expect_empty):
    assert api.request("GET", "/health") == {"status": "ok"}
    initial = api.feed()
    if expect_empty:
        assert initial == [], "A fresh volume must return an empty feed"
    token = secrets.token_hex(6)
    invalid_posts = [None, [], "hello", True, 7, {}, {"author": "a"}, {"content": "x"}]
    invalid_posts += [{"author": author, "content": "x"} for author in ["", "a" * 33, "ż", "a-b", " a", "a\n", 1, True, None]]
    invalid_posts += [{"author": "a", "content": content} for content in ["", " \n\t", "x" * 281, "🦆" * 281, 0, [], None]]
    for value in invalid_posts:
        api.request("POST", "/posts", raw=json.dumps(value).encode(), status=400)
    for path in ["/posts", "/like"]:
        for raw, mime in [(b"{", "application/json"), (b"", "application/json"), (b"{}", "text/plain")]:
            api.request("POST", path, raw=raw, content_type=mime, status=400)
    posts = []
    contents = ["x", "x" * 280, "🦆" * 280, "  Zażółć gęślą jaźń 🚀  ", "a\n'b\"<script>"]
    for index in range(27):
        content = contents[index] if index < len(contents) else f"{token} post {index}"
        author = "A" * 32 if index == 0 else f"u_{token}"
        post = api.request("POST", "/posts", {"author": author, "content": content}, 201)
        validate_post(post)
        assert post["author"] == author and post["content"] == content and post["likes"] == 0
        if posts:
            assert post["id"] > posts[-1]["id"], "IDs must increase"
        elif expect_empty:
            assert post["id"] == 1, "IDs must start at 1"
        assert api.feed()[0] == post, "Post not visible immediately after 201"
        posts.append(post)
    assert api.feed() == list(reversed(posts[-20:])), "Feed must contain the newest 20 posts"
    post = posts[-1]
    invalid_likes = [None, [], True, {}, {"post_id": post["id"]}, {"user": "a"}]
    invalid_likes += [{"post_id": value, "user": "a"} for value in [0, -1, True, 1.5, "1", None, 2**64]]
    invalid_likes += [{"post_id": post["id"], "user": value} for value in ["", "a" * 33, "a-b", "ż", "a\n", 1, None]]
    for value in invalid_likes:
        api.request("POST", "/like", raw=json.dumps(value).encode(), status=400)
    api.request("POST", "/like", {"post_id": 9223372036854775807, "user": "absent"}, 404)
    payload = {"post_id": post["id"], "user": post["author"]}
    for _ in range(3):
        assert api.request("POST", "/like", payload) == {"post_id": post["id"], "likes": 1, "liked": True}
        assert api.feed()[0]["likes"] == 1, "Like not visible immediately after 200"
    # Simultaneous duplicate likes must never double-count.
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        values = list(pool.map(lambda _: api.request("POST", "/like", payload), range(16)))
    assert all(value == {"post_id": post["id"], "likes": 1, "liked": True} for value in values)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: api.request("POST", "/like", {"post_id": post["id"], "user": f"other_{i}"}), range(12)))
    assert api.feed()[0]["likes"] == 13
    # Old posts outside the feed are still valid like targets.
    old = {"post_id": posts[0]["id"], "user": "old_post_user"}
    assert api.request("POST", "/like", old) == {"post_id": posts[0]["id"], "likes": 1, "liked": True}
    return {"feed": api.feed(), "old_like": old, "last_id": post["id"], "duplicate": payload}


def verify(api, state):
    assert api.request("GET", "/health") == {"status": "ok"}
    assert api.feed() == state["feed"], "Committed data changed across restart"
    assert api.request("POST", "/like", state["duplicate"]) == {"post_id": state["last_id"], "likes": 13, "liked": True}
    assert api.request("POST", "/like", state["old_like"])["likes"] == 1
    created = api.request("POST", "/posts", {"author": "restart", "content": "Still durable"}, 201)
    assert created["id"] > state["last_id"], "ID sequence regressed across restart"
    assert api.feed()[0] == created


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("--phase", choices=["prepare", "verify", "all"], default="all")
    parser.add_argument("--state-file", default="/tmp/vibezone-contract.json")
    parser.add_argument("--expect-empty", action="store_true")
    args = parser.parse_args()
    api = Contract(args.url)
    started = time.monotonic()
    try:
        if args.phase != "verify":
            state = prepare(api, args.expect_empty)
            Path(args.state_file).write_text(json.dumps(state, ensure_ascii=False))
        else:
            state = json.loads(Path(args.state_file).read_text())
        if args.phase != "prepare":
            verify(api, state)
        print(json.dumps({"passed": True, "checks": api.count, "phase": args.phase,
                          "restart_verified": args.phase == "verify", "seconds": round(time.monotonic() - started, 3)}))
    except Exception as error:
        print(json.dumps({"passed": False, "checks": api.count, "error": str(error)}))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
