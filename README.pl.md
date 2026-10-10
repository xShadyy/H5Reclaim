<p align="center">
  <img src="assets/h5reclaim-banner.png" alt="H5Reclaim: odzyskiwanie naukowych danych HDF5" width="760">
</p>

<p align="center">
  <a href="pyproject.toml"><img alt="Python 3.10+" src="https://img.shields.io/badge/Python-3.10%2B-3559F0?style=flat-square&amp;logo=python&amp;logoColor=white"></a>
  <a href="LICENSE"><img alt="Licencja: Apache 2.0" src="https://img.shields.io/badge/License-Apache%202.0-263557?style=flat-square"></a>
  <a href="docs/coverage.md"><img alt="Kontrolowany test: 95.4% użytecznych odzysków" src="https://img.shields.io/badge/Controlled%20benchmark-95.4%25-3559F0?style=flat-square"></a>
</p>

<p align="center">
  <strong>Języki:</strong>
  <a href="README.md" lang="en">English</a> · <a href="README.pl.md" lang="pl">Polski</a> · <a href="README.de.md" lang="de">Deutsch</a> · <a href="README.fr.md" lang="fr">Français</a> · <a href="README.es.md" lang="es">Español</a> · <a href="README.pt-BR.md" lang="pt-BR">Português (Brasil)</a><br>
  <a href="README.zh-CN.md" lang="zh-CN">简体中文</a> · <a href="README.ja.md" lang="ja">日本語</a> · <a href="README.ko.md" lang="ko">한국어</a> · <a href="README.ru.md" lang="ru">Русский</a> · <a href="README.ar.md" lang="ar">العربية</a>
</p>

<!-- English README SHA-256: 5a2c704dd8e97c5bb668d1c2f9e856b9e21568e44477487b74e281f694a66646 -->

H5Reclaim odzyskuje dane naukowe z uszkodzonych plików HDF5. Przenosi zachowane pomiary do nowego, użytecznego pliku po przerwanym zapisie, uszkodzeniu indeksów lub metadanych oraz ucięciu pliku. HDF5 to format, w którym wiele przyrządów i aplikacji naukowych przechowuje tablice, pomiary i związane z nimi metadane.

**Jedno polecenie wykrywa zbiory danych, dobiera metody odzyskiwania i tworzy nowy plik HDF5 wraz z przejrzystym raportem JSON.** Oryginał pozostaje nietknięty. H5Reclaim zachowuje odczytywalne pomiary z częściowo uszkodzonych zbiorów danych i kontynuuje odzyskiwanie pozostałej części pliku.

Wersja kandydująca 1.0.0rc1 osiągnęła **95.4% użytecznych wyników w 109 kontrolowanych próbach z uszkodzonymi plikami**: 77 odzysków w pełni zgodnych z oryginałem i 27 odzysków częściowych, bez błędnie zaakceptowanych wartości w tym zestawie prób. Te wygenerowane próby mierzą zadeklarowane rodzaje uszkodzeń, a nie przewidywany wskaźnik skuteczności dla innych plików. [Poznaj wyniki](docs/coverage.md).

[Szybki start](#quick-start) · [Instrukcja użytkowania](docs/usage.md) · [Zakres odzyskiwania](docs/coverage.md) · [Format raportu](docs/report-schema.md) · [Informacje o wydaniach](CHANGELOG.md)

<a id="quick-start"></a>
## Szybki start

Potrzebujesz Pythona 3.10 lub nowszego. Sklonuj repozytorium albo pobierz i rozpakuj jego archiwum ZIP z GitHub:

```sh
git clone https://github.com/xShadyy/H5Reclaim.git
cd H5Reclaim
python -m venv .venv
```

Aktywuj środowisko:

| System | Polecenie |
| --- | --- |
| Windows PowerShell | `.venv\Scripts\Activate.ps1` |
| Windows Command Prompt | `.venv\Scripts\activate.bat` |
| macOS / Linux | `. .venv/bin/activate` |

Zainstaluj narzędzie z katalogu głównego repozytorium i odzyskaj plik:

```sh
python -m pip install .
python -m h5reclaim rescue "damaged.h5"
```

Powstaną pliki `damaged.recovered.h5` i `damaged.recovered.report.json` obok pliku źródłowego. Istniejące pliki docelowe nigdy nie są nadpisywane. Podaj ścieżki jawnie, aby wybrać inną lokalizację lub powtórzyć uruchomienie:

```sh
python -m h5reclaim rescue "damaged.h5" --output "rescued.h5" --report "evidence.json"
```

Możesz także użyć zainstalowanego polecenia `h5reclaim`. Uruchom `python -m h5reclaim --help`, aby zobaczyć dostępne polecenia, albo `python -m h5reclaim rescue --help`, aby sprawdzić opcje odzyskiwania.

## Jak rozumieć wynik

Podsumowanie w terminalu wskazuje, czy odzyskiwanie jest pełne czy częściowe, oraz podaje lokalizacje plików wynikowych. Raport JSON wymienia odzyskane zbiory danych, metody odzyskiwania, nierozstrzygnięte pozycje i przywrócone metadane.

Przed użyciem opublikowanego pliku wynikowego sprawdź go względem zapisanego raportu:

```sh
python -m h5reclaim verify-result "damaged.recovered.h5" "damaged.recovered.report.json" --source "damaged.h5"
```

To sprawdza spójność raportu, kształtu zbioru danych, skrótu pliku źródłowego i mapy poprawności w procesie o ograniczonych zasobach. Nie odczytuje ani nie uwierzytelnia odzyskanych wartości pomiarowych; ścieżki odzyskiwania bez możliwej do sprawdzenia mapy zwracają wynik informujący o braku obsługi. Kody wyjścia i ograniczenia opisuje [instrukcja użytkowania](docs/usage.md#verify-a-published-result).

Jeśli dana ścieżka odzyskiwania publikuje **mapę statusu**, mapa wskazuje pozycje zawierające zaakceptowane pomiary. Do analizy wybieraj wartości na podstawie mapy wskazanej w raporcie; w nierozstrzygniętych pozycjach może być widoczna wartość wypełniająca, na przykład zero. Niektóre nienaruszone zbiory danych o układzie ciągłym oraz zbiory typu null nie mają możliwej do sprawdzenia mapy, więc przed analizą przeczytaj część raportu dotyczącą danej ścieżki odzyskiwania. Wcześniej zapisane punkty odniesienia i pakiety ochronne umożliwiają porównanie z wcześniejszym zapisem.

W przypadku zbiorów danych o stałym rozmiarze odczytaj odzyskane wartości z już zamaskowanymi nierozstrzygniętymi pozycjami:

```python
from h5reclaim import read_masked

values = read_masked(
    "damaged.recovered.h5", "damaged.recovered.report.json",
    "/experiment/readings", selection=(slice(0, 1000), Ellipsis),
)
```

Ten czytnik, działający z ograniczeniami zasobów, korzysta z mapy statusu podanej w raporcie dla zbioru danych. Maska wskazuje wartości zaakceptowane z uszkodzonego pliku wejściowego; nie dowodzi, że odpowiadają one wcześniejszemu zapisowi. Obsługiwane zakresy wyboru i ograniczenia opisuje [instrukcja użytkowania](docs/usage.md#read-results).

## Typowe zadania

| Cel | Polecenie |
| --- | --- |
| Sprawdź plik przed odzyskiwaniem | `python -m h5reclaim diagnose damaged.h5` |
| Odzyskaj jeden zbiór danych | `python -m h5reclaim rescue damaged.h5 --dataset /experiment/readings` |
| Podsumuj istniejący wynik | `python -m h5reclaim report damaged.recovered.report.json` |
| Sprawdź spójność pliku wynikowego i raportu | `python -m h5reclaim verify-result damaged.recovered.h5 damaged.recovered.report.json` |
| Wznów dłuższe odzyskiwanie | `python -m h5reclaim rescue damaged.h5 --resume-dir recovery-progress` |
| Wskaż pliki towarzyszące | `python -m h5reclaim rescue container.h5 --related-dir companion-files` |
| Znajdź zbiory danych po utracie ich nazw | `python -m h5reclaim discover damaged.h5 --json` |

Niektóre pliki wymagają dodatkowych kodeków kompresji. Zainstaluj `python -m pip install ".[filters]"` z katalogu głównego repozytorium, a następnie spróbuj ponownie. [Instrukcja użytkowania](docs/usage.md) omawia większe pliki, limity zasobów, pliki zależne, wznawianie oraz wcześniejsze pakiety ochronne.

## Zakres odzyskiwania

H5Reclaim odczytuje opis zbiorów danych z samego pliku, dlatego ten sam sposób pracy działa dla różnych eksperymentów, nazw zbiorów danych i kształtów tablic.

| Obszar | Zaimplementowana obsługa |
| --- | --- |
| Przechowywanie | Zbiory danych o układzie kompaktowym, ciągłym i porcjowanym; starsze i nowsze indeksy porcji |
| Wartości | Tablice liczbowe, rekordy złożone, łańcuchy o stałej i zmiennej długości, tablice o nieregularnych długościach, typy wyliczeniowe, referencje oraz puste zbiory danych i zbiory typu null |
| Uszkodzenia | Odzyskiwanie po uszkodzeniu powiązań indeksu, korekty metadanych i wymiarów porcji uzasadnione sumami kontrolnymi, naprawa nowszych sygnatur, flagi przerwanego zapisu, zachowane nagłówki zbiorów danych, nieczytelne porcje i fizyczne ucięcie końca pliku |
| Kompresja | DEFLATE, LZF, shuffle, Fletcher32 i obsługiwane opcjonalne kodeki dostarczane jako pakiety |
| Struktura pliku | Dostępne grupy, atrybuty, łącza, nazwane typy danych, referencje, skale wymiarów i nagłówki aplikacji |
| Zależności | Zewnętrzne przechowywanie, wirtualne zbiory danych, łącza zewnętrzne oraz pliki Family/Split z jawnie podanymi plikami towarzyszącymi lub manifestami |

W przypadku zapisów zabezpieczonych przed uszkodzeniem zachowane repliki i dane parzystości mogą również odtworzyć brakujące dane. Wykrywanie plików towarzyszących, wznawianie odzyskiwania i ograniczenia strumieniowego przetwarzania pomagają przy większych zadaniach naukowych.

**Test automatycznego odzyskiwania 1.0.0rc1** obejmuje 23 wygenerowane rodziny danych i układów. W **109 kontrolowanych próbach z uszkodzonymi plikami** uzyskano 77 wyników w pełni zgodnych z oryginałem, 27 częściowych i 5 odmów: **104 użyteczne wyniki (95.4%)**. Odzyskano 83.1% pierwotnych elementów na dokładnie tych samych współrzędnych, bez błędnie zaakceptowanych wartości i bez zmian plików źródłowych. Wszystkie 15 zadeklarowanych uszkodzeń wymiarów porcji dało użyteczne wyniki, także dla tablic pięciowymiarowych, scale-offset, łańcuchów o zmiennej długości, tablic o nieregularnych długościach i złożonych pól o zmiennej długości. Zobacz [pełny raport przypadków](benchmarks/results/v100rc1-release-coverage.json) lub [szczegóły zakresu](docs/coverage.md).

Zapisane wyniki testów wersji v0.14.0 obejmują:

| Test | Zapisany wynik |
| --- | --- |
| [Cztery nienaruszone pliki naukowe](benchmarks/results/v014-scientific-whole.json) | 251 zbiorów danych i 253 atrybuty porównane z zachowanymi oryginałami z pełną zgodnością |
| [Kontrolowane uszkodzenie powiązania indeksu GWOSC](benchmarks/results/v014-gwosc-controlled.json) | Odzyskano 128/128 porcji na dokładnie tych samych współrzędnych; zero błędnych porcji |
| [MATLAB 7.3, netCDF4 i NWB](benchmarks/results/v014-application-readers.json) | Niezależne czytniki otworzyły sześć nienaruszonych plików wynikowych lub plików po kontrolowanym uszkodzeniu; zero błędnie zaakceptowanych elementów, uszkodzone obszary pozostały nierozstrzygnięte |

[Instrukcja testów](benchmarks/README.md) wyjaśnia, jak odtworzyć te oceny. [Informacje o zbiorze plików](corpus/README.md) podają źródła plików naukowych.

## Współpraca i zgłaszanie problemów

Otwórz [zgłoszenie](https://github.com/xShadyy/H5Reclaim/issues), podając polecenie, wersje narzędzia i Pythona, zaobserwowany błąd oraz oczekiwany wynik. Mały plik umożliwiający odtworzenie problemu i jego raport pomagają odróżnić nieobsługiwany sposób przechowywania od błędu odzyskiwania.

Jeśli wykryjesz lukę bezpieczeństwa, postępuj zgodnie z [zasadami zgłaszania](SECURITY.md); przed udostępnieniem raportów sprawdź, czy nie zawierają wrażliwych ścieżek ani metadanych.

Do pracy nad projektem zainstaluj `python -m pip install -e ".[filters]"` i uruchom:

```sh
python -m unittest discover -s tests -q
```

Testy sprawdzają poprawność odzyskiwania, zachowanie pliku źródłowego i sposób postępowania z nierozstrzygniętymi danymi. Testy wydajności i pokrycia porównują zaakceptowane wartości z osobnymi oryginałami. [Przewodnik po repozytorium](docs/file-guide.md) opisuje implementację i cel poszczególnych plików.
[Instrukcja wydania](docs/releasing.md) dokumentuje budowanie wersji kandydującej, kontrolę tagu i pozostałe czynności przed stabilną wersją 1.0.

## Licencja

Kod źródłowy jest dostępny na warunkach [licencji Apache 2.0](LICENSE). Dołączone pliki naukowe mają [osobne informacje o pochodzeniu i licencjach](corpus/README.md).

<sub>Autor: Tymoteusz Netter.</sub>
