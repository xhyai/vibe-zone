# Aktualizacja: izolowany runner pull

Stan po integracji z 16.11.2026. Poprzedni audyt poniżej jest zapisem historycznym lokalnego schedulera. Bieżąca architektura oddziela publiczny web od wykonania kodu. Mikrus nie uruchamia zgłoszeń; dedykowany host odpytuje uwierzytelnione API. CPX22 nadal daje tylko wyniki próbne. Oficjalny host i docelowe obciążenie wymagają kalibracji w dniu wydarzenia.

| Granica | Zabezpieczenie i dowód |
| --- | --- |
| Web / host wykonawczy | Wyłącznie HTTPS pull z tokenem, bez przekazywania cookie admina. Web nie kontaktuje się z adresem runnera i nie ma socketu Docker. API ma limit ciała, limit żądań i stałoczasową kontrolę tokenu. Formularze admina nadal wymagają Basic Auth i CSRF. |
| Zadania | Transakcyjne przydzielenie, 90-sekundowa dzierżawa, heartbeat, limit prób, deadline i odrzucanie starego tokenu. Idempotentne completion. Błąd infrastruktury albo cleanup zatrzymuje publikację całego batcha. Testy obejmują równoległe claim, timeout, replay i różny profil wyniku. |
| Kod uczestnika | gVisor runsc, non-root, cap-drop ALL, no-new-privileges, rootfs read-only, zapis tylko w `/data`, CPU/RAM/PID oraz świeży ext4 do 1 GiB. Osobna sieć internal/isolated bez adresu IPv6 hosta. Brak internetowego DNS, publicznych portów i socketu Docker. Nie ma fallbacku do runc. |
| Import i build | Skopeo/rootless BuildKit wewnątrz runsc, oddzielny dysk 1 GiB, limity czasu/CPU/RAM/PID. Egress wyłącznie przez proxy HTTPS z allowlistą i odrzucaniem prywatnych IP. Nie są przekazywane sekrety ani katalog domowy operatora. Budowy nie nakładają się na falę pomiarów. |
| Archiwum obrazu | Bez wypakowywania ścieżek do hosta; odrzucenie symlinków w wejściu/wyjściu, liczba/rozmiar plików, hash każdej rozpakowanej warstwy, platforma, VOLUME i limit 100 MB. Kanoniczny eksport usuwa obce tagi. Rozmiar warstw jest normalizowany przed importem, także dla containerd. |
| Dane prywatne | Token tylko w plikach env 600 i pamięci agenta. Procesy zadań mają oczyszczone środowisko. Prywatne kontrole są tylko na hoście, nigdy w obrazie uczestnika lub repo. Publiczna projekcja wyniku nie zawiera logu builda, dzierżawy ani adresu hosta. |
| Porównywalność | Hash obejmuje profil, narzędzia, kod wykonawczy i wersje hosta. Ranking wybiera jeden hash. Trzy próby, brak cache pomiarów między batchami, jawny zakres U. Profile CPX22 pozostają testowe. |

Builder wymaga wewnętrznych helperów mapujących UID, dlatego tylko jego zewnętrzny kontener runsc ma standardowe capabilities i `no-new-privileges=false`. `--oci-worker-no-process-sandbox` pozwala instrukcjom Dockerfile oddziaływać na procesy jednorazowego BuildKit; granicą ochrony jest cały sandbox gVisora. Dysk builda pozwala wykonywać zależności, lecz host nigdy nie uruchamia plików z tego dysku. Pomiar ma oddzielny, bardziej restrykcyjny kontener i dysk `noexec`. Te ustawienia nie dają uczestnikowi uprawnień administratora hosta. Zobacz [ograniczenia rootless BuildKit](https://github.com/moby/buildkit/blob/master/docs/rootless.md) i [gVisor z Dockerem](https://gvisor.dev/docs/user_guide/quick_start/docker/).

Agent/broker to zaufany proces roota zarządzający Dockerem i mountami. Jego kompromitacja oznacza przejęcie dedykowanego hosta. gVisor ogranicza powierzchnię ataku, nie stanowi dowodu niemożliwości ucieczki. Docker nadal parsuje zweryfikowane archiwum. Należy aktualizować Docker/runsc/kernel, kalibrować ponownie po zmianach, chronić token i nie współdzielić hosta z innymi usługami.

Próby integracyjne wykryły automatyczny zapisywalny `/tmp` w runsc; jawny mount read-only zamknął tę lukę w polityce. Ponowny test sprawdził też `/dev/shm`. Testy hosta potwierdziły blokadę sieci zewnętrznej/metadata, brak socketu, read-only root, limity PID/RAM oraz ENOSPC i świeżość ext4 po odtworzeniu. Integracja sprawdza rzeczywiste API i przetrwanie potwierdzonych zapisów po SIGKILL, a także bezpieczny import i build przypiętego startera. Surowe dowody oraz dokładne wyniki kalibracji pozostają w prywatnym raporcie operatora. Nie wykonano exploitów kernela ani testu utraty zasilania VPS. Cel poniżej 10% rozrzutu nie jest gwarancją: krótkie próby p99 są szczególnie podatne na szum.

Weryfikacja produkcyjna objęła build repozytorium po SHA, trzy pełne pomiary, publikację oraz prywatne kontrole (5/5). Powtórne dostarczenie wyniku było idempotentne, obca dzierżawa dostała 409. Anulowanie kolejnego zadania zachowało ranking i usunęło jego zasoby. Końcowa kalibracja diagnostyczna startera i Go w obu profilach trwała 628,5 s: U i req/s zmieściły się w celu rozrzutu poniżej 10%, p99 nie. To ograniczenie krótkich prób na współdzielonym CPX22 pozostaje jawne.

English validation: the live pull/build/three-run/publication path and private checks passed. Duplicate completion, stale-lease rejection, cancellation and cleanup were verified. The diagnostic calibration took 628.5 seconds; U and throughput met the below-10% spread target, p99 did not.

Otwarte warunki konkursu: docelowy host minimum 6 CPU / 8 GiB, zapas generatora przy docelowych VU, zamrożenie profilu, przegląd źródeł finalistów i aktualny skan zależności. Baner wyłącza Kamil. [Runbook PL/EN](../deploy/RUNNER.md) opisuje instalację, awarie i usunięcie VPS.

English: the public host only serves the board. A dedicated runsc host pulls leased jobs over authenticated HTTPS. Builds/imports have bounded storage and an allowlisted egress proxy; measurements use non-root, read-only containers and separate isolated networks. Immutable sources, archive validation and normalized layer sizes precede admission. Stale leases, conflicting replay and infrastructure failures cannot publish a new board. The root agent and Docker remain trusted; gVisor is defense in depth, not proof against every escape. CPX22 is test-only. Official hardware, generator headroom, dependency review and calibration remain event-day requirements. See the bilingual runbook above.

# Audyt pierwotnego schedulera (zapis historyczny)

**Wniosek: poprawiono kod i zweryfikowano lokalny przepływ, ale uruchamianie dowolnych obrazów uczestników na tym samym publicznym Mikrusie nadal nie jest bezpiecznym modelem wdrożenia.** Domyślnie działa tylko web/Caddy. Scheduler wymaga jawnego profilu, zatwierdzonych lokalnych ID i przygotowanego środowiska. Automatyczny pull/clone/build został usunięty.

Zakres: cały kod i konfiguracja `starter/`, `bench/`, `leaderboard/`, `deploy/`, testy, zależności i dokumentacja. Zastosowano przegląd źródeł, niezależne przeglądy granic zaufania i poprawek oraz ograniczone testy lokalne z Dockerem. Nie było wcześniejszego kompatybilnego rejestru audytu. Nie wykonywano deployu, pushowania, SSH, skanowania VPS, połączeń z registry ani internetową bazą CVE. Nie przeprowadzano prób wyczerpania zasobów hosta ani exploitów kernela.

To audyt kodu i lokalnej implementacji, nie certyfikacja izolacji produkcyjnej ani gwarancja wykrycia wszystkich podatności. Nie deklarujemy formalnego kompletnego pokrycia wszystkich klas ataków. Fakty zależne od VPS, sterownika wolumenów i finalnego sprzętu są poniżej oddzielone od lokalnie sprawdzonych poprawek.

## Zmiany i znaleziska

Priorytety odnoszą się do wpływu pierwotnego problemu. Naprawione oznacza usunięcie wskazanej ścieżki lub wymuszenie odmowy działania bez wymaganego warunku; nie oznacza, że skonfigurowano zewnętrzną infrastrukturę. Nie potwierdzono podatności krytycznej typu container escape, przejęcie administratora lub wykonanie poleceń przez injection.

| ID | Waga | Znalezisko i skutek | Zmiana / dowód | Stan |
| --- | --- | --- | --- | --- |
| A01 | Wysoka | Anonimowe zgłoszenie uruchamiało pull lub złośliwy Dockerfile przed limitem 100 MB; transfer, rozpakowanie, build cache, CPU/RAM i checkout nie miały twardych granic | Usunięto clone/build/pull z runnera, także fallback pobierania narzędzi. Scheduler uruchamia tylko lokalne ID zatwierdzone przez admina. Regresje sprawdzają brak wywołania Dockera dla niedopuszczonych ścieżek | Naprawione w schedulerze; import pozostaje osobnym zadaniem operatora. |
| A02 | Wysoka | Zapisywalna warstwa obrazu i wolumen bez limitu pozwalały konsumować dysk poza limitem RAM | Read-only rootfs, ograniczone tmpfs; standard odmawia pracy bez backendu dyskowego i kontroli pojemności ≤1 GiB. Smoke ma tmpfs 64 MiB. Osobna próba 1 MiB potwierdziła ENOSPC bez zapełniania hosta | Brak nieograniczonego fallbacku; produkcyjny backend wymaga walidacji. |
| A03 | Wysoka | Wielkie/ciągnące się odpowiedzi SUT mogły zużywać nieograniczoną pamięć sondy i generatora | Python: 128 KiB, brak redirectów/kompresji, deadline całej odpowiedzi, także powolnych nagłówków. Sonda/k6: CPU, RAM, swap, PID, tmpfs, logi i rozmiar pliku. Regresje oversized/stream/slow headers/trickle; inspect prawdziwych kontenerów | Naprawione granice zasobów. K6 pobiera ciało przed kontrolą JS; jego ochroną jest limit całego procesu. |
| A04 | Wysoka | Dowolny odwiedzający mógł tworzyć nieograniczoną liczbę zespołów, wersji i drogich zadań | Jednorazowe zaproszenia wydawane przez admina, osobny kod aktualizacji, ponowne zatwierdzanie każdej wersji, transakcyjne kwoty 200/100/5000, wspólny limit formularza 30/min | Naprawione; publiczny bot nie może sam utworzyć zespołu. Regresje i UI. |
| A05 | Średnia | Nazwa obrazu dopuszczała `localhost`, adresy prywatne i arbitralny registry, przekazywane do daemonowego pull | Jawne ograniczenie nazw do Docker Hub/GHCR i lokalnych nazw CLI, a przede wszystkim brak zdalnego pobierania w schedulerze | Ścieżka usunięta. Nie wykonywano SSRF przeciw żadnej usłudze. |
| A06 | Średnia | Dziedziczone USER, HEALTHCHECK i dodatkowe VOLUME zwiększały powierzchnię wykonania; anonimowe wolumeny mogły pozostać po rm | UID/GID 10001, wyłączony image healthcheck, odrzucenie deklaracji VOLUME innych niż `/data`, `volume-nocopy`, `rm -fv`, brak bindów i socketu w SUT | Naprawione; regresja metadanych i inspect. Brak twierdzenia o potwierdzonym escape. |
| A07 | Średnia | Timeout eksportu zaczynał działać dopiero po EOF, a capture_output trzymał nieograniczone stdout/stderr | Limit 1 MiB wyjścia procesu, deadline i kill/reap; eksport ≤256 MiB / 120 s, licznik gzip bez pliku tymczasowego; limit czasu całego przebiegu | Naprawione; test dużego wyjścia i zawieszonego procesu. |
| A08 | Średnia | Akceptowano hasła z `.env.example`; brak rzeczywistego limitu prób Basic Auth | Odrzucanie placeholderów i krótkich sekretów; limit przed weryfikacją hasła, potwierdzona sesja admina nie zużywa puli przez 5 min. CSRF pozostaje obowiązkowy | Naprawione w aplikacji; silne losowe sekrety i ograniczenie admina na proxy są obowiązkiem operatora. |
| A09 | Średnia | Brak limitów usług Compose i logów oraz nieograniczona historia wyników zwiększały koszt błędów i długiego działania | Limity CPU/RAM/PID, read-only, ograniczony `/tmp`, rotacja logów 2×5 MB/usługa, 64 zakończone batche i 1000 własnych raportów; scheduler bez sieci, web produkcyjny bez egressu | Naprawione limity aplikacyjne; partycja Docker/DB/backupy wymagają kwot i alarmów. |
| A10 | Średnia | Mutowalne tagi baz/narzędzi zmieniały build i warunki pomiaru bez jawnej zmiany kodu | Piny digestów Python, Docker CLI, sondy, k6 i Caddy; usunięto Git/Buildx oraz niezablokowane apk add z obrazu leaderboardu | Częściowo: Python ma wersje, ale nadal brak hashy wheel/sdist i aktualnego skanu advisory. |
| A11 | Wysoka | Seed ignorował liczniki like; pomiar mógł kwalifikować serwis pomijający właściwą pracę na danych | Losowy seed wspólny dla batcha, dokładna walidacja każdego zapisu i końcowego feedu, transakcje kontrolne pod obciążeniem, losowe stare rekordy oraz snapshot rzeczywistego feedu przed SIGKILL | Naprawione wskazane luki testów; rozpoznawanie benchmarku nadal wymaga przeglądu źródeł. |
| A12 | Średnia | Nominalne VU mogły przejść przy niemal zerowym ruchu; latencja nie obejmowała zestawiania połączenia | Pełny czas żądania, minimum `ceil(0,2 × VU × hold)` żądań, liczba aktywnych VU z uwzględnieniem sondy, ukończona kontrola w holdzie, odrzucenie pustych metryk | Naprawione; regresje sprawdzają niedostateczny ruch, brak kontroli, błędy i granicę 500 ms. |
| A13 | Średnia | Pojedynczy szczęśliwy wynik był cache'owany bezterminowo; aktualizacja zgłoszenia zmieniała kolejność pomiaru | Trzy próby i mediana, brak cache między batchami, stałe ID zespołu jako kolejność; hash kodu/profilu, przypięte narzędzia i identyfikator hosta. Awaria generatora blokuje publikację całego batcha | Naprawione mechanizmy; szum i systematyczny dryf sprzętu pozostają. |
| A14 | Niska | Przy identycznych metrykach nazwa zespołu rozstrzygała remis, a UI zawsze nadawał różne miejsca | Oddzielenie kolejności wyświetlania od wyniku; wspólne miejsce przy równym kluczu, np. 1,1,3 | Naprawione. |

W przeglądzie nie znaleziono ścieżki SQL/command/path injection w parametryzowanych zapytaniach i wywołaniach argv. Jinja escapuje dane, a JS używa `textContent`; test z nazwą `<script>` nie staje się skryptem. Kody zespołów/zaproszeń są losowe, w SQLite zapisane jako SHA-256; aktualizacja istniejącej nazwy wymaga kodu, porównywanego stałoczasowo. Modyfikacje admina są chronione Basic Auth i CSRF. Nagłówki CSP/nosniff/no-store, HttpOnly/SameSite oraz produkcyjne Secure cookie są obecne. To dowody dotyczące sprawdzonych ścieżek, nie ogólne zapewnienie o braku wszystkich injection/XSS.

## Ryzyka pozostające i decyzje przed sobotą 21.11

| Priorytet operacyjny | Nierozstrzygnięty warunek / ryzyko | Wymagane działanie |
| --- | --- | --- |
| **Blokujące** | Docker współdzieli kernel; scheduler z socketem ma władzę administratora hosta. UID 10001 nie odbiera tej władzy | Nie uruchamiać dowolnych obrazów na publicznym, współdzielonym Mikrusie. Przygotować osobny disposable worker/VM lub zweryfikowany sandbox i separację danych frontu. Projekt nie zawiera zdalnej kolejki ani izolacji VM |
| **Blokujące** | Nie zweryfikowano produkcyjnego backendu dysku. `statvfs` nie dowodzi poprawności sterownika, quota projektowa może raportować cały dysk, wielokrotnie użyte urządzenie może zachować stare dane | Udowodnić twardy limit bajtów/inodów, ENOSPC, świeżość, kasowanie wolumenu i trwałość po awarii na docelowym backendzie. Nie zastępować tego tmpfs ani samym wpisem env |
| **Blokujące** | Pull/build/load nadal są operacjami na niezaufanych formatach i mogą wyczerpać zasoby przed limitem obrazu | Oddzielna jednorazowa maszyna importera z ograniczoną partycją, CPU/RAM/PID/logami/czasem, polityką egressu i bez sekretów. Przypiąć digest/platformę i zweryfikować źródła przed zatwierdzeniem ID |
| Wysoki | Brak kalibracji 500...30k VU na natywnym amd64; wspólny host, I/O i generator mogą ograniczać wyniki | Zamrozić sprzęt, dysk, wersje i profil; zmierzyć zapas generatora, wykonać próbę kilku zespołów. Min. 6 CPU / 8 GiB nie gwarantuje wydajności 30k VU. Smoke nie jest konkursem |
| Wysoki | Dane losowe i sondy nie dowodzą używania SQLite; da się rozpoznawać charakter ruchu | Przegląd źródeł finalistów i zgodności źródło-obraz. Nie przyznawać nagród wyłącznie na podstawie czarnej skrzynki |
| Średni | Na docelowym VPS nie sprawdzono AppArmor/SELinux, userns/rootless, patch level, firewall/NAT/DNS/TLS, dostępności IPv4/IPv6 ani backupów | Inspekcja przez operatora; test restore i HTTPS. Nie wyłączać seccomp ani nie dodawać privileged/host namespace dla kompatybilności |
| Średni | Piny nie są skanem podatności, brakuje hashy artefaktów pip i odtworzonego od zera obrazu produkcyjnego | Offline lokalny audyt nie rozstrzyga CVE. Przed wydarzeniem sprawdzić advisory/SBOM, zablokować wheel/sdist hashami i wykonać kontrolowany build/test dla docelowej platformy |
| Średni | Retencja nie ogranicza partycji Docker, obrazów importera ani zewnętrznych backupów; długi seed mnoży czas przez trzy | Kwoty i alarmy miejsca/inodów, archiwizacja, usuwanie wskazanych artefaktów. Zmierzyć realny czas batcha; 15 min to harmonogram startów, nie gwarancja publikacji |
| Niski | Globalny limit pierwszego logowania może opóźnić admina podczas ataku; ukrycie zgłoszenia nie jest banem | Ograniczyć panel na zaufanym proxy/VPN; chronić zaproszenia. Udokumentować procedurę blokowania zespołu i odzyskania dostępu, bez osłabiania CSRF/auth |

Zwykły Compose bez profilu `runner` nie uruchamia kodu uczestników. Przykładowe sekrety nie pozwalają uruchomić weba. Standard bez natywnego amd64, profilu hosta lub ograniczonego dysku nie powinien być naprawiany przez usunięcie bramki. Zalecana kolejność przed wydarzeniem: izolacja i storage  >  przygotowanie/import obrazów  >  pełny build i aktualizacje  >  kalibracja  >  próbny batch  >  restore/TLS  >  przegląd finalistów.

## Testy wykonane lokalnie

Środowisko: macOS/Apple Silicon, Docker Desktop Engine **29.7.2**, Compose **5.5.1**, LinuxKit **7.0.12**, 10 CPU i ok. 18,8 GB pamięci daemonu. SUT/sonda/k6: emulowane amd64. Istniejące niezwiązane usługi pozostawiono bez zmian. Testowy stack miał osobną nazwę `vz-audit`, port 8082, podsieci, tymczasowe dane i losowe sekrety testowe.

| Kontrola | Wynik |
| --- | --- |
| Końcowy unittest w Dockerze, bez sieci, read-only source/rootfs, UID 10001, 2 CPU / 512 MiB / 64 PID, tmpfs 32 MiB | **34/34 PASS** |
| Dwa pełne batche smoke przez UI i prawdziwy scheduler/Docker | **6/6 przebiegów PASS**, każdy 2/5/10 VU; każda mediana U=10 |
| Bramki każdego z sześciu przebiegów | 145 kontroli API + 6 po SIGKILL przed obciążeniem; analogicznie 145 + 6 po obciążeniu; dodatkowo 33 kontrole rzeczywistego feedu/starych rekordów po SIGKILL |
| Seed | Każdy smoke: 50 postów / 100 like, sprawdzone odpowiedzi i końcowy feed |
| UI Chromium/Playwright | **13 przepływów PASS**: formularz, własność, aktualizacja, admin, batch, odrzucenie/oczekiwanie, live refresh, zaproszenie, zatwierdzenie, ponowny pomiar, ukrycie, mobile 390 px, kiosk 1920 px; brak błędów JS |
| Rzeczywiste ograniczenia | Inspect potwierdził UID 10001, CPU/RAM/swap/PID, read-only, cap-drop ALL, no-new-privileges, brak bindów/socketu, log-driver none, limity tmpfs i sieć internal + isolated gateway, bez IPv6 |
| Mała próba izolacji | Tmpfs 1 MiB zwrócił ENOSPC przy próbie 2 MiB; rootfs nie przyjął zapisu; CapEff=0, Seccomp=2. Nie zapełniano dysku hosta |
| Celowo uszkodzone API | Prawidłowo odrzucone przed k6: POST /posts zwrócił 400 zamiast 201; JSON błędu zachowany, brak pozostałych zasobów runa |
| Zatrzymanie schedulera podczas kolejnego automatycznego batcha | Batch 3 oznaczony failed, poprzednie 1/2 pozostały published, kontenery runa i blokada usunięte |
| Kopia i restart weba | SQLite backup API + integrity_check PASS; po restarcie zachowane dwa zespoły i stany batchy. To lokalny test danych fixture, nie potwierdzenie backupu VPS |
| Compose | Oba pliki config PASS; dodatkowo sprawdzono rzeczywistą tablicę tmpfs i niepusty JSON BENCH_VOLUME_OPTIONS; lokalny stack rzeczywiście wystartował |
| Caddy | `caddy validate` z przypiętego obrazu, network=none i deklarowanymi capabilities: PASS; nie uruchamiano ACME/deploymentu |
| Git | `git diff --check` PASS, commity wyłącznie lokalne, brak remote/CI do sprawdzenia |

Końcowe 34 testy wykonano również po zmianie klasyfikacji brakującego storage na błąd infrastruktury i dopuszczeniu pełnego feedu z 20 maksymalnymi tekstami Unicode escapowanymi w JSON. Browser E2E używał zamrożonego obrazu testowego `sha256:6b846981efadee9afff80a106f1c5ec8c57775068dca8957ea089d6b579867e5`, zawierającego nowe źródła na wcześniej dostępnych lokalnych zależnościach. Nie pobierano nowych pakietów. **Nie wykonano czystego builda finalnego produkcyjnego Dockerfile z registry/pip**; ten krok wymaga osobnej kontrolowanej walidacji supply chain. Nie wykonano pełnego profilu standard v2 na emulowanym ARM ani testu rzeczywistego produkcyjnego dysku.

### Zmierzone wyniki smoke

Obraz baseline `sha256:361b3142774da94a8d963bf0d18d931060964ca663374ea75b488321b41a3faa`: **41 156 948 B** bez kompresji, **14 010 627 B** archiwum gzip. Obraz pozostaje poniżej 50 MB; nie optymalizowano celowo wolnego API.

| Raport mediany (UTC w ID) | U trzech prób | Mediana startu (9 próbek) | p99 przy U | RPS holda |
| --- | --- | ---: | ---: | ---: |
| `20261116T112258-a525d3d6-median.json` | 10 / 10 / 10 | 2168,671 ms | 54,84 ms | 5,2 |
| `20261116T112617-977ff2a6-median.json` | 10 / 10 / 10 | 2541,956 ms | 15,16 ms | 5,0 |

Różnica p99 dobrze ilustruje ograniczenia współdzielonego hosta i małych próbek. Żadnego z tych wyników nie należy traktować jako konkursowego. Hash profilu zamrożonego E2E: `81ac56160777f96aacbffd8213dd4102a8b37e8c43f96b66735f49b7c84e1178`. Późniejsze poprawki klasy błędu storage i limitu odpowiedzi (128 KiB, aby pomieścić poprawny maksymalny feed Unicode) zmieniają fingerprint kodu; zweryfikowano je regresjami, bez ponawiania niezmienionego ruchu smoke.

Polecenia odtworzenia testów i wymagane obrazy są w [bench/README.md](../bench/README.md); zasady dla uczestników w [SCORING.md](SCORING.md). Surowe dowody pozostają w prywatnym archiwum operatora.


## Decyzja zakresu wydania testowego - 16.11.2026

Etap wydania dopuszcza publikację kodu oraz tablicę demonstracyjną na Mikrusie, **bez uruchamiania dowolnych projektów uczestników**. Wysokie znaleziska kodowe A01-A04/A11 są naprawione powyżej. Blokady infrastruktury konkursowej pozostają obowiązujące; nie zostały uznane za spełnione przez sam deploy.

Na tym hoście scheduler ma obowiązkową niepustą listę `BENCH_ALLOWED_IMAGE_IDS`; zarówno panel zatwierdzania, jak i worker odrzucają ID spoza listy przed użyciem Dockera. Lista zawiera tylko lokalnie zbudowany i sprawdzony starter organizatora. Formularz nadal niczego nie pobiera ani nie buduje. Zmniejszono wyłącznie limity diagnostycznego smoke (SUT 256 MiB, generator 192 MiB, sonda 96 MiB, tmpfs 64 MiB); UI pokazuje wyniki testowe - poza konkursem. Regresja potwierdza odmowę wykonania nieautoryzowanego ID bez wywołania resolvera.

W ten sposób ścieżka A01 oraz ryzyka wrogiego obrazu/importu są niedostępne w wydaniu testowym. Nie uruchamiamy standardu ani nie przyznajemy nagród z tego VPS. Wysokie ryzyka kalibracji, izolacji, dysku i przeglądu źródeł **nadal blokują rozpoczęcie konkursu**. Automatyczne budowanie repozytoriów nie zostało przywrócone na współdzielonym serwerze; wymaga osobnej izolowanej usługi. Dowody końcowej walidacji i jawne odstępstwa są w [RELEASE.md](RELEASE.md).

Dodatkowa bramka wykryta podczas deployu: Docker 29/containerd raportuje `.Size` jako zawartość i rozpakowany storage, a runner używa tego pola bez normalizacji. Nie zmieniono punktacji ani ustawień istniejącego daemonu VPS. Konkurs wymaga zweryfikowanego jednolitego legacy store z nieskompresowanymi bajtami lub osobnej poprawki normalizacji; wyniki diagnostyczne z różnych store nie są porównywalne. Równość warstw i konfiguracji startera po imporcie potwierdzono. Szczegóły w [SCORING.md](SCORING.md) i [RELEASE.md](RELEASE.md).

## Dodatki konkursowe

Odznaki nie uczestniczą w kluczu głównego rankingu. Publiczny adapter prywatnych testów przepuszcza tylko status i liczniki. Wyjątki i treść testów nie trafiają do API. Web montuje wyłącznie plik bonusu, scheduler prywatny katalog kontroli; SUT nie widzi żadnego z nich. Publikacja bonusu jest jawna i chroniona Basic Auth oraz CSRF.

Prompt logi są zwykłymi linkami HTTPS na GitHub/GitLab, bez pobierania i renderowania HTML. Głosowanie używa podpisanego cookie, unikalnego hasha urządzenia w SQLite, CSRF, transakcji i stałego globalnego limitu. Nie rozpoznaje fizycznego urządzenia po usunięciu cookie. To prosta nagroda publiczności, nie główna punktacja.

Regresje obejmują uprawnienia, fałszywe cookies, równoległe głosy, ukrycie wyników przed zamknięciem, walidację linków, escapowanie ogłoszenia, niezależność U, próg obrazu 50 MB i brak prywatnych szczegółów w odpowiedziach.
