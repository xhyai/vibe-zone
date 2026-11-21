# Deployment record / Wdrożenie

The public site at https://devfest.atlasapx.pl runs on Mikrus. Images are built locally for `linux/amd64` and transferred over SSH. No GitHub Actions or Google Cloud are used. / Obrazy budujemy lokalnie dla `linux/amd64` i przesyłamy przez SSH na Mikrusa.

The host accepts only the reviewed organizer starter in smoke mode. Participant images require separate isolated infrastructure. The web service has no Docker socket; the scheduler has Docker authority and enforces an immutable image allowlist. / Ten host uruchamia wyłącznie sprawdzony starter w trybie smoke. Web nie ma socketu Dockera. Scheduler sprawdza listę dozwolonych ID obrazów.

| Service / Usługa | Limit |
| --- | --- |
| Web, scheduler, Caddy (each / każdy) | 192 MiB RAM, no swap, 1 CPU, 128 PIDs |
| Smoke SUT | 256 MiB RAM, 1 CPU, 64 MiB data tmpfs |
| Smoke generator | 192 MiB RAM, 1 CPU |
| Smoke probe / sonda | 96 MiB RAM, 0.5 CPU |

Cloudflare provides public HTTPS. Caddy uses an internal certificate; the current origin TLS mode is Full, not Full (strict). The firewall allows public web traffic from Cloudflare IPv6 ranges. Updates must preserve these rules and existing services. [Deployment procedure](../deploy/README.md).

Cloudflare zapewnia publiczne HTTPS. Caddy korzysta z certyfikatu wewnętrznego, w trybie Full. Przed konkursem należy przejść na Full (strict). Aktualizacje zachowują reguły zapory i inne usługi.

Initial release checks included API and crash durability, three-repetition smoke, bilingual browser flows, CSRF/XSS checks, local secret scans, SQLite backup restoration and public HTTPS verification. Smoke is diagnostic and does not qualify competition infrastructure. / Pierwsze wydanie sprawdzono testami API, trwałości po SIGKILL, smoke, PL/EN, CSRF/XSS, skanem sekretów i odtworzeniem SQLite.

Before competition / Przed konkursem:

- Prepare isolated build/import/run hosts and verified bounded disk storage. / Przygotować izolację i dysk z limitem.
- Calibrate native amd64 runners and generator headroom. / Skalibrować runnery i generator.
- Freeze image-size accounting, versions and host profile. / Ujednolicić rozmiary obrazów, wersje i profil hosta.
- Verify off-host backups, TLS Full (strict), and finalist source/image correspondence. / Sprawdzić kopie poza VPS, TLS i zgodność obrazów ze źródłami.

[AUDIT.md](AUDIT.md) records the remaining security gates; [SCORING.md](SCORING.md) defines admission and ranking.
