# Contributing / Współpraca

## English

Keep changes focused. Read [API](docs/API.md), [scoring](docs/SCORING.md) and [audit](docs/AUDIT.md) before changing behavior. Do not silently change competition rules or relax isolation. Participant implementations belong in your fork; optimization of the deliberately slow starter is the challenge.

Use Python 3.12+, Docker/buildx and, for browser checks, Playwright with Chromium. Install local Python dependencies in a virtual environment. Add all public text to `leaderboard/translations.json`; keep PL/EN, keyboard navigation, CSP, CSRF and escaping intact. Do not add remote fonts or inline scripts. `/start` uses `leaderboard/content.py`; update both participant guides when editing its instructions.

## Polski

Wprowadzaj zmiany o jednym celu. Przeczytaj [API](docs/API.md), [punktację](docs/SCORING.md) i [audyt](docs/AUDIT.md). Nie zmieniaj po cichu reguł konkursu ani izolacji. Rozwiązanie uczestnika rozwijaj we własnym forku; optymalizacja celowo wolnego startera jest zadaniem konkursowym.

Wymagane: Python 3.12+, Docker/buildx, a do testów przeglądarki Playwright z Chromium. Lokalne zależności instaluj w virtualenv. Teksty dodawaj do `leaderboard/translations.json`. Zachowaj PL/EN, obsługę klawiatury, CSP, CSRF i escapowanie. Bez zewnętrznych fontów i skryptów inline. Treść `/start` pochodzi z `leaderboard/content.py`; zmiany instrukcji nanieś też w obu poradnikach uczestnika.

## Local checks / Testy lokalne

```sh
python3 -m unittest discover -s bench/tests -v
node --check leaderboard/static/board.js
python3 -m bench.tests.homepage_ui
python3 -m bench.tests.extras_ui
# Build the exact deployment target locally / Zbuduj lokalnie platformę wdrożenia.
docker buildx build --platform linux/amd64 --load -t vibezone-leaderboard:local -f leaderboard/Dockerfile .
docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges \
  --user 10001:10001 --cpus 2 --memory 512m --memory-swap 512m --pids-limit 64 \
  --tmpfs /tmp:rw,nosuid,nodev,noexec,size=32m vibezone-leaderboard:local \
  python -m unittest discover -s bench/tests -v
git diff --check
gitleaks git --redact
```

The page matrix uses a temporary synthetic database, never the event database. It covers six pages, PL/EN, 1440 px desktop, 390 px mobile and a 1920 px event screen. Screenshots go to ignored `artifacts/homepage/ui/`. The read-only `--url` mode can verify a deployed smoke board with an existing ranked team. / Macierz stron używa tymczasowej bazy testowej. Obejmuje sześć stron w PL/EN na desktopie 1440 px, telefonie 390 px i ekran strefy 1920 px. Opcja `--url` sprawdza wdrożoną tablicę smoke tylko do odczytu; wymaga istniejącego zespołu w rankingu.

The full submission/approval/two-batch E2E is documented in [bench/README.md](bench/README.md); run it only against a separate local stack. / Pełny E2E zgłoszenia, zatwierdzenia i dwóch batchy opisuje [bench/README.md](bench/README.md). Uruchamiaj go tylko na osobnym lokalnym środowisku.

Build and test locally: **no GitHub Actions, no Google Cloud**. Never commit credentials, databases, raw runner reports or build output. Publish only when explicitly authorized. Deployment and backups: [deploy/README.md](deploy/README.md). / Buduj i testuj lokalnie: **bez GitHub Actions i Google Cloud**. Nie commituj sekretów, baz danych, surowych raportów ani artefaktów builda. Publikuj tylko na wyraźne polecenie. Deploy i backupy: [deploy/README.md](deploy/README.md).
