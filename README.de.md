<p align="center">
  <img src="assets/h5reclaim-banner.png" alt="H5Reclaim: Wiederherstellung wissenschaftlicher HDF5-Daten" width="760">
</p>

<p align="center">
  <a href="pyproject.toml"><img alt="Python 3.10+" src="https://img.shields.io/badge/Python-3.10%2B-3559F0?style=flat-square&amp;logo=python&amp;logoColor=white"></a>
  <a href="LICENSE"><img alt="Lizenz: Apache 2.0" src="https://img.shields.io/badge/License-Apache%202.0-263557?style=flat-square"></a>
  <a href="docs/coverage.md"><img alt="Kontrollierter Test: 95.4% nützliche Wiederherstellungen" src="https://img.shields.io/badge/Controlled%20benchmark-95.4%25-3559F0?style=flat-square"></a>
</p>

<p align="center">
  <strong>Sprachen:</strong>
  <a href="README.md" lang="en">English</a> · <a href="README.pl.md" lang="pl">Polski</a> · <a href="README.de.md" lang="de">Deutsch</a> · <a href="README.fr.md" lang="fr">Français</a> · <a href="README.es.md" lang="es">Español</a> · <a href="README.pt-BR.md" lang="pt-BR">Português (Brasil)</a><br>
  <a href="README.zh-CN.md" lang="zh-CN">简体中文</a> · <a href="README.ja.md" lang="ja">日本語</a> · <a href="README.ko.md" lang="ko">한국어</a> · <a href="README.ru.md" lang="ru">Русский</a> · <a href="README.ar.md" lang="ar">العربية</a>
</p>

<!-- English README SHA-256: 5a2c704dd8e97c5bb668d1c2f9e856b9e21568e44477487b74e281f694a66646 -->

H5Reclaim stellt wissenschaftliche Daten aus beschädigten HDF5-Dateien wieder her. Nach abgebrochenen Schreibvorgängen, defekten Indizes, beschädigten Metadaten oder abgeschnittenen Dateien überträgt es erhaltene Messwerte in eine neue, nutzbare Datei. HDF5 ist das Format, in dem viele wissenschaftliche Geräte und Anwendungen Arrays, Messwerte und die zugehörigen Metadaten speichern.

**Ein einziger Befehl findet die Datensätze, wählt Wiederherstellungsverfahren und erstellt eine neue HDF5-Datei mit einem übersichtlichen JSON-Bericht.** Das Original bleibt unverändert. H5Reclaim übernimmt lesbare Messwerte aus teilweise beschädigten Datensätzen und setzt die Wiederherstellung für den Rest der Datei fort.

Der Release Candidate 1.0.0rc1 erzielte **95.4% nützliche Wiederherstellungen in 109 kontrollierten Versuchen mit beschädigten Dateien**: 77 vollständig exakte und 27 teilweise Wiederherstellungen, ohne einen einzigen fälschlich akzeptierten Wert in dieser Versuchsreihe. Diese generierten Versuche messen die angegebenen Fehlerklassen und lassen keine erwartete Erfolgsquote für andere Dateien erkennen. [Ergebnisse ansehen](docs/coverage.md).

[Schnellstart](#quick-start) · [Anleitung](docs/usage.md) · [Wiederherstellungsumfang](docs/coverage.md) · [Berichtsformat](docs/report-schema.md) · [Versionshinweise](CHANGELOG.md)

<a id="quick-start"></a>
## Schnellstart

Du benötigst Python 3.10 oder neuer. Klone dieses Repository oder lade die ZIP-Datei von GitHub herunter und entpacke sie:

```sh
git clone https://github.com/xShadyy/H5Reclaim.git
cd H5Reclaim
python -m venv .venv
```

Aktiviere die Umgebung:

| System | Befehl |
| --- | --- |
| Windows PowerShell | `.venv\Scripts\Activate.ps1` |
| Windows Command Prompt | `.venv\Scripts\activate.bat` |
| macOS / Linux | `. .venv/bin/activate` |

Installiere das Tool im Stammverzeichnis des Repositorys und stelle eine Datei wieder her:

```sh
python -m pip install .
python -m h5reclaim rescue "damaged.h5"
```

Dadurch entstehen `damaged.recovered.h5` und `damaged.recovered.report.json` neben der Quelldatei. Vorhandene Zieldateien werden nie überschrieben. Mit expliziten Pfaden kannst du einen anderen Speicherort wählen oder einen Lauf wiederholen:

```sh
python -m h5reclaim rescue "damaged.h5" --output "rescued.h5" --report "evidence.json"
```

Der installierte Befehl `h5reclaim` funktioniert ebenfalls. Mit `python -m h5reclaim --help` siehst du die Befehle, mit `python -m h5reclaim rescue --help` die Wiederherstellungsoptionen.

## Ergebnis verstehen

Die Zusammenfassung im Terminal zeigt, ob die Wiederherstellung vollständig oder teilweise gelang, und nennt die Ausgabepfade. Der JSON-Bericht führt wiederhergestellte Datensätze, Wiederherstellungsverfahren, ungeklärte Positionen und wiederhergestellte Metadaten auf.

Prüfe die veröffentlichte Ausgabe vor ihrer Verwendung gegen den gespeicherten Bericht:

```sh
python -m h5reclaim verify-result "damaged.recovered.h5" "damaged.recovered.report.json" --source "damaged.h5"
```

Dabei werden der Bericht, die Form der Datensätze, der Hash der Quelldatei und die Konsistenz der Gültigkeitskarte in einem Worker mit begrenzten Ressourcen geprüft. Wiederhergestellte Messwerte werden dabei weder gelesen noch authentifiziert; Verfahren ohne überprüfbare Karte liefern das Ergebnis „nicht unterstützt“. Exit-Codes und Einschränkungen stehen in der [Anleitung](docs/usage.md#verify-a-published-result).

Wenn ein Wiederherstellungsverfahren eine **Statuskarte** ausgibt, zeigt diese, an welchen Positionen akzeptierte Messwerte liegen. Wähle die Werte für die Analyse anhand der im Bericht genannten Karte aus; unbekannte Positionen können einen Füllwert wie null anzeigen. Für manche intakten zusammenhängenden Datensätze und Null-Datensätze gibt es keine überprüfbare Karte. Ziehe deshalb vor der Analyse den Bericht zum jeweiligen Verfahren heran. Frühere Referenzstände und Schutzpakete ermöglichen den Vergleich mit einer älteren Aufnahme.

Bei Datensätzen mit fester Größe kannst du die wiederhergestellten Werte lesen, wobei unbekannte Positionen bereits maskiert sind:

```python
from h5reclaim import read_masked

values = read_masked(
    "damaged.recovered.h5", "damaged.recovered.report.json",
    "/experiment/readings", selection=(slice(0, 1000), Ellipsis),
)
```

Dieser Reader mit begrenzten Ressourcen verwendet die im Bericht angegebene Statuskarte des Datensatzes. Die Maske kennzeichnet Werte, die aus der beschädigten Eingabedatei akzeptiert wurden; sie belegt nicht, dass diese Werte einer früheren Aufnahme entsprechen. Unterstützte Auswahlen und Grenzen beschreibt die [Anleitung](docs/usage.md#read-results).

## Häufige Aufgaben

| Ziel | Befehl |
| --- | --- |
| Datei vor der Wiederherstellung untersuchen | `python -m h5reclaim diagnose damaged.h5` |
| Einen Datensatz wiederherstellen | `python -m h5reclaim rescue damaged.h5 --dataset /experiment/readings` |
| Vorhandenes Ergebnis zusammenfassen | `python -m h5reclaim report damaged.recovered.report.json` |
| Konsistenz von Ausgabe und Bericht prüfen | `python -m h5reclaim verify-result damaged.recovered.h5 damaged.recovered.report.json` |
| Längere Wiederherstellung fortsetzen | `python -m h5reclaim rescue damaged.h5 --resume-dir recovery-progress` |
| Zugehörige Dateien bereitstellen | `python -m h5reclaim rescue container.h5 --related-dir companion-files` |
| Datensätze nach Verlust ihrer Namen finden | `python -m h5reclaim discover damaged.h5 --json` |

Einige Dateien benötigen zusätzliche Kompressions-Codecs. Installiere `python -m pip install ".[filters]"` aus dem Stammverzeichnis des Repositorys und versuche es erneut. Die [Anleitung](docs/usage.md) behandelt größere Dateien, Ressourcenbudgets, abhängige Dateien, fortsetzbare Läufe und frühere Schutzpakete.

## Umfang der Wiederherstellung

H5Reclaim liest die Datensatzbeschreibungen aus der jeweiligen Datei. Derselbe Ablauf funktioniert daher für unterschiedliche Experimente, Datensatznamen und Array-Formen.

| Bereich | Implementierte Unterstützung |
| --- | --- |
| Speicherform | Kompakte, zusammenhängende und in Chunks aufgeteilte Datensätze; ältere und neuere Chunk-Indizes |
| Werte | Numerische Arrays, zusammengesetzte Datensätze, Zeichenketten fester und variabler Länge, Arrays mit unterschiedlich langen Einträgen, Aufzählungstypen, Referenzen sowie leere und Null-Datensätze |
| Schäden | Wiederherstellung bei defekten Indexverknüpfungen, durch Prüfsummen begründete Korrekturen an Metadaten und Chunk-Dimensionen, Reparaturen moderner Signaturen, Kennzeichen abgebrochener Schreibvorgänge, erhaltene Datensatz-Header, unlesbare Chunks und physisches Abschneiden des Dateiendes |
| Kompression | DEFLATE, LZF, shuffle, Fletcher32 und unterstützte optionale Codecs aus Zusatzpaketen |
| Dateistruktur | Verfügbare Gruppen, Attribute, Verknüpfungen, benannte Datentypen, Referenzen, Dimensionsskalen und Anwendungs-Header |
| Abhängigkeiten | Externer Speicher, virtuelle Datensätze, externe Verknüpfungen und Family/Split-Dateien mit ausdrücklich bereitgestellten zugehörigen Dateien oder Manifesten |

Bei Messungen, die vor einer Beschädigung geschützt wurden, können aufbewahrte Replikate und Paritätsdaten auch fehlende Daten rekonstruieren. Die Suche nach zugehörigen Dateien, fortsetzbare Wiederherstellung und Ressourcenbudgets für die Verarbeitung als Datenstrom unterstützen größere wissenschaftliche Arbeitsabläufe.

Der **Benchmark der automatischen Wiederherstellung für 1.0.0rc1** umfasst 23 generierte Daten- und Layoutfamilien. Die **109 kontrollierten Versuche mit beschädigten Dateien** ergaben 77 vollständig exakte Wiederherstellungen, 27 teilweise Wiederherstellungen und 5 Ablehnungen: **104 nützliche Ausgaben (95.4%)**. 83.1% der ursprünglichen Elemente wurden an genau ihren ursprünglichen Koordinaten wiederhergestellt, ohne fälschlich akzeptierte Werte und ohne veränderte Quelldateien. Alle 15 angegebenen Fehler der Chunk-Dimensionen lieferten nützliche Ergebnisse, darunter Arrays mit fünf Dimensionen, Scale-Offset, Zeichenketten variabler Länge, Arrays mit unterschiedlich langen Einträgen und zusammengesetzte Felder variabler Länge. Siehe den [vollständigen Fallbericht](benchmarks/results/v100rc1-release-coverage.json) oder die [Aufschlüsselung des Umfangs](docs/coverage.md).

Dokumentierte Auswertungen für v0.14.0 umfassen:

| Auswertung | Dokumentiertes Ergebnis |
| --- | --- |
| [Vier intakte wissenschaftliche Dateien](benchmarks/results/v014-scientific-whole.json) | 251 Datensätze und 253 Attribute wurden mit den aufbewahrten Originalen exakt verglichen |
| [Kontrollierter defekter GWOSC-Indexlink](benchmarks/results/v014-gwosc-controlled.json) | 128/128 Chunks an ihren exakten Koordinaten wiederhergestellt; keine falschen Chunks |
| [MATLAB 7.3, netCDF4 und NWB](benchmarks/results/v014-application-readers.json) | Unabhängige Reader öffneten sechs intakte Ausgaben bzw. Ausgaben nach kontrollierter Beschädigung; keine fälschlich akzeptierten Elemente, beschädigte Bereiche blieben unbekannt |

Die [Benchmark-Anleitung](benchmarks/README.md) erklärt, wie sich die Auswertungen reproduzieren lassen. Die [Korpus-Hinweise](corpus/README.md) enthalten die Quellenangaben für wissenschaftliche Dateien.

## Mitwirken und Probleme melden

Erstelle ein [Issue](https://github.com/xShadyy/H5Reclaim/issues) mit dem Befehl, den Versionen des Tools und von Python, dem beobachteten Fehler und dem erwarteten Ergebnis. Eine kleine Datei, mit der sich das Problem reproduzieren lässt, und ihr Bericht helfen, nicht unterstützte Speicherformen von Wiederherstellungsfehlern zu unterscheiden.

Melde Sicherheitslücken gemäß den [Regeln für Sicherheitsmeldungen](SECURITY.md); prüfe Berichte vor dem Teilen auf vertrauliche Pfade und Metadaten.

Installiere für die Entwicklung `python -m pip install -e ".[filters]"` und führe Folgendes aus:

```sh
python -m unittest discover -s tests -q
```

Die Tests prüfen die korrekte Wiederherstellung, die Unversehrtheit der Quelldatei und den Umgang mit ungeklärten Daten. Benchmarks vergleichen akzeptierte Werte mit getrennt aufbewahrten Originalen. Der [Repository-Leitfaden](docs/file-guide.md) zeigt die Implementierung und erklärt den Zweck der Dateien.
Das [Release-Handbuch](docs/releasing.md) dokumentiert den Candidate-Build, die Tag-Prüfung und die verbleibenden Schritte bis zur stabilen Version 1.0.

## Lizenz

Der Quellcode steht unter der [Apache-Lizenz 2.0](LICENSE). Für die mitgelieferten wissenschaftlichen Dateien gelten [gesonderte Quellenangaben und Lizenzen](corpus/README.md).

<sub>Erstellt von Tymoteusz Netter.</sub>
