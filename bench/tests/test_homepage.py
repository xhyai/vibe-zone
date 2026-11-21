"""Public navigation, recorded metrics and history privacy regressions."""
import json
import re
import tempfile
import unittest
from pathlib import Path

from leaderboard import store
from leaderboard.app import create_app
from leaderboard.content import GUIDE
from leaderboard.i18n import ENGLISH


def recorded_result(vus=10, profile_hash='a' * 64, status='passed'):
    return dict(status=status, max_vus=vus, image_bytes=41000000, cold_start_ms=120,
                p95_ms=12 if vus else None, p99_ms=20 if vus else None,
                throughput_rps=5.2 if vus else None, error_percent=0 if vus else None,
                profile={'name': 'smoke'}, profile_hash=profile_hash,
                finished_at='2026-11-16T12:00:00+00:00',
                repetitions=[{'max_vus': v} for v in [0, vus, vus]],
                cold_start_samples_ms=[100, 110, 120] * 3)


def publish(path, submission, result, state='published'):
    with store.connect(path) as db:
        batch = db.execute('INSERT INTO batches(started_at,finished_at,state) VALUES (1,2,?)', (state,)).lastrowid
        db.execute('INSERT INTO results VALUES (?,?,?)', (batch, submission, json.dumps(result)))
    return batch


class HomepageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = str(Path(self.directory.name) / 'board.db')
        self.app = create_app(dict(TESTING=True, DATABASE=self.path, SECRET_KEY='unit-test',
                                  ADMIN_PASSWORD='unit-test', BENCH_SMOKE=True))
        self.client = self.app.test_client()
        self.invitation = store.issue_invitation(self.path)
        self.submission, self.token = store.submit(self.path, '<script>team</script>', 'team/api:v1', invitation=self.invitation)

    def test_routes_languages_csp_and_legacy_post(self):
        for lang, title in [('pl', 'Jak zacząć'), ('en', 'Get started')]:
            for path in ['/', '/leaderboard', '/submit', '/start', '/screen', '/team/1']:
                response = self.client.get(path + '?lang=' + lang)
                self.assertEqual(response.status_code, 200, path)
                self.assertIn(f'<html lang="{lang}">', response.text)
                self.assertIn("script-src 'self'", response.headers['Content-Security-Policy'])
                self.assertIn('test-banner', response.text)
                self.assertNotIn('<script>team</script>', response.text)
            self.assertIn(title, self.client.get('/start').text)
        self.assertEqual(self.client.get('/submissions?lang=pl').location, '/submit?lang=pl')
        self.assertEqual(self.client.get('/team/999').status_code, 404)
        csrf = re.search('name="csrf_token" value="([^"]+)"', self.client.get('/submit').text)[1]
        self.assertEqual(self.client.post('/submissions', data=dict(csrf_token=csrf, nickname='<script>team</script>',
            source='team/api:v2', team_token=self.token)).status_code, 201)
        self.assertEqual(self.client.post('/submissions').status_code, 400)
        self.assertNotIn('<form', self.client.get('/screen').text)
        self.app.config['BENCH_SMOKE'] = False
        self.assertNotIn('class="wrap test-banner"', self.client.get('/').text)

    def test_metrics_use_existing_samples_and_keep_missing_null(self):
        publish(self.path, self.submission, recorded_result(0))
        row = store.board(self.path)['ranking'][0]
        self.assertIsNone(row['result']['p99_ms'])
        self.assertEqual(row['stats'], dict(measurements=3, vus_min=0, vus_max=0,
                         startup_samples=9, startup_min_ms=100, startup_max_ms=120))
        self.assertEqual(store.measurement_stats({})['measurements'], 0)
        html = self.client.get('/leaderboard?lang=en').text
        self.assertIn('-', html)
        self.assertNotIn('0.0 ms', html)
        self.assertIn('docker image inspect .Size', html)

    def test_only_published_visible_history_is_public(self):
        publish(self.path, self.submission, recorded_result())
        publish(self.path, self.submission, {**recorded_result(), 'error': self.token, 'secret': self.invitation}, 'failed')
        self.assertEqual(len(store.team_history(self.path, 1)['history']), 1)
        for response in [self.client.get('/api/leaderboard'), self.client.get('/team/1')]:
            self.assertNotIn(self.token, response.text)
            self.assertNotIn(self.invitation, response.text)
            self.assertNotIn('token_hash', response.text)
        second, _ = store.submit(self.path, '<script>team</script>', 'team/api:v2', self.token)
        with store.connect(self.path) as db:
            db.execute('UPDATE submissions SET hidden=1 WHERE id=?', (second,))
        self.assertIsNone(store.team_history(self.path, 1))
        self.assertEqual(self.client.get('/team/1').status_code, 404)
        self.assertEqual(store.board(self.path)['ranking'], [])
        self.assertEqual(store.board(self.path)['summary']['teams'], 0)

    def test_trend_does_not_connect_different_profiles_or_failures(self):
        publish(self.path, self.submission, recorded_result(profile_hash='b' * 64))
        publish(self.path, self.submission, recorded_result(5))
        publish(self.path, self.submission, recorded_result(10))
        detail = store.team_history(self.path, 1)
        self.assertEqual(detail['trend_values'], [5, 10])
        self.assertEqual(len(detail['history']), 3)
        publish(self.path, self.submission, recorded_result(status='failed'))
        self.assertEqual(store.team_history(self.path, 1)['trend_values'], [])

    def test_summary_and_top_five_preserve_ranks(self):
        batch = publish(self.path, self.submission, recorded_result())
        for i in range(2, 8):
            sid, _ = store.submit(self.path, f'Team {i}', 'team/api', invitation=store.issue_invitation(self.path))
            with store.connect(self.path) as db:
                db.execute('INSERT INTO results VALUES (?,?,?)', (batch, sid, json.dumps(recorded_result())))
        board = store.board(self.path)
        self.assertEqual(board['summary'], dict(teams=7, submissions=7, batches=1, best_vus=10))
        self.assertEqual([row['rank'] for row in board['ranking']], [1] * 7)
        home = self.client.get('/').text
        self.assertEqual(home.count('<span class="place">'), 5)
        self.assertNotIn('Team 7', home)

    def test_catalog_covers_templates_script_and_guide(self):
        root = Path(__file__).resolve().parents[2] / 'leaderboard'
        for path in [*root.joinpath('templates').glob('*.html'), root / 'static/board.js']:
            for quote, text in re.findall(r'\bt\(([\'"])(.*?)\1\)', path.read_text()):
                self.assertIn(text, ENGLISH, f'{path.name}: {text}')
        for step in GUIDE:
            self.assertIn(step['title'], ENGLISH)
            self.assertIn(step['body'], ENGLISH)


if __name__ == '__main__':
    unittest.main()
