"""One bounded job. Invoked with a sanitized environment by the pull agent."""
import fcntl
import json
import time
import os
from pathlib import Path
import signal
import sys

from bench import artifacts, runner
from bench.isolated import IsolatedRun
from leaderboard import remote


def execute(job, output, slot):
    output.mkdir(parents=True, exist_ok=True)
    if runner.fingerprint(job['config']) != job['profile_hash']:
        raise runner.InfrastructureError('Runner and leaderboard code/profile fingerprints differ')
    spec = job['artifact']
    reference = spec['reference'] + ('@' + spec['revision'] if spec['kind'] == 'repo' else '')
    if remote.artifact(reference) != spec:
        raise ValueError('Invalid artifact specification')
    artifact = artifacts.Artifact(spec)
    result = {'status': 'failed', 'max_vus': 0, 'profile': job['config'], 'profile_hash': job['profile_hash']}
    try:
        (output / 'phase').write_text('building' if spec['kind'] == 'repo' else 'preparing')
        with open('/run/lock/vibe-zone-import.lock', 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            artifact.prepare()
        if job.get('barrier'):
            (output / 'phase').write_text('ready')
            deadline = time.monotonic() + 1800
            while not Path(job['barrier']).exists():
                if time.monotonic() > deadline:
                    raise runner.InfrastructureError('Preparation barrier timed out')
                time.sleep(.2)
        (output / 'phase').write_text('measuring')
        measurements = []
        for index in range(3):
            run = IsolatedRun(artifact.image, job['config'], output, slot, artifact.size)
            if index == 2:
                run.extra_flags = [f for f in job['flags'] if f in {'hidden', 'chaos', 'bonus'}]
            value = run.run()
            measurements.append(value)
            result = value
            if value['status'] != 'passed':
                break
        else:
            result = runner.aggregate(measurements)
            if 'pilot' in job['flags']:
                smoke = {**job['config'], 'name': 'smoke', 'levels': [2, 5, 10], 'seed_posts': 50,
                         'seed_likes': 100, 'ramp_seconds': 2, 'hold_seconds': 5}
                pilot = IsolatedRun(artifact.image, smoke, output, slot, artifact.size)
                pilot.extra_flags = ['hidden']
                check = pilot.run()
                if check.get('error_kind') == 'infrastructure' or check.get('cleanup_errors'):
                    raise runner.InfrastructureError('Pilot infrastructure failed')
                green = check['status'] == 'passed' and check.get('extras', {}).get('hidden', {}).get('status') == 'passed'
                result.setdefault('extras', {})['pilot_smoke'] = {'status': 'passed' if green else 'failed'}
        result['source'] = reference
        if spec['kind'] == 'repo':
            result['source_commit'] = spec['revision']
    except BaseException as error:
        result.update(status='failed', error=str(error)[-2000:],
                      error_kind='infrastructure' if isinstance(error, (runner.InfrastructureError, KeyboardInterrupt)) else 'submission')
    finally:
        (output / 'phase').write_text('cleanup')
        errors = artifact.cleanup()
        if errors:
            result.update(status='failed', error_kind='infrastructure', cleanup_errors=errors)
        result.update(profile=job['config'], profile_hash=job['profile_hash'])
        document = {'result': result, 'log': artifact.log}
        (output / 'complete.tmp').write_text(json.dumps(document, allow_nan=False))
        (output / 'complete.tmp').replace(output / 'complete.json')
    return result


if __name__ == '__main__':
    def stop(_sig, _frame):
        raise KeyboardInterrupt('Job terminated')
    signal.signal(signal.SIGTERM, stop)
    task = json.loads(Path(sys.argv[1]).read_text())
    os.environ['BENCH_SEED'] = task['seed']
    os.environ['VZ_RUNNER_ROLE'] = 'competition' if task['config']['runner_role'] == 'official' else 'test'
    try:
        execute(task, Path(sys.argv[1]).parent, int(sys.argv[2]))
    except Exception:
        # Protocol/version failures cannot be reported as a participant failure.
        raise
