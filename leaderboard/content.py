"""Participant guide content; translated through the shared catalog."""
GUIDE = [{'title': 'Przygotuj narzędzia i fork',
  'body': 'Potrzebujesz Git, Python 3.12+ oraz działającego Dockera z buildx. W GitHub kliknij '
          'Fork, a potem sklonuj swój fork. Wszystkie kolejne polecenia wykonuj w katalogu '
          'repozytorium. Zastąp YOUR_ACCOUNT swoją nazwą konta.',
  'code': 'docker version\n'
          'docker buildx version\n'
          'git clone https://github.com/YOUR_ACCOUNT/vibe-zone.git\n'
          'cd vibe-zone'},
 {'title': 'Zbuduj i uruchom starter',
  'body': 'Starter celowo jest wolny. Zachowaj API, możesz zmienić cały język i implementację. '
          'Serwer słucha na 0.0.0.0:8080, a SQLite zapisuje w /data. Poniższy kontener jest '
          'wyłącznie do lokalnej pracy z własnym kodem.',
  'code': 'docker buildx build --platform linux/amd64 --load -t vibezone-starter:local starter\n'
          'docker run -d --name vz-starter --platform linux/amd64 \\\n'
          '  --cpus 1 --memory 2g --memory-swap 2g --pids-limit 512 \\\n'
          '  -e PORT=8080 -e DATA_DIR=/data -p 127.0.0.1:8080:8080 \\\n'
          '  -v vz-starter-data:/data vibezone-starter:local\n'
          'curl --fail http://localhost:8080/health'},
 {'title': 'Sprawdź kontrakt API',
  'body': 'Checker zapisuje dane testowe. Sprawdza walidację, feed, Unicode, widoczność zapisów '
          'oraz idempotencję polubień. Sam checker nie restartuje kontenera; pełną próbę SIGKILL i '
          'trwałości wykonuje smoke w następnym kroku.',
  'code': 'python3 bench/tests/api_check.py http://localhost:8080'},
 {'title': 'Uruchom lokalny smoke bench',
  'body': 'Najpierw pobierz przypięte narzędzia. Runner niczego sam nie pobiera. Smoke wykonuje '
          'trzy pomiary 2/5/10 VU, sprawdza dane po SIGKILL, zapisuje JSON w results/ i sprząta '
          'własne kontenery. Używa 256 MiB RAM i /data na tmpfs: to test działania, nie wynik '
          'konkursowy ani test trwałości po restarcie hosta. Przy kolizji podsieci ustaw '
          'BENCH_SUBNET na wolny zakres. Docker musi obsługiwać tryb sieci isolated.',
  'code': 'docker pull --platform linux/amd64 '
          'python@sha256:c5ba3deef00c7758561a0f03c26f44f10589263d40e732b8748e0d1f38f6dd89\n'
          'docker pull --platform linux/amd64 '
          'grafana/k6@sha256:f21270290d702cbf0a7d6ba5d7ed100b63bcb233b558b885ed787547b3488279\n'
          'bench/run_bench.sh vibezone-starter:local --pull never --smoke'},
 {'title': 'Optymalizuj na podstawie pomiarów',
  'body': 'Sprawdź plany zapytań, liczbę odczytów SQLite, koszt połączeń i blokad oraz rozmiar '
          'warstw obrazu. Zmieniaj jedną rzecz naraz i powtarzaj testy. Nie usuwaj walidacji ani '
          'gwarancji trwałości; wszystkie potwierdzone zapisy muszą przetrwać SIGKILL. Nie '
          'hardkoduj odpowiedzi ani danych benchmarku.',
  'code': ''},
 {'title': 'Sprawdź rozmiar i platformę',
  'body': 'Obraz musi mieć mniej niż 100 000 000 B bez kompresji; cel to mniej niż 50 MB. Zdalny '
          'runner sumuje nieskompresowane warstwy TAR i sprawdza ich hashe. Pole Size w lokalnym '
          'inspect jest orientacyjne i zależy od image store. Odznaka za obraz poniżej 50 MB nie '
          'dodaje U.',
  'code': "docker image inspect vibezone-starter:local --format '{{.Os}}/{{.Architecture}} "
          "{{.Size}} bytes'"},
 {'title': 'Opublikuj publiczny obraz linux/amd64',
  'body': 'Wybierz GHCR albo Docker Hub i zastąp YOUR_ACCOUNT nazwą konta. Zaloguj się lokalnie; '
          'GHCR wymaga uprawnień do publikowania pakietów. Ustaw pakiet jako publiczny i sprawdź '
          'anonimowy pull. Nie dodawaj tokenów do repozytorium. Po każdej zmianie kodu przebuduj '
          'obraz i powtórz testy przed publikacją.',
  'code': 'docker login ghcr.io\n'
          'docker buildx build --platform linux/amd64 --load \\\n'
          '  -t ghcr.io/YOUR_ACCOUNT/vibe-zone:v1 starter\n'
          'docker push ghcr.io/YOUR_ACCOUNT/vibe-zone:v1\n'
          '\n'
          '# Docker Hub\n'
          'docker login\n'
          'docker tag ghcr.io/YOUR_ACCOUNT/vibe-zone:v1 docker.io/YOUR_ACCOUNT/vibe-zone:v1\n'
          'docker push docker.io/YOUR_ACCOUNT/vibe-zone:v1'},
 {'title': 'Zgłoś i zachowaj kod zespołu',
  'body': 'Poproś organizatora o jednorazowe zaproszenie. W formularzu podaj nazwę zespołu, '
          'zaproszenie i publiczny obraz. Kod zespołu zobaczysz tylko raz: zapisz go prywatnie. '
          'Przy aktualizacji użyj tej samej nazwy i kodu, bez nowego zaproszenia. Każda wersja '
          'wymaga zatwierdzenia digestu obrazu albo pełnego commita repozytorium. Runner pobiera '
          'lub buduje tylko tę wersję w izolacji. Zmiana taga nie aktualizuje zatwierdzonego '
          'zgłoszenia. Batch startuje co 15 minut i publikuje wynik po wszystkich trzech '
          'pomiarach; może trwać dłużej niż kwadrans.',
  'code': ''},
 {'title': 'Sprawdź typowe błędy',
  'body': 'Port: nasłuchuj na 0.0.0.0 i PORT=8080, nie tylko na localhost. Dane: używaj '
          'DATA_DIR=/data; rootfs jest tylko do odczytu, proces ma UID/GID 10001. Sygnały: przekaż '
          'SIGTERM do serwera (exec w entrypoincie) i poprawnie zakończ proces. Trwałość: poprawne '
          'zamknięcie nie wystarczy - potwierdzone posty i like muszą przetrwać także SIGKILL. Nie '
          'polegaj na pamięci procesu. /health musi zwrócić poprawny JSON w 30 sekund. Konflikt '
          'nazwy lokalnego kontenera: zatrzymaj i usuń tylko własny vz-starter; wolumen z danymi '
          'pozostaje.',
  'code': 'docker stop vz-starter\ndocker rm vz-starter'}]

GLOSSARY = [('U', 'Wynik zespołu: mediana najwyższych zaliczonych poziomów użytkowników z trzech prób.'),
 ('VU',
  'Wirtualny użytkownik. Program wykonuje żądania podobnie jak jedna osoba korzystająca z '
  'aplikacji.'),
 ('p99', 'Czas, w którym mieści się 99% odpowiedzi. Pozostały 1% może trwać dłużej.'),
 ('cold start', 'Czas od uruchomienia kontenera do gotowości aplikacji do obsługi żądań.'),
 ('smoke', 'Krótki test działania przed pełnym pomiarem. Nie daje wyniku konkursowego.'),
 ('Docker',
  'Obraz Docker to paczka z aplikacją i jej zależnościami. Kontener to uruchomiona kopia tej '
  'paczki.'),
 ('linux/amd64',
  'System i rodzaj procesora, dla których budujesz obraz. Takich używa runner, także gdy pracujesz '
  'na Macu.'),
 ('SQLite',
  'Baza danych zapisana w pliku. W tym zadaniu przechowuje posty i polubienia w katalogu /data.'),
 ('Trwałość zapisu',
  'Potwierdzony post lub like musi pozostać w bazie po awaryjnym restarcie aplikacji.')]
