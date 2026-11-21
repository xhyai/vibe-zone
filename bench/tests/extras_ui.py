"""Bilingual browser checks for organizer controls and audience interactions."""
import json
import tempfile
import threading
from pathlib import Path
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server
from bench.tests.homepage_ui import QuietHandler
from bench.tests.test_homepage import publish, recorded_result
from leaderboard import store, extras
from leaderboard.app import create_app


def main():
    output = Path('artifacts/extras/interactions')
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as directory:
        path = str(Path(directory) / 'board.db')
        spec = Path(directory) / 'bonus.json'
        spec.write_text(json.dumps({'pl': 'Specyfikacja testowa <b>PL</b>', 'en': 'Fixture specification <b>EN</b>'}))
        app = create_app(dict(TESTING=True, DATABASE=path, SECRET_KEY='browser-fixture-only',
                              ADMIN_PASSWORD='browser-fixture-only', BONUS_SPEC_FILE=str(spec), BENCH_SMOKE=True))
        sid, _ = store.submit(path, 'Browser team', 'team/api:v1', invitation=store.issue_invitation(path), metadata={'language': 'Lisp'})
        publish(path, sid, {**recorded_result(), 'extras': {'hidden': {'status': 'passed', 'passed': 5, 'total': 5}}})
        server = make_server('127.0.0.1', 0, app, threaded=True, request_handler=QuietHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f'http://127.0.0.1:{server.server_port}'
        errors = []
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch()
                for lang in ['pl', 'en']:
                    for size, width in [('desktop', 1440), ('mobile', 390)]:
                        context = browser.new_context(viewport={'width': width, 'height': 1000}, http_credentials={'username': 'admin', 'password': 'browser-fixture-only'})
                        page = context.new_page()
                        page.on('pageerror', lambda error: errors.append(str(error)))
                        page.goto(url + '/admin?lang=' + lang)
                        for name in extras.DEFAULTS:
                            page.locator(f'input[name="{name}"]').check()
                        page.locator('form[action="/admin/extras"] button').click()
                        page.locator('form[action$="/language"] input[name="verified"]').check()
                        page.locator('form[action$="/language"] button').click()
                        if lang == 'pl' and size == 'desktop':
                            page.locator('form[action="/admin/bonus/announce"] button').click()
                        page.locator('button[name="state"][value="open"]').click()
                        page.goto(url + '/submit?lang=' + lang)
                        expect(page.locator('#prompt-log')).to_be_visible()
                        expect(page.locator('#ai-tool')).to_be_visible()
                        for route in ['/leaderboard', '/screen', '/team/1']:
                            page.goto(url + route + '?lang=' + lang)
                            expect(page.locator('.badges').first).to_contain_text('Najmniejszy obraz' if lang == 'pl' else 'Smallest image')
                            expect(page.locator('.checks').first).to_contain_text('5/5')
                            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                            if route != '/team/1':
                                expect(page.locator('#bonus-spec')).to_contain_text('<b>' + lang.upper() + '</b>')
                                expect(page.locator('#bonus-spec b')).to_have_count(0)
                                expect(page.locator('#pilot-ranking')).to_be_visible()
                            page.screenshot(path=str(output / f'{route.strip("/").replace("/","-")}-{lang}-{size}.png'), full_page=True)
                        page.goto(url + '/vote?lang=' + lang)
                        page.locator('input[name="participant_id"]').check()
                        page.locator('form button').click()
                        expect(page.locator('[role="status"]')).to_be_visible()
                        expect(page.locator('input[name="participant_id"]')).to_have_count(0)
                        page.goto(url + '/admin?lang=' + lang)
                        page.locator('button[name="state"][value="closed"]').click()
                        page.goto(url + '/vote?lang=' + lang)
                        expect(page.locator('h2')).to_have_text('Wyniki głosowania' if lang == 'pl' else 'Voting results')
                        page.screenshot(path=str(output / f'vote-{lang}-{size}.png'), full_page=True)
                        page.goto(url + '/start?lang=' + lang + '#ai-pilot')
                        expect(page.locator('#ai-pilot')).to_be_visible()
                        context.close()
                browser.close()
        finally:
            server.shutdown()
    assert not errors, errors
    (output / 'checks.json').write_text(json.dumps({'passed': True, 'languages': ['pl', 'en'], 'viewports': [1440, 390], 'browser_errors': errors}, indent=2)+'\n')
    print('PASS: extras admin, language verification, bonus escaping, badges, voting and AI Pilot in PL/EN, desktop/mobile')


if __name__ == '__main__':
    main()
