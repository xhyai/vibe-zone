# Vibe Zone · Instrukcja uczestnika

[PL](PARTICIPANT_GUIDE.pl.md) · [EN](PARTICIPANT_GUIDE.md) · [Web](https://devfest.atlasapx.pl/start?lang=pl) · [API](API.md) · [Scoring / Punktacja](SCORING.md)

Wyniki na CPX22 są testowe. Buildy i pomiary wykonuje osobny runner gVisor; Mikrus udostępnia tylko tablicę. Oficjalny host i profil wybiera organizator w dniu wydarzenia.

## 1. Przygotuj narzędzia i fork

Potrzebujesz Git, Python 3.12+ oraz działającego Dockera z buildx. W GitHub kliknij Fork, a potem sklonuj swój fork. Wszystkie kolejne polecenia wykonuj w katalogu repozytorium. Zastąp YOUR_ACCOUNT swoją nazwą konta.

```sh
docker version
docker buildx version
git clone https://github.com/YOUR_ACCOUNT/vibe-zone.git
cd vibe-zone
```

## 2. Zbuduj i uruchom starter

Starter celowo jest wolny. Zachowaj API, możesz zmienić cały język i implementację. Serwer słucha na 0.0.0.0:8080, a SQLite zapisuje w /data. Poniższy kontener jest wyłącznie do lokalnej pracy z własnym kodem.

```sh
docker buildx build --platform linux/amd64 --load -t vibezone-starter:local starter
docker run -d --name vz-starter --platform linux/amd64 \
  --cpus 1 --memory 2g --memory-swap 2g --pids-limit 512 \
  -e PORT=8080 -e DATA_DIR=/data -p 127.0.0.1:8080:8080 \
  -v vz-starter-data:/data vibezone-starter:local
curl --fail http://localhost:8080/health
```

## 3. Sprawdź kontrakt API

Checker zapisuje dane testowe. Sprawdza walidację, feed, Unicode, widoczność zapisów oraz idempotencję polubień. Sam checker nie restartuje kontenera; pełną próbę SIGKILL i trwałości wykonuje smoke w następnym kroku.

```sh
python3 bench/tests/api_check.py http://localhost:8080
```

## 4. Uruchom lokalny smoke bench

Najpierw pobierz przypięte narzędzia. Runner niczego sam nie pobiera. Smoke wykonuje trzy pomiary 2/5/10 VU, sprawdza dane po SIGKILL, zapisuje JSON w results/ i sprząta własne kontenery. Używa 256 MiB RAM i /data na tmpfs: to test działania, nie wynik konkursowy ani test trwałości po restarcie hosta. Przy kolizji podsieci ustaw BENCH_SUBNET na wolny zakres. Docker musi obsługiwać tryb sieci isolated.

```sh
docker pull --platform linux/amd64 python@sha256:c5ba3deef00c7758561a0f03c26f44f10589263d40e732b8748e0d1f38f6dd89
docker pull --platform linux/amd64 grafana/k6@sha256:f21270290d702cbf0a7d6ba5d7ed100b63bcb233b558b885ed787547b3488279
bench/run_bench.sh vibezone-starter:local --pull never --smoke
```

## 5. Optymalizuj na podstawie pomiarów

Sprawdź plany zapytań, liczbę odczytów SQLite, koszt połączeń i blokad oraz rozmiar warstw obrazu. Zmieniaj jedną rzecz naraz i powtarzaj testy. Nie usuwaj walidacji ani gwarancji trwałości; wszystkie potwierdzone zapisy muszą przetrwać SIGKILL. Nie hardkoduj odpowiedzi ani danych benchmarku.

## 6. Sprawdź rozmiar i platformę

Obraz musi mieć mniej niż 100 000 000 B bez kompresji; cel to mniej niż 50 MB. Zdalny runner sumuje nieskompresowane warstwy TAR i sprawdza ich hashe. Pole Size w lokalnym inspect jest orientacyjne i zależy od image store. Odznaka za obraz poniżej 50 MB nie dodaje U.

```sh
docker image inspect vibezone-starter:local --format '{{.Os}}/{{.Architecture}} {{.Size}} bytes'
```

## 7. Opublikuj publiczny obraz linux/amd64

Wybierz GHCR albo Docker Hub i zastąp YOUR_ACCOUNT nazwą konta. Zaloguj się lokalnie; GHCR wymaga uprawnień do publikowania pakietów. Ustaw pakiet jako publiczny i sprawdź anonimowy pull. Nie dodawaj tokenów do repozytorium. Po każdej zmianie kodu przebuduj obraz i powtórz testy przed publikacją.

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

## 8. Zgłoś i zachowaj kod zespołu

Poproś organizatora o jednorazowe zaproszenie. W formularzu podaj nazwę zespołu, zaproszenie i publiczny obraz. Kod zespołu zobaczysz tylko raz: zapisz go prywatnie. Przy aktualizacji użyj tej samej nazwy i kodu, bez nowego zaproszenia. Każda wersja wymaga zatwierdzenia digestu obrazu albo pełnego commita repozytorium. Runner pobiera lub buduje tylko tę wersję w izolacji. Zmiana taga nie aktualizuje zatwierdzonego zgłoszenia. Batch startuje co 15 minut i publikuje wynik po wszystkich trzech pomiarach; może trwać dłużej niż kwadrans.

## 9. Sprawdź typowe błędy

Port: nasłuchuj na 0.0.0.0 i PORT=8080, nie tylko na localhost. Dane: używaj DATA_DIR=/data; rootfs jest tylko do odczytu, proces ma UID/GID 10001. Sygnały: przekaż SIGTERM do serwera (exec w entrypoincie) i poprawnie zakończ proces. Trwałość: poprawne zamknięcie nie wystarczy - potwierdzone posty i like muszą przetrwać także SIGKILL. Nie polegaj na pamięci procesu. /health musi zwrócić poprawny JSON w 30 sekund. Konflikt nazwy lokalnego kontenera: zatrzymaj i usuń tylko własny vz-starter; wolumen z danymi pozostaje.

```sh
docker stop vz-starter
docker rm vz-starter
```

[Submit / Zgłoś](https://devfest.atlasapx.pl/submit?lang=pl) · [Leaderboard](https://devfest.atlasapx.pl/leaderboard?lang=pl)
