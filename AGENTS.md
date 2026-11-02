# Jak pracujemy

- Z jednego briefu dowieź kompletną apkę zgodną z celem i taste użytkownika.
- Prosto, mało i porządnie. Poprawność i bezpieczeństwo → czytelność → mało kodu i zależności.
- Komunikuj się z użytkownikiem po polsku. Dokumentację projektu pisz po angielsku.
- Ta baza określa sposób pracy. Wymagania i doświadczenia konkretnego projektu zapisuj w jego `docs/`, poza wspólną bazą.

## Wykonanie

- Przeczytaj potrzebny kod, jego użycia i właściwe docs. Ustal wymagane zachowanie i ryzyka.
- Czytaj selektywnie. Nie ładuj całych sesji ani wielokrotnie tych samych danych bez powodu.

## Delegowanie

- Główny agent domyślnie implementuje i odpowiada za wynik. Gdy powiem tobie abyś odpalał duzo sub-agentów: odpalaj je sensownie, aby optymalizować czas wdrożenia, researchu. Tak aby kilku agentów mogło pracować nad codebase, wspólnie.
- Deleguj, gdy niezależna praca albo świeża ocena daje więcej niż koszt przekazania kontekstu.
- Jeden właściciel zadania. Przekazuj cel, zakres, kryteria odbioru.
- Sam dobieraj model i ustawiaj high reasoning:
  - Luna 6: babysitting, śledzenie postępu, rutynowe kontrole i porządkowanie docs.
  - Terra 6.1: szukanie kodu, analiza i namierzanie błędów.
  - Sol 6.1: implementacje, przegląd bezpieczeństwa.
- Istotne zmiany daj świeżemu agentowi do oceny. Sprawdzaj jego uwagi przed poprawianiem kodu.
- Nie uruchamiaj całego zespołu do drobnej zmiany.

## Rust i struktura kodu

- Zawsze używaj Cargo workspace. Zacznij od jednego crate'a; kolejne dodawaj dla potrzebnej teraz odpowiedzialności.
- Trzymaj powiązany kod razem. Małe moduły, minimalna widoczność, jasne granice.
- Preferuj funkcje i konkretne typy. Każda abstrakcja, zależność i publiczne API musi mieć obecne zastosowanie.
- Purystyczny, idiomatyczny kod. Mało LoC, jeśli nie tracimy czytelności, poprawności ani bezpieczeństwa.
- Świadomie używaj ownership i borrowing. Bez odruchowego `clone`, `Arc/Mutex`, generyków, makr i async.
- Enumy opisują poprawne stany. `derive` ma zachowywać inwarianty i nie ujawniać sekretów.
- Oczekiwane błędy zwracaj jako `Result` z przydatnym kontekstem. Bez połykania błędów i pozornego sukcesu.
- Dane z zewnątrz nie mogą powodować panik. W testach `unwrap/expect` są dozwolone.
- Waliduj wejście, uprawnienia i zużycie zasobów. Chroń sekrety. Do wrażliwych operacji używaj sprawdzonych bibliotek.
- `unsafe` tylko gdy potrzebne, lokalnie i z opisanymi inwariantami bezpieczeństwa.
- Komentarze wyjaśniają decyzje i ograniczenia. Nie opisują oczywistego kodu.

```text
projekt/
├── Cargo.toml              # [workspace], members = ["crates/app"]
├── crates/
│   └── app/
│       ├── Cargo.toml
│       ├── src/
│       └── tests/
└── docs/
```

## Testy

- Nie chcemy miliona testów. Chcę testy, które sprawdzają realne zachowanie i istotne ryzyka.
- Preferuj integracyjne i E2E: aplikacja jako black box, wejście → obserwowalny wynik, ważne przepływy i błędy.
- Grupuj powiązane przypadki, ale zachowuj czytelny powód porażki. Bez jednego wielkiego testu wszystkiego.
- Unit test dodawaj dla nietrywialnej logiki, gdy taniej i pewniej sprawdza dany problem.
- Regresja błędu ma wykrywać problem przed poprawką i przechodzić po niej. Sprawdź też poprawne wejście.
- Bez testów banałów, kopii implementacji, dublowania scenariuszy i gonienia coverage.
- Uruchom właściwe dla zmiany fmt, Clippy i testy. Powtarzaj lub poszerzaj kontrole przy zmianie kodu, błędzie albo niewyjaśnionym ryzyku.

## Docs: co, kiedy i po co

- Na początku czytaj `docs/INDEX.md`, jeśli istnieje, potem tylko dokumenty potrzebne do zadania.
- Na końcu zadania zapisz przydatne trwałe fakty i popraw nieaktualne informacje. Nie dopisuj nic na siłę.
- Twórz plik lub katalog, gdy jest potrzebny i nie ma właściwego miejsca. Bez pustych szablonów ani obowiązkowych planów i milestones.
- Pisz krótko, w bulletpointach. Dokumentuj kontrakty, decyzje, sprawdzone komendy i pułapki. Hipotezy oznaczaj wprost.
- Każda informacja ma jedno źródło: linkuj zamiast kopiować kod lub inne docs.
- Dokument zacznij od tytułu oraz pól `Purpose:` i `Read when:`. Dodaj tylko nagłówki potrzebne do tematu.
- `INDEX.md` jest mapą: dla każdego istniejącego dokumentu podaj link, kiedy go czytać i co wyjaśnia. Aktualizuj go po dodaniu, usunięciu lub zmianie celu dokumentu.

```text
docs/
├── INDEX.md                # Co gdzie jest i kiedy to czytać.
├── PROJECT.md              # Cel, wymagania i granice produktu.
├── STRUCTURE.md            # Tree projektu: co gdzie jest i po co.
├── TASTE.md                # Preferencje dotyczące tego produktu.
├── project/
│   └── <topic>.md          # Kontrakt i ograniczenia danego obszaru.
├── memory/
│   └── <topic>.md          # Sytuacja, wniosek, dowód i kiedy stosować.
└── research/
    └── <topic>.md          # Pytanie, źródła, ustalenia, decyzja i otwarte kwestie.
```

- `STRUCTURE.md` wyjaśnia, dlaczego istnieją ważne katalogi i pliki. `TASTE.md` podaje przykłady i rzeczy, których unikać.
- W `project/` zapisuj wymagania, kontrakty i granice trudne do odczytania z kodu.
- W `memory/` zapisuj wnioski z nieoczywistych problemów i sprawdzone usprawnienia pracy, aby nie powtarzać błędów.
- W `research/` zachowuj wyniki warte ponownego użycia, także po przywróceniu kontekstu. Przed kolejnym researchem sprawdź istniejące wyniki.
- Aktualizuj wiedzę, gdy pojawią się nowe fakty. Usuwaj nieaktualne lub zdublowane informacje zamiast dopisywać kolejne wersje.

- Po zadaniu oceń strukturę katalogów i plików w obszarze zmian. Jeśli zrobiła się chaotyczna, uprość ją: połącz powiązane pliki, usuń zbędne warstwy i nazwij rzeczy jasno.
- Stosuj KISS: każdy plik i katalog ma mieć konkretny cel. Nie dziel kodu na wiele małych plików bez potrzeby.
- Przy zmianie struktury popraw importy, odnośniki i `docs/STRUCTURE.md`. Nie ruszaj niezwiązanych obszarów bez potrzeby.
- Po zadaniu zastanów się, co poszło nie tak, co spowolniło pracę i co można następnym razem zrobić lepiej.
- Jeśli wniosek przyda się ponownie, zapisz go w `docs/memory/<topic>.md`: **sytuacja → przyczyna → sprawdzone rozwiązanie → kiedy stosować**.
- Oddziel sprawdzone rozwiązania od pomysłów. Nie rób ogólnej zasady z pojedynczego przypadku.
- Przed pracą w danym obszarze sprawdź `docs/INDEX.md` i odpowiednie wpisy w `memory/`, aby nie powtarzać znanych błędów.
- Aktualizuj istniejący wpis zamiast tworzyć duplikat. Usuwaj nieaktualną wiedzę. Nie zapisuj oczywistości ani historii każdej sesji.

## Codebase Memory MCP

- Jeśli Codebase Memory MCP jest dostępne, używaj go do poznawania kodu i zależności.
- Szukaj elementów przez `search_graph`, sprawdzaj wywołania przez `trace_path`, a potrzebny kod czytaj przez `get_code_snippet`.
- Przed większą zmianą sprawdź powiązania i zakres wpływu. Po zmianie upewnij się, że indeks jest aktualny.
- Gdy wynik jest niepełny lub narzędzie niedostępne, użyj zwykłego wyszukiwania i odczytu plików. Weryfikuj ważne ustalenia w kodzie.

## Git

- Po ukończeniu i sprawdzeniu ważnej, spójnej zmiany zrób commit z krótkim opisem efektu.
- Przed commitem sprawdź diff i status. Dodaj tylko pliki należące do zadania; zachowaj zmiany użytkownika.
- Nie commituj sekretów, artefaktów ani przypadkowych plików. Nie wypychaj zmian bez upoważnienia.
- W odpowiedzi podaj, co zostało zapisane w commicie i jakie kontrole wykonano.
