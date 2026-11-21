import unittest
from leaderboard.charts import EXAMPLE, result_chart, load_chart
from bench.tests import test_homepage as fixtures
from bench.tests.test_homepage import publish, recorded_result


class ExplanationTests(unittest.TestCase):
    setUp = fixtures.HomepageTests.setUp

    def test_home_explains_api_and_score_without_javascript(self):
        publish(self.path, self.submission, recorded_result())
        for lang, text in [('pl', 'Przykład, nie wynik zespołu.'), ('en', 'Example, not a team result.')]:
            html = self.client.get('/?lang=' + lang).text
            for marker in ['flow-title', 'load-title', 'POST /posts', 'GET /feed', 'POST /like', '/start#ai-pilot', text, '01.01.1970 01:00:02']:
                self.assertIn(marker, html)
            self.assertNotRegex(html, 'id="last-update"[^>]*>not yet<')
        self.assertEqual(EXAMPLE['score'], 100)

    def test_plot_uses_recorded_median_run_and_skips_missing_values(self):
        result = recorded_result(10)
        result['repetitions'][1]['levels'] = [dict(vus=2, p99_ms=10, passed=True),
            dict(vus=10, p99_ms=40, passed=True), dict(vus=20, p99_ms=None, passed=False)]
        publish(self.path, self.submission, result)
        chart = result_chart(result)
        self.assertEqual(chart['repetition'], 2)
        self.assertEqual([p['vus'] for p in chart['points']], [2, 10])
        self.assertIn('load-title', self.client.get('/team/1').text)
        self.assertIsNone(result_chart(recorded_result()))
        self.assertIsNone(result_chart({**result, 'status': 'failed'}))
        self.assertIsNone(load_chart([dict(vus=2, p99_ms=float('nan'))], 0))
