<p align="center">
  <img src="../assets/h5reclaim-banner.png" alt="H5Reclaim: odzyskiwanie naukowych danych HDF5" width="760">
</p>

<p align="center">
  <a href="../pyproject.toml"><img alt="Python 3.10+" src="https://img.shields.io/badge/Python-3.10%2B-3559F0?style=flat-square&amp;logo=python&amp;logoColor=white"></a>
  <a href="../LICENSE"><img alt="Licencja: Apache 2.0" src="https://img.shields.io/badge/License-Apache%202.0-263557?style=flat-square"></a>
  <a href="https://github.com/xShadyy/H5Reclaim/blob/main/benchmarks/results/v130-release-coverage.json"><img alt="Kontrolowany test: 95.4% użytecznych odzysków" src="https://img.shields.io/badge/Controlled%20benchmark-95.4%25-3559F0?style=flat-square"></a>
</p>

<p align="center">
  <strong>Języki:</strong>
  <a href="../README.md" lang="en">English</a> · <a href="README.pl.md" lang="pl">Polski</a> · <a href="README.de.md" lang="de">Deutsch</a> · <a href="README.fr.md" lang="fr">Français</a> · <a href="README.es.md" lang="es">Español</a> · <a href="README.pt-BR.md" lang="pt-BR">Português (Brasil)</a><br>
  <a href="README.zh-CN.md" lang="zh-CN">简体中文</a> · <a href="README.ja.md" lang="ja">日本語</a> · <a href="README.ko.md" lang="ko">한국어</a> · <a href="README.ru.md" lang="ru">Русский</a> · <a href="README.ar.md" lang="ar">العربية</a>
</p>

H5Reclaim odzyskuje dane naukowe z uszkodzonych plików HDF5. Przenosi zachowane pomiary do nowego, użytecznego pliku po przerwanym zapisie, uszkodzeniu indeksów lub metadanych oraz ucięciu pliku. HDF5 to format, w którym wiele przyrządów i aplikacji naukowych przechowuje tablice, pomiary i związane z nimi metadane.

**Jedno polecenie wykrywa zbiory danych, dobiera metody odzyskiwania i tworzy nowy plik HDF5 wraz z przejrzystym raportem JSON.** Oryginał pozostaje nietknięty. H5Reclaim zachowuje odczytywalne pomiary z częściowo uszkodzonych zbiorów danych i kontynuuje odzyskiwanie pozostałej części pliku.

W 109 kontrolowanych próbach z uszkodzonymi plikami wersja 1.3.0 dała 104 użyteczne wyniki: 77 w pełni zgodnych i 27 częściowych. Pięć prób zakończyło się odmową, a żadna błędna wartość nie została zaakceptowana. Wynik jest taki sam jak w zestawie prób wersji 1.0.0. [Wyniki wersji 1.3.0](../benchmarks/results/v130-release-coverage.json).

Wersja 1.3.0 naprawia dwa uszkodzone łącza w tym samym węźle indeksu, przenosi wybrane wartości o zmiennej długości i referencje, obejmuje więcej struktur wolnej przestrzeni oraz zachowuje potwierdzone aliasy odłączonych grup. W [dziesięciu kontrolowanych przypadkach skompresowanych danych](../benchmarks/results/v130-streaming-recovery.json), w tym z dwoma łączami w jednym węźle, nie zaakceptowano błędnych wartości.

Zobacz [przewodnik użytkownika](../USAGE.md), aby poznać narzędzia inspekcji i naprawy, obsługiwane formaty, weryfikację oraz odzyskiwanie wielu plików.

<a id="quick-start"></a>
## Szybki start

Potrzebujesz Pythona 3.10 lub nowszego. Sklonuj repozytorium albo pobierz i rozpakuj jego archiwum ZIP z GitHub:

```sh
git clone https://github.com/xShadyy/H5Reclaim.git
cd H5Reclaim
```

Zainstaluj narzędzie z katalogu głównego repozytorium i odzyskaj plik:

```sh
python -m pip install .
python -m h5reclaim rescue "damaged.h5"
```

## Jak rozumieć wynik

Podsumowanie w terminalu wskazuje, czy odzyskiwanie jest pełne czy częściowe, oraz podaje lokalizacje plików wynikowych. Raport JSON wymienia odzyskane zbiory danych, metody odzyskiwania, nierozstrzygnięte pozycje i przywrócone metadane.

Jeśli dana ścieżka odzyskiwania publikuje **mapę statusu**, mapa wskazuje pozycje zawierające zaakceptowane pomiary. Do analizy wybieraj wartości na podstawie mapy wskazanej w raporcie; w nierozstrzygniętych pozycjach może być widoczna wartość wypełniająca, na przykład zero. Niektóre nienaruszone zbiory danych o układzie ciągłym oraz zbiory typu null nie mają możliwej do sprawdzenia mapy, więc przed analizą przeczytaj część raportu dotyczącą danej ścieżki odzyskiwania. Wcześniej zapisane punkty odniesienia i pakiety ochronne umożliwiają porównanie z wcześniejszym zapisem.

## Licencja

Kod źródłowy jest dostępny na warunkach [licencji Apache 2.0](../LICENSE). Dołączone pliki naukowe mają [osobne informacje o pochodzeniu i licencjach](https://github.com/xShadyy/H5Reclaim/blob/main/corpus/README.md).

<sub>Autor: Tymoteusz Netter.</sub>
