#!/usr/bin/env python3
"""Local Docker benchmark runner. All workload containers use an internal network."""
import argparse
import base64
import gzip
import hashlib
import json
import math
import os
import re
import secrets
import selectors
import signal
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_LEVELS = [500, 1000, 2000, 3000, 5000, 7500, 10000, 15000, 20000, 30000]
IMAGE = re.compile(r"(?:[a-z0-9]+(?:[._-][a-z0-9]+)*/)*[a-z0-9]+(?:[._-][a-z0-9]+)*(?::[\w][\w.-]{0,127}|@sha256:[a-f0-9]{64})?\Z", re.ASCII)
IMAGE_ID = re.compile(r"sha256:[a-f0-9]{64}\Z")
REPO = re.compile(r"https://(?:github\.com|gitlab\.com)/[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+(?:\.git)?\Z")
PROBE_IMAGE = "python@sha256:c5ba3deef00c7758561a0f03c26f44f10589263d40e732b8748e0d1f38f6dd89"
K6_IMAGE = "grafana/k6@sha256:f21270290d702cbf0a7d6ba5d7ed100b63bcb233b558b885ed787547b3488279"


class InfrastructureError(RuntimeError):
    """The host/tooling cannot produce a comparable competition measurement."""


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def command(args, timeout=120, check=True):
    # Bound both output streams while the command runs, not after communicate().
    process = subprocess.Popen([str(a) for a in args], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    buffers = {process.stdout: bytearray(), process.stderr: bytearray()}
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            for stream in buffers:
                selector.register(stream, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"{args[0]} {args[1]} timed out")
                for key, _ in selector.select(min(remaining, 1)):
                    chunk = os.read(key.fileobj.fileno(), 8192)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    buffers[key.fileobj].extend(chunk)
                    if sum(map(len, buffers.values())) > 1024 * 1024:
                        raise RuntimeError("Command output exceeds 1 MiB")
        process.wait(timeout=max(.1, deadline - time.monotonic()))
        result = subprocess.CompletedProcess(args, process.returncode,
            buffers[process.stdout].decode(errors="replace"), buffers[process.stderr].decode(errors="replace"))
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stdout.close()
        process.stderr.close()
    if check and result.returncode:
        raise RuntimeError(f"{args[0]} {args[1]} failed ({result.returncode}): {(result.stderr or result.stdout)[-3000:]}")
    return result


def docker(*args, **kwargs):
    return command(["docker", *args], **kwargs)


def install_file(container, source, destination):
    # docker cp does not reliably address live tmpfs mounts; write only trusted scripts via exec.
    encoded = base64.b64encode(Path(source).read_bytes()).decode("ascii")
    if len(encoded) > 128 * 1024 or not re.fullmatch(r"/tmp/[a-zA-Z0-9_/.-]+", destination):
        raise ValueError("Invalid trusted script installation")
    docker("exec", container, "sh", "-c", f"printf '%s' '{encoded}' | base64 -d > '{destination}'")


def validate_source(source):
    if source.startswith("https://"):
        if not REPO.fullmatch(source) or any(part in {".", ".."} for part in source.split("/")[3:]):
            raise ValueError("Use a public https://github.com/owner/repo or https://gitlab.com/owner/repo URL")
        return "repo"
    if IMAGE_ID.fullmatch(source):
        return "image"
    if not IMAGE.fullmatch(source) or len(source) > 255:
        raise ValueError("Invalid Docker image reference")
    first = source.split("/")[0]
    if "/" in source and ("." in first or ":" in first or first == "localhost") and first not in {"ghcr.io", "docker.io"}:
        raise ValueError("Only Docker Hub and GHCR image names are accepted")
    return "image"


def profile(smoke=False):
    return {"name": "smoke" if smoke else "standard", "version": 2, "platform": "linux/amd64", "cpu": 1, "memory_bytes": 268435456 if smoke else 2147483648,
            "levels": [2, 5, 10] if smoke else DEFAULT_LEVELS,
            "ramp_seconds": 2 if smoke else 20, "hold_seconds": 5 if smoke else 20,
            "seed_posts": 50 if smoke else 10000, "seed_likes": 100 if smoke else 100000,
            "mix": {"feed": 70, "like": 20, "posts": 10}, "think_seconds": [1, 3],
            "max_p99_ms": 500, "max_image_bytes": 100000000, "size_basis": "uncompressed",
            "repetitions": 3, "min_requests_per_vu_second": .2, "data_limit_bytes": 1073741824,
            "storage": "tmpfs-diagnostic" if smoke else "quota-disk",
            "generator_gomaxprocs": 1 if smoke else None,
            "generator_cpus": 1 if smoke else 4, "generator_memory": "192m" if smoke else "4g",
            "probe_cpus": 0.5 if smoke else 1, "probe_memory": "96m" if smoke else "256m", "max_run_seconds": 600 if smoke else 5400}


def fingerprint(config):
    digest = hashlib.sha256(json.dumps(config, sort_keys=True).encode())
    digest.update(K6_IMAGE.encode())
    digest.update(PROBE_IMAGE.encode())
    digest.update(config.get("host_profile", os.environ.get("BENCH_HOST_PROFILE", "local-unverified")).encode())
    for name in ["runner.py", "http_client.py", "seed.py", "verify_loaded.py", "tests/api_check.py", "k6/load.js", "profiles.py", "isolated.py", "artifacts.py", "remote_job.py"]:
        digest.update((ROOT / name).read_bytes())
    return digest.hexdigest()


def resolve_source(source, pull="never"):
    kind = validate_source(source)
    if kind == "repo":
        raise ValueError("Repository builds are disabled; import an image on an isolated builder and approve its local ID")
    if pull != "never":
        raise ValueError("Automatic pulls are disabled; import a bounded image on an isolated worker first")
    info = json.loads(docker("image", "inspect", source).stdout)[0]
    if info["Architecture"] != "amd64" or info["Os"] != "linux":
        raise ValueError("Image must target linux/amd64")
    if set(info.get("Config", {}).get("Volumes") or {}) - {"/data"}:
        raise ValueError("Image may declare only the /data volume")
    return {"source": source, "source_kind": kind, "source_commit": None, "image_id": info["Id"],
            "registry_digests": info.get("RepoDigests", []), "image_bytes": info["Size"]}


def compressed_size(image):
    # Exactly the byte count of docker save | gzip, streamed without a giant in-memory tar.
    class Counter:
        count = 0
        def write(self, data):
            self.count += len(data)
            return len(data)
        def tell(self):
            return self.count
    archive = Counter()
    save = subprocess.Popen(["docker", "save", image], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 120
        total = 0
        with gzip.GzipFile(fileobj=archive, mode="wb", compresslevel=6, mtime=0) as zipped:
            with selectors.DefaultSelector() as selector:
                selector.register(save.stdout, selectors.EVENT_READ)
                while True:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("docker save deadline exceeded")
                    if not selector.select(1):
                        continue
                    chunk = os.read(save.stdout.fileno(), 65536)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > 256 * 1024 * 1024:
                        raise ValueError("Image archive exceeds 256 MiB")
                    zipped.write(chunk)
        if save.wait(timeout=max(.1, deadline - time.monotonic())):
            raise RuntimeError("docker save failed")
        return archive.tell()
    finally:
        save.stdout.close()
        if save.poll() is None:
            save.kill()
            save.wait()


def metric(raw, name, field, default=None):
    value = raw.get("metrics", {}).get(name, {}).get("values", {}).get(field, default)
    if value is not None and (type(value) not in (int, float) or not math.isfinite(value)):
        return default
    return value


def score_level(raw, vus, config, exit_code):
    count = metric(raw, "hold_requests", "count", 0)
    duration = raw.get("state", {}).get("testRunDurationMs", 0)
    p99 = metric(raw, "hold_latency", "p(99)") if count > 0 else None
    error_rate = metric(raw, "response_errors", "rate")
    hold_error_rate = metric(raw, "hold_errors", "rate")
    full_hold = duration >= (config["ramp_seconds"] + config["hold_seconds"]) * 1000 - 50
    minimum = math.ceil(vus * config["hold_seconds"] * config["min_requests_per_vu_second"])
    active = metric(raw, "vus", "max", 0)
    semantic_errors = metric(raw, "semantic_errors", "count", 0)
    integrity_checks = metric(raw, "integrity_completed", "count", 0)
    passed = (exit_code == 0 and full_hold and count >= minimum and active >= vus + 1 and error_rate == 0 and hold_error_rate == 0
              and semantic_errors == 0 and integrity_checks > 0
              and p99 is not None and p99 < config["max_p99_ms"])
    return {"vus": vus, "passed": passed, "full_hold": full_hold, "hold_requests": count,
            "minimum_requests": minimum, "observed_vus": active, "semantic_errors": semantic_errors,
            "integrity_completed": integrity_checks,
            "throughput_rps": count / config["hold_seconds"] if count > 0 else None,
            "p95_ms": metric(raw, "hold_latency", "p(95)") if count > 0 else None,
            "p99_ms": p99, "error_percent": error_rate * 100 if error_rate is not None else None,
            "hold_error_percent": hold_error_rate * 100 if hold_error_rate is not None and count > 0 else None,
            "attempt_requests": metric(raw, "http_reqs", "count", 0),
            "attempt_throughput_rps": metric(raw, "http_reqs", "rate"),
            "attempt_p95_ms": metric(raw, "http_req_duration", "p(95)"),
            "attempt_p99_ms": metric(raw, "http_req_duration", "p(99)"),
            "duration_ms": duration, "k6_exit_code": exit_code}


class Run:
    def __init__(self, source, config, output_dir, pull):
        self.id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
        self.prefix = "vz-" + self.id.lower()
        self.network = self.prefix
        self.sut = self.prefix + "-sut"
        self.probe = self.prefix + "-probe"
        self.k6 = self.prefix + "-k6"
        self.volume = self.prefix + "-data"
        self.output = Path(output_dir).resolve()
        self.output.mkdir(parents=True, exist_ok=True)
        self.config, self.source, self.pull = config, source, pull
        self.lock_acquired = False
        self.lock_name = "vibezone-benchmark-lock"
        self.extra_flags = []
        self.result = {"id": self.id, "started_at": utcnow(), "source": source, "status": "failed",
                       "profile": config, "profile_hash": fingerprint(config), "max_vus": 0, "levels": [],
                       "throughput_rps": None, "p95_ms": None, "p99_ms": None, "error_percent": None}

    def create_data(self):
        if self.config["name"] == "smoke":
            driver, options = "local", {"type": "tmpfs", "device": "tmpfs",
                "o": "size=64m,uid=10001,gid=10001,mode=0700,nosuid,nodev,noexec"}
        else:
            driver = os.environ.get("BENCH_VOLUME_DRIVER")
            options = json.loads(os.environ.get("BENCH_VOLUME_OPTIONS") or "{}")
            if not driver or not options or options.get("type") == "tmpfs":
                raise InfrastructureError("Standard profile requires a quota-backed disk volume: BENCH_VOLUME_DRIVER / BENCH_VOLUME_OPTIONS")
        args = [part for key, value in options.items() for part in ["--opt", f"{key}={value}"]]
        docker("volume", "create", "--label", "vibezone.run=" + self.id, "--driver", driver, *args, self.volume)

    def resolve(self):
        return resolve_source(self.source, self.pull)

    def sut_options(self):
        return []

    def sut_filesystems(self):
        return ["--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=64m,mode=1777", "--shm-size", "16m"]

    def generator_options(self):
        return []

    def create_sut(self):
        docker("create", "--pull=never", "--platform", "linux/amd64", "--name", self.sut,
               "--label", "vibezone.run=" + self.id, "--network", self.network, "--network-alias", "sut",
               "--cpus", self.config["cpu"], "--memory", self.config["memory_bytes"], "--memory-swap", self.config["memory_bytes"], "--pids-limit", "512",
               "--user", "10001:10001", "--read-only", "--no-healthcheck",
               *self.sut_filesystems(),
               "--ulimit", "nofile=4096:4096", "--ulimit", "fsize=1073741824:1073741824", "--ulimit", "core=0",
               "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--log-driver", "none",
               "--dns", "127.0.0.1", "--stop-timeout", "5",
               "-e", "PORT=8080", "-e", "DATA_DIR=/data", "-e", "HOME=/tmp",
               "--mount", f"type=volume,source={self.volume},target=/data,volume-nocopy", *self.sut_options(), self.result["image_id"])

    def healthy(self, start=False):
        started = time.monotonic()
        if start:
            docker("start", self.sut)
        script = """import sys,time
sys.path.insert(0,'/tmp')
from http_client import request_json
end=time.monotonic()+28
while time.monotonic()<end:
 try:
  status,value=request_json('http://sut:8080/health',timeout=.5)
  assert status==200 and value=={'status':'ok'}
  break
 except Exception: time.sleep(.05)
else: raise SystemExit('health timeout')
"""
        docker("exec", self.probe, "python", "-c", script, timeout=31)
        elapsed = (time.monotonic() - started) * 1000
        if elapsed > 30000:
            raise RuntimeError("Health took longer than 30 seconds")
        return round(elapsed, 3)

    def check_host(self, host):
        if self.config["name"] != "smoke" and (host["Architecture"] not in ("x86_64", "amd64") or host["NCPU"] < 6
                or host["MemTotal"] < 8 * 1024**3 or self.result["host_profile"] == "local-unverified"):
            raise InfrastructureError("Standard ranking requires native amd64, >=6 CPUs, >=8 GiB and an explicit BENCH_HOST_PROFILE")

    def acquire_lock(self, probe_image):
        docker("create", "--pull=never", "--platform", "linux/amd64", "--name", self.lock_name,
               "--label", "vibezone.run=" + self.id, "--network", "none", "--read-only", "--entrypoint", "/bin/true", probe_image)
        self.lock_acquired = True

    def create_network(self):
        subnet_args = ["--subnet", os.environ["BENCH_SUBNET"]] if os.environ.get("BENCH_SUBNET") else []
        docker("network", "create", "--internal", "--opt", "com.docker.network.bridge.gateway_mode_ipv4=isolated",
               *subnet_args, "--label", "vibezone.run=" + self.id, self.network)

    def execute(self):
        print(f"[{self.id}] Resolving {self.source}", file=sys.stderr, flush=True)
        self.result.update(self.resolve())
        if self.result["image_bytes"] >= self.config["max_image_bytes"]:
            raise ValueError("Image must be smaller than 100,000,000 uncompressed bytes")
        self.result["image_gzip_bytes"] = compressed_size(self.result["image_id"])
        probe_image = PROBE_IMAGE
        k6_image = K6_IMAGE
        for image in [probe_image, k6_image]:
            if docker("image", "inspect", image, check=False).returncode:
                raise InfrastructureError(f"Preload the pinned tool image offline: {image}")
        self.result["tools"] = {name: json.loads(docker("image", "inspect", image).stdout)[0]["Id"]
                                for name, image in [("probe", probe_image), ("k6", k6_image)]}
        host = json.loads(docker("info", "--format", "{{json .}}").stdout)
        self.result["host"] = {key: host.get(key) for key in ["Architecture", "NCPU", "MemTotal", "KernelVersion", "Driver"]}
        self.result["host_profile"] = self.config.get("host_profile", os.environ.get("BENCH_HOST_PROFILE", "local-unverified"))
        self.check_host(host)
        self.acquire_lock(probe_image)
        self.create_network()
        self.create_data()
        docker("run", "--pull=never", "-d", "--platform", "linux/amd64", "--name", self.probe, "--network", self.network,
               "--label", "vibezone.run=" + self.id, "--user", "10001:10001", "--read-only", "--no-healthcheck",
               "--cpus", self.config["probe_cpus"], "--memory", self.config["probe_memory"], "--memory-swap", self.config["probe_memory"],
               "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=16m,mode=1777", "--log-driver", "none",
               "--ulimit", "fsize=1048576:1048576", "--ulimit", "core=0", "--dns", "127.0.0.1",
               "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "64",
               "--mount", f"type=volume,source={self.volume},target=/data,volume-nocopy",
               probe_image, "sleep", "infinity")
        # The probe keeps a diagnostic tmpfs volume mounted across SUT restarts.
        disk = docker("exec", self.probe, "python", "-c",
            "import os,json; s=os.statvfs('/data'); print(json.dumps({'bytes':s.f_blocks*s.f_frsize,'writable':os.access('/data',os.W_OK),'empty':not os.listdir('/data')}))")
        storage = json.loads(disk.stdout)
        if not storage["writable"] or not storage["empty"] or not 0 < storage["bytes"] <= self.config["data_limit_bytes"]:
            raise InfrastructureError("Data volume must be empty, writable by UID 10001 and expose a filesystem/quota <=1 GiB")
        self.result["storage"] = storage
        docker("exec", self.probe, "mkdir", "/tmp/tests")
        install_file(self.probe, ROOT / "tests/api_check.py", "/tmp/tests/api_check.py")
        install_file(self.probe, ROOT / "seed.py", "/tmp/seed.py")
        install_file(self.probe, ROOT / "http_client.py", "/tmp/http_client.py")
        install_file(self.probe, ROOT / "verify_loaded.py", "/tmp/verify_loaded.py")
        self.create_sut()
        self.result["cold_start_samples_ms"] = []
        for index in range(3):
            if index:
                docker("stop", "--time", "5", self.sut)
                docker("rm", "-v", self.sut)
                self.create_sut()
            self.result["cold_start_samples_ms"].append(self.healthy(start=True))
        self.result["cold_start_ms"] = statistics.median(self.result["cold_start_samples_ms"])
        print(f"[{self.id}] Checking API and crash durability", file=sys.stderr, flush=True)
        checked = docker("exec", self.probe, "python", "/tmp/tests/api_check.py", "http://sut:8080",
                         "--phase", "prepare", "--expect-empty", timeout=180)
        self.result["correctness"] = json.loads(checked.stdout)
        docker("kill", "--signal", "KILL", self.sut)
        self.healthy(start=True)
        checked = docker("exec", self.probe, "python", "/tmp/tests/api_check.py", "http://sut:8080", "--phase", "verify", timeout=60)
        self.result["durability"] = json.loads(checked.stdout)
        # Seed a fresh volume so every implementation sees identical IDs and data sizes.
        docker("rm", "-fv", self.sut)
        docker("exec", self.probe, "python", "-c",
               "import os,shutil; [(shutil.rmtree('/data/'+n) if os.path.isdir('/data/'+n) and not os.path.islink('/data/'+n) else os.unlink('/data/'+n)) for n in os.listdir('/data')]")
        self.create_sut()
        self.healthy(start=True)
        print(f"[{self.id}] Seeding {self.config['seed_posts']} posts / {self.config['seed_likes']} likes", file=sys.stderr, flush=True)
        seeded = docker("exec", self.probe, "python", "/tmp/seed.py", "http://sut:8080", "--posts", self.config["seed_posts"],
                        "--likes", self.config["seed_likes"], "--key", os.environ.get("BENCH_SEED", secrets.token_hex(32)),
                        timeout=3600)
        self.result["seed"] = json.loads(seeded.stdout)
        previous = 0
        for level in self.config["levels"]:
            print(f"[{self.id}] Ramping to {level} VU", file=sys.stderr, flush=True)
            env = {"BASE_URL": "http://sut:8080", "VUS": level, "PREV_VUS": previous,
                   "RAMP_SECONDS": self.config["ramp_seconds"], "HOLD_SECONDS": self.config["hold_seconds"],
                   "SEED_POSTS": self.config["seed_posts"]}
            # Pinned k6 uses Go 1.24: its default follows host CPUs, not the cgroup quota.
            if self.config.get("generator_gomaxprocs"):
                env["GOMAXPROCS"] = self.config["generator_gomaxprocs"]
            env_args = [part for key, value in env.items() for part in ["-e", f"{key}={value}"]]
            docker("create", "--pull=never", "--platform", "linux/amd64", "--name", self.k6, "--network", self.network,
                   "--label", "vibezone.run=" + self.id, "--user", "10001:10001", "--read-only", "--no-healthcheck",
                   "--cpus", self.config["generator_cpus"], "--memory", self.config["generator_memory"],
                   "--memory-swap", self.config["generator_memory"], "--pids-limit", "128", "--log-driver", "none",
                   "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=16m,mode=1777", "--ulimit", "core=0",
                   "--ulimit", "fsize=1048576:1048576", "--dns", "127.0.0.1",
                   "--cap-drop", "ALL", "--security-opt", "no-new-privileges", *self.generator_options(), *env_args,
                   "--entrypoint", "/bin/sh", k6_image, "-c",
                   "while [ ! -f /tmp/ready ]; do sleep .1; done; k6 run --quiet /tmp/load.js >/tmp/k6.log 2>&1; echo $? >/tmp/exit; sleep infinity")
            docker("start", self.k6)
            install_file(self.k6, ROOT / "k6/load.js", "/tmp/load.js")
            docker("exec", self.k6, "touch", "/tmp/ready")
            deadline = time.monotonic() + self.config["ramp_seconds"] + self.config["hold_seconds"] + 60
            while True:
                state = json.loads(docker("inspect", "--format", "{{json .State}}", self.k6).stdout)
                if not state["Running"] or state["OOMKilled"]:
                    raise InfrastructureError("Generator stopped/OOM: measurement invalid")
                done = docker("exec", self.k6, "cat", "/tmp/exit", check=False, timeout=10)
                if done.returncode == 0:
                    code = int(done.stdout.strip())
                    break
                if time.monotonic() > deadline:
                    raise InfrastructureError("Generator deadline exceeded")
                time.sleep(.5)
            if code not in (0, 99):
                raise InfrastructureError(f"Generator exited {code}: measurement invalid")
            raw_path = self.output / f"{self.id}-k6-{level}.json"
            copied = docker("exec", self.k6, "cat", "/tmp/summary.json", check=False)
            if copied.returncode:
                log = docker("exec", self.k6, "tail", "-c", "2000", "/tmp/k6.log", check=False)
                raise InfrastructureError(f"k6 exited {code} without a summary: {(log.stderr + log.stdout)[-2000:]}")
            raw_path.write_text(copied.stdout)
            raw = json.loads(raw_path.read_text())
            scored = score_level(raw, level, self.config, code)
            self.result["levels"].append(scored)
            stats = docker("exec", self.k6, "cat", "/sys/fs/cgroup/cpu.stat").stdout
            counters = dict(line.split() for line in stats.splitlines())
            throttled = int(counters.get("throttled_usec", "0")) / max(1, scored["duration_ms"] * 1000)
            scored["generator_throttled_ratio"] = throttled
            docker("rm", "-fv", self.k6)
            if throttled > .1:
                raise InfrastructureError("Generator CPU throttling exceeded 10%; measurement invalid")
            if scored["semantic_errors"]:
                raise ValueError("Incorrect successful HTTP response under load: disqualified")
            if not scored["passed"]:
                break
            self.result.update({key: scored[key] for key in ["throughput_rps", "p95_ms", "p99_ms", "error_percent"]})
            self.result["max_vus"] = level
            previous = level
        # Snapshot the measured feed BEFORE correctness checks would push it out of the latest 20.
        snapshot_args = ["--posts", self.config["seed_posts"], "--likes", self.config["seed_likes"]]
        docker("exec", self.probe, "python", "/tmp/verify_loaded.py", "capture", *snapshot_args, timeout=180)
        docker("kill", "--signal", "KILL", self.sut)
        self.healthy(start=True)
        checked = docker("exec", self.probe, "python", "/tmp/verify_loaded.py", "verify", *snapshot_args, timeout=180)
        self.result["measured_feed_restart_verified"] = json.loads(checked.stdout)
        checked = docker("exec", self.probe, "python", "/tmp/tests/api_check.py", "http://sut:8080",
                         "--phase", "prepare", timeout=180)
        self.result["post_load_correctness"] = json.loads(checked.stdout)
        docker("kill", "--signal", "KILL", self.sut)
        self.healthy(start=True)
        checked = docker("exec", self.probe, "python", "/tmp/tests/api_check.py", "http://sut:8080",
                         "--phase", "verify", timeout=60)
        self.result["post_load_durability"] = json.loads(checked.stdout)
        # U=0 is a valid, correctly implemented service which failed the first load level.
        self.result["status"] = "passed"
        if self.extra_flags:
            sys.path.insert(0, str(ROOT.parent))
            from bench.extras import execute
            self.result["extras"] = execute(self, self.extra_flags)

    def cleanup(self):
        actions = [("rm", "-fv", name) for name in [self.k6, self.sut, self.probe]]
        actions += [("volume", "rm", self.volume), ("network", "rm", self.network)]
        for action in actions:
            try:
                result = docker(*action, check=False, timeout=5)
                if result.returncode and "no such" not in result.stderr.lower() and "not found" not in result.stderr.lower():
                    self.result.setdefault("cleanup_errors", []).append(result.stderr[-500:])
            except Exception as error:
                self.result.setdefault("cleanup_errors", []).append(str(error)[-500:])
        # Preserve the daemon-wide lock if any run resources could still exist.
        if self.lock_acquired and not self.result.get("cleanup_errors"):
            try:
                docker("rm", "-fv", self.lock_name, timeout=5)
            except Exception as error:
                self.result.setdefault("cleanup_errors", []).append(str(error)[-500:])

    def run(self):
        def deadline(_signum, _frame):
            raise TimeoutError("Total run deadline exceeded")
        previous_handler = signal.signal(signal.SIGALRM, deadline)
        signal.alarm(self.config["max_run_seconds"])
        try:
            self.execute()
        except (Exception, KeyboardInterrupt) as error:
            self.result["error"] = str(error) or "Interrupted"
            self.result["error_kind"] = "infrastructure" if isinstance(error, InfrastructureError) else "submission"
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous_handler)
            self.cleanup()
            if self.result.get("cleanup_errors"):
                self.result["status"] = "failed"
                self.result["error_kind"] = "infrastructure"
                self.result.setdefault("error", "Run cleanup incomplete; operator inspection required")
            self.result["finished_at"] = utcnow()
            path = self.output / f"{self.id}.json"
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self.result, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
            temporary.replace(path)
            print(path, flush=True)
        return self.result


def aggregate(results):
    """Median discrete level; all three correctness/durability gates are mandatory."""
    if len(results) != 3 or any(r["status"] != "passed" for r in results):
        raise ValueError("All three repetitions must complete without correctness/infrastructure failure")
    if len({(r["image_id"], r["profile_hash"]) for r in results}) != 1:
        raise ValueError("Repetitions must use the same image and profile")
    result = dict(results[0])
    result["repetitions"] = [{key: r.get(key) for key in ["id", "max_vus", "levels", "correctness", "durability",
                              "post_load_correctness", "post_load_durability", "measured_feed_restart_verified",
                              "cold_start_samples_ms", "storage"]} for r in results]
    for field in ["levels", "correctness", "durability", "post_load_correctness", "post_load_durability", "measured_feed_restart_verified"]:
        result.pop(field, None)
    result["max_vus"] = statistics.median(r["max_vus"] for r in results)
    samples = [level for r in results for level in r["levels"] if level["vus"] == result["max_vus"] and level["passed"]]
    result["cold_start_samples_ms"] = [v for r in results for v in r["cold_start_samples_ms"]]
    result["cold_start_ms"] = statistics.median(result["cold_start_samples_ms"])
    for field in ["throughput_rps", "p95_ms", "p99_ms", "error_percent"]:
        result[field] = statistics.median(s[field] for s in samples) if samples else None
    result["finished_at"] = results[-1]["finished_at"]
    if "extras" in results[-1]:
        result["extras"] = results[-1]["extras"]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", help="Docker image or public GitHub/GitLab repository URL")
    parser.add_argument("--smoke", action="store_true", help="Short local run; never compare with the standard ranking")
    parser.add_argument("--pull", choices=["never"], default="never")
    parser.add_argument("--results", default=os.environ.get("RESULTS_DIR", str(ROOT.parent / "results")))
    args = parser.parse_args()
    def interrupt(_signum, _frame):
        raise KeyboardInterrupt("Runner terminated")
    signal.signal(signal.SIGTERM, interrupt)
    config = profile(args.smoke)
    results = []
    for index in range(config["repetitions"]):
        run = Run(args.image, config, args.results, args.pull)
        if index == config["repetitions"] - 1:
            run.extra_flags = os.environ.get("VZ_EXTRAS_FLAGS", "").split()
        result = run.run()
        results.append(result)
        if result["status"] != "passed":
            return 1
    result = aggregate(results)
    path = Path(args.results) / (results[0]["id"] + "-median.json")
    path.write_text(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
    print(path.resolve(), flush=True)
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
