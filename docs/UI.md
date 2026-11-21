# Kierunek interfejsu

Leaderboard to tablica wyników zawodów programistycznych podczas DevFestu, czytelna również z kilku metrów.

- Kolory: event blue `#1a4bcf`, jasny błękit `#eaf0ff`, biel `#ffffff`, tekst `#172332`, pomarańcz `#e65b21`, zieleń `#1d6b46`.
- Typografia: Avenir Next / Trebuchet MS / system sans; duży, zwarty tytuł, cyfry tabelaryczne w wynikach. Bez zewnętrznych fontów.
- Układ: nagłówek wydarzenia, szeroki niebieski pas Vibe Zone, tabela wyników; poniżej formularz i historia zgłoszeń. Wszystko wyrównane do lewej, liczby do prawej.
- Na ekranie strefy `/screen` pozostaje ranking z większą typografią i czasem aktualizacji. Na telefonie tabela przewija się we własnym obszarze.
- Wyrazisty element: tytuł w pasie koloru z geometrycznym motywem nawiasów; reszta służy porównywaniu wyników.

Przegląd przed implementacją: zamiast typowego dashboardu z kafelkami metryk używamy jednej tabeli zawodów. Numeracja oznacza wyłącznie rzeczywiste miejsca w rankingu. Bez gradientów, dekoracyjnych animacji i sztucznych wyników.

## Strony publiczne po rozdzieleniu

- `/`: duży niebieski hero z motywem `{ }`, datą i miejscem, trzema endpointami API oraz CTA. Niżej opis, cztery rzeczywiste kroki, skrót zasad, top 5 i siedem pytań FAQ.
- `/leaderboard`: pełna tabela z p95/p99, req/s, błędami i rozrzutem U. Cztery zbiorcze liczniki są jednym pasem, nie osobnymi kafelkami. Objaśnienie metryk pod tabelą.
- `/submit`: wydzielony istniejący formularz i lista zgłoszeń; `/start`: numerowana instrukcja i przewijane bloki poleceń.
- `/team/<id>`: historia zachowanych batchy i wykres ostatniej porównywalnej serii U. Dane liczbowe są też tekstem; wykres ma dostępny opis.
- `/screen`: nadal automatycznie odświeżany ranking bez formularza, z dodatkowym req/s i licznikami. Sześć kolumn mieści się na ekranie 1920 px.
- Nawigacja, widoczny focus, skip link, semantyczne nagłówki i PL/EN są wspólne. FAQ używa natywnego `details`. Tabele i polecenia przewijają się we własnym obszarze, bez poszerzania strony mobilnej.

Plan zachowuje paletę i typografię opisaną wyżej. Charakter strony buduje istniejący motyw programistycznych nawiasów i rzeczywisty kontrakt API. Bez nowych fontów, bibliotek UI, gradientów i animacji.
