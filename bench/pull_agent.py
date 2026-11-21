"""Outbound-only host agent. The bearer token never enters job environments."""
import concurrent.futures
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

from bench import profiles, runner
from bench.isolated import volume

LOG = logging.getLogger('vibezone.runner')
STOP = threading.Event()


class Client:
    def __init__(self):
        self.url = os.environ['LEADERBOARD_URL'].rstrip('/')
        self.token = os.environ['RUNNER_TOKEN']
        self.address = os.environ['RUNNER_ADDRESS']
        if not self.url.startswith('https://') or len(self.token) < 32:
            raise ValueError('Verified HTTPS and a random token are required')
        self.opener = urllib.request.build_opener(NoRedirect)
        self.opener.addheaders = [('User-Agent', 'VibeZoneRunner/1.0 (+https://github.com/bsisduck/vibe-zone)'),
                                  ('Accept', 'application/json')]

    def call(self, operation, data):
        request = urllib.request.Request(self.url + '/api/runner/' + operation,
            data=json.dumps({'address': self.address, **data}, allow_nan=False).encode(),
            headers={'Content-Type':'application/json', 'X-Runner-Token':self.token}, method='POST')
        with self.opener.open(request, timeout=15) as response:
            content = response.read(1024 * 1024 + 1)
            if len(content) > 1024 * 1024:
                raise ValueError('Server response too large')
            return json.loads(content)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('Runner API redirects are not allowed')


def metadata():
    info = json.loads(runner.docker('info', '--format', '{{json .}}').stdout)
    if 'runsc' not in info.get('Runtimes', {}):
        raise runner.InfrastructureError('runsc is not installed')
    return {'cpus': info['NCPU'], 'memory_bytes': info['MemTotal'], 'architecture': info['Architecture'],
            'role': os.environ.get('RUNNER_ROLE', 'test'), 'kernel': info['KernelVersion'],
            'runtime': runner.command(['runsc', '--version']).stdout.splitlines()[0], 'engine': info['ServerVersion'],
            'version': profiles.host_fingerprint({'code': runner.fingerprint({'name':'agent-version'}),
                'tools': json.loads(runner.docker('image', 'inspect', os.environ.get('VZ_TOOLS_IMAGE', 'vibezone-runner-tools:local')).stdout)[0]['Id']})}


def recover():
    """The exclusive host lock is held; remove only this agent's reserved prefixes."""
    names = runner.docker('ps', '-a', '--format', '{{.Names}}').stdout.splitlines()
    for name in names:
        if re.fullmatch(r'vzr-[a-z0-9-]+', name):
            runner.docker('rm', '-fv', name)
    networks = runner.docker('network', 'ls', '--format', '{{.Name}}').stdout.splitlines()
    for name in networks:
        if re.fullmatch(r'vzr-[a-z0-9-]+', name):
            runner.docker('network', 'rm', name)
    for path in Path('/srv/vibe-zone-runner/.volumes').glob('vh-vzr-*.ext4'):
        volume('destroy', path.stem)
    tags = runner.docker('image', 'ls', '--format', '{{.Repository}}:{{.Tag}}').stdout.splitlines()
    for tag in tags:
        if re.fullmatch(r'vibezone-job:[a-f0-9]{32}', tag):
            runner.docker('image', 'rm', tag)


def stop_process(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=50)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
            raise runner.InfrastructureError('Job cleanup did not finish; stop agent for recovery')


def work(client, job, slot, state):
    directory = state / f"job-{job['id']}-{job['lease'][:12]}"
    directory.mkdir(mode=0o700)
    task_file = directory / 'job.json'
    task_file.write_text(json.dumps(job))
    environment = {k: v for k, v in os.environ.items() if k in
                   {'PATH', 'LANG', 'VZ_PRIVATE_DIR', 'VZ_TOOLS_IMAGE', 'BUILD_ALLOWED_HOSTS'}}
    environment.update(PYTHONUNBUFFERED='1', PYTHONDONTWRITEBYTECODE='1')
    identity = {'id':job['id'], 'lease':job['lease']}
    renewed = time.monotonic()
    with (directory / 'worker.log').open('w') as log:
        process = subprocess.Popen([sys.executable, '-m', 'bench.remote_job', str(task_file), str(slot)],
                                   stdout=log, stderr=log, env=environment)
        try:
            while process.poll() is None:
                if STOP.wait(10) or time.time() > job['deadline']:
                    stop_process(process)
                    return
                phase = (directory / 'phase').read_text() if (directory / 'phase').exists() else 'preparing'
                try:
                    client.call('heartbeat', {**identity, 'phase':'preparing' if phase == 'ready' else phase})
                    renewed = time.monotonic()
                except urllib.error.HTTPError as error:
                    if error.code in {400,401,409}:
                        stop_process(process)
                        return
                except (OSError, ValueError):
                    pass
                if time.monotonic() - renewed > 45:
                    stop_process(process)
                    return
            document_path = directory / 'complete.json'
            if not document_path.is_file() or document_path.stat().st_size > 900000:
                raise runner.InfrastructureError('Job did not produce a bounded completion document')
            document = json.loads(document_path.read_text())
            if not document.get('log'):
                with (directory / 'worker.log').open('rb') as stream:
                    stream.seek(max(0, stream.seek(0, 2) - 65536))
                    document['log'] = stream.read(65536).decode('utf-8', errors='replace')
            document['log'] = document['log'].encode()[-65536:].decode('utf-8', errors='ignore')
            for attempt in range(4):
                try:
                    client.call('complete', {**identity, **document})
                    LOG.info('Completed job %s', job['id'])
                    if document['result'].get('cleanup_errors'):
                        raise runner.InfrastructureError('Cleanup failed; stop for operator inspection')
                    return
                except urllib.error.HTTPError as error:
                    if error.code < 500:
                        raise
                except (OSError, ValueError):
                    pass
                STOP.wait(min(2 ** attempt, 8))
            raise runner.InfrastructureError('Result delivery failed; completion is saved locally')
        finally:
            stop_process(process)


def prune_state(state, keep=64):
    completed = sorted((p for p in state.iterdir() if re.fullmatch(r'job-[0-9]+-[a-f0-9]{12}', p.name)
                        and not p.is_symlink() and (p / 'complete.json').is_file()), key=lambda p: p.stat().st_mtime)
    for directory in completed[:-keep]:
        shutil.rmtree(directory)
    for path in state.glob('wave-*'):
        if re.fullmatch(r'wave-[a-f0-9]{12}', path.name):
            path.unlink()


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
    state = Path(os.environ.get('RUNNER_STATE', '/var/lib/vibe-zone-agent'))
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    signal.signal(signal.SIGINT, lambda *_: STOP.set())
    client = Client()
    with (state / 'agent.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        recover()
        info = metadata()
        # Keep the original CLI scheduler from starting on this managed host.
        sentinel = runner.docker('inspect', 'vibezone-benchmark-lock', check=False)
        if sentinel.returncode == 0:
            existing = json.loads(sentinel.stdout)[0]
            if existing.get('Config', {}).get('Labels', {}).get('vibezone.agent') != 'true':
                raise runner.InfrastructureError('Another benchmark owns the daemon lock')
            runner.docker('rm', '-fv', 'vibezone-benchmark-lock')
        runner.docker('create', '--name', 'vibezone-benchmark-lock', '--label', 'vibezone.agent=true',
                      '--network', 'none', '--read-only', '--entrypoint', '/bin/true', runner.PROBE_IMAGE)
        maximum = profiles.capacity({**info, 'fingerprint':profiles.host_fingerprint(info)}, 'parallel')
        tasks = {}
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=maximum) as pool:
                registered = False
                last_register = 0
                while not STOP.is_set():
                    for slot, future in list(tasks.items()):
                        if future.done():
                            try:
                                future.result()
                            except BaseException:
                                STOP.set()
                                raise
                            del tasks[slot]
                    try:
                        if not registered or time.monotonic() - last_register > 30:
                            client.call('register', {'metadata': info})
                            registered, last_register = True, time.monotonic()
                        if not tasks:
                            prune_state(state)
                            wave = []
                            for slot in range(maximum):
                                job = client.call('claim', {})['job']
                                if not job:
                                    break
                                wave.append(job)
                            if wave:
                                barrier = state / ('wave-' + wave[0]['lease'][:12])
                                for slot, job in enumerate(wave):
                                    job['barrier'] = str(barrier)
                                    LOG.info('Claimed job %s in slot %s', job['id'], slot)
                                    tasks[slot] = pool.submit(work, client, job, slot, state)
                        if tasks and not barrier.exists():
                            ready = True
                            for slot, job in enumerate(wave):
                                phase = state / f"job-{job['id']}-{job['lease'][:12]}" / 'phase'
                                if not tasks.get(slot) or tasks[slot].done():
                                    continue
                                if not phase.exists() or phase.read_text() not in {'ready', 'cleanup'}:
                                    ready = False
                            if ready:
                                barrier.touch(mode=0o600)
                    except (OSError, ValueError) as error:
                        LOG.warning('Runner connection unavailable (%s)', type(error).__name__)
                    STOP.wait(3)
                for future in tasks.values():
                    future.result()
        finally:
            STOP.set()
            runner.docker('rm', '-fv', 'vibezone-benchmark-lock')


if __name__ == '__main__':
    main()
