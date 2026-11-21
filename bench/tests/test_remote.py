import json
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from bench import profiles, runner
from leaderboard import remote, store
from leaderboard.app import create_app


class RemoteTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = str(Path(directory.name) / 'board.db')
        self.app = create_app(dict(TESTING=True, DATABASE=self.path, SECRET_KEY='tests', ADMIN_PASSWORD='tests',
                                  REMOTE_RUNNER=True, RUNNER_TOKEN='test-only-' * 8))
        self.headers = {'X-Runner-Token': 'test-only-' * 8}
        self.client = self.app.test_client()
        self.metadata = dict(cpus=2, memory_bytes=3800 * 1024**2, role='test', kernel='kernel',
                             runtime='runsc version', engine='docker', version='code', architecture='amd64')
        self.assertEqual(self.post('register', address='test.example', metadata=self.metadata).status_code, 200)
        with store.connect(self.path) as db:
            remote.configure(db, 'test.example', 'full', {}, True, time.time())
            db.execute('UPDATE control SET requested=1')
        self.sid, _ = store.submit(self.path, 'Team', 'team/api', invitation=store.issue_invitation(self.path))
        with store.connect(self.path) as db:
            db.execute('UPDATE submissions SET approved_artifact=?', (json.dumps(remote.artifact('team/api@sha256:' + 'a'*64)),))

    def post(self, operation, **data):
        return self.client.post('/api/runner/' + operation, json=data, headers=self.headers)

    def job(self):
        response = self.post('claim', address='test.example')
        self.assertEqual(response.status_code, 200, response.text)
        return response.json['job']

    def result(self, job):
        return dict(status='passed', profile=job['config'], profile_hash=job['profile_hash'],
                    image_id='sha256:'+'b'*64, image_bytes=40000000, cold_start_ms=500,
                    max_vus=10, p99_ms=45, error_percent=0,
                    repetitions=[dict(max_vus=10)]*3)

    def test_auth_csrf_and_public_projection(self):
        self.assertEqual(self.client.post('/api/runner/claim', json={}).status_code, 401)
        self.assertEqual(self.client.post('/admin/runner', json={}, headers=self.headers).status_code, 400)
        self.assertNotIn('test.example', self.client.get('/api/leaderboard').text)
        self.assertNotIn('test-only-', self.client.get('/').text)
        self.assertEqual(self.post('register', address='https://127.0.0.1', metadata=self.metadata).status_code, 400)

    def test_atomic_publication_and_exact_replay(self):
        job = self.job()
        self.assertIsNone(self.job())
        self.assertIsNone(store.board(self.path)['last_update'])
        payload = dict(address='test.example', id=job['id'], lease=job['lease'], result=self.result(job))
        response = self.post('complete', **payload)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(store.board(self.path)['ranking']), 1)
        self.assertTrue(self.post('complete', **payload).json['duplicate'])
        payload['result']['image_bytes'] += 1
        self.assertEqual(self.post('complete', **payload).status_code, 409)

    def test_stale_lease_cannot_publish_and_attempts_are_bounded(self):
        job = self.job()
        with store.connect(self.path) as db:
            remote.expire(db, time.time() + 100)
        self.assertEqual(self.post('complete', address='test.example', id=job['id'], lease=job['lease'], result=self.result(job)).status_code, 409)
        with store.connect(self.path) as db:
            db.execute('UPDATE jobs SET available_at=0')
        replacement = self.job()
        self.assertNotEqual(replacement['lease'], job['lease'])
        with store.connect(self.path) as db:
            db.execute('UPDATE jobs SET attempt=3')
            remote.expire(db, time.time() + 100)
            self.assertEqual(db.execute('SELECT state FROM batches').fetchone()[0], 'failed')
        self.assertIsNone(store.board(self.path)['last_update'])

    def test_infrastructure_failure_and_bad_profile_never_publish(self):
        job = self.job()
        result = self.result(job)
        result['profile_hash'] = 'c'*64
        payload = dict(address='test.example', id=job['id'], lease=job['lease'], result=result)
        self.assertEqual(self.post('complete', **payload).status_code, 400)
        result.update(profile_hash=job['profile_hash'], status='failed', error_kind='infrastructure')
        self.assertEqual(self.post('complete', **payload).status_code, 200)
        self.assertIsNone(store.board(self.path)['last_update'])

    def test_concurrent_pulls_cannot_overbook_full_profile(self):
        def claim(_):
            with store.connect(self.path) as db:
                db.execute('BEGIN IMMEDIATE')
                return remote.claim(db, {'address':'test.example'}, time.time())['job']
        with ThreadPoolExecutor(max_workers=8) as pool:
            jobs = list(pool.map(claim, range(8)))
        self.assertEqual(sum(j is not None for j in jobs), 1)

    def test_parallel_batch_waits_for_every_team_and_keeps_profile(self):
        store.submit(self.path, 'Second team', 'second/api', invitation=store.issue_invitation(self.path))
        with store.connect(self.path) as db:
            db.execute('UPDATE submissions SET approved_artifact=?', (json.dumps(remote.artifact('team/api@sha256:' + 'a'*64)),))
            remote.configure(db, 'test.example', 'parallel', {}, True, time.time())
        first, second = self.job(), self.job()
        self.assertIsNone(self.job())
        for index, job in enumerate((first, second)):
            response = self.post('complete', address='test.example', id=job['id'], lease=job['lease'], result=self.result(job))
            self.assertEqual(response.status_code, 200, response.text)
            board = store.board(self.path)
            self.assertEqual(len(board['ranking']), 2 if index else 0)
        self.assertEqual(board['selected_profile'], first['profile_hash'])
        self.assertTrue(all(row['result']['profile_hash'] == first['profile_hash'] for row in board['ranking']))

    def test_machine_body_limit_allows_results_but_bounds_payload(self):
        job = self.job()
        payload = dict(address='test.example', id=job['id'], lease=job['lease'], result=self.result(job), log='x'*60000)
        self.assertEqual(self.post('complete', **payload).status_code, 200)
        payload['log'] = 'x' * (1024 * 1024)
        self.assertEqual(self.post('complete', **payload).status_code, 413)

    def test_profile_budget_and_banner_guard(self):
        host = {**self.metadata, 'fingerprint': 'a'*64}
        self.assertEqual(profiles.make(host, 'parallel')['slots'], 2)
        with self.assertRaises(ValueError):
            profiles.make(host, 'parallel', {'memory_bytes': 2*profiles.GIB})
        with self.assertRaises(ValueError):
            profiles.make({**host, 'role':'official'})
        with store.connect(self.path) as db:
            with self.assertRaises(ValueError):
                remote.configure(db, 'test.example', 'full', {}, False, time.time())
        self.assertIn('test-banner', self.client.get('/').text)

    def test_only_immutable_safe_sources_are_approved(self):
        for value in ['team/api:latest', 'localhost/x@sha256:'+'a'*64, 'https://github.com/a/b@main', 'https://github.com/a/..@'+'a'*40]:
            with self.assertRaises(ValueError, msg=value):
                remote.artifact(value)
        self.assertEqual(remote.artifact('https://github.com/a/b@'+'a'*40)['kind'], 'repo')
