"""Read-only public-page matrix; without --url, use an isolated synthetic fixture."""
import argparse
import json
import tempfile
import threading
from pathlib import Path

from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server, WSGIRequestHandler

from bench.tests.test_homepage import recorded_result, publish
from leaderboard import store
from leaderboard.app import create_app


class QuietHandler(WSGIRequestHandler):
    def log(self, *args, **kwargs):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url')
    parser.add_argument('--output', default='artifacts/homepage/ui')
    args = parser.parse_args()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    errors, checks, blocked_analytics = [], [], []

    def console_message(message):
        if message.type != 'error':
            return
        text = message.text
        # Cloudflare injects this optional beacon at the edge. The site's unchanged
        # self-only CSP correctly blocks it; record it separately, never allow it.
        if (text.startswith("Loading the script 'https://static.cloudflareinsights.com/beacon.min.js/")
                and 'violates the following Content Security Policy directive' in text
                and "script-src 'self'" in text and 'The action has been blocked.' in text):
            blocked_analytics.append(text)
        else:
            errors.append(text)
    with tempfile.TemporaryDirectory() as directory:
        server = None
        if not args.url:
            path = str(Path(directory) / 'board.db')
            app = create_app(dict(TESTING=True, DATABASE=path, SECRET_KEY='browser-fixture-only',
                                  ADMIN_PASSWORD='browser-fixture-only', BENCH_SMOKE=True))
            for i in range(1, 8):
                sid, _ = store.submit(path, f'QA Team {i}', 'team/api:v1', invitation=store.issue_invitation(path))
                if i == 1:
                    publish(path, sid, recorded_result(5))
                    publish(path, sid, recorded_result(10))
                    batch = publish(path, sid, recorded_result(10))
                else:
                    with store.connect(path) as db:
                        db.execute('INSERT INTO results VALUES (?,?,?)', (batch, sid, json.dumps(recorded_result())))
            server = make_server('127.0.0.1', 0, app, request_handler=QuietHandler, threaded=True)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            args.url = f'http://127.0.0.1:{server.server_port}'
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch()
                for lang in ['pl', 'en']:
                    for size, width, height in [('desktop', 1440, 1000), ('mobile', 390, 844)]:
                        context = browser.new_context(locale='pl-PL' if lang == 'pl' else 'en-US', viewport={'width': width, 'height': height})
                        page = context.new_page()
                        page.on('pageerror', lambda error: errors.append(str(error)))
                        page.on('console', console_message)
                        data = page.request.get(args.url + '/api/leaderboard').json()
                        team_id = data['ranking'][0]['participant_id']
                        for name, route in [('home','/'), ('leaderboard','/leaderboard'), ('submit','/submit'), ('start','/start'), ('screen','/screen'), ('team',f'/team/{team_id}')]:
                            response = page.goto(args.url + route + '?lang=' + lang)
                            assert response.status == 200, (route, response.status)
                            expect(page.locator('html')).to_have_attribute('lang', lang)
                            expect(page.locator('h1')).to_have_count(1)
                            expect(page.locator('.test-banner')).to_be_visible()
                            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (route, lang, size, 'overflow')
                            if name == 'home':
                                expect(page.locator('.flow-diagram')).to_be_visible()
                                expect(page.locator('.load-chart')).to_be_visible()
                                expect(page.locator('.api-example-grid article')).to_have_count(3)
                                page.locator('.glossary summary').click()
                                expect(page.locator('.glossary dt')).to_have_count(9)
                                assert page.locator('#last-update').inner_text() not in ['jeszcze brak', 'not yet']
                                assert page.locator('#ranking-rows .place').count() <= 5
                                page.locator('.faq summary').first.click()
                                expect(page.locator('.faq details').first).to_have_attribute('open', '')
                                page.locator('.faq summary').first.click()
                            if name == 'start':
                                expect(page.locator('.guide-steps > li')).to_have_count(9)
                            if name == 'submit':
                                expect(page.locator('form[action="/submissions"]')).to_have_count(1)
                                assert page.locator('#team-token').get_attribute('type') == 'password'
                            if name == 'screen':
                                expect(page.locator('form')).to_have_count(0)
                            if name == 'leaderboard':
                                with page.expect_response('**/api/leaderboard?lang=' + lang, timeout=16000):
                                    pass
                                expect(page.locator('#refresh-status')).to_have_text('Odświeżanie co 10 s' if lang == 'pl' else 'Refreshes every 10 seconds')
                                expect(page.locator('#ranking-rows a').first).to_have_attribute('href', f'/team/{team_id}')
                                assert len(page.locator('#ranking-rows tr').first.locator('td').all()) == 9
                            page.screenshot(path=str(output / f'{name}-{lang}-{size}.png'), full_page=True)
                            checks.append(f'{name}/{lang}/{size}')
                        # Keyboard skip and persistent language switch, plus legacy bookmarks.
                        page.goto(args.url + '/?lang=' + lang)
                        page.keyboard.press('Tab')
                        expect(page.locator('.skip-link')).to_be_focused()
                        page.keyboard.press('Enter')
                        assert page.evaluate("document.activeElement.id === 'main'")
                        page.goto(args.url + '/?lang=' + lang + '#submit-title')
                        page.wait_for_url('**/submit?lang=' + lang + '#submit-title')
                        other = 'en' if lang == 'pl' else 'pl'
                        page.get_by_role('link', name=other.upper(), exact=True).click()
                        expect(page.locator('html')).to_have_attribute('lang', other)
                        page.goto(args.url + '/start')
                        expect(page.locator('html')).to_have_attribute('lang', other)
                        context.close()
                # The explanations and timestamps also work without JavaScript.
                context = browser.new_context(java_script_enabled=False, viewport={'width':390, 'height':844})
                page = context.new_page()
                for lang in ['pl', 'en']:
                    page.goto(args.url + '/?lang=' + lang)
                    expect(page.locator('.flow-diagram')).to_be_visible()
                    expect(page.locator('.load-chart')).to_be_visible()
                    page.locator('.glossary summary').click()
                    expect(page.locator('.glossary dl')).to_be_visible()
                    assert page.locator('#last-update').inner_text() not in ['jeszcze brak', 'not yet']
                    page.screenshot(path=str(output / f'home-{lang}-nojs.png'), full_page=True)
                context.close()
                # The event screen must fit at its target viewport without a horizontal table scroll.
                page = browser.new_page(viewport={'width':1920, 'height':1080})
                page.goto(args.url + '/screen?lang=pl')
                assert page.locator('.table-scroll').evaluate('(node) => node.scrollWidth <= node.clientWidth')
                page.screenshot(path=str(output / 'screen-pl-kiosk.png'), full_page=True)
                browser.close()
        finally:
            if server:
                server.shutdown()
    assert not errors, errors
    (output / 'checks.json').write_text(json.dumps({'passed':True,'pages':checks,'browser_errors':errors,
        'blocked_cloudflare_analytics':len(blocked_analytics)},indent=2)+'\n')
    print(f'PASS: {len(checks)} page/language/viewport combinations; live refresh, keyboard, legacy links and 1920px kiosk')
    if blocked_analytics:
        print(f'CSP correctly blocked {len(blocked_analytics)} injected Cloudflare analytics scripts; policy unchanged.')


if __name__ == '__main__':
    main()
