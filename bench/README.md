# Runner CLI and isolated pull agent

`bench/runner.py` remains the local CLI for trusted images. `bench/pull_agent.py` is the host-only outbound agent; `remote_job.py` executes approved jobs through gVisor and bounded storage. Do not run the agent on the web host. Installation, profiles, image normalization and event-day calibration: [RUNNER.md (PL/EN)](../deploy/RUNNER.md).

CLI jest do lokalnych testów zaufanych obrazów. Agent pull działa wyłącznie na osobnym hoście. Nie zmienia reguł lokalnego smoke; wyników różnych profili nie porównujemy.

# Testy i benchmark

Aktualne zasady uczestnika i dokładny algorytm: [SCORING.md](../docs/SCORING.md). Granice bezpieczeństwa i warunki uruchomienia publicznego: [AUDIT.md](../docs/AUDIT.md).

## Uruchomienie lokalne

Python 3.12+, Docker Engine obsługujący wewnętrzną sieć z `gateway_mode_ipv4=isolated` oraz wcześniej przygotowane obrazy. Runner **nie wykonuje pull, clone ani build**, również dla narzędzi. Pobranie/przygotowanie obrazu to osobny krok operatora na izolowanej maszynie, z limitem dysku, RAM, CPU i czasu; limit końcowego obrazu nie ogranicza kosztu jego pobrania/budowy.

```sh
# Trzy diagnostyczne pomiary lokalnego obrazu; bez internetu.
BENCH_SUBNET=10.231.42.0/24 bench/run_bench.sh vibezone-starter:local --pull never --smoke
```

Domyślny katalog raportów to `results/`; można podać `--results PATH`. `--pull never` jest jedyną dozwoloną polityką. Adres repo jest rozpoznawany, ale runner odmawia jego budowania. Lokalny CLI przyjmuje nazwę/ID lokalnego obrazu; scheduler przyjmuje wyłącznie ID zatwierdzone przez administratora.

Narzędzia muszą być dostępne lokalnie pod digestami z `runner.py`:

- `python@sha256:c5ba3deef00c7758561a0f03c26f44f10589263d40e732b8748e0d1f38f6dd89`;
- `grafana/k6@sha256:f21270290d702cbf0a7d6ba5d7ed100b63bcb233b558b885ed787547b3488279`.

Zmiana narzędzi wymaga zmiany kodu, walidacji i nowego profilu. Nie ma automatycznego pobierania po braku obrazu ani podmiany przez zmienne środowiskowe.

## Standard a smoke

Standard wymaga natywnego amd64, co najmniej 6 CPU / 8 GiB całego hosta, stałego `BENCH_HOST_PROFILE` i ograniczonego dyskowego `/data`. Te zasoby są tylko minimum; 30k VU może wymagać znacznie więcej. Generator jest ograniczony do 4 CPU / 4 GiB. Nie ma zdalnej kolejki ani oddzielnej VM generatora w tej implementacji.

`BENCH_VOLUME_DRIVER` i `BENCH_VOLUME_OPTIONS` (obiekt JSON) wybierają wcześniej przygotowany, zaufany backend wolumenów. Backend musi tworzyć **świeży, pusty, zapisywalny dla UID 10001 system plików o twardym limicie ≤1 GiB** i usuwać dane wraz z wolumenem. `statvfs` ma pokazywać ograniczoną pojemność; quota projektowa raportująca pojemność całego dużego dysku zostanie odrzucona. Operator musi osobno potwierdzić ENOSPC i semantykę usuwania/odtwarzania. Nie wystarczy deklaracja limitu w env. Zwykły local volume bez limitu i tmpfs w standardzie są niedopuszczalne. Brak konfiguracji zatrzymuje pomiar.

Smoke korzysta z local volume na tmpfs 64 MiB, podtrzymywanego przez sondę przez cały przebieg. Dane przetrwają SIGKILL/restart SUT, lecz nie koniec przebiegu ani restart hosta. Smoke nie jest testem wydajności/trwałości dysku i nie jest wynikiem konkursowym. Od wydania 16.11 SUT ma 256 MiB RAM, k6 192 MiB / 1 CPU (`GOMAXPROCS=1`), sonda 96 MiB / 0,5 CPU; zasady standardu nie zmieniają się.

| Zmienna | Znaczenie |
| --- | --- |
| `BENCH_SUBNET` | Wolna podsieć, np. `10.231.42.0/24` |
| `BENCH_HOST_PROFILE` | Identyfikator zamrożonego sprzętu/dysku i konfiguracji standardu |
| `BENCH_VOLUME_DRIVER` | Zaufany driver dyskowego wolumenu z ograniczoną pojemnością |
| `BENCH_VOLUME_OPTIONS` | Opcje tego drivera jako JSON; brak domyślnych opcji produkcyjnych |
| `RESULTS_DIR` | Katalog raportów |
| `BENCH_SEED` | Wewnętrzny sekret danych wspólny dla batcha; scheduler losuje nowy |

## Raporty i diagnostyka

`<id>.json` opisuje pojedynczy przebieg; `<pierwszy-id>-median.json` zawiera agregację trzech prób i ich poziomy w `repetitions`. Surowe podsumowania k6: `<id>-k6-<VU>.json`. `passed` dopuszcza U=0. `failed` zawiera błąd; `error_kind=infrastructure` oznacza brak wiarygodnego pomiaru i zatrzymuje publikację całego batcha. Błąd bramki uczestnika nie zatrzymuje pomiaru pozostałych zespołów.

Seed ma limit 3600 s, cały przebieg standard 5400 s, smoke 600 s. Trzy powtórzenia mogą więc trwać odpowiednio ponad cztery godziny albo pół godziny w skrajnym przypadku. Interwał schedulera 15 min **nie jest SLA ukończenia**. Baseline z celowym skanowaniem likes może bardzo długo seedować 100k rekordów. Przed wydarzeniem trzeba zmierzyć czas batcha na docelowym sprzęcie i ograniczyć liczbę dopuszczanych równocześnie wersji.

Generator mierzy również własny throttling CPU. Brak OOM/throttlingu nie dowodzi braku szumu lub przeciążenia hosta. K6 ogranicza pamięć całego kontenera; kontrola długości odpowiedzi w JS odbywa się po jej pobraniu i nie jest limitem transferu. Klient Python ma twardy limit 128 KiB, brak redirectów i całkowity deadline obejmujący nagłówki oraz ciało.

## Testy

```sh
docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges \
  --user 10001:10001 --cpus 2 --memory 512m --memory-swap 512m --pids-limit 64 \
  --ulimit fsize=2097152:2097152 --ulimit core=0 --tmpfs /tmp:rw,nosuid,nodev,noexec,size=32m \
  vibezone-leaderboard:local python -m unittest discover -s bench/tests -v
python3 bench/tests/api_check.py http://localhost:8080
```

Testy HTTP modyfikują dane wskazanego API. Fazy `--phase prepare` i `--phase verify` należy rozdzielić rzeczywistym SIGKILL/restartem; tryb `all` sam nie restartuje serwera. Runner robi to automatycznie przed i po obciążeniu.

Test UI wymaga lokalnego Playwright/Chromium, osobnej instancji testowej z `BENCH_SMOKE=1`, działającego schedulera i wcześniej załadowanego baseline:

```sh
python3 bench/tests/ui_check.py --url http://localhost:8081 \
  --env-file leaderboard/.env --image-id sha256:PELNE_64_ZNAKI_HEX
```

Sprawdza zaproszenie, kod zespołu, zatwierdzenie obrazu, dwa rzeczywiste batche po trzy pomiary, brak cache między batchami, ukrycie, odświeżanie, mobile i kiosk. Zostawia dane testowe; nie kieruj go do tablicy wydarzenia.

## Sprzątanie

Runner usuwa tylko własne kontenery (`rm -fv`), wolumen i sieć. Kontener `vibezone-benchmark-lock` jest blokadą całego daemonu dla równoległych CLI/schedulerów. Niepełne sprzątanie zatrzymuje publikację i zachowuje blokadę. Po awarii operator sprawdza zasoby konkretnego `vibezone.run=<id>`, usuwa wyłącznie zweryfikowane pozostałości i dopiero potem zwalnia blokadę. Nie wykonuj globalnego `prune` i nie usuwaj blokady aktywnego testu.

## Optional checks / Testy dodatkowe

The scheduler snapshots extra settings once per batch. `VZ_EXTRAS_FLAGS` selects `hidden`, `chaos`, `bonus`; `VZ_PRIVATE_DIR` points to trusted operator checks outside the public repository. They run after the third public measurement and cannot modify U. No private checks are bundled in the image. Only aggregate counts/status are returned. Private exceptions are not published. / Scheduler pobiera ustawienia raz na batch. Prywatne testy działają po trzecim pomiarze i nie zmieniają U. Obraz publiczny ich nie zawiera; wyjątki nie są publikowane.

Chaos requires an isolated competition runner or the exact allowlisted starter in smoke. AI Pilot uses public plus hidden smoke checks, with an additional smoke series when the main batch uses standard. / Chaos wymaga runnera konkursowego albo dozwolonego startera w smoke. AI Pilot wymaga publicznych i ukrytych testów smoke.

Run `python3 -m bench.tests.extras_ui` for bilingual organizer/voting interactions. `test_text.py` checks tracked source punctuation, preserving intentional UTF-8 test data. / Dodatkowy test Playwright sprawdza panel i głosowanie. Lint tekstu obejmuje pliki śledzone, pozostawiając celowe dane UTF-8.
