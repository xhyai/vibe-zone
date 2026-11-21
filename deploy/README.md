# Mikrus deployment · devfest.atlasapx.pl

Production uses web/Caddy on Mikrus and a separate outbound gVisor runner. Keep `VZ_REMOTE_RUNNER=1`, configure the shared private token and stop the local scheduler. CPX22 results remain test-only. [Runner installation and event-day runbook (PL/EN)](RUNNER.md). The local smoke deployment below remains available only for reviewed organizer images.

Produkcja: web/Caddy na Mikrusie, osobny runner gVisor w modelu pull. Ustaw `VZ_REMOTE_RUNNER=1`, prywatny token i wyłącz lokalny scheduler. Wyniki CPX22 są testowe. Dalsze instrukcje lokalnego smoke dotyczą wyłącznie sprawdzonych obrazów organizatora.

## Inspect and prepare

Inspect `docker ps`, listening ports, running services, free RAM/disk and network subnets before installation. Do not stop existing services or prune Docker. Use a new `/opt/vibe-zone` directory. Keep SSH access details outside the public repository.

Build trusted images **locally on the Mac**, not using GitHub Actions:

```sh
docker buildx build --platform linux/amd64 --load -t vibezone-starter:local starter
docker buildx build --platform linux/amd64 --target web --load -t vibezone-web:local -f leaderboard/Dockerfile .
docker buildx build --platform linux/amd64 --target runner --load -t vibezone-leaderboard:local -f leaderboard/Dockerfile .
```

Transfer source and the three images to the host using the operator's SSH connection and `docker save` / `docker load`. On the VPS, explicitly preload the pinned probe and k6 images listed in [bench/README.md](../bench/README.md), and the pinned Caddy image from Compose. These are trusted tool imports, not participant image imports. Read IDs again on the destination: classic and containerd image stores can expose different IDs for the same image. Verify platform, RootFS layer hashes and Config after import; do not reuse the Mac-local ID in the VPS allowlist.

Generate `deploy/.env` **on the VPS**, using `umask 077` and `secrets.token_urlsafe(48)` separately for `SECRET_KEY` and `ADMIN_PASSWORD`. Set mode 600; never print or commit the secrets. Set:

- `DOMAIN=devfest.atlasapx.pl`, `BENCH_SMOKE=1`.
- `BENCH_ALLOWED_IMAGE_IDS=` the complete **VPS-local** ID from `docker image inspect vibezone-starter:local --format '{{.Id}}'`. An empty allowlist prevents starting this deployment's runner.
- `DOCKER_GID=` group ID from `stat -c %g /var/run/docker.sock`.
- Free `EDGE_SUBNET`, `BOARD_SUBNET`, `BENCH_SUBNET` values from the example. Leave standard storage configuration empty on this host.

## TLS and networking

The proxied Cloudflare AAAA record points at the VPS IPv6. Cloudflare provides public IPv4/IPv6 and browser-trusted TLS. Compose publishes only **`[::]:80` and `[::]:443` TCP**; web has no host port. Caddy redirects HTTP and uses `tls internal` for encrypted origin traffic, avoiding ACME HTTP-01 through the proxy.

This is compatible with the configured Cloudflare **Full** mode, which accepts an origin self-signed certificate. It is not Full (strict): Full does not authenticate the origin certificate. Before the event, prefer a Cloudflare Origin CA certificate and Full (strict), or a validated public certificate. Sources: [Cloudflare Full](https://developers.cloudflare.com/ssl/origin-configuration/ssl-modes/full/), [Caddy TLS](https://caddyserver.com/docs/caddyfile/directives/tls).

The release found UFW active with incoming traffic denied, which initially caused Cloudflare 522. Seven scoped rules were added for TCP 80/443 on `eth0`, **from the official [Cloudflare IPv6 ranges](https://www.cloudflare.com/ips-v6/) to this origin's IPv6 only**. All existing rules were preserved; a private backup of `/etc/ufw` is at `/root/vibe-zone-ufw-before-release`. Do not reset UFW or open these ports to all sources.

To reproduce after reviewing the current official ranges, repeat `ufw allow in on eth0 proto tcp from CF_CIDR to "$ORIGIN_IPV6" port 80,443 comment 'Vibe Zone Cloudflare'` for each IPv6 prefix. Supply the origin address from private operator configuration. Review range changes periodically. IPv6 publication uses the host listener and INPUT rules on this deployment; check the actual Docker forwarding path again before changing networking. Public Cloudflare access and the origin's local IPv6 diagnostic were verified. An independent external non-Cloudflare IPv6 rejection test remains pending because the release workstation has no IPv6 route. Existing trusted Tailscale/loopback access rules remain intact.

## Start and verify

From `/opt/vibe-zone`:

```sh
docker compose --env-file deploy/.env -f deploy/docker-compose.yml --profile runner config --quiet
docker compose --env-file deploy/.env -f deploy/docker-compose.yml --profile runner up -d --no-build
docker compose --env-file deploy/.env -f deploy/docker-compose.yml --profile runner ps
curl -4 -fsS https://devfest.atlasapx.pl/health
curl -4 -fsS 'https://devfest.atlasapx.pl/screen?lang=en'
# Direct origin diagnostic: supply the private operator-configured ORIGIN_IPV6.
curl -6 -k --resolve "devfest.atlasapx.pl:443:[$ORIGIN_IPV6]" https://devfest.atlasapx.pl/health
```

Direct HTTPS diagnostics can send the domain as TLS SNI with `--resolve`. Caddy also sets `default_sni` for the task's direct-IP curl with an explicit domain Host header. Internal certificates are not browser-trusted; `-k` is only for that direct diagnostic. Public HTTPS checks must verify certificates normally.

Open `/admin` (username `admin`, password in the server env file), issue a one-use invitation, submit the starter, approve its ID, then run one batch. Check `/`, `/screen`, the PL/EN switch, `0% errors`, the test-results banner and three repetitions in `/api/leaderboard`. Never approve an unreviewed participant artifact on this host. Both web approval and the worker enforce the allowlist.

## Resource budget and competition gate

Each permanent service has a 192 MiB RAM/no-swap cap, 1 CPU, 128 PIDs, read-only rootfs, bounded `/tmp`, and rotated logs (2×5 MB). One smoke run adds SUT 256 MiB, generator 192 MiB (`GOMAXPROCS=1`) and probe 96 MiB, with `/data` on 64 MiB tmpfs. These limits leave headroom on a 2 GiB VPS but do not reserve CPU against other host services. Monitor RAM, load, space and inodes; fail closed on generator/infrastructure errors.

The scheduler has the host Docker socket and therefore administrative Docker authority; `network_mode: none` does not remove that authority. Only trusted local artifacts are permitted. Web is on an internal network; Caddy also has an edge network. The benchmark creates its own internal isolated network without external DNS or host mounts. Do not enable privileged mode or disable seccomp.

**Competition gate:** native amd64 ≥6 CPU / ≥8 GiB is only a minimum; validate generator headroom at 30k VU. Use an isolated disposable environment for hostile code, separate bounded import/build storage, a verified disk quota ≤1 GiB with fresh-volume/ENOSPC/deletion/crash tests, and a frozen `BENCH_HOST_PROFILE`. Never bypass these checks or replace competition disk storage with tmpfs. Freeze rules and rerun all participants after changes. The remote runner normalizes uncompressed TAR layer bytes and verifies hashes before admission. The legacy local CLI still uses Docker inspect Size; do not compare those profiles. No Google Cloud or Actions deployment is configured.

## Backup and update

Wait for a batch to finish and stop **only this scheduler** before changing code. Use SQLite's backup API rather than copying a live database without its WAL:

```sh
docker compose --env-file deploy/.env -f deploy/docker-compose.yml exec -T web \
  python -c "import sqlite3; s=sqlite3.connect('/data/leaderboard.sqlite3'); d=sqlite3.connect('/data/backup.sqlite3'); s.backup(d); print(d.execute('PRAGMA integrity_check').fetchone()[0]); d.close(); s.close()"
docker compose --env-file deploy/.env -f deploy/docker-compose.yml cp web:/data/backup.sqlite3 /root/vibe-zone-backup.sqlite3
```

Protect backups as private operator data and test restoration separately. Also back up `.env`, results and Caddy's certificate volume. Transfer a validated new image, then `up -d --no-build` with the same project name and volumes. Do not use `down -v` during upgrades. Retention is 64 completed batches / 1,000 runner reports; host disk quotas and backup rotation are separate operator tasks.

Pinned image digests improve repeatability but do not replace an advisory/SBOM scan. Python dependencies have exact versions; artifact hashes and current dependency vulnerability review remain pre-event work.

## Private checks and optional categories

Keep organizer materials outside this repository and Docker build context. Production mounts `/opt/vibe-zone-private/checks` read-only into the scheduler and only `/opt/vibe-zone-private/bonus.json` into web. Supply trusted `hooks.py` plus its helpers under checks and a UTF-8 JSON object with plain-text `pl` and `en` values for the bonus. Limit access to root and service UID/GID 10001 (directories 750, files 640). Web cannot read hidden tests. Do not mount private files into the participant container. The adapter accepts only status and aggregate counts; missing or broken checks report unavailable and never qualify AI Pilot.

The admin page controls features. Defaults: badges and hidden checks on, others off. Publishing the bonus copies the private bilingual text into the database once. Backups now include private materials, settings, pilot timing and vote hashes. Do not enable extras on the event board just to test them; use an isolated local database.

`VZ_RUNNER_ROLE=competition` is an operator assertion for an independently prepared competition runner, not a sandbox setting. Production fixes it to `smoke`; chaos additionally requires an exact allowlisted starter ID. Existing standard-profile infrastructure gates remain mandatory.

Build the smaller web image separately from the scheduler image:

```sh
docker buildx build --platform linux/amd64 --target web --load -t vibezone-web:local -f leaderboard/Dockerfile .
docker buildx build --platform linux/amd64 --target runner --load -t vibezone-leaderboard:local -f leaderboard/Dockerfile .
```

Transfer both images. Web omits the Docker CLI. Caddy already compresses with zstd/gzip; versioned static files now receive a one-year immutable cache header from web. HTML, API, forms and voting remain no-store and use no-transform to prevent edge script injection without relaxing CSP. See [Cloudflare automatic analytics injection](https://developers.cloudflare.com/web-analytics/get-started/). Edge-managed robots.txt remains under the operator's Cloudflare policy.

Materiały organizatora pozostają poza repo i kontekstem builda. Scheduler otrzymuje prywatne testy tylko do odczytu, web wyłącznie plik bonusu. Brak testów oznacza brak wyniku, nigdy zaliczenie. Kopie zapasowe obejmują także materiały prywatne, ustawienia, czasy pilotów i hashe głosów. Nie włączaj dodatków na publicznej tablicy tylko na potrzeby testu.
