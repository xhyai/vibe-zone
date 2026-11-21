# Vibe Zone · Participant guide

[PL](PARTICIPANT_GUIDE.pl.md) · [EN](PARTICIPANT_GUIDE.md) · [Web](https://devfest.atlasapx.pl/start?lang=en) · [API](API.md) · [Scoring / Punktacja](SCORING.md)

CPX22 results are test-only. A separate gVisor runner builds and measures submissions; Mikrus only serves the board. The organizer selects the official host and profile on event day.

## 1. Prepare your tools and fork

You need Git, Python 3.12+ and a running Docker installation with buildx. Click Fork on GitHub, then clone your fork. Run all subsequent commands from the repository root. Replace YOUR_ACCOUNT with your account name.

```sh
docker version
docker buildx version
git clone https://github.com/YOUR_ACCOUNT/vibe-zone.git
cd vibe-zone
```

## 2. Build and run the starter

The starter is deliberately slow. Preserve the API; you can replace the language and implementation. Listen on 0.0.0.0:8080 and store SQLite in /data. This container is for local development with your own code only.

```sh
docker buildx build --platform linux/amd64 --load -t vibezone-starter:local starter
docker run -d --name vz-starter --platform linux/amd64 \
  --cpus 1 --memory 2g --memory-swap 2g --pids-limit 512 \
  -e PORT=8080 -e DATA_DIR=/data -p 127.0.0.1:8080:8080 \
  -v vz-starter-data:/data vibezone-starter:local
curl --fail http://localhost:8080/health
```

## 3. Check the API contract

The checker writes test data. It checks validation, feed order, Unicode, write visibility and like idempotency. The checker alone does not restart the container; the next smoke step performs the full SIGKILL and persistence check.

```sh
python3 bench/tests/api_check.py http://localhost:8080
```

## 4. Run the local smoke benchmark

First pull the pinned tools. The runner never downloads them automatically. Smoke runs three measurements at 2/5/10 VU, checks data after SIGKILL, writes JSON to results/ and removes its own containers. It uses 256 MiB RAM and tmpfs for /data: this is a functional check, not a competition result or host-restart durability test. If the subnet conflicts, set BENCH_SUBNET to a free range. Docker must support isolated network mode.

```sh
docker pull --platform linux/amd64 python@sha256:c5ba3deef00c7758561a0f03c26f44f10589263d40e732b8748e0d1f38f6dd89
docker pull --platform linux/amd64 grafana/k6@sha256:f21270290d702cbf0a7d6ba5d7ed100b63bcb233b558b885ed787547b3488279
bench/run_bench.sh vibezone-starter:local --pull never --smoke
```

## 5. Optimize using measurements

Inspect query plans, SQLite read counts, connection and locking costs, and image layers. Change one thing at a time and repeat the checks. Preserve validation and durability; every acknowledged write must survive SIGKILL. Do not hardcode responses or benchmark data.

## 6. Check size and platform

The image must be below 100,000,000 uncompressed bytes; the target is below 50 MB. The remote runner sums uncompressed TAR layers and verifies their hashes. Local inspect Size is an estimate that depends on the image store. The badge for images below 50 MB does not add U.

```sh
docker image inspect vibezone-starter:local --format '{{.Os}}/{{.Architecture}} {{.Size}} bytes'
```

## 7. Publish a public linux/amd64 image

Choose GHCR or Docker Hub and replace YOUR_ACCOUNT with your account name. Log in locally; GHCR requires package publishing permission. Make the package public and verify an anonymous pull. Never add tokens to the repository. After every code change, rebuild and repeat the tests before publishing.

```sh
docker login ghcr.io
docker buildx build --platform linux/amd64 --load \
  -t ghcr.io/YOUR_ACCOUNT/vibe-zone:v1 starter
docker push ghcr.io/YOUR_ACCOUNT/vibe-zone:v1

# Docker Hub
docker login
docker tag ghcr.io/YOUR_ACCOUNT/vibe-zone:v1 docker.io/YOUR_ACCOUNT/vibe-zone:v1
docker push docker.io/YOUR_ACCOUNT/vibe-zone:v1
```

## 8. Submit and save your team code

Ask the organizer for a one-use invitation. Enter your team name, invitation and public image in the form. Your team code is shown only once: save it privately. For updates, use the same name and code without a new invitation. Every version needs approval of an image digest or a full repository commit. The runner only imports or builds that version in isolation. Changing a tag does not update an approved submission. Batches start every 15 minutes and publish after all three runs; they may take longer than a quarter hour.

## 9. Check common mistakes

Port: listen on 0.0.0.0 and PORT=8080, not just localhost. Data: use DATA_DIR=/data; the root filesystem is read-only and the process uses UID/GID 10001. Signals: forward SIGTERM to the server (exec in your entrypoint) and shut down cleanly. Durability: clean shutdown is not enough - acknowledged posts and likes must also survive SIGKILL. Do not rely on process memory. /health must return valid JSON within 30 seconds. Local container name conflict: stop and remove only your own vz-starter; its data volume remains.

```sh
docker stop vz-starter
docker rm vz-starter
```

[Submit / Zgłoś](https://devfest.atlasapx.pl/submit?lang=en) · [Leaderboard](https://devfest.atlasapx.pl/leaderboard?lang=en)
