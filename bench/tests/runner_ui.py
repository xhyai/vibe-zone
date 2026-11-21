"""Organizer controls in both languages and viewports, using only a temporary DB."""
import json
import tempfile
import threading
import time
from pathlib import Path
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server
from bench.tests.homepage_ui import QuietHandler
from leaderboard import remote, store
from leaderboard.app import create_app


def main():
    output=Path('artifacts/runner/admin-ui');output.mkdir(parents=True,exist_ok=True)
    errors=[]
    with tempfile.TemporaryDirectory() as directory:
        dbpath=str(Path(directory)/'board.db')
        app=create_app(dict(TESTING=True,DATABASE=dbpath,SECRET_KEY='ui-tests',ADMIN_PASSWORD='ui-tests',
                            REMOTE_RUNNER=True,RUNNER_TOKEN='ui-fixture-'*8))
        with store.connect(dbpath) as db:
            for name,cpus,ram,role in [('test-host',2,3800*1024**2,'test'),('official-host',6,8*1024**3,'official')]:
                remote.register(db,dict(address=name,metadata=dict(cpus=cpus,memory_bytes=ram,role=role,
                    kernel='test',runtime='runsc test',engine='test',version='test',architecture='amd64')),time.time())
        server=make_server('127.0.0.1',0,app,threaded=True,request_handler=QuietHandler)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        try:
            with sync_playwright() as p:
                browser=p.chromium.launch()
                for lang in ['pl','en']:
                    for width in [390,1440]:
                        context=browser.new_context(viewport=dict(width=width,height=1000),http_credentials=dict(username='admin',password='ui-tests'))
                        page=context.new_page();page.on('pageerror',lambda error:errors.append(str(error)))
                        with store.connect(dbpath) as db:
                            db.execute('UPDATE runners SET seen_at=?',(time.time(),))
                        page.goto(f'http://127.0.0.1:{server.server_port}/admin?lang={lang}')
                        form=page.locator('form[action="/admin/runner"]')
                        form.locator('[name=address]').select_option('test-host')
                        form.locator('[name=profile]').select_option('parallel')
                        form.locator('[name=slots]').fill('2')
                        form.locator('[name=levels]').fill('[10,50]')
                        form.locator('[name=test_banner]').check()
                        form.locator('button').click()
                        expect(page.locator('h1')).to_have_text('Panel organizatora' if lang=='pl' else 'Organizer panel')
                        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                        with store.connect(dbpath) as db:
                            control=db.execute('SELECT * FROM runner_control').fetchone()
                            assert control['active']=='test-host' and control['profile']=='parallel'
                            assert json.loads(control['overrides'])==dict(slots=2,levels=[10,50])
                        page.screenshot(path=str(output/f'admin-{lang}-{width}.png'),full_page=True)
                        form=page.locator('form[action="/admin/runner"]')
                        form.locator('[name=address]').select_option('official-host')
                        form.locator('[name=profile]').select_option('full')
                        form.locator('[name=test_banner]').uncheck()
                        form.locator('button').click()
                        page.goto(f'http://127.0.0.1:{server.server_port}/?lang={lang}')
                        expect(page.locator('.test-banner')).to_be_hidden()
                        with store.connect(dbpath) as db:db.execute('UPDATE runners SET seen_at=0')
                        page.reload()
                        expect(page.locator('.test-banner')).to_be_visible()
                        context.close()
                browser.close()
        finally:server.shutdown()
    assert not errors,errors
    print('PASS: runner selection, profiles, load controls and banner guard; PL/EN, mobile/desktop')


if __name__=='__main__':main()
