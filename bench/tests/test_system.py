import base64
import hashlib
import json
import re
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

from bench import runner
from leaderboard import store, worker
from leaderboard.app import create_app


class ScoreTests(unittest.TestCase):
    def raw(self, **changes):
        values = {"hold_requests": {"count": 2000}, "hold_latency": {"p(99)": 499, "p(95)": 100},
                  "response_errors": {"rate": 0}, "hold_errors": {"rate": 0}, "vus": {"max": 501}, "integrity_completed": {"count": 2}}
        values.update(changes)
        return {"state": {"testRunDurationMs": 40000}, "metrics": {key: {"values": value} for key, value in values.items()}}

    def test_full_clean_level_passes(self):
        result = runner.score_level(self.raw(), 500, runner.profile(), 0)
        self.assertTrue(result["passed"])
        self.assertEqual(result["throughput_rps"], 100)

    def test_rejects_threshold_boundary_errors_missing_and_partial(self):
        for raw, code in [(self.raw(hold_latency={"p(99)": 500}), 0), (self.raw(response_errors={"rate": .001}), 0),
                          (self.raw(hold_errors={"rate": .1}), 0), (self.raw(hold_requests={"count": 0}), 0),
                          (self.raw(hold_latency={"p(99)": float("nan")}), 0), (self.raw(), 99), ({}, 0)]:
            with self.subTest(raw=raw, code=code):
                self.assertFalse(runner.score_level(raw, 500, runner.profile(), code)["passed"])
        raw = self.raw()
        raw["state"]["testRunDurationMs"] = 39900
        self.assertFalse(runner.score_level(raw, 500, runner.profile(), 0)["passed"])

    def test_source_validation_prevents_options_and_arbitrary_urls(self):
        for value in ["--privileged", "x;touch /tmp/pwn", "file:///etc/passwd", "http://localhost/a", "https://github.com/../repo", "https://github.com/a/b?x=1"]:
            with self.assertRaises(ValueError):
                runner.validate_source(value)
        self.assertEqual(runner.validate_source("ghcr.io/team/api:latest"), "image")
        self.assertEqual(runner.validate_source("https://github.com/team/api.git"), "repo")

    def test_aborted_ramp_keeps_attempt_metrics_without_fake_zero_latency(self):
        # Shape observed in the full baseline run: k6 emits zero-valued empty trends.
        raw = self.raw(hold_requests={"count": 0}, hold_latency={"p(99)": 0, "p(95)": 0},
                       http_reqs={"count": 81, "rate": 10.312},
                       http_req_duration={"p(99)": 5000.718, "p(95)": 5000.113})
        raw["state"]["testRunDurationMs"] = 7854
        result = runner.score_level(raw, 500, runner.profile(), 99)
        self.assertFalse(result["passed"])
        for key in ["p95_ms", "p99_ms", "throughput_rps", "hold_error_percent"]:
            self.assertIsNone(result[key])
        self.assertEqual(result["attempt_p99_ms"], 5000.718)
        self.assertEqual(result["attempt_requests"], 81)

    def test_exact_image_limit_fails_before_start(self):
        with tempfile.TemporaryDirectory() as directory:
            run = runner.Run("team/api:v1", runner.profile(), directory, "never")
            with mock.patch.object(runner, "resolve_source", return_value={"image_id": "sha256:test", "image_bytes": 100000000}), \
                 mock.patch.object(runner, "compressed_size", return_value=12), mock.patch.object(runner, "docker") as docker:
                with self.assertRaisesRegex(ValueError, "smaller than"):
                    run.execute()
                docker.assert_not_called()

    def test_report_survives_daemon_failure_during_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            run = runner.Run("team/api:v1", runner.profile(), directory, "never")
            with mock.patch.object(run, "execute", side_effect=RuntimeError("daemon lost")), \
                 mock.patch.object(runner, "docker", side_effect=TimeoutError("daemon unavailable")):
                result = run.run()
            saved = json.loads((Path(directory) / f"{run.id}.json").read_text())
            self.assertEqual(saved["status"], "failed")
            self.assertEqual(saved["error"], "daemon lost")
            self.assertEqual(len(result["cleanup_errors"]), 5)


class BoardTests(unittest.TestCase):
    def submit(self, nickname, source, token="", invitation=None):
        return store.submit(self.path, nickname, source, token, invitation if invitation is not None else store.issue_invitation(self.path))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "board.sqlite3")
        self.app = create_app({"TESTING": True, "DATABASE": self.path, "SECRET_KEY": "test-only", "ADMIN_PASSWORD": "test-admin"})
        self.client = self.app.test_client()
        self.auth = {"Authorization": "Basic " + base64.b64encode(b"admin:test-admin").decode()}
        self.counter = 0

    def tearDown(self):
        self.temp.cleanup()

    def csrf(self):
        html = self.client.get("/submit").get_data(as_text=True)
        return re.search(r'name="csrf_token" value="([^"]+)"', html)[1]

    def start(self):
        with store.connect(self.path) as db:
            db.execute("UPDATE control SET requested=1")
            for row in db.execute("SELECT id,source FROM submissions").fetchall():
                db.execute("UPDATE submissions SET approved_image=? WHERE id=?",
                           ("sha256:" + hashlib.sha256(row["source"].encode()).hexdigest(), row["id"]))
        return worker.claim(self.path)

    def resolved(self, source):
        return {"image_id": source, "source": source, "image_bytes": 41000000}

    def benchmark(self, resolved):
        self.counter += 1
        # During execution, previous published ranking must still be visible.
        return {"id": f"test-{self.counter}", "status": "passed", "image_id": resolved["image_id"], "max_vus": 500,
                "image_bytes": 41000000, "cold_start_ms": 600, "p99_ms": 200, "profile": runner.profile()}

    def test_submit_code_ownership_and_escaped_names(self):
        token = self.csrf()
        response = self.client.post("/submissions", data={"csrf_token": token, "nickname": "<script>x</script>", "source": "team/api:v1", "invite_code": store.issue_invitation(self.path)})
        self.assertEqual(response.status_code, 201)
        code = re.search(r'<code class="team-code">([^<]+)', response.get_data(as_text=True))[1]
        html = self.client.get("/submit").get_data(as_text=True)
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("<script>x</script>", html)
        self.assertNotIn(code, self.client.get("/api/leaderboard").get_data(as_text=True))
        payload = {"csrf_token": token, "nickname": "<script>x</script>", "source": "team/api:v2"}
        self.assertEqual(self.client.post("/submissions", data=payload).status_code, 400)
        payload["team_token"] = code
        self.assertEqual(self.client.post("/submissions", data=payload).status_code, 201)
        with store.connect(self.path) as db:
            self.assertEqual(len(store.latest(db)), 1)
            self.assertEqual(store.latest(db)[0]["source"], "team/api:v2")

    def test_admin_auth_csrf_and_hidden_latest_never_revives_old(self):
        self.assertEqual(self.client.post("/submissions", data={"csrf_token": "", "nickname": "NoCSRF", "source": "team/api:v1"}).status_code, 400)
        first, code = self.submit("Team", "team/api:v1")
        latest, _ = self.submit("team", "team/api:v2", code)
        token = self.csrf()
        self.assertEqual(self.client.post("/admin/run", data={"csrf_token": "zażółć"}, headers=self.auth).status_code, 400)
        self.assertEqual(self.client.get("/admin").status_code, 401)
        self.assertEqual(self.client.get("/admin", headers=self.auth).status_code, 200)
        self.assertEqual(self.client.post("/admin/run", headers=self.auth).status_code, 400)
        self.assertEqual(self.client.post("/admin/run", data={"csrf_token": token}).status_code, 401)
        self.assertEqual(self.client.post(f"/admin/submissions/{latest}/hide", data={"csrf_token": token}, headers=self.auth).status_code, 303)
        with store.connect(self.path) as db:
            self.assertEqual(store.latest(db), [])
        self.assertEqual(self.client.post("/admin/run", data={"csrf_token": token}, headers=self.auth).status_code, 303)

    def test_schedule_no_overlap_and_snapshot(self):
        first, code = self.submit("Team", "team/api:v1")
        self.assertIsNone(worker.claim(self.path, now=1))
        batch, snapshot = self.start()
        self.submit("Team", "team/api:v2", code)
        self.assertIsNone(worker.claim(self.path, now=time.time() + 10000))
        self.assertEqual([row["id"] for row in snapshot], [first])
        self.assertEqual(store.board(self.path)["ranking"], [])
        worker.run_batch(self.path, batch, snapshot, self.benchmark, self.resolved)
        self.assertEqual(store.board(self.path)["ranking"][0]["source"], "team/api:v1")

    def test_each_batch_remeasures_instead_of_reusing_a_lucky_result(self):
        _, code = self.submit("Team", "team/api:v1")
        for _ in range(2):
            worker.run_batch(self.path, *self.start(), self.benchmark, self.resolved)
        self.assertEqual(self.counter, 2)
        self.assertNotIn("reused", store.board(self.path)["ranking"][0]["result"])
        self.submit("Team", "team/api:v2", code)
        worker.run_batch(self.path, *self.start(), self.benchmark, self.resolved)
        self.assertEqual(self.counter, 3)

    def test_identical_images_share_only_within_the_same_batch(self):
        self.submit("First team", "team/api:v1")
        self.submit("Second team", "team/api:v1")
        previous_id = None
        for _ in range(2):
            worker.run_batch(self.path, *self.start(), self.benchmark, self.resolved)
            first, second = store.board(self.path)["ranking"]
            self.assertTrue(second["result"]["reused"])
            self.assertEqual(first["result"]["id"], second["result"]["id"])
            self.assertNotEqual(first["result"]["id"], previous_id)
            previous_id = first["result"]["id"]
        self.assertEqual(self.counter, 2)

    def test_failure_does_not_stop_other_teams_and_publication_is_atomic(self):
        self.submit("Broken", "broken/api:v1")
        self.submit("Working", "working/api:v1")
        def benchmark(resolved):
            self.assertEqual(store.board(self.path)["ranking"], [])
            if "broken" in resolved["source"]:
                raise RuntimeError("Contract failed")
            return self.benchmark(resolved)
        worker.run_batch(self.path, *self.start(), benchmark, self.resolved)
        data = store.board(self.path)
        self.assertEqual(data["ranking"][0]["nickname"], "Working")
        self.assertEqual(data["rejected"][0]["nickname"], "Broken")
        self.assertIsNotNone(data["last_update"])

    def test_rank_tie_breakers(self):
        base = {"max_vus": 500, "image_bytes": 40, "cold_start_ms": 20, "p99_ms": 10}
        cases = [("p99", {"p99_ms": 9}), ("size", {"image_bytes": 39}), ("cold", {"cold_start_ms": 19}),
                 ("users", {"max_vus": 1000}), ("base", {})]
        rows = [{"nickname": name, "result": {**base, **changes}} for name, changes in cases]
        self.assertEqual([r["nickname"] for r in sorted(rows, key=store.ranking_key)], ["users", "size", "cold", "p99", "base"])


if __name__ == "__main__":
    unittest.main()
