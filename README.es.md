<p align="center">
  <img src="assets/h5reclaim-banner.png" alt="H5Reclaim: recuperación de datos científicos HDF5" width="760">
</p>

<p align="center">
  <a href="pyproject.toml"><img alt="Python 3.10+" src="https://img.shields.io/badge/Python-3.10%2B-3559F0?style=flat-square&amp;logo=python&amp;logoColor=white"></a>
  <a href="LICENSE"><img alt="Licencia: Apache 2.0" src="https://img.shields.io/badge/License-Apache%202.0-263557?style=flat-square"></a>
  <a href="docs/coverage.md"><img alt="Evaluación controlada: 95,4 % de recuperaciones útiles" src="https://img.shields.io/badge/Controlled%20benchmark-95.4%25-3559F0?style=flat-square"></a>
</p>

<p align="center">
  <strong>Idiomas:</strong>
  <a href="README.md" lang="en">English</a> · <a href="README.pl.md" lang="pl">Polski</a> · <a href="README.de.md" lang="de">Deutsch</a> · <a href="README.fr.md" lang="fr">Français</a> · <a href="README.es.md" lang="es">Español</a> · <a href="README.pt-BR.md" lang="pt-BR">Português (Brasil)</a><br>
  <a href="README.zh-CN.md" lang="zh-CN">简体中文</a> · <a href="README.ja.md" lang="ja">日本語</a> · <a href="README.ko.md" lang="ko">한국어</a> · <a href="README.ru.md" lang="ru">Русский</a> · <a href="README.ar.md" lang="ar">العربية</a>
</p>

<!-- English README SHA-256: 5a2c704dd8e97c5bb668d1c2f9e856b9e21568e44477487b74e281f694a66646 -->

H5Reclaim recupera datos científicos de archivos HDF5 dañados. Reúne las mediciones que siguen intactas en un archivo nuevo y utilizable tras escrituras interrumpidas, índices rotos, daños en los metadatos o truncamientos. Muchos instrumentos y programas científicos usan HDF5 para almacenar matrices, mediciones y sus metadatos.

**Un solo comando descubre los conjuntos de datos, selecciona los métodos de recuperación y crea un archivo HDF5 nuevo con un informe JSON claro.** El archivo original permanece intacto. H5Reclaim conserva las mediciones legibles de conjuntos de datos parcialmente dañados y sigue recuperando el resto del archivo.

La versión candidata 1.0.0rc1 logró **un 95,4 % de recuperaciones útiles en 109 pruebas controladas con archivos dañados**: 77 recuperaciones completamente exactas y 27 parciales, sin valores incorrectos aceptados en este conjunto de pruebas. Estas pruebas generadas miden las clases de fallos declaradas, no una tasa de éxito esperada en otros archivos. [Consulta los resultados](docs/coverage.md).

[Inicio rápido](#quick-start) · [Guía de uso](docs/usage.md) · [Cobertura de recuperación](docs/coverage.md) · [Formato del informe](docs/report-schema.md) · [Notas de la versión](CHANGELOG.md)

<a id="quick-start"></a>
## Inicio rápido

Necesitas Python 3.10 o una versión posterior. Clona este repositorio, o descarga y extrae su archivo ZIP desde GitHub:

```sh
git clone https://github.com/xShadyy/H5Reclaim.git
cd H5Reclaim
python -m venv .venv
```

Activa el entorno:

| Sistema | Comando |
| --- | --- |
| Windows PowerShell | `.venv\Scripts\Activate.ps1` |
| Windows Command Prompt | `.venv\Scripts\activate.bat` |
| macOS / Linux | `. .venv/bin/activate` |

Instala el proyecto desde la raíz del repositorio y recupera un archivo:

```sh
python -m pip install .
python -m h5reclaim rescue "damaged.h5"
```

Esto crea `damaged.recovered.h5` y `damaged.recovered.report.json` junto al archivo de origen. Los archivos de destino existentes nunca se sobrescriben. Indica las rutas explícitamente para elegir otra ubicación o repetir una ejecución:

```sh
python -m h5reclaim rescue "damaged.h5" --output "rescued.h5" --report "evidence.json"
```

También puedes usar el comando instalado `h5reclaim`. Ejecuta `python -m h5reclaim --help` para ver los comandos, o `python -m h5reclaim rescue --help` para consultar las opciones de recuperación.

## Comprender el resultado

El resumen del terminal indica si la recuperación es completa o parcial y muestra dónde se encuentran los archivos creados. El informe JSON identifica los conjuntos de datos recuperados, los métodos de recuperación, las posiciones sin resolver y los metadatos restaurados.

Comprueba el archivo creado con el informe guardado antes de utilizarlo:

```sh
python -m h5reclaim verify-result "damaged.recovered.h5" "damaged.recovered.report.json" --source "damaged.h5"
```

Esto comprueba la coherencia del informe, la forma de los conjuntos de datos, el hash del archivo de origen y el mapa de validez en un proceso con límites de recursos. No lee ni autentica los valores de las mediciones recuperadas; los métodos sin un mapa comprobable devuelven un resultado de tipo «no compatible». La [guía de uso](docs/usage.md#verify-a-published-result) explica los códigos de salida y las limitaciones.

Cuando un método publica un **mapa de estado**, este muestra qué posiciones contienen mediciones aceptadas. Usa el mapa indicado en el informe para seleccionar los valores que vas a analizar; las posiciones desconocidas pueden mostrar un valor de relleno, como cero. Algunos conjuntos de datos contiguos intactos y conjuntos de datos nulos no tienen un mapa comprobable: consulta su informe específico antes de analizarlos. Las referencias previas y los conjuntos de protección permiten comparar los resultados con una captura anterior.

Para los conjuntos de datos de tamaño fijo, lee los valores recuperados con las posiciones desconocidas ya enmascaradas:

```python
from h5reclaim import read_masked

values = read_masked(
    "damaged.recovered.h5", "damaged.recovered.report.json",
    "/experiment/readings", selection=(slice(0, 1000), Ellipsis),
)
```

Este lector con límites de recursos usa el mapa de estado indicado en el informe del conjunto de datos. La máscara identifica los valores aceptados del archivo dañado; no demuestra que coincidan con una captura anterior. La [guía de uso](docs/usage.md#read-results) detalla las selecciones admitidas y las limitaciones.

## Flujos de trabajo habituales

| Objetivo | Comando |
| --- | --- |
| Inspeccionar un archivo antes de recuperarlo | `python -m h5reclaim diagnose damaged.h5` |
| Recuperar un conjunto de datos | `python -m h5reclaim rescue damaged.h5 --dataset /experiment/readings` |
| Resumir un resultado existente | `python -m h5reclaim report damaged.recovered.report.json` |
| Comprobar la coherencia del archivo creado y el informe | `python -m h5reclaim verify-result damaged.recovered.h5 damaged.recovered.report.json` |
| Reanudar una recuperación prolongada | `python -m h5reclaim rescue damaged.h5 --resume-dir recovery-progress` |
| Proporcionar archivos complementarios | `python -m h5reclaim rescue container.h5 --related-dir companion-files` |
| Encontrar conjuntos de datos cuyos nombres se han perdido | `python -m h5reclaim discover damaged.h5 --json` |

Algunos archivos requieren códecs de compresión adicionales. Instala `python -m pip install ".[filters]"` desde la raíz del repositorio y vuelve a intentarlo. La [guía de uso](docs/usage.md) explica cómo trabajar con archivos grandes, límites de recursos, archivos dependientes, ejecuciones reanudables y conjuntos de protección previos.

## Cobertura de recuperación

H5Reclaim lee las descripciones de los conjuntos de datos del propio archivo, de modo que el mismo procedimiento funciona con distintos experimentos, nombres de conjuntos de datos y formas de matrices.

| Área | Cobertura implementada |
| --- | --- |
| Almacenamiento | Conjuntos de datos compactos, contiguos y fragmentados; índices de fragmentos antiguos y modernos |
| Valores | Matrices numéricas, registros compuestos, cadenas de longitud fija y variable, matrices irregulares, enumeraciones, referencias y conjuntos de datos vacíos y nulos |
| Daños | Recuperación de enlaces de índice rotos, correcciones de metadatos y dimensiones de fragmentos justificadas por sumas de verificación, reparación de firmas modernas, indicadores de escritura interrumpida, cabeceras de conjuntos de datos que han sobrevivido, fragmentos ilegibles y truncamiento físico del final del archivo |
| Compresión | DEFLATE, LZF, shuffle, Fletcher32 y códecs opcionales compatibles incluidos en el paquete |
| Estructura del archivo | Grupos, atributos, enlaces, tipos de datos con nombre, referencias, escalas de dimensiones y cabeceras de aplicaciones disponibles |
| Dependencias | Almacenamiento externo, conjuntos de datos virtuales, enlaces externos y archivos Family/Split con archivos complementarios o manifiestos proporcionados expresamente |

En adquisiciones protegidas antes del daño, las réplicas conservadas y la paridad también pueden reconstruir los datos faltantes. La búsqueda de archivos complementarios, la reanudación de recuperaciones y los límites de recursos para el procesamiento en flujo facilitan el trabajo con archivos científicos de mayor tamaño.

La **evaluación de recuperación automática 1.0.0rc1** cubre 23 familias generadas de datos y estructuras. Sus **109 pruebas controladas con archivos dañados** produjeron 77 recuperaciones completamente exactas, 27 parciales y 5 rechazos: **104 resultados útiles (95,4 %)**. Recuperó el 83,1 % de los elementos originales en sus coordenadas exactas, sin valores incorrectos aceptados ni archivos de origen modificados. Los 15 fallos declarados de dimensiones de fragmentos produjeron resultados útiles, incluidos los casos con matrices de rango cinco, scale-offset, cadenas de longitud variable, matrices irregulares y campos compuestos de longitud variable. [Consulta el informe completo de casos](benchmarks/results/v100rc1-release-coverage.json) o el [desglose de cobertura](docs/coverage.md).

Las evaluaciones registradas de v0.14.0 incluyen:

| Evaluación | Resultado registrado |
| --- | --- |
| [Cuatro archivos científicos intactos](benchmarks/results/v014-scientific-whole.json) | 251 conjuntos de datos y 253 atributos comparados exactamente con los originales conservados |
| [Enlace de índice GWOSC roto de forma controlada](benchmarks/results/v014-gwosc-controlled.json) | 128/128 fragmentos recuperados en sus coordenadas exactas; cero fragmentos incorrectos |
| [MATLAB 7.3, netCDF4 y NWB](benchmarks/results/v014-application-readers.json) | Lectores independientes abrieron seis resultados intactos o con daños controlados; sin elementos incorrectos aceptados, y las zonas dañadas permanecieron desconocidas |

La [guía de evaluaciones](benchmarks/README.md) explica cómo reproducir las pruebas. Las [notas del corpus](corpus/README.md) contienen la atribución de los archivos científicos.

## Contribuir y comunicar problemas

Abre una [incidencia](https://github.com/xShadyy/H5Reclaim/issues) con el comando, las versiones de la herramienta y de Python, el error observado y el resultado esperado. Un archivo pequeño que permita reproducir el problema, junto con su informe, ayuda a distinguir un tipo de almacenamiento no compatible de un fallo de recuperación.

Para comunicar una vulnerabilidad, sigue la [política de comunicación de problemas de seguridad](SECURITY.md); comprueba si los informes contienen rutas o metadatos sensibles antes de compartirlos.

Para colaborar en el desarrollo, instala `python -m pip install -e ".[filters]"` y ejecuta:

```sh
python -m unittest discover -s tests -q
```

Las pruebas comprueban la exactitud de la recuperación, la conservación de los archivos de origen y el tratamiento de los datos sin resolver. Las evaluaciones comparan los valores aceptados con originales conservados por separado. La [guía del repositorio](docs/file-guide.md) describe la implementación y explica la presencia de esos archivos.
La [guía de publicación](docs/releasing.md) documenta la compilación de la versión candidata, los controles de la etiqueta y las comprobaciones pendientes antes de la versión estable 1.0.

## Licencia

El código fuente está disponible bajo la [licencia Apache 2.0](LICENSE). Los archivos científicos incluidos tienen [atribuciones y licencias independientes](corpus/README.md).

<sub>Creado por Tymoteusz Netter.</sub>
