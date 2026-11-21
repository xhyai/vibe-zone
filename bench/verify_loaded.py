"""Verify measured feed and random old seeded records before/after a crash."""
import argparse
import json
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "tests"))
from api_check import Contract


def capture(api, posts, likes):
    samples = []
    for post_id in secrets.SystemRandom().sample(range(1, posts + 1), min(32, posts)):
        payload = {"post_id": post_id, "user": secrets.token_hex(12)}
        first = api.request("POST", "/like", payload)
        initial = likes // posts + int(post_id <= likes % posts)
        assert first["likes"] >= initial + 1, "Seeded likes disappeared under load"
        assert api.request("POST", "/like", payload) == first, "Loaded like is not idempotent"
        other = {"post_id": post_id, "user": secrets.token_hex(12)}
        second = api.request("POST", "/like", other)
        assert second["likes"] == first["likes"] + 1, "Loaded like counter did not increment exactly once"
        samples.append({"payload": payload, "expected": second})
    return {"feed": api.feed(), "samples": samples}


def verify(api, state):
    assert api.feed() == state["feed"], "Measured workload feed changed across SIGKILL"
    for item in state["samples"]:
        assert api.request("POST", "/like", item["payload"]) == item["expected"], "Measured old data changed across SIGKILL"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["capture", "verify"])
    parser.add_argument("--posts", type=int, required=True)
    parser.add_argument("--likes", type=int, required=True)
    args = parser.parse_args()
    api = Contract("http://sut:8080")
    state_file = Path("/tmp/loaded-state.json")
    if args.phase == "capture":
        state_file.write_text(json.dumps(capture(api, args.posts, args.likes)))
    else:
        verify(api, json.loads(state_file.read_text()))
    print(json.dumps({"passed": True, "phase": args.phase, "checks": api.count}))


if __name__ == "__main__":
    main()
