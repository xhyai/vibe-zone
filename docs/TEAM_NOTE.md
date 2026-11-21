# Vibe Zone: team note / notatka zespołu

DevFest Warsaw, 21.11.2026. Organizer / organizator: Kamil.

[Website / Strona](https://devfest.atlasapx.pl/) | [Repository](https://github.com/bsisduck/vibe-zone) | [Event screen / Ekran](https://devfest.atlasapx.pl/screen)

## Polski

Zbuduj mini social API z SQLite w dowolnym języku. Zrób fork, zmień starter, zbuduj obraz `linux/amd64`, uruchom smoke i opublikuj własny obraz w GHCR lub Docker Hub. Poproś o zaproszenie, zgłoś obraz i zachowaj kod zespołu. Każda wersja wymaga zatwierdzenia organizatora. [Instrukcja](PARTICIPANT_GUIDE.pl.md).

Wygrywa najwyższa mediana zaliczonych poziomów U z trzech prób: 0% błędów, p99 <500 ms. Remisy rozstrzyga obraz, start, p99; dokładny remis oznacza wspólne miejsce. Limit: 1 vCPU / 2 GiB, obraz <100 MB, cel <50 MB. Batche co 15 minut, publikacja po całej serii. [Punktacja](SCORING.md).

Mikrus pokazuje wyniki testowe sprawdzonego startera. Uczestnicy potrzebują osobnych runnerów. Przed wydarzeniem trzeba sprawdzić izolację, ograniczony dysk, rozmiary obrazów i generator. [Stan wdrożenia](RELEASE.md).

## English

Build a SQLite mini social API in any language. Fork, change the starter, build `linux/amd64`, run smoke and publish your own GHCR or Docker Hub image. Get an invitation, submit and save the team code. Each version requires organizer approval. [Guide](PARTICIPANT_GUIDE.md).

Highest median qualifying U from three trials wins: 0% errors, p99 <500 ms. Ties use image size, startup, then p99; exact ties share a place. Limits: 1 vCPU / 2 GiB, image <100 MB, target <50 MB. Batches every 15 minutes publish after the whole series. [Scoring](SCORING.md).

CPX22 results are test-only. A separate gVisor runner builds and measures submissions; Mikrus only serves the board. The organizer selects the official host and profile on event day. [Deployment status](RELEASE.md).

Dodatki / Extras: osobne odznaki za rozmiar <50 MB, start, p99 i zweryfikowany nietypowy język. Ukryte testy pokazują tylko zaliczone/łącznie. Chaos, bonus, prompt logi, głosowanie i AI Pilot włącza organizator. Żaden dodatek nie zmienia U. Nie publikuj materiałów organizatora przed ogłoszeniem. / Separate badges cover size <50 MB, startup, p99 and verified unusual languages. Hidden checks expose only counts. The organizer enables chaos, bonus, prompt logs, voting and AI Pilot. None changes U. Keep organizer materials private until announcement.
