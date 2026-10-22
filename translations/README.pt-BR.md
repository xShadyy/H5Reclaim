<p align="center">
  <img src="../assets/h5reclaim-banner.png" alt="H5Reclaim: recuperação de dados científicos em HDF5" width="760">
</p>

<p align="center">
  <a href="../pyproject.toml"><img alt="Python 3.10+" src="https://img.shields.io/badge/Python-3.10%2B-3559F0?style=flat-square&amp;logo=python&amp;logoColor=white"></a>
  <a href="../LICENSE"><img alt="Licença: Apache 2.0" src="https://img.shields.io/badge/License-Apache%202.0-263557?style=flat-square"></a>
  <a href="https://github.com/xShadyy/H5Reclaim/blob/main/benchmarks/results/v100rc1-release-coverage.json"><img alt="Benchmark controlado: 95,4% de recuperações úteis" src="https://img.shields.io/badge/Controlled%20benchmark-95.4%25-3559F0?style=flat-square"></a>
</p>

<p align="center">
  <strong>Idiomas:</strong>
  <a href="../README.md" lang="en">English</a> · <a href="README.pl.md" lang="pl">Polski</a> · <a href="README.de.md" lang="de">Deutsch</a> · <a href="README.fr.md" lang="fr">Français</a> · <a href="README.es.md" lang="es">Español</a> · <a href="README.pt-BR.md" lang="pt-BR">Português (Brasil)</a><br>
  <a href="README.zh-CN.md" lang="zh-CN">简体中文</a> · <a href="README.ja.md" lang="ja">日本語</a> · <a href="README.ko.md" lang="ko">한국어</a> · <a href="README.ru.md" lang="ru">Русский</a> · <a href="README.ar.md" lang="ar">العربية</a>
</p>

O H5Reclaim recupera dados científicos de arquivos HDF5 danificados. Ele transfere medições preservadas para um novo arquivo utilizável após gravações interrompidas, índices quebrados, danos aos metadados e truncamento. HDF5 é o formato usado por muitos instrumentos e aplicativos científicos para armazenar matrizes, medições e seus metadados.

**Um único comando encontra os conjuntos de dados, seleciona os métodos de recuperação e cria um novo arquivo HDF5 com um relatório JSON claro.** O arquivo original permanece intacto. O H5Reclaim preserva as medições legíveis de conjuntos de dados parcialmente danificados e continua recuperando o restante do arquivo.

A versão 1.0.0 alcançou **95,4% de recuperações úteis em 109 testes controlados com arquivos danificados**: 77 recuperações totalmente exatas e 27 parciais, sem valores incorretos aceitos nesse conjunto de testes. Esses testes gerados medem as classes de falhas declaradas, não uma taxa de sucesso esperada para outros arquivos. [Explore os resultados](https://github.com/xShadyy/H5Reclaim/blob/main/benchmarks/results/v100rc1-release-coverage.json).

<a id="quick-start"></a>
## Primeiros passos

Você precisa do Python 3.10 ou mais recente. Clone este repositório ou baixe e extraia seu ZIP do GitHub:

```sh
git clone https://github.com/xShadyy/H5Reclaim.git
cd H5Reclaim
```

Instale a partir da raiz do repositório e recupere um arquivo:

```sh
python -m pip install .
python -m h5reclaim rescue "damaged.h5"
```

## Entenda o resultado

O resumo no terminal indica se a recuperação foi completa ou parcial e informa os locais dos arquivos produzidos. O relatório JSON identifica os conjuntos de dados recuperados, os métodos de recuperação, as posições não resolvidas e os metadados restaurados.

Quando um método publica um **mapa de estado**, ele mostra quais posições contêm medições aceitas. Use o mapa indicado no relatório para selecionar valores para análise; posições desconhecidas podem exibir um valor de preenchimento, como zero. Alguns conjuntos de dados contíguos intactos e conjuntos de dados nulos não têm um mapa verificável; consulte o relatório específico do método antes da análise. Cópias de referência anteriores e pacotes de proteção permitem comparar com uma captura anterior.

## Licença

O código-fonte está disponível sob a [Licença Apache 2.0](../LICENSE). Os arquivos científicos incluídos têm [atribuições e licenças separadas](https://github.com/xShadyy/H5Reclaim/blob/main/corpus/README.md).

<sub>Criado por Tymoteusz Netter.</sub>
