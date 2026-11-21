import base64
import json
import re
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from bench import extras as private_adapter, runner
from bench.tests.test_homepage import publish, recorded_result
from leaderboard import extras, store, worker
from leaderboard.app import create_app


class ExtrasTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / 'board.db')
        self.spec = Path(self.temp.name) / 'announcement.json'
        self.spec.write_text(json.dumps({'pl': '<b>private announcement PL</b>', 'en': 'private announcement EN'}))
        self.app = create_app(dict(TESTING=True, DATABASE=self.path, SECRET_KEY='test-only',
                                   ADMIN_PASSWORD='test-admin', BONUS_SPEC_FILE=str(self.spec)))
        self.client = self.app.test_client()
        self.auth = {'Authorization': 'Basic ' + base64.b64encode(b'admin:test-admin').decode()}

    def tearDown(self):
        self.temp.cleanup()

    def csrf(self, client=None):
        client = client or self.client
        client.get('/submit')
        with client.session_transaction() as session:
            return session['csrf']

    def post(self, route, data=None, admin=False, client=None):
        client = client or self.client
        return client.post(route, data={'csrf_token': self.csrf(client), **(data or {})}, headers=self.auth if admin else {})

    def enable(self, **changes):
        with store.connect(self.path) as db:
            for name, value in changes.items():
                extras.save(db, name, value)

    def team(self, name='Test', **metadata):
        return store.submit(self.path, name, 'team/api:v1', invitation=store.issue_invitation(self.path), metadata=metadata)

    def test_defaults_and_admin_boundaries(self):
        with store.connect(self.path) as db:
            config = extras.settings(db)
        self.assertTrue(config['badges'] and config['hidden'])
        self.assertFalse(any(config[k] for k in ('chaos', 'prompt_logs', 'voting', 'bonus', 'ai_pilot')))
        self.assertEqual(self.post('/admin/extras', {'ai_pilot': '1'}, admin=True).status_code, 400)
        for route in ['/admin/extras', '/admin/bonus/announce', '/admin/voting', '/admin/submissions/1/language']:
            self.assertEqual(self.client.post(route, headers=self.auth).status_code, 400)
            self.assertEqual(self.post(route).status_code, 401)
        self.assertEqual(self.post('/admin/extras', {'badges': '1', 'voting': '1'}, admin=True).status_code, 303)
        self.assertEqual(self.post('/admin/voting', {'state': 'open'}, admin=True).status_code, 303)
        self.post('/admin/extras', {'badges': '1'}, admin=True)
        with store.connect(self.path) as db:
            self.assertEqual(extras.settings(db)['vote_state'], 'closed')

    def test_prompt_links_and_escaping(self):
        for link in ['http://github.com/a/b', 'javascript:alert(1)', 'https://github.com@evil.test/a/b',
                     'https://github.com/a/b%0a', 'https://github.com:444/a/b', 'https://github.com/a/b\\x',
                     'https://github.com/a/<script>', 'https://localhost/a/b']:
            with self.subTest(link=link), self.assertRaises(ValueError):
                extras.metadata(prompt_log=link)
        self.enable(prompt_logs=True)
        sid, token = self.team('<script>alert(1)</script>', language='<b>Rust</b>',
                               prompt_log='https://gist.github.com/person/abc', ai_tool='<svg onload=alert(1)>')
        html = self.client.get('/team/1?lang=en').get_data(as_text=True)
        self.assertIn('&lt;script&gt;', html)
        self.assertNotIn('<svg onload', html)
        self.assertIn('rel="nofollow noopener noreferrer"', html)
        self.enable(prompt_logs=False)
        self.assertNotIn('gist.github.com/person', self.client.get('/team/1').get_data(as_text=True))
        self.post(f'/admin/submissions/{sid}/language', {'verified': '1'}, admin=True)
        newer, _ = store.submit(self.path, '<script>alert(1)</script>', 'team/api:v2', token, metadata={'language': 'Go'})
        with store.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT language_verified FROM submissions WHERE id=?', (newer,)).fetchone()[0], 0)

    def test_badges_boundaries_ties_and_main_score(self):
        rows = []
        for i, size in enumerate([49999999, 49999999, 50000000]):
            rows.append({'participant_id': i, 'result': {**recorded_result(), 'image_bytes': size},
                         'language': 'Lisp', 'language_verified': i != 2})
        keys = [store.ranking_key(r) for r in rows]
        extras.attach(rows, extras.DEFAULTS, set())
        self.assertEqual([store.ranking_key(r) for r in rows], keys)
        self.assertIn('smallest', rows[0]['badges'])
        self.assertIn('smallest', rows[1]['badges'])
        self.assertNotIn('smallest', rows[2]['badges'])
        self.assertIn('language', rows[0]['badges'])
        self.assertNotIn('language', rows[2]['badges'])
        rows[0]['result']['max_vus'] = 0
        rows[0]['result']['p99_ms'] = None
        rows[1]['result']['status'] = 'failed'
        extras.attach(rows, extras.DEFAULTS, set())
        self.assertNotIn('latency', rows[0]['badges'])
        self.assertEqual(rows[1]['badges'], [])
        extras.attach(rows, {**extras.DEFAULTS, 'badges': False}, set())
        self.assertTrue(all(not r['badges'] for r in rows))

    def test_hidden_projection_no_private_diagnostics(self):
        sid, _ = self.team()
        result = recorded_result()
        result['extras'] = {'hidden': {'status': 'passed', 'passed': 3, 'total': 4, 'cases': ['private-canary']},
                            'chaos': {'status': 'failed', 'details': 'private-canary'}}
        result['secret'] = 'private-canary'
        result['repetitions'][0]['private'] = 'private-canary'
        publish(self.path, sid, result)
        for route in ['/api/leaderboard', '/team/1', '/screen']:
            self.assertNotIn('private-canary', self.client.get(route).get_data(as_text=True))
        row = self.client.get('/api/leaderboard').json['ranking'][0]
        self.assertEqual(row['extras']['hidden'], {'status': 'failed', 'passed': 3, 'total': 4})
        self.assertNotIn('chaos', row['extras'])
        self.assertEqual(row['result']['max_vus'], result['max_vus'])
        for bad in [None, {'hidden': {'status': 'passed'}}, {'hidden': {'passed': True, 'total': 2}},
                    {'hidden': {'passed': 99, 'total': 2}}]:
            self.assertNotEqual(extras.public_result(bad).get('hidden', {}).get('status'), 'passed')

    def test_bonus_is_private_until_announcement_and_escaped(self):
        for route in ['/', '/screen', '/api/leaderboard', '/admin']:
            self.assertNotIn('private announcement', self.client.get(route, headers=self.auth).get_data(as_text=True))
        self.assertEqual(self.post('/admin/bonus/announce', admin=True).status_code, 400)
        self.enable(bonus=True)
        self.assertEqual(self.post('/admin/bonus/announce', admin=True).status_code, 303)
        html = self.client.get('/screen?lang=pl').get_data(as_text=True)
        self.assertIn('&lt;b&gt;private announcement PL&lt;/b&gt;', html)
        self.assertNotIn('<b>private announcement', html)
        self.spec.write_text('invalid')
        self.assertEqual(self.post('/admin/bonus/announce', admin=True).status_code, 303)
        self.assertIn('private announcement EN', self.client.get('/screen?lang=en').get_data(as_text=True))
        self.enable(bonus=False)
        self.assertNotIn('private announcement', self.client.get('/screen').get_data(as_text=True))

    def test_votes_csrf_tampering_duplicate_and_closed_results(self):
        self.team()
        self.enable(voting=True, vote_state='open')
        self.client.get('/vote')
        self.assertEqual(self.client.post('/vote', data={'participant_id': '1'}).status_code, 400)
        self.assertEqual(self.post('/vote', {'participant_id': '1'}).status_code, 303)
        self.assertEqual(self.post('/vote', {'participant_id': '1'}).status_code, 400)
        with self.client.session_transaction() as session:
            session.clear()
        self.assertEqual(self.post('/vote', {'participant_id': '1'}).status_code, 400)
        self.assertNotIn('Voting results', self.client.get('/vote?lang=en').get_data(as_text=True))
        other = self.app.test_client()
        other.set_cookie('vz_device', 'forged')
        self.assertEqual(self.post('/vote', {'participant_id': '1'}, client=other).status_code, 400)
        self.enable(vote_state='closed')
        self.assertIn('Test</a>: 1', self.client.get('/vote?lang=en').get_data(as_text=True))
        self.assertEqual(self.post('/vote', {'participant_id': '1'}, client=other).status_code, 400)
        self.enable(voting=False)
        self.assertNotIn('Test</a>: 1', self.client.get('/vote?lang=en').get_data(as_text=True))

    def test_votes_serialized_and_hidden_teams_excluded(self):
        sid, _ = self.team()
        self.enable(voting=True, vote_state='open')
        def cast(_):
            try:
                with store.connect(self.path) as db:
                    extras.cast_vote(db, 'same-device', 1)
                return True
            except ValueError:
                return False
        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(sum(pool.map(cast, range(8))), 1)
        self.post(f'/admin/submissions/{sid}/hide', admin=True)
        with store.connect(self.path) as db, self.assertRaises(ValueError):
            extras.cast_vote(db, 'another-device', 1)
        self.assertNotIn('Test</a>', self.client.get('/vote').get_data(as_text=True))

    def test_pilot_uses_invitation_time_not_batch_time_and_survives_retention(self):
        self.enable(ai_pilot=True)
        invitation = store.issue_invitation(self.path, pilot=True)
        with store.connect(self.path) as db:
            db.execute('UPDATE invitations SET issued_at=?', (time.time()-123,))
        sid, token = store.submit(self.path, 'Pilot', 'team/api:v1', invitation=invitation)
        with store.connect(self.path) as db:
            submission = dict(db.execute('SELECT * FROM submissions WHERE id=?', (sid,)).fetchone())
            result = {**recorded_result(), 'extras': {'hidden': {'status': 'passed', 'passed': 5, 'total': 5}}}
            extras.record_green(db, submission, result, True)
            first = dict(db.execute('SELECT * FROM pilot_green').fetchone())
            self.assertAlmostEqual(first['elapsed_seconds'], 123, delta=1)
            extras.record_green(db, {**submission, 'created_at': time.time()+100}, result, True)
            self.assertEqual(dict(db.execute('SELECT * FROM pilot_green').fetchone()), first)
        new_sid, _ = store.submit(self.path, 'Pilot', 'team/api:v2', token)
        with store.connect(self.path) as db:
            self.assertEqual(extras.public_state(db)['pilot'][0]['submission_id'], sid)
        self.post(f'/admin/submissions/{new_sid}/hide', admin=True)
        with store.connect(self.path) as db:
            self.assertEqual(extras.public_state(db)['pilot'], [])

    def test_hidden_pilot_result_can_be_replaced_by_a_later_pass(self):
        self.enable(ai_pilot=True)
        invite = store.issue_invitation(self.path, pilot=True)
        first, token = store.submit(self.path, 'Pilot', 'team/api:v1', invitation=invite)
        result = {**recorded_result(), 'extras': {'hidden': {'status': 'passed', 'passed': 5, 'total': 5}}}
        with store.connect(self.path) as db:
            submission = dict(db.execute('SELECT * FROM submissions WHERE id=?', (first,)).fetchone())
            extras.record_green(db, submission, result, True)
        second, _ = store.submit(self.path, 'Pilot', 'team/api:v2', token)
        self.post(f'/admin/submissions/{first}/hide', admin=True)
        with store.connect(self.path) as db:
            self.assertEqual(extras.public_state(db)['pilot'], [])
            submission = dict(db.execute('SELECT * FROM submissions WHERE id=?', (second,)).fetchone())
            extras.record_green(db, submission, result, True)
            self.assertEqual(extras.public_state(db)['pilot'][0]['submission_id'], second)

    def test_missing_private_checks_fail_closed_without_main_score_change(self):
        run = runner.Run('team/api:v1', runner.profile(True), self.temp.name, 'never')
        run.result.update(image_id='sha256:'+'a'*64, status='passed', max_vus=10)
        with mock.patch.dict('os.environ', {'VZ_PRIVATE_DIR': self.temp.name, 'VZ_RUNNER_ROLE': 'smoke', 'BENCH_ALLOWED_IMAGE_IDS': ''}):
            result = private_adapter.execute(run, ['hidden', 'chaos'])
        self.assertEqual(result, {'hidden': {'status': 'unavailable'}})
        self.assertEqual(run.result['max_vus'], 10)
        self.assertEqual(run.result['status'], 'passed')

    def test_migration_is_idempotent_and_keeps_existing_data(self):
        self.team()
        for _ in range(2):
            store.initialize(self.path)
        self.assertEqual(store.board(self.path)['summary']['teams'], 1)
        with store.connect(self.path) as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')

    def test_assets_cache_and_public_html_do_not_share_forms(self):
        html = self.client.get('/submit?lang=en')
        self.assertEqual(html.headers['Cache-Control'], 'no-store, no-transform')
        url = re.search(r'href="([^"]+style.css[^"]+)"', html.get_data(as_text=True))[1]
        response = self.client.get(url)
        self.assertIn('immutable', response.headers['Cache-Control'])
        response.close()
        response = self.client.get(url + '&lang=pl')
        self.assertNotIn('Set-Cookie', response.headers)
        self.assertNotIn('Cookie', response.headers.get('Vary', ''))
        response.close()
        missing = self.client.get(url.replace('style.css', 'missing.css'))
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.headers['Cache-Control'], 'no-store, no-transform')
        self.assertEqual(self.client.get('/robots.txt').status_code, 200)
        self.assertNotIn('board.js', self.client.get('/start').text)
        self.assertNotIn('id="translations"', self.client.get('/submit').text)
        self.assertIn('board.js', self.client.get('/leaderboard').text)

    def test_board_query_count_does_not_grow_with_teams(self):
        import sqlite3
        original = sqlite3.connect
        counts = []
        def measured(*args, **kwargs):
            connection = original(*args, **kwargs)
            connection.set_trace_callback(lambda sql: counts.append(sql) if sql.lstrip().upper().startswith('SELECT') else None)
            return connection
        self.team('first')
        with mock.patch.object(store.sqlite3, 'connect', side_effect=measured):
            store.board(self.path)
        first = len(counts)
        for i in range(20):
            self.team(f'team {i}')
        counts.clear()
        with mock.patch.object(store.sqlite3, 'connect', side_effect=measured):
            store.board(self.path)
        self.assertEqual(len(counts), first)
        self.assertLessEqual(first, 12)

    def test_vote_rate_limit_and_secure_cookie(self):
        self.app.config['SESSION_COOKIE_SECURE'] = True
        self.team()
        self.enable(voting=True, vote_state='open')
        response = self.client.get('/vote')
        cookie = next(v for v in response.headers.getlist('Set-Cookie') if v.startswith('vz_device='))
        self.assertIn('Secure', cookie)
        self.assertIn('HttpOnly', cookie)
        self.assertIn('SameSite=Strict', cookie)
        csrf = self.csrf()
        with mock.patch.object(store, 'allow_request', return_value=False):
            self.assertEqual(self.client.post('/vote', data={'csrf_token': csrf, 'participant_id': '1'}).status_code, 429)

    def test_batch_publishes_hidden_and_first_green_atomically(self):
        self.enable(ai_pilot=True)
        invite = store.issue_invitation(self.path, pilot=True)
        sid, _ = store.submit(self.path, 'Pilot', 'team/api:v1', invitation=invite)
        image = 'sha256:'+'a'*64
        with store.connect(self.path) as db:
            db.execute('UPDATE submissions SET approved_image=?', (image,))
            db.execute('UPDATE control SET requested=1')
        work = worker.claim(self.path)
        result = {**recorded_result(), 'extras': {'hidden': {'status': 'passed', 'passed': 5, 'total': 5}}}
        with mock.patch.dict('os.environ', {'BENCH_ALLOWED_IMAGE_IDS': image}):
            worker.run_batch(self.path, *work, benchmark=lambda _: result, resolve=lambda _: {'image_id': image}, smoke=True)
        board = store.board(self.path)
        self.assertEqual(len(board['extras']['pilot']), 1)
        self.assertIn('pilot', board['ranking'][0]['badges'])
        self.assertEqual(board['ranking'][0]['result']['max_vus'], 10)

    def test_cleanup_accepts_lowercase_missing_resource_errors(self):
        import subprocess
        run = runner.Run('team/api:v1', runner.profile(True), self.temp.name, 'never')
        run.lock_acquired = True
        with mock.patch.object(runner, 'docker', return_value=subprocess.CompletedProcess([], 1, '', 'no such volume')) as docker:
            run.cleanup()
        self.assertNotIn('cleanup_errors', run.result)
        self.assertEqual(docker.call_args.args, ('rm', '-fv', 'vibezone-benchmark-lock'))
