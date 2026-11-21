import base64
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bench import runner
from leaderboard import store, worker
from leaderboard.app import create_app


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / 'board.db')
        self.allowed = 'sha256:' + 'a' * 64
        self.app = create_app(dict(TESTING=True, DATABASE=self.path, SECRET_KEY='test-only',
                                  ADMIN_PASSWORD='test-admin', BENCH_SMOKE=True,
                                  ALLOWED_IMAGE_IDS=[self.allowed]))
        self.client = self.app.test_client()

    def test_language_negotiation_switch_and_post_errors(self):
        for language, text in [('pl-PL, en;q=0.8', 'Dołącz do gry'), ('en-US, pl;q=0.5', 'Join the challenge')]:
            response = self.client.get('/submit', headers={'Accept-Language': language})
            self.assertIn(text, response.text)
            self.assertIn('Accept-Language', response.headers['Vary'])
        response = self.client.get('/?lang=pl', headers={'Accept-Language': 'en'})
        self.assertEqual(response.headers['Content-Language'], 'pl')
        self.assertIn('Wyniki testowe', response.text)
        self.assertIn('Sesja formularza wygasła', self.client.post('/submissions').text)
        self.assertIn('Nie znaleziono strony', self.client.get('/missing').text)
        self.assertIn('Wyniki testowe', self.client.get('/screen').text)
        self.client.get('/?lang=en')
        self.assertIn('Your form session expired', self.client.post('/submissions').text)
        self.assertIn('Administrator password required', self.client.get('/admin').text)
        self.assertIn('Page not found', self.client.get('/missing').text)
        self.assertIn('Test results', self.client.get('/screen?lang=invalid').text)
        token = re.search(r'name="csrf_token" value="([^"]+)"', self.client.get('/submit').text)[1]
        response = self.client.post('/submissions', data=dict(csrf_token=token, nickname='Team', source='https://localhost/'))
        self.assertIn('Use a public https://github.com/', response.text)
        response = self.client.post('/submissions', data=dict(csrf_token=token, nickname='Team', source='team/api'))
        self.assertIn('Your first submission requires', response.text)

    def test_allowlist_blocks_approval_and_execution(self):
        image = 'sha256:' + 'b' * 64
        submission, _ = store.submit(self.path, 'Team', 'team/api', invitation=store.issue_invitation(self.path))
        token = re.search(r'name="csrf_token" value="([^"]+)"', self.client.get('/submit').text)[1]
        auth = {'Authorization': 'Basic ' + base64.b64encode(b'admin:test-admin').decode()}
        response = self.client.post(f'/admin/submissions/{submission}/approve', headers=auth,
                                    data=dict(csrf_token=token, image_id=image))
        self.assertEqual(response.status_code, 400)
        with store.connect(self.path) as db:
            db.execute('UPDATE submissions SET approved_image=?', (image,))
            db.execute('UPDATE control SET requested=1')
        with mock.patch.dict('os.environ', {'BENCH_ALLOWED_IMAGE_IDS': self.allowed}), \
             mock.patch.object(runner, 'resolve_source') as resolve:
            worker.run_batch(self.path, *worker.claim(self.path), smoke=True)
        resolve.assert_not_called()
        response = self.client.get('/api/leaderboard?lang=en').json
        self.assertIn("organizer's approved starter", response['rejected'][0]['result']['error'])

    def test_smoke_caps_leave_room_for_web_and_host(self):
        smoke = runner.profile(True)
        self.assertEqual(smoke['memory_bytes'], 256 * 1024**2)
        self.assertEqual(runner.profile()['memory_bytes'], 2 * 1024**3)
        with tempfile.TemporaryDirectory() as directory:
            run = runner.Run('team/api', smoke, directory, 'never')
            run.result['image_id'] = self.allowed
            with mock.patch.object(runner, 'docker') as docker:
                run.create_sut()
            args = docker.call_args.args
            self.assertEqual(args[args.index('--memory') + 1], smoke['memory_bytes'])
