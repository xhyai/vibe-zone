# Izolowany runner / Isolated runner

Leaderboard działa na Mikrusie. Kod uczestników wykonuje osobny host amd64 z Dockerem i gVisorem. Agent wysyła wyłącznie połączenia HTTPS do leaderboardu. Host nie udostępnia portu aplikacji ani API. Wymagany jest klucz SSH operatora.

## Konfiguracja i profile

Web: `VZ_REMOTE_RUNNER=1` i losowy `RUNNER_TOKEN` (minimum 32 znaki) w `deploy/.env`. Agent: ten sam token w `/etc/vibe-zone-runner.env`, tryb 600. Nie podawaj tokenu w URL, argumentach CLI ani logach.

```ini
LEADERBOARD_URL=https://devfest.atlasapx.pl
RUNNER_ADDRESS=event-2026
RUNNER_ROLE=official
RUNNER_TOKEN=GENERATE_A_RANDOM_PRIVATE_VALUE
RUNNER_STATE=/var/lib/vibe-zone-agent
VZ_PRIVATE_DIR=/opt/vibe-zone-private/checks
```

`RUNNER_ADDRESS` jest nazwą hosta w panelu, nie adresem, z którym web się łączy. Agent rejestruje zasoby, wersje i ostatni kontakt. Organizator wybiera aktywny host w `/admin`. Zmiana hosta lub profilu wymaga zakończenia albo anulowania bieżącego batcha. Rejestracja nowego hosta sama nie usuwa banera. Przełącznik banera domyślnie pozostaje włączony; wyłączenie wymaga dostępnego hosta `official`. Przy utracie kontaktu baner wraca.

| Profil | CPX22, rola test | Host oficjalny |
| --- | --- | --- |
| `full` | 1 slot; aplikacja 1 CPU / 2 GiB; k6 0,9 CPU / 256 MiB | 1 slot; aplikacja 1 CPU / 2 GiB; k6 4 CPU / 4 GiB |
| `parallel` | do 2 slotów; aplikacja 0,5 CPU / 1 GiB, k6 0,45 CPU / 256 MiB na slot | liczba slotów wyliczona z CPU/RAM; aplikacja 1 CPU / 1 GiB, k6 1 CPU / 256 MiB na slot |

Panel pozwala zmienić liczbę slotów, CPU/RAM aplikacji, rosnącą listę VU i czas narastania/pomiaru. Walidacja zostawia pamięć dla hosta i odrzuca przekroczenia budżetu. Pełny profil oficjalny wymaga minimum 6 CPU / 8 GiB i zachowuje poziomy 500...30000 VU. To minimum sprzętowe, nie gwarancja wydajności generatora. CPX22 jest wyłącznie do prób. Domyślnie wybieraj `full`; wstępna kalibracja hosta nie wykazała wzrostu łącznej przepustowości przy dwóch slotach.

Każdy batch zapisuje konfigurację i jej hash, obejmujący kod pomiarowy, narzędzia i wersje hosta. Ranking wybiera jeden profil; historia zachowuje wcześniejsze wyniki. Wynik to mediana trzech prób. Pokazujemy liczbę prób i zakres U. Przełączenie profilu wymaga ponownego pomiaru zespołów. Budowy/importy są szeregowane, a fala pomiarowa czeka na przygotowanie swoich obrazów. Nie uruchamiaj innych zadań na hoście podczas pomiarów.

## Zgłoszenia, izolacja i awarie

Organizator zatwierdza `ghcr.io/team/api@sha256:...`, Docker Hub z digestem albo `https://github.com/team/repo@FULL_COMMIT_SHA` (również GitLab). Rootowy Dockerfile ma pierwszeństwo; bez niego używany jest `starter/Dockerfile` z kontekstem `starter/`. Lokalne `sha256:...` służy sprawdzonym obrazom operatora na wybranym hoście. Nowa wersja wymaga nowego zatwierdzenia.

Obrazy przechodzą przez Skopeo w gVisorze. Repo buduje rootless BuildKit w jednorazowym gVisorze, z limitem 1 CPU, 1 GiB RAM, 512 PID, 10 minut na etap i 1 GiB dysku na źródła/cache/eksport. Build może wykonywać pliki wyłącznie na swoim dysku roboczym; `/data` pomiaru pozostaje `noexec`. Mapowanie UID BuildKit wymaga wewnętrznych helperów setuid: tylko builder ma `no-new-privileges=false` i standardowy zestaw capabilities wewnątrz gVisora. Nie otrzymuje host namespaces, socketu Docker ani sekretów. Instrukcje builda współdzielą przestrzeń procesów tego jednorazowego sandboxa.

Sieć builda jest wewnętrzna. Jedynym wyjściem jest proxy HTTPS CONNECT na porcie 443, z listą serwisów zależności w `deploy/runner/proxy.py`. Proxy odrzuca adresy prywatne i przypina rozwiązaną trasę. Nowy serwis dodaje operator przez `BUILD_ALLOWED_HOSTS`, po przeglądzie. Zależności HTTP i prywatne registry nie są obsługiwane. Skomplikowane buildy przekraczające 1 GiB wymagają przygotowania obrazu przez uczestnika. Nie zwiększaj limitów podczas konkursowego batcha.

Przed `docker load` importer ogranicza archiwum do 256 MiB, sprawdza platformę, hashe i rozpakowany rozmiar warstw poniżej 100 MB, odrzuca dodatkowe wolumeny i usuwa obce tagi. Nie wypakowuje ścieżek archiwum do hosta. Rozmiar jest taki sam dla klasycznego image store i containerd; `.Size` Dockera nie służy tu punktacji.

Jawny read-only mount `/tmp` zapobiega automatycznemu dodaniu zapisywalnego tmpfs przez runsc. `TMPDIR` i `HOME` wskazują `/data`, IPC jest wyłączone. Aplikacja zawsze używa `runsc`, UID/GID 10001, read-only rootfs, cap-drop ALL, no-new-privileges, limitów CPU/RAM/PID, zapisu wyłącznie do własnego dyskowego `/data` do 1 GiB. Każdy pomiar ma osobną sieć internal/isolated, bez IPv6 hosta, internetu i docker.sock. Nie ma fallbacku do runc. Sonda i k6 są przypiętymi narzędziami organizatora.

Dzierżawa trwa 90 s i jest odnawiana co 10 s. Po utracie łączności agent przerywa zadanie przed ponownym przydzieleniem. Maksymalnie trzy próby; błędy infrastruktury i cleanup blokują publikację batcha. Identyczny ponowny wynik jest idempotentny, a stary token dzierżawy nie może zastąpić nowego. Panel pokazuje zadania i prywatne logi. Agent zapisuje raporty w `RUNNER_STATE`. Odtworzenie po awarii usuwa tylko zasoby z zarezerwowanymi nazwami `vzr-*`, `vh-vzr-*`, `vibezone-job:*`; nie używa globalnego prune. Nie twórz pod tymi nazwami innych zasobów.

## Runbook dnia eventu

1. Wynajmij czysty, dedykowany host Ubuntu 26.04 amd64, minimum 6 CPU / 8 GiB i ponad 50 GiB wolnego dysku przed instalacją (40 GiB zostaje zarezerwowane). Wgraj klucz SSH. Nie zmieniaj publicznego serwera WWW.
2. Na komputerze operatora: aktualny czysty checkout i prywatny katalog `VibeZone-private/runner-host`, `.runner-token` zgodny z webem oraz prywatna implementacja Go i kontrole. Uruchom jedno polecenie z tego prywatnego katalogu:

   ```sh
   ./setup.sh --event root@NEW_HOST --address event-2026 --role official
   ```

   Wrapper wykonuje hardening, instaluje Docker/gVisor i dysk, sprawdza nową sesję SSH, przesyła kod, a następnie buduje narzędzia i obrazy referencyjne na runnerze. Następnie wykonuje krótką kalibrację i uruchamia usługę. `--skip-host-setup` jest tylko do aktualizacji przygotowanego hosta po zakończeniu batcha i zatrzymaniu usługi agenta.
3. Kalibracja diagnostyczna: trzy próby startera i Go w obu profilach, 10/50 VU, 2 s narastania i 5 s pomiaru, mały seed. Ma skończyć się przed 15 minutami. Raport w `/root/runner-integration/calibration-*/summary.json`; sprawdź zero błędów, rozrzut i sprzątanie. To test odtworzenia środowiska, nie potwierdzenie obsługi 30000 VU.
4. W `/admin` wybierz nowy host i `full`. Uruchom próbny batch z docelowym obciążeniem, sprawdź zapas generatora oraz identyczne profile wyników. Dopiero Kamil decyduje o wyłączeniu banera. Jeśli zmienisz limity, zmierz wszystkie zespoły ponownie.
5. Na Mikrusie pozostają web i Caddy. Wyłącz lokalny scheduler: `docker compose --env-file deploy/.env -f deploy/docker-compose.yml --profile runner stop scheduler`. Obrazy uczestników nigdy nie trafiają na Mikrusa.

## Utrzymanie i usunięcie hosta

Logi: `journalctl -u vibe-zone-runner`; stan: `systemctl status vibe-zone-runner`. Nie wypisuj pliku env. Raporty i logi buildów są prywatne. Przed aktualizacją poczekaj na batch, zrób SQLite backup API weba, zatrzymaj agenta, wgraj ten sam release do weba i runnera, ponów krótką kalibrację. Zmiana kernela, runsc lub kodu zmienia profil. Błąd cleanup wymaga inspekcji przed wznowieniem.

Na noc: anuluj batch w panelu, `systemctl stop vibe-zone-runner` na hoście. Start rano: `systemctl start vibe-zone-runner`. Samo zatrzymanie usługi nie zatrzymuje naliczania opłat za VPS.

Po wydarzeniu: anuluj bieżący batch w panelu, zatrzymaj usługę i skopiuj potrzebne raporty do prywatnego archiwum; usuń token runnera z obu środowisk albo obróć go dla pozostałych hostów. Usuń VPS w konsoli dostawcy dopiero po sprawdzeniu backupu. To usuwa dyski i kończy naliczanie opłat; nie wykonuje tego instalator.

## English

The Mikrus host serves only web/Caddy. The dedicated amd64 runner makes outbound HTTPS requests with `X-Runner-Token`; no inbound application port is required. Put the shared random token in the web env and `/etc/vibe-zone-runner.env`, both mode 600. Set the leaderboard URL, a unique `RUNNER_ADDRESS` label and `RUNNER_ROLE=test|official`. Registration does not activate a host or remove the test banner. The organizer selects an online host/profile in `/admin`; Kamil decides when to remove the banner. Offline or test hosts always show it.

`full` uses one slot. On CPX22: application 1 CPU/2 GiB, generator 0.9 CPU/256 MiB. `parallel` permits two scaled slots (0.5 CPU/1 GiB application, 0.45 CPU/256 MiB generator each). The official full profile preserves 500...30000 VU, 1 CPU/2 GiB application and a 4 CPU/4 GiB generator, with a minimum 6 CPU/8 GiB host. Official parallel capacity is derived from resources. Admin controls slots, application limits, VU levels and durations within the resource budget. Every batch freezes its profile/hash; ranking never combines different profiles. Median of three runs and U range remain visible.

Approve public image digests or a GitHub/GitLab repository URL followed by `@` and a full commit SHA. The builder uses the root Dockerfile, or `starter/Dockerfile` if absent. Rootless BuildKit runs inside a disposable gVisor sandbox: 1 CPU, 1 GiB RAM/disk, 512 PIDs, ten minutes per stage, no secrets or Docker socket. Only that builder permits internal UID-mapping helpers and an executable work disk. The application has stricter flags: runsc, UID 10001, read-only root, all capabilities dropped, no-new-privileges, writes only to fresh disk `/data` (`TMPDIR=/data`, `HOME=/data`, IPC disabled, explicit read-only `/tmp`). There is no runc fallback. Each measurement uses a separate internal network with host IPv6 disabled.

Build dependencies pass through an HTTPS allowlist proxy, with private destinations rejected. Add reviewed dependency domains through `BUILD_ALLOWED_HOSTS`. HTTP dependencies/private registries are unsupported; builds exceeding the disk budget must be prepared by participants. Imports have bounded archive parsing, platform/digest verification, canonical tags and normalized uncompressed layer sizes below 100 MB. Containers, temporary volumes and imported images are removed after jobs. Reviewed local reference images and pinned tools remain cached.

Leases last 90 seconds, renewed every ten seconds. Connection loss stops work before reassignment; retry count is bounded. Stale leases and conflicting replay are rejected. Identical completions are idempotent. Infrastructure or cleanup errors stop atomic publication. Logs are organizer-only. Reserved recovery prefixes are `vzr-*`, `vh-vzr-*` and `vibezone-job:*`; never use these for unrelated work.

**Event-day runbook:** provision a dedicated Ubuntu 26.04 amd64 host with at least 6 CPU/8 GiB, more than 50 GiB free disk before setup (40 GiB is reserved), and SSH keys. From the private `runner-host` directory run `./setup.sh --event root@NEW_HOST --address event-2026 --role official`. The wrapper hardens the host, checks another SSH connection, transfers the release and builds tools/reference images on the runner, calibrates and starts the agent. Prerequisites: clean release checkout, private token, checks and Go reference. For an existing host, finish/cancel its batch and stop the agent before using `--skip-host-setup`. The short three-run calibration in both profiles should finish within 15 minutes; it tests 10/50 VU, not the official capacity ceiling. Inspect its private JSON report, select the registered host in admin, then run the intended competition load before Kamil disables the banner. Stop the old Mikrus scheduler.

For overnight shutdown use `systemctl stop vibe-zone-runner`, and start it the next morning. Check `journalctl -u vibe-zone-runner` for failures. VPS billing continues while the service is stopped. Before updates, finish/cancel the batch, back up SQLite, stop the agent, deploy the same release to both machines and recalibrate. After the event archive private reports, revoke/rotate the runner token and delete the VPS through the provider console after verifying backups.
