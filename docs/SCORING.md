# Vibe Zone scoring - runner profiles v3 / Profile runnera v3

Profiles `full` and `parallel` freeze host versions, CPU/RAM, VU levels and timing per batch. The ranking selects one profile hash. CPX22 is test-only; the official `full` profile preserves the standard load below. / Profile `full` i `parallel` zapisują wersje hosta, CPU/RAM, poziomy VU i czas w każdym batchu. Ranking wybiera jeden hash profilu. CPX22 służy testom; oficjalny `full` zachowuje standardowe obciążenie opisane poniżej. [Runbook](../deploy/RUNNER.md).

## English

The endpoint contract remains in [API.md](API.md). The reduced smoke resource limits below were introduced for the test release on 16 November 2026; **the definition of U and the tie-break order are unchanged**. Remote v3 has its own resource/code fingerprint and normalized image-size measurement.

### Submission and admission

1. Ask the organizer for a one-use invitation. Your first submission returns a separate private team update code.
2. Submit a public Docker Hub/GHCR image name or GitHub/GitLab repository URL. The form does not download or build anything.
3. The organizer reviews the source and approves an immutable **image digest or repository commit**. The isolated runner imports or builds that version. Every new version needs approval. Updating a registry tag does not change an approved image.
4. A batch snapshots the latest visible submission per team. Unapproved entries wait without invoking Docker. Starts are scheduled every 15 minutes, but completion can take longer. Publication is atomic after the full batch.

Images must target **linux/amd64** and be **strictly smaller than 100,000,000 uncompressed TAR layer bytes**. The 50 MB target has no extra bonus: fewer bytes always win a size tie. Gzipped archive size is informational.

**Remote runner v3:** sum the byte lengths of uncompressed layer TAR streams, including TAR headers/padding, after verifying the diff hashes. Config JSON and compressed archive bytes do not count. This is independent of Docker's classic/containerd store. Legacy local smoke uses inspect `.Size`; its values must not be compared to v3. Rerun all teams when changing profiles.

The standard container receives **1 vCPU, 2 GiB RAM, no extra swap, 512 PIDs and 4096 file descriptors**. It runs as UID/GID 10001:10001 with a read-only root filesystem, writes only to fresh `/data` (`TMPDIR=/data`, no shared-memory IPC) on a bounded disk filesystem (≤1 GiB). No other declared volumes, root, internet or external services. The image's healthcheck is disabled; an independent probe checks `/health`.

### One measurement

Every image completes **three full measurements**, each starting with fresh data:

1. Verify the local image, size and host configuration.
2. Start three new containers on the same empty or freshly initialized `/data`. Measure `docker start` to valid `/health` JSON, including CLI/probe overhead, excluding pull time. Limit: 30 seconds. This is container readiness, not a guarantee of cold host caches.
3. Check validation, formats, feed order, Unicode, read-after-write and sequential/concurrent like idempotency. Kill with SIGKILL and verify persistence after restart.
4. Reset data and seed **10,000 posts / 100,000 unique likes** through the API. Random authors, contents and users use a shared secret per batch and the same distribution across teams. Verify acknowledgements, exact counters and the resulting feed. Seeding is outside the load measurement.
5. Run k6 levels **500, 1000, 2000, 3000, 5000, 7500, 10000, 15000, 20000, 30000 VU**, each with **20 seconds ramp + 20 seconds hold** in a separate k6 process. Data survives between levels; `startVUs` uses the previous passing level. Traffic: 70% feed reads, 20% likes, 10% posts, with 1-3 seconds think time.
6. After load, verify duplicate likes and exact counter changes on 32 random older posts, snapshot the actual loaded feed, SIGKILL and compare after restart. Repeat contract and durability checks. Fresh test records do not replace checking measured data.

Incorrect API behavior, invalid successful responses or loss of acknowledged data disqualify the entire image, even if a lower load level passed. An ordinary failed load threshold ends the ramp and preserves the highest earlier passing level.

### Passing a level

All conditions must hold:

- Complete ramp and hold, and observe at least the target VU count (plus the integrity-check VU).
- **Zero response errors** over the entire attempt, including ramp and integrity checks.
- Completed integrity checks and completed main-traffic requests that started during the hold.
- At least **`ceil(0.2 × VU × hold_seconds)`** such requests: 2,000 for 500 VU and 20 seconds.
- **p99 < 500 ms**; exactly 500 fails.

`hold_latency` measures the full request and response validation, including connection wait/setup and receipt, using a 1 ms clock. It is not just `http_req_duration`. HTTP timeout is 5 seconds; redirects are disabled. Hold throughput is qualifying request count / nominal hold duration; p95/p99 use those hold samples. `attempt_*` fields are diagnostic only. This is a closed VU model, not a fixed RPS test.

The first failed level stops the ramp. A measurement's `U` is its highest passing level. **U=0 is valid** when correctness passes but the first load level fails; winning latency/throughput are then `null`, not zero.

### Three measurements and ties

All three correctness and persistence gates must pass; correctness failures are not discarded as noise.

| Priority | Calculation |
| --- | --- |
| Highest U | Median of three discrete `max_vus`, e.g. `[500,1000,2000]  >  1000` |
| Smallest image | Uncompressed byte count of the identical image |
| Fastest startup | Median of all nine startup samples |
| Lowest p99 at U | Median p99 from passing holds **at U**, from repetitions reaching U (at least two if U>0) |

p95 and RPS are aggregated at U in the same way. Do not average VU levels or use latency from a higher level. Exact ties across all four criteria share a place, e.g. **1,1,3**. Team name and submission order never break a tie. Prize splitting requires a rule announced by organizers.

### Comparability and limitations

- Team order follows the stable team ID. Updates do not move teams; three repetitions run consecutively. This reduces random variation but not systematic host drift.
- Each batch remeasures images. Identical IDs may reuse all three measurements **within that batch only**; there is no cross-batch cache.
- Infrastructure failures (generator OOM, invalid k6 exit, CPU throttling exceeding 10% of the attempt, missing host/storage configuration, incomplete cleanup) stop publication. The previous complete ranking remains.
- Standard requires native amd64, at least 6 CPU / 8 GiB host RAM, a frozen `BENCH_HOST_PROFILE` and verified bounded disk storage. Remote official `full` gives k6 4 CPU / 4 GiB and the probe 0.2 CPU / 96 MiB. The legacy local standard uses a 1 CPU / 256 MiB probe. These minima do not guarantee 30,000 VU capacity. Calibrate and freeze the environment before the competition; rerun everything after changes.
- **Smoke (test release):** 50 posts / 100 likes, 2/5/10 VU, 2-second ramp + 5-second hold, three repetitions. SUT: 1 CPU / **256 MiB**; k6: 1 CPU / **192 MiB**, `GOMAXPROCS=1`; probe: 0.5 CPU / **96 MiB**; `/data`: **64 MiB tmpfs**. All containers have no additional swap. These caps replace the older smoke limits (2 GiB SUT, 512 MiB k6, 256 MiB probe/tmpfs). The UI labels results as tests. Smoke checks container restart, not disk durability or host restart, and cannot determine prizes or be compared with standard scores.
- Production Mikrus serves only web/Caddy. Its local scheduler is disabled. The optional legacy smoke scheduler only accepts the reviewed organizer starter allowlist.
- Three repetitions cannot eliminate noise from other processes, disk, temperature, emulation or a shared VPS. A small Mikrus is not a competition benchmark host.
- Random checks cannot prove SQLite usage or exclude benchmark recognition. Review finalists' source and its correspondence to the approved image before awarding prizes.

JSON reports include immutable image/tool IDs, a profile/code fingerprint, host settings, all three repetitions and their levels, and the median. Different fingerprints/profiles are not comparable.

---

## Polski

Zasady po audycie z 16.11.2026. Kontrakt endpointów pozostaje w [API.md](API.md).

## Zgłoszenie i dopuszczenie

1. Organizator wydaje jednorazowe zaproszenie do utworzenia zespołu. Pierwsze zgłoszenie zwraca osobny kod do aktualizacji tego zespołu.
2. Podajesz publiczną nazwę obrazu Docker Hub/GHCR albo adres repozytorium GitHub/GitLab. Formularz niczego nie pobiera i nie buduje.
3. Organizator sprawdza źródło i zatwierdza **digest obrazu lub pełny commit repozytorium**. Izolowany runner pobiera lub buduje tę wersję. Każda nowa wersja wymaga ponownego zatwierdzenia; zmiana taga w registry sama nie zmienia zatwierdzonego obrazu.
4. Batch bierze najnowsze nieukryte zgłoszenie każdego zespołu. Niezatwierdzone zgłoszenie czeka na organizatora i nie uruchamia Dockera.

Obraz musi mieć platformę `linux/amd64` i rozmiar **ściśle mniejszy niż 100 000 000 B nieskompresowanych warstw TAR**. Cel 50 MB nie daje osobnej premii: przy remisie zawsze wygrywa mniejsza liczba bajtów. Rozmiar `docker save | gzip` jest informacyjny i nie wpływa na wynik.

**Zdalny runner v3:** sumuje długości nieskompresowanych strumieni TAR warstw, łącznie z nagłówkami i paddingiem, po sprawdzeniu ich hashy. JSON konfiguracji i rozmiar kompresji nie wchodzą do tej sumy. Wynik nie zależy od klasycznego/containerd image store. Lokalne smoke nadal używa inspect `.Size`; nie porównuj go z v3. Po zmianie profilu zmierz wszystkie zespoły ponownie.

Kontener otrzymuje 1 vCPU, 2 GiB RAM bez dodatkowego swapu, limit 512 procesów i 4096 otwartych deskryptorów. Działa jako UID/GID `10001:10001`, z systemem plików tylko do odczytu, zapisem wyłącznie w osobnym `/data` (`TMPDIR=/data`, bez IPC shared memory) o pojemności maksymalnie 1 GiB. Nie może deklarować innych `VOLUME`, wymagać roota, internetu ani usług zewnętrznych. Własny Docker HEALTHCHECK jest wyłączony; gotowość sprawdza niezależny klient przez `/health`.

## Jeden pomiar

Każdy obraz przechodzi **trzy pełne pomiary**. Każdy zaczyna z pustymi danymi i obejmuje:

1. Weryfikację lokalnego obrazu, rozmiaru i ustawień środowiska.
2. Trzy starty nowych kontenerów na tym samym jeszcze pustym, ewentualnie już zainicjalizowanym `/data`. Mierzymy od wywołania `docker start` do poprawnego JSON `/health`; limit 30 s. Pomiar obejmuje narzut Docker CLI i sondowania, nie obejmuje pobierania obrazu. To czas gotowości kontenera, nie gwarantowany start z pustymi cache hosta.
3. Testy walidacji, formatów, kolejności feedu, Unicode, read-after-write, sekwencyjnej i współbieżnej idempotencji. SIGKILL i ponowny start muszą zachować dane.
4. Wyczyszczenie danych i seed: **10 000 postów oraz 100 000 unikalnych polubień** przez API. Autorzy, treści i użytkownicy wynikają z losowego sekretu batcha; rozmiary i rozkład są wspólne dla zgłoszeń w batchu. Sprawdzamy każde potwierdzenie, dokładne liczniki polubień i końcowy feed. Seed nie wchodzi do pomiaru wydajności.
5. Kolejne poziomy k6: **500, 1000, 2000, 3000, 5000, 7500, 10000, 15000, 20000, 30000 VU**. Każdy poziom ma **20 s rampy i 20 s holda**. Jest osobnym procesem k6; dane pozostają między poziomami. `startVUs` odpowiada poprzedniemu zaliczonemu poziomowi.
6. Po zatrzymaniu ruchu: próby idempotencji i dokładnego przyrostu liczników na losowych 32 starych postach, snapshot rzeczywistego feedu, SIGKILL i porównanie danych po restarcie. Następnie ponowne testy kontraktu i ich trwałości. Nowe posty testowe nie zastępują snapshotu danych z pomiaru.

Niepoprawne API, błędna odpowiedź HTTP 2xx z niewłaściwymi danymi lub utrata danych dyskwalifikują zgłoszenie w danym batchu. Błąd transportu, status HTTP inny niż wymagany albo przekroczenie progu podczas obciążenia kończą rampę na niezaliczonym poziomie; wcześniejsze poprawnie zaliczone poziomy pozostają wynikiem, jeżeli końcowe bramki nadal przechodzą.

## Co oznacza VU i zaliczony poziom

VU to klient realizujący w pętli jedno losowane żądanie, a następnie czekający 1-3 s. Mix głównego ruchu wynosi średnio **70% `GET /feed`, 20% `POST /like`, 10% `POST /posts`**. Losowanie nie gwarantuje dokładnych proporcji w krótkiej próbce.

Dodatkowy, nieliczony do wyniku VU wykonuje podczas tego ruchu losowe transakcje: nowy post  >  like  >  feed  >  powtórny like  >  feed. Weryfikuje dokładny licznik, idempotencję i widoczność własnych danych, jeżeli post nadal należy do najnowszych 20. Co najmniej jedna pełna poprawna transakcja musi rozpocząć się w holdzie i zakończyć w tym poziomie. Jej żądania zwiększają rzeczywiste obciążenie, ale nie wchodzą do raportowanej przepustowości głównego ruchu.

Poziom `V` zaliczamy tylko wtedy, gdy jednocześnie:

- k6 zakończył się kodem 0, wykonał pełny czas rampy i holda (tolerancja zegara 50 ms), a obserwowane maksimum VU wynosi co najmniej `V + 1`, łącznie z klientem kontroli;
- liczba błędów odpowiedzi w całej próbie, także w rampie i kontroli spójności, wynosi **zero**;
- są zakończone kontrole spójności oraz żądania głównego ruchu rozpoczęte w holdzie;
- hold zawiera co najmniej **`ceil(0,2 × V × czas_holda_w_s)`** takich żądań; przykładowo dla 500 VU i 20 s potrzeba co najmniej 2000 żądań;
- **p99 < 500 ms**, ściśle: 500 ms nie przechodzi.

Latencję mierzymy zegarem wokół wywołania HTTP i walidacji odpowiedzi: obejmuje oczekiwanie na połączenie, zestawienie połączenia, odbiór całej odpowiedzi oraz jej sprawdzenie. Używamy trendu `hold_latency`, a nie samego `http_req_duration`. Rozdzielczość `Date.now()` wynosi 1 ms. Timeout HTTP to 5 s, przekierowania są wyłączone.

Przepustowość = liczba zakończonych żądań głównego ruchu **rozpoczętych w holdzie** / nominalny czas holda. Minimalna liczba żądań przeciwdziała zdobywaniu nominalnych VU bez wykonywania pracy; nie zmienia modelu na stały RPS. p95 i p99 liczy k6 z próbek tego holda. Pola `attempt_*` są diagnostyką całej próby i nie wpływają na ranking.

Pierwszy niezaliczony poziom kończy rampę. `U` jednego pomiaru to najwyższy zaliczony poziom. **U = 0 jest dopuszczalne**: API może być poprawne, lecz nie zaliczyć nawet 500 VU. Wtedy zwycięskie p95/p99 i przepustowość mają wartość `null`, a nie sztuczne zero.

## Wynik trzech pomiarów i remisy

Wszystkie trzy pomiary muszą zakończyć bramki poprawności i trwałości. Nie odrzucamy najgorszego błędu poprawności jako szumu. Dla poprawnego zgłoszenia:

| Kryterium, w kolejności | Obliczenie |
| --- | --- |
| **U, malejąco** | Mediana trzech dyskretnych `max_vus`, np. `[500, 1000, 2000]  >  1000` |
| **Rozmiar, rosnąco** | Bajty nieskompresowanego, identycznego obrazu |
| **Start, rosnąco** | Mediana wszystkich dziewięciu próbek startu |
| **p99 przy U, rosnąco** | Mediana p99 z zaliczonych holdów **na poziomie U**, z tych powtórzeń, które go zaliczyły; co najmniej dwa dla U > 0 |

Analogicznie agregujemy p95 i RPS na poziomie U. Nie uśredniamy poziomów VU i nie bierzemy p99 z wyższego poziomu tylko dlatego, że jedno powtórzenie doszło dalej.

Dokładnie równe cztery kryteria oznaczają **wspólne miejsce**, np. `1, 1, 3`. Nazwa zespołu ani kolejność zgłoszenia nie rozstrzygają remisu. Do ewentualnego podziału nagród potrzebna jest wcześniej ogłoszona decyzja organizatora, nie ukryty tie-breaker.

## Porównywalność i ograniczenia

- Kolejność zespołów jest stała według ID utworzenia zespołu; aktualizowanie zgłoszenia jej nie zmienia. Trzy powtórzenia danego obrazu odbywają się kolejno. To ogranicza losowość, ale nie usuwa systematycznego dryfu hosta.
- Każdy batch mierzy obrazy od nowa. Ten sam ID obrazu w kilku zgłoszeniach **wewnątrz jednego batcha** może współdzielić cały wynik trzech prób. Nie ma cache między batchami.
- Publikujemy cały batch w jednej transakcji. Rozpoznany problem infrastruktury (np. OOM generatora, nieprawidłowy exit k6, throttling CPU generatora >10% czasu próby, brak wymaganego środowiska lub niepełne sprzątanie) zatrzymuje publikację; pozostaje poprzedni pełny ranking.
- Profil standard wymaga natywnego amd64, minimum 6 CPU i 8 GiB RAM całego hosta, identyfikatora `BENCH_HOST_PROFILE` oraz ograniczonego dyskowego `/data`. Zdalny oficjalny `full` daje k6 limit 4 CPU / 4 GiB, a sondzie 0,2 CPU / 96 MiB. Dawny lokalny standard używa sondy 1 CPU / 256 MiB. To minima ochronne, **nie gwarancja**, że generator obsłuży 30 000 VU; potrzebna jest kalibracja i zapas zasobów. Zasoby generatora i próg trzeba zamrozić przed konkursem, a po zmianie przeliczyć wszystkie wyniki.
- Profil **smoke**: 50/100 seed, 2/5/10 VU, 2 s rampy + 5 s hold, trzy powtórzenia, SUT 1 CPU / 256 MiB, k6 1 CPU / 192 MiB (`GOMAXPROCS=1`), sonda 0,5 CPU / 96 MiB, `/data` na tmpfs 64 MiB. Sprawdza działanie i restart kontenera; **nie mierzy wydajności dysku ani trwałości po restarcie hosta**. UI oznacza go jako wyniki testowe. Nie wolno używać go do nagród ani porównywać ze standardem.
- Trzy powtórzenia nie eliminują wpływu innych procesów, dysku, temperatury, emulacji lub obciążenia VPS. Mały Mikrus nie jest hostem pełnego konkursowego benchmarku.
- Losowe dane i kontrole nie dowodzą, że implementacja faktycznie używa SQLite, ani nie wykluczają rozpoznawania benchmarku. Przed nagrodami wymagany jest przegląd źródeł finalistów i porównanie ich z zatwierdzonym obrazem.

Raport JSON zawiera ID obrazu, narzędzia przypięte do digestów, hash profilu i skryptów, parametry hosta, trzy raporty prób z poziomami oraz wynik mediany. Przy zmianie reguł zmienia się hash; wyników z różnych profili nie należy mieszać.

Wydanie 16.11 najpierw zmniejszyło limity lokalnego smoke (wcześniej SUT 2 GiB, k6 512 MiB, sonda i tmpfs 256 MiB). Następnie zdalny runner v3 otrzymał własne profile i normalizację rozmiaru obrazu. Definicja U oraz kolejność rozstrzygania remisów pozostają takie same. Mikrus obsługuje tylko web/Caddy; lokalny scheduler jest wyłączony. Kod uczestników wykonuje oddzielny izolowany runner.

Smoke ustawia `GOMAXPROCS=1` dla przypiętego k6/Go 1.24, aby jego współbieżność odpowiadała limitowi 1 CPU. Próg odrzucenia throttlingu >10% nie zmienia się. / Smoke sets `GOMAXPROCS=1` for pinned k6/Go 1.24 to match its 1-CPU quota; the >10% throttling rejection threshold is unchanged. [Go runtime explanation](https://go.dev/blog/container-aware-gomaxprocs).

## Extra categories / Dodatkowe kategorie

These categories do not change U, admission limits or the main tie-breakers. Badge winners come from the current published batch and visible submissions. Exact category ties share the badge. Smoke badges are test results, not prizes.

| Badge | Rule |
| --- | --- |
| Smallest image | Smallest recorded image strictly below 50,000,000 bytes |
| Fastest startup | Lowest measured median startup among public-test passes, including U=0 |
| Lowest p99 | Lowest measured p99 at each team's qualifying U>0 |
| Unusual language | Best main-ranking result among languages verified by the organizer for that submission |
| Survivor | Optional recovery and acknowledged-write checks pass during a separate load round |
| Bonus | Announced extra contract checks pass |
| AI Pilot | A pilot invitation leads to a submission passing every public and hidden smoke check |

Hidden checks run after public measurements. Only passed/total and status are public. Failure or missing private tooling does not change U and cannot award a pass. Bonus tests start in batches whose settings snapshot follows the announcement. The specification is published in PL/EN on the site and event screen; it is not in this repository before announcement. Chaos is limited to competition runners, or the allowlisted organizer starter in smoke mode.

Fastest to green is a separate ascending-time ranking: invitation issue to the creation of the first qualifying submission. Batch queue and validation time do not count. Updates retain the original start time; the first passing time survives report retention. Standard batches run a separate smoke validation for pilot entries. Invite legacy entries without a recorded start time again for this track. Prompt golf uses organizer-issued missions, AI-assisted fixes only, pass/fail first and elapsed time second; compare times within one mission. The organizer keeps mission and timing records privately.

Audience voting is optional. One signed device cookie permits one vote for the event; deleting cookies or using another browser can bypass this simple device limit. No fingerprinting, IP-address storage or accounts are used. CSRF, a global 120-attempt/minute limit and a 10,000-vote cap bound the endpoint. Counts appear only while voting is closed and enabled. Reopening preserves votes. Hidden teams are excluded. This is an audience prize, independent of U.

Badges and hidden checks are enabled by default. Chaos, prompt logs, voting, bonus and AI Pilot start disabled. The administrator can toggle each extra and each measured badge, verify language per version, announce the bonus once, and open/close voting. Disabling an extra hides its output; re-enabling preserves prior data. Prizes, unusual-language eligibility, announcement time and recovery parameters must be agreed before use. Freeze the private checks for an event; rerun all affected entries after a change.

Dodatki nie zmieniają U, limitów ani rozstrzygania remisów głównego rankingu. Odznaki wynikają z bieżącego opublikowanego batcha i widocznych zgłoszeń. Remisy w kategorii dają wspólną odznakę. Smoke pozostaje poza konkursem.

| Odznaka | Zasada |
| --- | --- |
| Najmniejszy obraz | Najmniejszy zmierzony obraz, ściśle poniżej 50 000 000 B |
| Najszybszy start | Najniższa mediana startu po zaliczeniu publicznych testów, także przy U=0 |
| Najniższe p99 | Najniższe zmierzone p99 na zaliczonym poziomie U>0 zespołu |
| Nietypowy język | Najlepszy wynik głównego rankingu wśród wersji z językiem zweryfikowanym przez organizatora |
| Survivor | Zaliczone kontrole powrotu usługi i potwierdzonych zapisów podczas osobnej próby obciążenia |
| Bonus | Zaliczone testy ogłoszonego dodatkowego kontraktu |
| AI Pilot | Zgłoszenie z zaproszeniem pilot zalicza wszystkie publiczne i ukryte testy smoke |

Ukryte testy działają po pomiarach publicznych. Pokazujemy tylko zaliczone/łącznie i status. Niezaliczenie lub brak prywatnych narzędzi nie zmienia U i nie daje zaliczenia. Bonus jest testowany od batcha, którego ustawienia pobrano po ogłoszeniu. Specyfikacja pojawia się w PL/EN na stronie i ekranie strefy; wcześniej pozostaje poza repo. Chaos działa na runnerze konkursowym lub wyłącznie dla startera z listy dozwolonych obrazów w smoke.

Fastest to green to osobny ranking czasu: od wydania zaproszenia do utworzenia pierwszego zgłoszenia zaliczającego testy. Oczekiwanie na batch i walidację nie wydłuża wyniku. Aktualizacje zachowują moment startu, a pierwszy wynik pozostaje po usunięciu starych raportów. Batch standardowy wykonuje dodatkowy smoke dla pilota. Starsze zgłoszenia bez czasu zaproszenia wymagają nowego zaproszenia do tej ścieżki. Prompt golf: prywatna misja organizatora, poprawki wyłącznie z pomocą AI, najpierw zaliczenie, potem czas. Porównujemy czasy tej samej misji.

Głosowanie jest opcjonalne. Podpisane cookie pozwala oddać jeden głos w wydarzeniu. Usunięcie cookie lub inna przeglądarka omija tę prostą ochronę. Nie stosujemy fingerprintingu, zapisu IP ani kont. Endpoint ma CSRF, limit 120 prób/min globalnie i maksymalnie 10 000 głosów. Liczby widać tylko przy zamkniętym, włączonym głosowaniu. Ponowne otwarcie zachowuje głosy. Ukryte zespoły nie są pokazywane. Nagroda publiczności jest niezależna od U.

Domyślnie działają odznaki i ukryte testy. Pozostałe dodatki są wyłączone. Administrator przełącza dodatki i poszczególne kategorie pomiarowe, weryfikuje język każdej wersji, ogłasza bonus jednokrotnie i otwiera/zamyka głosowanie. Wyłączenie ukrywa wynik, zachowując dane. Nagrody, kwalifikację języków, godzinę bonusu i parametry powrotu ustalamy przed użyciem. Prywatne testy trzeba zamrozić na wydarzenie; zmiana wymaga ponowienia pomiarów wszystkich zainteresowanych zespołów.
