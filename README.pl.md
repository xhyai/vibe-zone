# Vibe Zone · DevFest Warsaw 2026

**Sobota, 21 listopada 2026 · Google for Startups Campus, Plac Konesera 10, Warszawa.**

Przyspiesz mini social API: `GET /feed`, `POST /posts`, `POST /like`, oparte na SQLite. Dowolny język, solo lub zespół, AI mile widziane. Zasoby konkursowe: **1 vCPU / 2 GiB RAM**, obraz **<100 MB bez kompresji**, cel **<50 MB**.

[Strona główna](https://devfest.atlasapx.pl/?lang=pl) · [Jak zacząć](https://devfest.atlasapx.pl/start?lang=pl) · [Ranking](https://devfest.atlasapx.pl/leaderboard?lang=pl) · [Zgłoś](https://devfest.atlasapx.pl/submit?lang=pl) · [Ekran strefy](https://devfest.atlasapx.pl/screen?lang=pl) · [English](README.md)

Wyniki na CPX22 są testowe. Buildy i pomiary wykonuje osobny runner gVisor; Mikrus udostępnia tylko tablicę. Oficjalny host i profil wybiera organizator w dniu wydarzenia. [Instrukcja runnera](deploy/RUNNER.md).

## Quickstart w pięciu komendach

Najpierw zrób fork i sklonuj repozytorium. W jego katalogu głównym, z uruchomionym Dockerem/buildx i Pythonem 3.12+:

```sh
docker buildx build --platform linux/amd64 --load -t vibezone-starter:local starter
docker pull --platform linux/amd64 python@sha256:c5ba3deef00c7758561a0f03c26f44f10589263d40e732b8748e0d1f38f6dd89
docker pull --platform linux/amd64 grafana/k6@sha256:f21270290d702cbf0a7d6ba5d7ed100b63bcb233b558b885ed787547b3488279
bench/run_bench.sh vibezone-starter:local --pull never --smoke
docker image inspect vibezone-starter:local --format '{{.Os}}/{{.Architecture}} {{.Size}} bytes'
```

Smoke sprawdza kontrakt API, losowe dane, współbieżne polubienia i trwałość po wymuszonym restarcie, a potem wykonuje trzy krótkie pomiary. Raporty trafiają do `results/`. Smoke używa 256 MiB RAM i ograniczonego tmpfs; nie sprawdza trwałości dysku po restarcie hosta i nie daje wyników konkursowych. Przy kolizji sieci Dockera ustaw wolny `BENCH_SUBNET`.

## Dołącz krok po kroku

[Instrukcja uczestnika](docs/PARTICIPANT_GUIDE.pl.md) opisuje lokalny start, testy kontraktu, optymalizację, rozmiar obrazu, publikację na GHCR/Docker Hub i typowe błędy. Poproś organizatora o jednorazowe zaproszenie, a potem zgłoś publiczny obraz `linux/amd64`. Zapisz prywatny kod zespołu do aktualizacji. Każda wersja wymaga zatwierdzenia digestu obrazu albo commita repozytorium; zdalny runner przygotowuje ją w ograniczonym sandboxie.

Start batcha jest planowany co 15 minut: trzy pomiary każdego obrazu, publikacja całej serii naraz. Wygrywa najwyższa mediana zaliczonego poziomu VU, przy **0% błędów i p99 <500 ms**. Remis: mniejszy obraz, niższa mediana startu, niższe p99 przy tym poziomie; identyczne wyniki dzielą miejsce. Obowiązują [pełne zasady punktacji](docs/SCORING.md), w tym znormalizowany rozmiar obrazu i oddzielne profile wyników.

## Struktura repozytorium

| Ścieżka | Zawartość |
| --- | --- |
| [starter/](starter/README.md) | Celowo wolny baseline Python / Flask / SQLite |
| [bench/](bench/README.md) | Kontrakt, trwałość, k6 i wyniki JSON |
| [leaderboard/](leaderboard/README.md) | Strona PL/EN, ranking, zgłoszenia, historia zespołu i admin |
| [deploy/](deploy/README.md) | Lokalne buildy i wdrożenie na Mikrusie |
| [docs/](docs/PARTICIPANT_GUIDE.pl.md) | Instrukcja uczestnika, [API](docs/API.md), [punktacja](docs/SCORING.md), [audyt](docs/AUDIT.md), [raport strony](docs/HOMEPAGE.md) |

[Współpraca i testy lokalne](CONTRIBUTING.md) · [Weryfikacja wydania](docs/RELEASE.md) · [Licencja MIT](LICENSE)

Bez GitHub Actions i Google Cloud. Web nie ma socketu Dockera, a scheduler wykonuje tylko zatwierdzone lokalne obrazy o niezmiennym ID. Przed przygotowaniem infrastruktury konkursowej przeczytaj audyt.

Osobne odznaki obejmują obraz <50 MB, start, p99 i nietypowy język zweryfikowany przez organizatora. Ukryte testy pokazują tylko liczbę zaliczeń. Opcjonalnie: Survivor, ogłaszany Bonus, prompt logi, [głosowanie](https://devfest.atlasapx.pl/vote?lang=pl) i [AI Pilot](https://devfest.atlasapx.pl/start?lang=pl#ai-pilot) dla osób nieprogramujących. Dodatki nie zmieniają U. [Zasady kategorii](docs/SCORING.md#extra-categories--dodatkowe-kategorie).
