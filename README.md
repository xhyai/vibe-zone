# Vibe Zone · DevFest Warsaw 2026

**Saturday, 21 November 2026 · Google for Startups Campus, Plac Konesera 10, Warsaw.**

Build a faster mini social API: `GET /feed`, `POST /posts`, `POST /like`, backed by SQLite. Any language, solo or a team, AI tools welcome. Competition resources: **1 vCPU / 2 GiB RAM**, image **<100 MB uncompressed**, target **<50 MB**.

[Home](https://devfest.atlasapx.pl/?lang=en) · [Get started](https://devfest.atlasapx.pl/start?lang=en) · [Leaderboard](https://devfest.atlasapx.pl/leaderboard?lang=en) · [Submit](https://devfest.atlasapx.pl/submit?lang=en) · [Event screen](https://devfest.atlasapx.pl/screen?lang=en) · [Polski](README.pl.md)

CPX22 results are test-only. A separate gVisor runner builds and measures submissions; Mikrus only serves the board. The organizer selects the official host and profile on event day. [Runner runbook](deploy/RUNNER.md).

## Quickstart in five commands

Fork and clone this repository first. From its root, with Docker/buildx running and Python 3.12+ installed:

```sh
docker buildx build --platform linux/amd64 --load -t vibezone-starter:local starter
docker pull --platform linux/amd64 python@sha256:c5ba3deef00c7758561a0f03c26f44f10589263d40e732b8748e0d1f38f6dd89
docker pull --platform linux/amd64 grafana/k6@sha256:f21270290d702cbf0a7d6ba5d7ed100b63bcb233b558b885ed787547b3488279
bench/run_bench.sh vibezone-starter:local --pull never --smoke
docker image inspect vibezone-starter:local --format '{{.Os}}/{{.Architecture}} {{.Size}} bytes'
```

Smoke checks the API contract, randomized data, concurrent likes and forced-restart persistence, then runs three short measurements. Reports go to `results/`. Smoke uses 256 MiB RAM and bounded tmpfs; it does not test host-restart disk durability or produce competition scores. If Docker networks conflict, set a free `BENCH_SUBNET`.

## Join the challenge

Follow the [participant guide](docs/PARTICIPANT_GUIDE.md) for local startup, contract tests, optimization, image size, public GHCR/Docker Hub publishing and troubleshooting. Ask the organizer for a one-use invitation, then submit your public `linux/amd64` image. Save the private team code for updates. Each version needs organizer approval of an immutable image digest or repository commit; the remote runner prepares it in a bounded sandbox.

Batches are scheduled every 15 minutes, with three measurements per image and atomic publication. The highest median qualifying VU level wins, with **0% errors and p99 <500 ms**. Ties: smaller image, lower median startup, lower p99 at that level; exact ties share a place. [Full scoring rules](docs/SCORING.md), including normalized image sizes and profile separation, remain authoritative.

## Repository

| Path | Contents |
| --- | --- |
| [starter/](starter/README.md) | Deliberately slow Python / Flask / SQLite baseline |
| [bench/](bench/README.md) | Contract tests, durability, k6 and JSON measurements |
| [leaderboard/](leaderboard/README.md) | PL/EN website, ranking, submissions, team history and admin |
| [deploy/](deploy/README.md) | Local builds and Mikrus deployment |
| [docs/](docs/PARTICIPANT_GUIDE.md) | Participant guide, [API](docs/API.md), [scoring](docs/SCORING.md), [audit](docs/AUDIT.md), [homepage report](docs/HOMEPAGE.md) |

[Contributing and local tests](CONTRIBUTING.md) · [Release evidence](docs/RELEASE.md) · [MIT license](LICENSE)

No GitHub Actions or Google Cloud. Web has no Docker socket; isolated runners pull approved jobs over HTTPS. See the audit before preparing competition infrastructure.

Separate badges cover image size below 50 MB, startup, p99 and organizer-verified unusual languages. Hidden correctness checks publish only pass counts. Optional extras: Survivor, announced Bonus, prompt logs, [audience vote](https://devfest.atlasapx.pl/vote?lang=en), and [AI Pilot](https://devfest.atlasapx.pl/start?lang=en#ai-pilot) for people new to programming. These never change U. [Category rules](docs/SCORING.md#extra-categories--dodatkowe-kategorie).
