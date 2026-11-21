# Leaderboard

Flask + SQLite, formularz, `/screen`, odświeżanie co 10 s, panel `/admin` z Basic Auth i CSRF. Aktualne zasady: [SCORING.md](../docs/SCORING.md); ryzyka wdrożenia: [AUDIT.md](../docs/AUDIT.md).

## Zdalny runner / Remote runner

Wdrożenie konkursowe korzysta z [transportu pull i gVisora](../deploy/RUNNER.md). Web przyjmuje rejestrację/claim/heartbeat/complete z tokenem, bez dostępu do Dockera. Admin wybiera aktywny host, profil i obciążenie oraz zatwierdza digest obrazu lub commit repo. Każdy batch ma niezmienny profil; ranking pokazuje wyłącznie porównywalne wyniki. Baner domyślnie zostaje. Mikrus nie wykonuje obrazów uczestników.

The competition deployment uses the [outbound gVisor runner](../deploy/RUNNER.md). The admin chooses host/load/profile and approves immutable images or repository commits. The web has no Docker authority. Leases, bounded retries and atomic publication protect results. Profiles are ranked separately and the test banner stays on by default.

The following local scheduler instructions are for trusted smoke testing. / Poniższy lokalny scheduler służy testom smoke zaufanych obrazów.

## Lokalnie

```sh
cp leaderboard/.env.example leaderboard/.env
# Ustaw osobne losowe SECRET_KEY (min. 32 znaki) i ADMIN_PASSWORD (min. 20 znaków).
# BENCH_SMOKE=1 tylko dla prób lokalnych; przykładowe hasła są odrzucane.
docker compose --env-file leaderboard/.env -f leaderboard/docker-compose.yml up -d --build
# Włączenie schedulera wymaga jawnego profilu i przygotowanych obrazów:
docker compose --env-file leaderboard/.env -f leaderboard/docker-compose.yml --profile runner up -d --build
```

Adres: <http://localhost:8081>, ekran `/screen`, admin `/admin` (login `admin`). Port jest związany z loopback. `DOCKER_GID` musi odpowiadać grupie socketu. Web nie otrzymuje socketu; scheduler ma przez niego uprawnienia administracyjne do hosta i nie powinien działać na publicznym VPS wraz z niezaufanym kodem.

## Obsługa zgłoszeń

1. Admin wydaje jednorazowe zaproszenie i przekazuje je jednemu zespołowi. Pokazywane jest raz, w bazie pozostaje hash.
2. Zespół podaje zaproszenie przy pierwszym zgłoszeniu. Otrzymuje osobny kod do kolejnych wersji swojej nazwy; w bazie również przechowywany jest tylko hash.
3. Publiczny obraz/repo jest informacją dla organizatora. Scheduler **nie pobiera źródeł, nie buduje Dockerfile i nie odświeża tagów**.
4. Organizator przygotowuje obraz na osobnej izolowanej maszynie, weryfikuje pochodzenie i limity, importuje na przygotowany worker i wpisuje w panelu pełne lokalne ID `sha256:...`. Web sam nie może sprawdzić Dockera; scheduler weryfikuje ID, platformę, rozmiar i konfigurację przed startem.
5. Nowa wersja zgłoszenia wymaga ponownego zatwierdzenia. Panel pokazuje najnowsze zgłoszenie każdego zespołu, aby historia nie zasłaniała oczekujących.

Limity: 200 zaproszeń/zespołów, 100 wersji na zespół, 5000 zgłoszeń łącznie, 30 prób przesłania formularza na minutę dla całej instancji. Kwoty są wspólne między workerami, transakcyjne w SQLite. Zaproszenie zużywa się atomowo z utworzeniem zespołu. Publiczne anonimowe tworzenie zespołów jest wyłączone.

Logowanie bez wcześniej potwierdzonej sesji admina: maksymalnie 20 prób/min globalnie, sprawdzane **przed** weryfikacją hasła. Poprawna sesja admina przez pięć minut nie korzysta z tej puli, ale nadal wymaga poprawnego Basic Auth. Globalny limit może opóźnić pierwsze logowanie podczas ataku; przed wydarzeniem ogranicz admina dodatkowo na zaufanym proxy/VPN. Nie ufamy klientowemu `X-Forwarded-For` jako kluczowi limitu.

## Scheduler i publikacja

O :00/:15/:30/:45 scheduler bierze snapshot najnowszych nieukrytych zgłoszeń. Kolejność to stałe ID zespołu, a nie ID ostatniej aktualizacji. Zatwierdzone obrazy są mierzone sekwencyjnie po trzy razy. Nie ma cache między batchami; identyczne ID mogą współdzielić wynik w tym samym batchu. Algorytm i remisy opisuje SCORING.md.

Publikacja jest atomowa. Podczas pomiaru widoczny jest poprzedni pełny ranking. Rozpoznany błąd infrastruktury albo niepełne sprzątanie zatrzymują publikację. Ręczny przycisk kolejkuje najwyżej jeden kolejny batch. Flock, transakcyjny stan batcha oraz blokada daemonu chronią przed nakładaniem testów. Długie batche pomijają minione kwadranse.

Ukrycie usuwa zgłoszenie z tablicy i przyszłych pomiarów, bez przywracania starszej wersji. Nie jest banem zespołu. Rozpoczęty już pomiar może się skończyć, ale ukryty wynik nie zostanie pokazany. Nowa wersja znów wymaga zatwierdzenia.

Baza zachowuje ostatnie 64 zakończone batche; scheduler zachowuje do 1000 najnowszych własnych plików raportów JSON. Starszą historię operator powinien archiwizować przed usunięciem. Raport mediany zawiera poziomy wszystkich trzech prób, więc nie zależy wyłącznie od surowych plików k6.

`down` zachowuje produkcyjne named volumes; `down -v` usuwa dane. Kopie wykonuj przez SQLite backup API wraz z prywatnym archiwum wyników i testem odtworzenia. Po awarii sprawdź pozostałe zasoby `vibezone.run` oraz blokadę daemonu przed wznowieniem. Nie publikuj sekretów, kodów ani pliku `.env`.

## PL / EN i wydanie testowe

Interfejs, `/screen`, formularz i błędy wybierają PL/EN z `Accept-Language` (także pl-PL/en-US). Przełącznik zapisuje wybór w sesji; odświeżanie API zachowuje język strony. `BENCH_SMOKE=1` dodaje widoczny baner wyników testowych także przed pierwszym batchem. `BENCH_ALLOWED_IMAGE_IDS` ogranicza zatwierdzanie i wykonywanie do wskazanych pełnych ID; na wdrożeniu Mikrus pole jest obowiązkowe i zawiera tylko starter organizatora. Szczegóły: [deploy](../deploy/README.md).

The board, screen, form and errors support PL/EN, selected from browser language or the remembered switch. Smoke mode always displays a test-results banner. Production disables this local scheduler and uses the separate isolated pull runner. See the English [participant guide](../README.md) and [deployment guide](../deploy/README.md).

## Public pages / Strony publiczne

`/` introduces the challenge; `/leaderboard` contains the live ranking; `/submit` contains the existing form (POST `/submissions` remains compatible); `/start` has the participant guide; `/team/<id>` shows public measurement history. `/screen` remains the live event view. All pages support `?lang=pl` and `?lang=en`. / `/` przedstawia challenge, `/leaderboard` to ranking, `/submit` to formularz, `/start` to instrukcja, `/team/<id>` to historia zespołu, a `/screen` pozostaje ekranem wydarzenia. Wszystkie strony obsługują PL/EN.

Public history excludes hidden submissions and teams whose latest submission was hidden. History is limited by the existing 64-batch retention. Statistics are derived from existing reports; scoring and the runner are unchanged. / Historia nie ujawnia ukrytych zgłoszeń ani zespołów z ukrytą najnowszą wersją. Obowiązuje istniejąca retencja 64 batchy. Statystyki pochodzą z istniejących raportów; scoring i runner pozostają bez zmian.

## Dodatki / Extras

Panel przełącza odznaki, prywatne kontrole, chaos, prompt logi, głosowanie, bonus i AI Pilot. Domyślnie aktywne są tylko odznaki i ukryte testy. Bez prywatnego pakietu testy zgłoszą brak wyniku. Własny lokalny plik Compose override może zamontować prywatne kontrole tylko do schedulera, a plik bonusu tylko do weba. Ścieżki i uprawnienia: [deploy/README.md](../deploy/README.md#private-checks-and-optional-categories). Nigdy nie dodawaj tych plików do repo ani obrazu.

The admin controls separate categories, checks, voting and announcements. Missing private tooling reports unavailable. Use a private local Compose override to mount checks into the scheduler and only the bonus file into web. Web uses a smaller image without Docker CLI. The scheduler has its own build target. [Category rules](../docs/SCORING.md#extra-categories--dodatkowe-kategorie).
