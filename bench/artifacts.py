"""Disposable, bounded imports and builds; only verified archives enter Docker."""
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tarfile
import uuid

from bench import runner
from bench.isolated import isolate_network, volume

BUILDKIT = 'moby/buildkit@sha256:5987568f92dafbfb679879de59c1cd8d26597f5f6635c314e166164e2615870c'
TOOLS = os.environ.get('VZ_TOOLS_IMAGE', 'vibezone-runner-tools:local')
MAX_ARCHIVE = 256 * 1024**2
MAX_IMAGE = 100000000


def verified_archive(source, target, tag):
    """Rebuild a minimal Docker archive without extracting any participant path."""
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or not 0 < st.st_size <= MAX_ARCHIVE:
            raise ValueError('Image archive exceeds 256 MiB')
        with os.fdopen(fd, 'rb', closefd=False) as stream, tarfile.open(fileobj=stream, mode='r:') as archive:
            members = {}
            for member in archive:
                if len(members) >= 2000 or member.name in members:
                    raise ValueError('Too many or duplicate archive members')
                members[member.name] = member
            def read(name, maximum):
                entry = members.get(name)
                if not entry or not entry.isfile() or entry.size > maximum or entry.size < 0:
                    raise ValueError('Invalid image archive member')
                return archive.extractfile(entry).read(maximum + 1)
            manifests = json.loads(read('manifest.json', 65536))
            if not isinstance(manifests, list) or len(manifests) != 1:
                raise ValueError('Exactly one image required')
            manifest = manifests[0]
            config = read(manifest['Config'], 1024**2)
            document = json.loads(config)
            if document.get('architecture') != 'amd64' or document.get('os') != 'linux':
                raise ValueError('Image must target linux/amd64')
            if set(document.get('config', {}).get('Volumes') or {}) - {'/data'}:
                raise ValueError('Image may only declare /data')
            layers = manifest['Layers']
            if not isinstance(layers, list) or not 1 <= len(layers) <= 128 or len(set(layers)) != len(layers):
                raise ValueError('Invalid layers')
            diffs = document.get('rootfs', {}).get('diff_ids', [])
            if len(diffs) != len(layers):
                raise ValueError('Layer count mismatch')
            image_bytes = 0
            target_fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(target_fd, 'wb') as target_stream, tarfile.open(fileobj=target_stream, mode='w') as output:
                def add(name, body):
                    member = tarfile.TarInfo(name)
                    member.size = len(body)
                    output.addfile(member, io.BytesIO(body))
                canonical_layers = []
                for index, (name, digest) in enumerate(zip(layers, diffs)):
                    body = read(name, MAX_IMAGE - image_bytes)
                    if body.startswith(b'\x1f\x8b'):
                        with gzip.GzipFile(fileobj=io.BytesIO(body)) as uncompressed:
                            body = uncompressed.read(MAX_IMAGE - image_bytes + 1)
                    image_bytes += len(body)
                    if image_bytes >= MAX_IMAGE or 'sha256:' + hashlib.sha256(body).hexdigest() != digest:
                        raise ValueError('Image exceeds 100 MB or layer hash mismatch')
                    canonical = f'{index}/layer.tar'
                    add(canonical, body)
                    canonical_layers.append(canonical)
                config_name = hashlib.sha256(config).hexdigest() + '.json'
                add(config_name, config)
                add('manifest.json', json.dumps([dict(Config=config_name, RepoTags=[tag], Layers=canonical_layers)]).encode())
            return image_bytes
    finally:
        os.close(fd)


class Artifact:
    def __init__(self, spec):
        self.spec = spec
        self.name = 'vzr-import-' + uuid.uuid4().hex[:16]
        self.disk = 'vh-' + self.name
        self.tag = 'vibezone-job:' + uuid.uuid4().hex
        self.log = ''
        self.image = None
        self.size = None
        self.local = spec['kind'] == 'local'

    def command(self, args, timeout=600):
        result = runner.docker(*args, timeout=timeout, check=False)
        self.log = (self.log + result.stdout + result.stderr)[-60000:]
        if result.returncode:
            raise ValueError('Artifact preparation failed; see organizer log')
        return result

    def sandbox(self, name, image, command, builder=False):
        args = ['run', '--name', name, '--pull=never', '--runtime', 'runsc', '--network', self.name,
                '--platform', 'linux/amd64', '--user', '1000:1000', '--cpus', '1', '--memory', '1g',
                '--memory-swap', '1g', '--pids-limit', '512', '--log-driver', 'none', '--no-healthcheck',
                '--read-only', '--tmpfs', '/tmp:rw,nosuid,nodev,size=32m,mode=1777', '--shm-size', '16m',
                '--dns', '127.0.0.1', '--add-host', 'proxy:' + self.proxy_ip, '--mount', f'type=volume,source={self.disk},target=/work,volume-nocopy',
                '-e', 'HTTPS_PROXY=http://proxy:3128', '-e', 'HTTP_PROXY=http://proxy:3128',
                '-e', 'https_proxy=http://proxy:3128', '-e', 'http_proxy=http://proxy:3128',
                '-e', 'NO_PROXY=localhost,127.0.0.1', '-e', 'TMPDIR=/work/tmp', '-e', 'XDG_RUNTIME_DIR=/work/run',
                '-e', 'XDG_DATA_HOME=/work/xdg', '-e', 'DOCKER_CONFIG=/work/docker', '--ulimit', 'core=0', '--ulimit', 'fsize=268435456:268435456']
        if builder:
            # The setuid mapping helpers elevate only inside runsc's user-space kernel.
            args += ['--security-opt', 'no-new-privileges=false']
        else:
            args += ['--cap-drop', 'ALL', '--security-opt', 'no-new-privileges']
        return self.command(args + ['--entrypoint', 'sh', image, '-ec', command])

    def prepare(self):
        if self.local:
            info = runner.resolve_source(self.spec['reference'])
            self.image = info['image_id']
            # Normalize even reviewed local images on Docker's containerd store.
            path = volume('create', self.disk, uid=1000)
            archive = path / 'image.tar'
            with archive.open('wb') as output:
                import subprocess
                process = subprocess.Popen(['docker', 'save', self.image], stdout=output, stderr=subprocess.DEVNULL)
                try:
                    process.wait(timeout=120)
                    if process.returncode:
                        raise ValueError('Cannot export reviewed image')
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait()
            self.size = verified_archive(archive, path / 'checked.tar', self.tag)
            return self
        path = volume('create', self.disk, uid=1000)
        if self.spec['kind'] == 'repo':
            runner.command(['mount', '-o', 'remount,exec,nodev,nosuid', str(path)])
        for name in ('tmp', 'run', 'xdg'):
            (path / name).mkdir(mode=0o700)
            os.chown(path / name, 1000, 1000)
        runner.docker('network', 'create', '--internal', '--opt', 'com.docker.network.bridge.gateway_mode_ipv4=isolated', self.name)
        isolate_network(self.name)
        runner.docker('run', '--pull=never', '-d', '--name', self.name + '-proxy', '--network', 'bridge', '--read-only',
                      '--user', '1000:1000', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                      '--cpus', '.2', '--memory', '96m', '--memory-swap', '96m', '--pids-limit', '64',
                      '--log-driver', 'none', '-e', 'BUILD_ALLOWED_HOSTS=' + os.environ.get('BUILD_ALLOWED_HOSTS', ''),
                      '--entrypoint', 'python', TOOLS, '/proxy.py')
        runner.docker('network', 'connect', '--alias', 'proxy', self.name, self.name + '-proxy')
        runner.docker('exec', self.name + '-proxy', 'python', '-c',
                      "import socket,time\nfor i in range(100):\n try:\n  socket.create_connection(('127.0.0.1',3128),timeout=.2).close();break\n except OSError: time.sleep(.1)\nelse: raise SystemExit('Proxy startup timeout')", timeout=20)
        self.proxy_ip = json.loads(runner.docker('inspect', self.name + '-proxy').stdout)[0]['NetworkSettings']['Networks'][self.name]['IPAddress']
        if self.spec['kind'] == 'image':
            # Registry syntax has already been validated by the queue and again by the executor.
            self.sandbox(self.name + '-fetch', TOOLS,
                         'skopeo --tmpdir /work/tmp --override-os linux --override-arch amd64 copy --retry-times 2 docker://' +
                         self.spec['reference'] + ' docker-archive:/work/image.tar:vibezone-import:checked')
        else:
            source, revision = self.spec['reference'], self.spec['revision']
            self.sandbox(self.name + '-fetch', TOOLS,
                'git -c core.hooksPath=/dev/null init /work/source; cd /work/source; '
                'git -c credential.helper= -c protocol.file.allow=never fetch --depth=1 ' + source + ' ' + revision + '; '
                'git -c core.hooksPath=/dev/null checkout --detach FETCH_HEAD; '
                'test "$(git rev-parse HEAD)" = ' + revision)
            runner.docker('rm', '-fv', self.name + '-fetch')
            self.sandbox(self.name + '-build', BUILDKIT,
                'context=/work/source; if [ ! -f "$context/Dockerfile" ]; then context=/work/source/starter; fi; '
                'BUILDKITD_FLAGS="--root /work/cache --oci-worker-no-process-sandbox --oci-worker-snapshotter=native" '
                'buildctl-daemonless.sh build --frontend dockerfile.v0 --local context="$context" --local dockerfile="$context" '
                '--opt platform=linux/amd64 --opt build-arg:HTTPS_PROXY=http://' + self.proxy_ip + ':3128 '
                '--opt build-arg:HTTP_PROXY=http://' + self.proxy_ip + ':3128 --output type=docker,compression=uncompressed,dest=/work/image.tar', builder=True)
        # Stop all code that can modify the archive before reading it on the host.
        for suffix in ('-fetch', '-build', '-proxy'):
            runner.docker('rm', '-fv', self.name + suffix, check=False)
        self.size = verified_archive(path / 'image.tar', path / 'checked.tar', self.tag)
        runner.docker('load', '-i', path / 'checked.tar', timeout=120)
        self.image = runner.resolve_source(self.tag)['image_id']
        return self

    def cleanup(self):
        errors = []
        for suffix in ('-fetch', '-build', '-proxy'):
            value = runner.docker('rm', '-fv', self.name + suffix, check=False)
            if value.returncode and 'no such' not in value.stderr.lower():
                errors.append(value.stderr[-500:])
        if not self.local:
            value = runner.docker('image', 'rm', self.tag, check=False)
            if value.returncode and 'no such' not in value.stderr.lower():
                errors.append(value.stderr[-500:])
        value = runner.docker('network', 'rm', self.name, check=False)
        if value.returncode and 'not found' not in value.stderr.lower():
            errors.append(value.stderr[-500:])
        try:
            volume('destroy', self.disk)
        except Exception as error:
            errors.append(str(error)[-500:])
        return errors
