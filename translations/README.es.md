<p align="center">
  <img src="../assets/h5reclaim-banner.png" alt="H5Reclaim: recuperación de datos científicos HDF5" width="760">
</p>

<p align="center">
  <a href="../pyproject.toml"><img alt="Python 3.10+" src="https://img.shields.io/badge/Python-3.10%2B-3559F0?style=flat-square&amp;logo=python&amp;logoColor=white"></a>
  <a href="../LICENSE"><img alt="Licencia: Apache 2.0" src="https://img.shields.io/badge/License-Apache%202.0-263557?style=flat-square"></a>
  <a href="https://github.com/xShadyy/H5Reclaim/blob/main/benchmarks/results/v100rc1-release-coverage.json"><img alt="Evaluación controlada: 95,4 % de recuperaciones útiles" src="https://img.shields.io/badge/Controlled%20benchmark-95.4%25-3559F0?style=flat-square"></a>
</p>

<p align="center">
  <strong>Idiomas:</strong>
  <a href="../README.md" lang="en">English</a> · <a href="README.pl.md" lang="pl">Polski</a> · <a href="README.de.md" lang="de">Deutsch</a> · <a href="README.fr.md" lang="fr">Français</a> · <a href="README.es.md" lang="es">Español</a> · <a href="README.pt-BR.md" lang="pt-BR">Português (Brasil)</a><br>
  <a href="README.zh-CN.md" lang="zh-CN">简体中文</a> · <a href="README.ja.md" lang="ja">日本語</a> · <a href="README.ko.md" lang="ko">한국어</a> · <a href="README.ru.md" lang="ru">Русский</a> · <a href="README.ar.md" lang="ar">العربية</a>
</p>

H5Reclaim recupera datos científicos de archivos HDF5 dañados. Reúne las mediciones que siguen intactas en un archivo nuevo y utilizable tras escrituras interrumpidas, índices rotos, daños en los metadatos o truncamientos. Muchos instrumentos y programas científicos usan HDF5 para almacenar matrices, mediciones y sus metadatos.

**Un solo comando descubre los conjuntos de datos, selecciona los métodos de recuperación y crea un archivo HDF5 nuevo con un informe JSON claro.** El archivo original permanece intacto. H5Reclaim conserva las mediciones legibles de conjuntos de datos parcialmente dañados y sigue recuperando el resto del archivo.

La versión 1.0.0 logró **un 95,4 % de recuperaciones útiles en 109 pruebas controladas con archivos dañados**: 77 recuperaciones completamente exactas y 27 parciales, sin valores incorrectos aceptados en este conjunto de pruebas. Estas pruebas generadas miden las clases de fallos declaradas, no una tasa de éxito esperada en otros archivos. [Consulta los resultados](https://github.com/xShadyy/H5Reclaim/blob/main/benchmarks/results/v100rc1-release-coverage.json).

La [guía de uso](../USAGE.md) explica las herramientas de inspección y reparación, los formatos compatibles, la verificación y el procesamiento por lotes.

<a id="quick-start"></a>
## Inicio rápido

Necesitas Python 3.10 o una versión posterior. Clona este repositorio, o descarga y extrae su archivo ZIP desde GitHub:

```sh
git clone https://github.com/xShadyy/H5Reclaim.git
cd H5Reclaim
```

Instala el proyecto desde la raíz del repositorio y recupera un archivo:

```sh
python -m pip install .
python -m h5reclaim rescue "damaged.h5"
```

## Comprender el resultado

El resumen del terminal indica si la recuperación es completa o parcial y muestra dónde se encuentran los archivos creados. El informe JSON identifica los conjuntos de datos recuperados, los métodos de recuperación, las posiciones sin resolver y los metadatos restaurados.

Cuando un método publica un **mapa de estado**, este muestra qué posiciones contienen mediciones aceptadas. Usa el mapa indicado en el informe para seleccionar los valores que vas a analizar; las posiciones desconocidas pueden mostrar un valor de relleno, como cero. Algunos conjuntos de datos contiguos intactos y conjuntos de datos nulos no tienen un mapa comprobable: consulta su informe específico antes de analizarlos. Las referencias previas y los conjuntos de protección permiten comparar los resultados con una captura anterior.

## Licencia

El código fuente está disponible bajo la [licencia Apache 2.0](../LICENSE). Los archivos científicos incluidos tienen [atribuciones y licencias independientes](https://github.com/xShadyy/H5Reclaim/blob/main/corpus/README.md).

<sub>Creado por Tymoteusz Netter.</sub>
