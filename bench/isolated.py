"""Host-only executor, with no fallback from gVisor or bounded disk storage."""
import json
import os
import re
from pathlib import Path
from bench import runner

HELPER = '/usr/local/lib/vibe-zone/bounded-volume.sh'


def volume(action, name, mib=1024, uid=10001):
    if not re.fullmatch(r'vh-vzr-[a-z0-9-]{1,40}', name):
        raise ValueError('Invalid managed volume name')
    runner.command([HELPER, action, name, str(mib)], timeout=60)
    if action == 'create':
        path = Path('/srv/vibe-zone-runner/volumes') / name
        lost = path / 'lost+found'
        if lost.is_dir():
            lost.rmdir()
        os.chown(path, uid, uid)
        return path


def isolate_network(name):
    info = json.loads(runner.docker('network', 'inspect', name).stdout)[0]
    bridge = 'br-' + info['Id'][:12]
    path = Path('/proc/sys/net/ipv6/conf') / bridge / 'disable_ipv6'
    if path.exists():
        path.write_text('1')
    if not info['Internal'] or info['Options'].get('com.docker.network.bridge.gateway_mode_ipv4') != 'isolated':
        raise runner.InfrastructureError('Network is not isolated')


class IsolatedRun(runner.Run):
    def __init__(self, image, config, output, slot, image_bytes=None):
        super().__init__(image, config, output, 'never')
        self.prefix = 'vzr-' + self.id.lower()
        self.network = self.prefix
        self.sut, self.probe, self.k6 = [self.prefix + '-' + k for k in ('sut', 'probe', 'k6')]
        self.volume = 'vh-' + self.prefix
        self.lock_name = f'vzr-slot-{slot}'
        self.image_bytes = image_bytes

    def resolve(self):
        value = super().resolve()
        if self.image_bytes is not None:
            value['image_bytes'] = self.image_bytes
        return value

    def check_host(self, host):
        if (host['Architecture'] not in {'amd64', 'x86_64'} or host['NCPU'] < self.config['min_host_cpus']
                or host['MemTotal'] < self.config['min_host_memory_bytes'] or 'runsc' not in host.get('Runtimes', {})):
            raise runner.InfrastructureError('Host does not satisfy the assigned isolated profile')
        if self.config.get('runtime') != 'runsc' or self.config.get('storage') != 'bounded-ext4':
            raise runner.InfrastructureError('No unisolated runner fallback')

    def create_data(self):
        volume('create', self.volume)

    def sut_options(self):
        return ['--runtime', 'runsc', '-e', 'TMPDIR=/data', '-e', 'HOME=/data'] + (['--cpuset-cpus', '0'] if self.config['runner_role'] == 'test' else [])

    def sut_filesystems(self):
        # runsc otherwise inserts a writable /tmp even with a read-only root.
        return ['--ipc', 'none', '--tmpfs', '/tmp:ro,noexec,nosuid,nodev,size=1m']

    def generator_options(self):
        return ['--cpuset-cpus', '1'] if self.config['runner_role'] == 'test' else []

    def create_network(self):
        super().create_network()
        isolate_network(self.network)

    def cleanup(self):
        for name in (self.k6, self.sut, self.probe):
            try:
                state = runner.docker('rm', '-fv', name, check=False, timeout=15)
                if state.returncode and 'no such' not in state.stderr.lower():
                    raise RuntimeError(state.stderr[-500:])
            except Exception as error:
                self.result.setdefault('cleanup_errors', []).append(str(error)[-500:])
        for action in (lambda: volume('destroy', self.volume),
                       lambda: runner.docker('network', 'rm', self.network, check=False)):
            try:
                value = action()
                if value and value.returncode and 'not found' not in value.stderr.lower():
                    raise RuntimeError(value.stderr[-500:])
            except Exception as error:
                self.result.setdefault('cleanup_errors', []).append(str(error)[-500:])
        if self.lock_acquired and not self.result.get('cleanup_errors'):
            runner.docker('rm', '-fv', self.lock_name)
