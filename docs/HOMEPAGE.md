# Website / Strona wydarzenia

The homepage links the API challenge, instructions and current top five. The ranking, form and team history have separate URLs. All pages support Polish and English, keyboard navigation and phone screens.

Strona główna opisuje konkurs i pokazuje pięć pierwszych zespołów. Ranking, formularz i historia zespołu mają własne adresy. Wszystkie strony działają w PL/EN, z klawiatury i na telefonie.

| Page / Strona | URL |
| --- | --- |
| Home / Wydarzenie | `/` |
| Ranking | `/leaderboard` |
| Submission / Zgłoszenie | `/submit` |
| Instructions / Instrukcja | `/start` |
| Event screen / Ekran strefy | `/screen` |
| Team history / Historia | `/team/<id>` |

`?lang=pl` and `?lang=en` select and remember the language. Old home-page bookmarks redirect to the corresponding page. / Parametr wybiera i zapamiętuje język. Stare zakładki kierują na odpowiednią stronę.

Measurements come from published runner reports. Missing values display `-`, never zero. The trend includes only consecutive passing results with the same profile. p50 and latency per endpoint are not collected. Hidden submissions and ownership credentials do not appear in team history. / Dane pochodzą z opublikowanych raportów. Brak pomiaru to `-`. Wykres łączy tylko zaliczone wyniki tego samego profilu. Historia nie ujawnia kodów zespołów.

The initial deployment passed 44 unit tests, the 24-case PL/EN desktop/mobile browser matrix and two real three-run smoke batches. Its SQLite backup was restored and checked; every previous participant, submission and result survived the update. / Pierwsze wdrożenie przeszło 44 testy jednostkowe, 24 warianty przeglądarkowe i dwa batche po trzy próby smoke. Zweryfikowano odtworzenie kopii SQLite i zachowanie danych.

Run `python3 -m bench.tests.homepage_ui` for the isolated browser matrix, or add `--url https://devfest.atlasapx.pl` for read-only deployment checks. [Screenshots](screenshots/README.md), [local checks](../CONTRIBUTING.md), [deployment](../deploy/README.md).
