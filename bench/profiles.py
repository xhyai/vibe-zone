"""Versioned profiles shared by the queue and the isolated executor."""
import hashlib
import json
from bench.runner import profile

GIB = 1024 ** 3


def capacity(host, name):
    if name not in {'full', 'parallel'}:
        raise ValueError('Unknown profile')
    cpus, memory = host['cpus'], host['memory_bytes']
    if cpus < 2 or memory < 3 * GIB:
        raise ValueError('Runner requires at least 2 CPUs and 3 GiB')
    if host['role'] == 'official' and (cpus < 6 or memory < 8 * GIB):
        raise ValueError('Official runner requires at least 6 CPUs and 8 GiB')
    return 1 if name == 'full' else max(1, min(8, int(cpus // 2 if cpus >= 6 else 2), int((memory - GIB / 2) // (1.4 * GIB))))


def make(host, name='full', overrides=None):
    slots = capacity(host, name)
    official = host['role'] == 'official'
    result = profile(False)
    result.update(name=name, version=3, runtime='runsc', runner_role=host['role'],
                  host_profile=host['fingerprint'], slots=slots, storage='bounded-ext4', writable_paths=['/data'],
                  generator_gomaxprocs=1, min_host_cpus=2, min_host_memory_bytes=3 * GIB,
                  seed_posts=1000, seed_likes=3000, ramp_seconds=5, hold_seconds=20,
                  levels=[10, 25, 50, 100, 200], max_run_seconds=1800,
                  generator_cpus=.9, generator_memory='256m', probe_cpus=.2, probe_memory='96m')
    if name == 'parallel':
        result.update(cpu=1 if official else .5, memory_bytes=GIB,
                      generator_cpus=1 if official else .45, levels=[5, 10, 25, 50, 100])
    if official and name == 'full':
        result.update({k: v for k, v in profile(False).items() if k in
                       ('levels', 'seed_posts', 'seed_likes', 'ramp_seconds', 'hold_seconds',
                        'max_run_seconds', 'generator_cpus', 'generator_memory')})
        result.update(generator_gomaxprocs=4, min_host_cpus=6, min_host_memory_bytes=8 * GIB)
    allowed = {'slots', 'cpu', 'memory_bytes', 'levels', 'ramp_seconds', 'hold_seconds'}
    overrides = overrides or {}
    if set(overrides) - allowed:
        raise ValueError('Unknown profile setting')
    result.update(overrides)
    if type(result['slots']) is not int or not 1 <= result['slots'] <= slots:
        raise ValueError('Slot count exceeds host capacity')
    if type(result['cpu']) not in (int, float) or not .25 <= result['cpu'] <= 1:
        raise ValueError('CPU must be between 0.25 and 1')
    if type(result['memory_bytes']) is not int or not 256 * 1024**2 <= result['memory_bytes'] <= 2 * GIB:
        raise ValueError('Memory must be between 256 MiB and 2 GiB')
    levels = result['levels']
    maximum = 30000 if official else 1000
    if (not isinstance(levels, list) or not 1 <= len(levels) <= 10 or
            any(type(v) is not int or not 1 <= v <= maximum for v in levels) or sorted(set(levels)) != levels):
        raise ValueError('Use 1-10 increasing user levels within the host limit')
    for key in ('ramp_seconds', 'hold_seconds'):
        if type(result[key]) is not int or not 2 <= result[key] <= 120:
            raise ValueError('Durations must be between 2 and 120 seconds')
    generator_memory = int(result['generator_memory'][:-1]) * (GIB if result['generator_memory'][-1] == 'g' else 1024**2)
    if result['slots'] * (result['memory_bytes'] + generator_memory + 96 * 1024**2) > host['memory_bytes'] - 512 * 1024**2:
        raise ValueError('Profile leaves insufficient host memory')
    if result['slots'] * (result['cpu'] + result['generator_cpus']) > host['cpus']:
        raise ValueError('Profile exceeds host CPU capacity')
    return result


def host_fingerprint(metadata):
    return hashlib.sha256(json.dumps(metadata, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
