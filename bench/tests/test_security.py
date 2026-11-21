"""Bounded regressions for the trust boundaries found during the audit."""
import base64
import hashlib
import json
import re
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from bench import runner
from bench.http_client import request_json, MAX_RESPONSE
from leaderboard import store, worker
from leaderboard.app import create_app


class AdmissionTests(unittest.TestCase):
    def test_remote_acquisition_is_disabled_without_a_docker_call(self):
        with mock.patch.object(runner, "docker") as docker:
            for source, pull in [("https://github.com/team/api", "never"), ("ghcr.io/team/api:v1", "always")]:
                with self.assertRaises(ValueError):
                    runner.resolve_source(source, pull)
            docker.assert_not_called()

    def test_source_destinations_and_options(self):
        for source in ["localhost:5000/api", "127.0.0.1/api", "10.0.0.1/api", "evil.example/api", "docker.io.evil/api",
                       "--privileged", "file:///a", "https://github.com/a/..", "a/b:tag;id", "a/b@sha256:bad"]:
            with self.subTest(source=source), self.assertRaises(ValueError):
                runner.validate_source(source)

    def test_extra_image_volumes_are_rejected(self):
        info = {"Architecture": "amd64", "Os": "linux", "Config": {"Volumes": {"/extra": {}}}}
        with mock.patch.object(runner, "docker", return_value=mock.Mock(stdout=json.dumps([info]))):
            with self.assertRaisesRegex(ValueError, "only the /data"):
                runner.resolve_source("team/api:v1")

    def test_standard_storage_cannot_fall_back_to_unbounded_volume(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(runner.os.environ, {}, clear=True):
            run = runner.Run("team/api:v1", runner.profile(), directory, "never")
            with mock.patch.object(runner, "docker") as docker, self.assertRaisesRegex(runner.InfrastructureError, "quota-backed"):
                run.create_data()
            docker.assert_not_called()

    def test_subprocess_output_and_wall_time_are_bounded(self):
        with self.assertRaisesRegex(RuntimeError, "output exceeds"):
            runner.command([sys.executable, "-c", "import sys; sys.stdout.write('x'*1100000)"], timeout=3)
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            runner.command([sys.executable, "-c", "import time; time.sleep(5)"], timeout=.1)
        self.assertLess(time.monotonic() - started, 3)

    def test_failed_cleanup_keeps_daemon_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            run = runner.Run("team/api:v1", runner.profile(True), directory, "never")
            run.lock_acquired = True
            with mock.patch.object(runner, "docker", return_value=mock.Mock(returncode=1, stderr="daemon unavailable")) as docker:
                run.cleanup()
            self.assertTrue(run.result["cleanup_errors"])
            self.assertFalse(any("vibezone-benchmark-lock" in call.args for call in docker.call_args_list))


class HttpBoundaryTests(unittest.TestCase):
    def setUp(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_GET(self):
                try:
                    if self.path == "/slow-headers":
                        self.wfile.write(b"HTTP/1.1 200 OK\r\nX-Slow: ")
                        for _ in range(30):
                            self.wfile.write(b"x"); self.wfile.flush(); time.sleep(.02)
                        return
                    if self.path == "/redirect":
                        self.send_response(302); self.send_header("Location", "/should-not-follow"); self.end_headers(); return
                    self.send_response(200); self.send_header("Content-Type", "application/json")
                    if self.path == "/oversized":
                        self.send_header("Content-Length", str(MAX_RESPONSE + 1))
                    self.end_headers()
                    if self.path == "/max-feed":
                        posts = [{"id": i, "author": "a" * 32, "content": "🦆" * 280,
                                  "created_at": "2026-11-16T12:00:00.000Z", "likes": 0} for i in range(20, 0, -1)]
                        self.wfile.write(json.dumps({"posts": posts}).encode())
                    elif self.path == "/stream":
                        self.wfile.write(b" " * (MAX_RESPONSE + 1))
                    elif self.path == "/slow":
                        for _ in range(30):
                            self.wfile.write(b" "); self.wfile.flush(); time.sleep(.02)
                    else:
                        self.wfile.write(b'{"status":"ok"}')
                except (BrokenPipeError, ConnectionResetError):
                    pass
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()

    def test_normal_json_and_declared_and_streamed_limits(self):
        self.assertEqual(request_json(self.url + "/ok"), (200, {"status": "ok"}))
        for path in ["/oversized", "/stream", "/redirect"]:
            with self.subTest(path=path), self.assertRaises(ValueError):
                request_json(self.url + path)

    def test_slow_trickle_has_a_total_deadline(self):
        for path in ["/slow", "/slow-headers"]:
            started = time.monotonic()
            with self.subTest(path=path), self.assertRaises(TimeoutError):
                request_json(self.url + path, timeout=.1)
            self.assertLess(time.monotonic() - started, .5)

    def test_limit_allows_a_full_ascii_escaped_unicode_feed(self):
        status, value = request_json(self.url + "/max-feed")
        self.assertEqual(status, 200)
        self.assertEqual(len(value["posts"]), 20)
        self.assertGreater(len(json.dumps(value).encode()), 64 * 1024)
        self.assertEqual(value["posts"][0]["content"], "🦆" * 280)


class WebBoundaryTests(unittest.TestCase):
    def submit(self, nickname, source, token="", invitation=None):
        return store.submit(self.path, nickname, source, token, invitation if invitation is not None else store.issue_invitation(self.path))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "board.sqlite3")
        self.app = create_app({"TESTING": True, "DATABASE": self.path, "SECRET_KEY": "test-only", "ADMIN_PASSWORD": "test-admin"})
        self.client = self.app.test_client()
        self.csrf = re.search('name="csrf_token" value="([^"]+)"', self.client.get("/submit").get_data(as_text=True))[1]
        self.auth = {"Authorization": "Basic " + base64.b64encode(b"admin:test-admin").decode()}

    def tearDown(self):
        self.temp.cleanup()

    def test_unapproved_submission_never_reaches_docker(self):
        self.submit("Pending", "https://github.com/team/api")
        with store.connect(self.path) as db:
            db.execute("UPDATE control SET requested=1")
        with mock.patch.object(runner, "resolve_source") as resolve:
            worker.run_batch(self.path, *worker.claim(self.path))
            resolve.assert_not_called()
        self.assertIn("zatwierdzenie", store.board(self.path)["rejected"][0]["result"]["error"])

    def test_approval_requires_admin_csrf_and_immutable_id_and_new_version_resets_it(self):
        sid, token = self.submit("Team", "team/api:v1")
        url = f"/admin/submissions/{sid}/approve"
        payload = {"csrf_token": self.csrf, "image_id": "sha256:" + "a" * 64}
        self.assertEqual(self.client.post(url, data=payload).status_code, 401)
        self.assertEqual(self.client.post(url, headers=self.auth).status_code, 400)
        self.assertEqual(self.client.post(url, data={**payload, "image_id": "team/api:latest"}, headers=self.auth).status_code, 400)
        self.assertEqual(self.client.post(url, data=payload, headers=self.auth).status_code, 303)
        self.submit("Team", "team/api:v2", token)
        with store.connect(self.path) as db:
            self.assertIsNone(store.latest(db)[0]["approved_image"])

    def test_atomic_capacity_and_history_limits(self):
        with mock.patch.object(store, "MAX_TEAMS", 1), mock.patch.object(store, "MAX_TEAM_SUBMISSIONS", 2):
            _, token = self.submit("One", "team/api:v1")
            with self.assertRaises(ValueError):
                self.submit("Two", "team/api:v1")
            self.submit("One", "team/api:v2", token, invitation="")
            with self.assertRaises(ValueError):
                self.submit("One", "team/api:v3", token, invitation="")
        with store.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM participants").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM submissions").fetchone()[0], 2)

    @mock.patch("leaderboard.store.time.time", return_value=time.time() + 60)
    def test_limits_are_shared_across_sessions_and_forwarded_headers(self, _clock):
        for _ in range(30):
            self.assertTrue(store.allow_request(self.path, "submissions", 30))
        response = self.client.post("/submissions", data={"csrf_token": self.csrf, "nickname": "x", "source": "team/api:v1"},
                                    headers={"X-Forwarded-For": "203.0.113.9"})
        self.assertEqual(response.status_code, 429)
        for _ in range(20):
            self.assertEqual(self.client.get("/admin").status_code, 401)
        self.assertEqual(self.client.get("/admin").status_code, 429)
        self.assertEqual(self.client.get("/admin", headers=self.auth).status_code, 429)
        with mock.patch("leaderboard.store.time.time", return_value=time.time() + 61):
            self.assertEqual(self.client.get("/admin", headers=self.auth).status_code, 200)

    def test_example_secrets_refused_outside_tests(self):
        with self.assertRaises(RuntimeError):
            create_app({"DATABASE": self.path, "SECRET_KEY": "replace-with-a-random-secret", "ADMIN_PASSWORD": "replace-with-a-long-admin-password"})

    def test_team_order_does_not_change_with_resubmission(self):
        _, token = self.submit("A", "team/api:v1")
        self.submit("B", "team/api:v1")
        self.submit("A", "team/api:v2", token)
        with store.connect(self.path) as db:
            self.assertEqual([r["nickname"] for r in store.latest(db)], ["A", "B"])

    def test_invitation_is_required_and_consumed_atomically(self):
        with self.assertRaises(ValueError):
            self.submit("No invite", "team/api:v1", invitation="")
        code = store.issue_invitation(self.path)
        self.submit("First", "team/api:v1", invitation=code)
        with self.assertRaises(ValueError):
            self.submit("Second", "team/api:v1", invitation=code)
        self.assertEqual(self.client.post("/admin/invitations", data={"csrf_token": self.csrf}).status_code, 401)
        self.assertEqual(self.client.post("/admin/invitations", data={"csrf_token": self.csrf}, headers=self.auth).status_code, 201)

    def test_infrastructure_error_preserves_previous_publication(self):
        self.submit("Team", "team/api:v1")
        with store.connect(self.path) as db:
            db.execute("UPDATE submissions SET approved_image=?", ("sha256:" + "a" * 64,))
            db.execute("UPDATE control SET requested=1")
        with self.assertRaises(InterruptedError):
            worker.run_batch(self.path, *worker.claim(self.path),
                             benchmark=lambda _: {"status": "failed", "error_kind": "infrastructure", "error": "Generator OOM"},
                             resolve=lambda _: {"image_id": "same"})
        with store.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT state FROM batches").fetchone()[0], "failed")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM results").fetchone()[0], 0)


class ScoringBoundaryTests(unittest.TestCase):
    def test_old_seed_data_cannot_be_replaced_with_constant_like_counters(self):
        from bench.verify_loaded import capture
        fake = mock.Mock()
        fake.request.return_value = {"post_id": 1, "likes": 1, "liked": True}
        with self.assertRaisesRegex(AssertionError, "Seeded likes disappeared"):
            capture(fake, 1, 10)

    def test_measured_feed_is_checked_before_new_posts_hide_it(self):
        from bench.verify_loaded import verify
        fake = mock.Mock()
        fake.feed.return_value = []
        with self.assertRaisesRegex(AssertionError, "Measured workload feed"):
            verify(fake, {"feed": [{"id": 123}], "samples": []})

    def test_nominal_vus_without_work_cannot_pass(self):
        from test_system import ScoreTests
        raw = ScoreTests().raw(hold_requests={"count": 1})
        self.assertFalse(runner.score_level(raw, 500, runner.profile(), 0)["passed"])
        for changes in [{"integrity_completed": {"count": 0}}, {"vus": {"max": 500}}, {"semantic_errors": {"count": 1}}]:
            self.assertFalse(runner.score_level(ScoreTests().raw(**changes), 500, runner.profile(), 0)["passed"])

    def test_median_uses_discrete_levels_and_metrics_at_that_level(self):
        runs = []
        for i, maximum in enumerate([500, 1000, 2000]):
            runs.append({"id": str(i), "status": "passed", "image_id": "same", "profile_hash": "same", "finished_at": "now",
                         "max_vus": maximum, "cold_start_samples_ms": [10, 20, 30],
                         "levels": [{"vus": v, "passed": True, "p99_ms": v / 10, "p95_ms": v / 20,
                                     "throughput_rps": v, "error_percent": 0} for v in [500, 1000, 2000] if v <= maximum]})
        result = runner.aggregate(runs)
        self.assertEqual(result["max_vus"], 1000)
        self.assertEqual(result["p99_ms"], 100)
        self.assertNotIn("levels", result)
        self.assertEqual(len(result["repetitions"]), 3)
        runs[2]["status"] = "failed"
        with self.assertRaises(ValueError):
            runner.aggregate(runs)

    def test_exact_ties_ignore_names(self):
        value = {"max_vus": 500, "image_bytes": 40, "cold_start_ms": 20, "p99_ms": 10}
        self.assertEqual(store.ranking_key({"nickname": "A", "result": value}), store.ranking_key({"nickname": "Z", "result": value}))
