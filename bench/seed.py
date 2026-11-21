#!/usr/bin/env python3
"""Secret-derived, reproducible batch seed with exact response verification."""
import argparse
import hashlib
import hmac
import json
import secrets
import re
from http_client import request_json


def seed(url, posts, likes, key=None):
    key = (key or secrets.token_hex(32)).encode()
    def token(label):
        return hmac.new(key, label.encode(), hashlib.sha256).hexdigest()[:24]
    def send(path, payload):
        status, value = request_json(url + path, "POST", payload, timeout=15)
        assert status == (201 if path == "/posts" else 200)
        return value
    tail = []
    for i in range(posts):
        payload = {"author": token(f"author:{i % 100}"), "content": token(f"post:{i}") + " · Zażółć 🚀"}
        post = send("/posts", payload)
        assert type(post.get("id")) is int and post["id"] == i + 1, "Seed requires an empty database with IDs starting at 1"
        assert type(post.get("likes")) is int and isinstance(post.get("created_at"), str)
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", post["created_at"])
        assert post["author"] == payload["author"] and post["content"] == payload["content"] and post["likes"] == 0
        tail = (tail + [post])[-20:]
    for i in range(likes):
        post_id = i % posts + 1
        result = send("/like", {"post_id": post_id, "user": token(f"like:{i // posts}")})
        assert type(result.get("post_id")) is int and type(result.get("likes")) is int and result.get("liked") is True
        assert result == {"post_id": post_id, "likes": i // posts + 1, "liked": True}, "Incorrect seeded like counter"
    for post in tail:
        post["likes"] = likes // posts + int(post["id"] <= likes % posts)
    status, feed = request_json(url + "/feed")
    assert status == 200 and feed == {"posts": list(reversed(tail))}, "Seed feed is stale or fabricated"
    print(json.dumps({"posts": posts, "likes": likes}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--posts", type=int, default=10000)
    parser.add_argument("--likes", type=int, default=100000)
    parser.add_argument("--key")
    args = parser.parse_args()
    assert args.posts > 0 and args.likes >= 0
    seed(args.url.rstrip("/"), args.posts, args.likes, args.key)
