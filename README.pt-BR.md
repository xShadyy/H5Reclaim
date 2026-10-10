<p align="center">
  <img src="assets/h5reclaim-banner.png" alt="H5Reclaim: recuperação de dados científicos em HDF5" width="760">
</p>

<p align="center">
  <a href="pyproject.toml"><img alt="Python 3.10+" src="https://img.shields.io/badge/Python-3.10%2B-3559F0?style=flat-square&amp;logo=python&amp;logoColor=white"></a>
  <a href="LICENSE"><img alt="Licença: Apache 2.0" src="https://img.shields.io/badge/License-Apache%202.0-263557?style=flat-square"></a>
  <a href="docs/coverage.md"><img alt="Benchmark controlado: 95,4% de recuperações úteis" src="https://img.shields.io/badge/Controlled%20benchmark-95.4%25-3559F0?style=flat-square"></a>
</p>

<p align="center">
  <strong>Idiomas:</strong>
  <a href="README.md" lang="en">English</a> · <a href="README.pl.md" lang="pl">Polski</a> · <a href="README.de.md" lang="de">Deutsch</a> · <a href="README.fr.md" lang="fr">Français</a> · <a href="README.es.md" lang="es">Español</a> · <a href="README.pt-BR.md" lang="pt-BR">Português (Brasil)</a><br>
  <a href="README.zh-CN.md" lang="zh-CN">简体中文</a> · <a href="README.ja.md" lang="ja">日本語</a> · <a href="README.ko.md" lang="ko">한국어</a> · <a href="README.ru.md" lang="ru">Русский</a> · <a href="README.ar.md" lang="ar">العربية</a>
</p>

<!-- English README SHA-256: 5a2c704dd8e97c5bb668d1c2f9e856b9e21568e44477487b74e281f694a66646 -->

O H5Reclaim recupera dados científicos de arquivos HDF5 danificados. Ele transfere medições preservadas para um novo arquivo utilizável após gravações interrompidas, índices quebrados, danos aos metadados e truncamento. HDF5 é o formato usado por muitos instrumentos e aplicativos científicos para armazenar matrizes, medições e seus metadados.

**Um único comando encontra os conjuntos de dados, seleciona os métodos de recuperação e cria um novo arquivo HDF5 com um relatório JSON claro.** O arquivo original permanece intacto. O H5Reclaim preserva as medições legíveis de conjuntos de dados parcialmente danificados e continua recuperando o restante do arquivo.

A versão candidata 1.0.0rc1 alcançou **95,4% de recuperações úteis em 109 testes controlados com arquivos danificados**: 77 recuperações totalmente exatas e 27 parciais, sem valores incorretos aceitos nesse conjunto de testes. Esses testes gerados medem as classes de falhas declaradas, não uma taxa de sucesso esperada para outros arquivos. [Explore os resultados](docs/coverage.md).

[Comece aqui](#quick-start) · [Guia de uso](docs/usage.md) · [Cobertura da recuperação](docs/coverage.md) · [Formato do relatório](docs/report-schema.md) · [Notas da versão](CHANGELOG.md)

<a id="quick-start"></a>
## Primeiros passos

Você precisa do Python 3.10 ou mais recente. Clone este repositório ou baixe e extraia seu ZIP do GitHub:

```sh
git clone https://github.com/xShadyy/H5Reclaim.git
cd H5Reclaim
python -m venv .venv
```

Ative o ambiente:

| Sistema | Comando |
| --- | --- |
| Windows PowerShell | `.venv\Scripts\Activate.ps1` |
| Windows Command Prompt | `.venv\Scripts\activate.bat` |
| macOS / Linux | `. .venv/bin/activate` |

Instale a partir da raiz do repositório e recupere um arquivo:

```sh
python -m pip install .
python -m h5reclaim rescue "damaged.h5"
```

Isso cria `damaged.recovered.h5` e `damaged.recovered.report.json` ao lado do arquivo de origem. Destinos existentes nunca são sobrescritos. Use caminhos explícitos para escolher outro local ou repetir a execução:

```sh
python -m h5reclaim rescue "damaged.h5" --output "rescued.h5" --report "evidence.json"
```

O comando instalado `h5reclaim` também funciona. Execute `python -m h5reclaim --help` para consultar os comandos ou `python -m h5reclaim rescue --help` para ver as opções de recuperação.

## Entenda o resultado

O resumo no terminal indica se a recuperação foi completa ou parcial e informa os locais dos arquivos produzidos. O relatório JSON identifica os conjuntos de dados recuperados, os métodos de recuperação, as posições não resolvidas e os metadados restaurados.

Confira o arquivo produzido com o relatório salvo antes de usá-lo:

```sh
python -m h5reclaim verify-result "damaged.recovered.h5" "damaged.recovered.report.json" --source "damaged.h5"
```

Essa verificação confere a consistência do relatório, do formato dos conjuntos de dados, do hash da origem e do mapa de validade em um processo com limites de recursos. Ela não lê nem autentica os valores das medições recuperadas; métodos sem um mapa verificável retornam um resultado de não suportado. Consulte o [guia de uso](docs/usage.md#verify-a-published-result) para os códigos de saída e os limites.

Quando um método publica um **mapa de estado**, ele mostra quais posições contêm medições aceitas. Use o mapa indicado no relatório para selecionar valores para análise; posições desconhecidas podem exibir um valor de preenchimento, como zero. Alguns conjuntos de dados contíguos intactos e conjuntos de dados nulos não têm um mapa verificável; consulte o relatório específico do método antes da análise. Cópias de referência anteriores e pacotes de proteção permitem comparar com uma captura anterior.

Para conjuntos de dados de tamanho fixo, leia os valores recuperados com as posições desconhecidas já mascaradas:

```python
from h5reclaim import read_masked

values = read_masked(
    "damaged.recovered.h5", "damaged.recovered.report.json",
    "/experiment/readings", selection=(slice(0, 1000), Ellipsis),
)
```

Esse leitor com limites de recursos usa o mapa de estado informado para o conjunto de dados. A máscara identifica valores aceitos a partir da entrada danificada; ela não estabelece que esses valores correspondam a uma captura anterior. Consulte o [guia de uso](docs/usage.md#read-results) para as seleções e os limites suportados.

## Fluxos de trabalho comuns

| Objetivo | Comando |
| --- | --- |
| Inspecionar um arquivo antes da recuperação | `python -m h5reclaim diagnose damaged.h5` |
| Recuperar um conjunto de dados | `python -m h5reclaim rescue damaged.h5 --dataset /experiment/readings` |
| Resumir um resultado existente | `python -m h5reclaim report damaged.recovered.report.json` |
| Conferir a consistência do arquivo produzido e do relatório | `python -m h5reclaim verify-result damaged.recovered.h5 damaged.recovered.report.json` |
| Retomar uma recuperação demorada | `python -m h5reclaim rescue damaged.h5 --resume-dir recovery-progress` |
| Fornecer arquivos auxiliares | `python -m h5reclaim rescue container.h5 --related-dir companion-files` |
| Encontrar conjuntos de dados após a perda de seus nomes | `python -m h5reclaim discover damaged.h5 --json` |

Alguns arquivos exigem codecs de compactação adicionais. Instale `python -m pip install ".[filters]"` a partir da raiz do repositório e tente novamente. O [guia de uso](docs/usage.md) aborda arquivos maiores, limites de recursos, arquivos dependentes, execuções retomáveis e pacotes de proteção anteriores.

## Cobertura da recuperação

O H5Reclaim lê as descrições dos conjuntos de dados no próprio arquivo. Por isso, o mesmo fluxo de trabalho funciona com diferentes experimentos, nomes de conjuntos de dados e formatos de matrizes.

| Área | Cobertura implementada |
| --- | --- |
| Armazenamento | Conjuntos de dados compactos, contíguos e em blocos; índices de blocos antigos e modernos |
| Valores | Matrizes numéricas, registros compostos, strings de tamanho fixo e variável, matrizes irregulares, enums, referências e conjuntos de dados vazios e nulos |
| Danos | Recuperação de ligações quebradas em índices, correções de metadados e dimensões de blocos justificadas por checksums, reparos de assinaturas modernas, indicadores de gravação interrompida, cabeçalhos de conjuntos de dados preservados, blocos ilegíveis e truncamento físico da parte final |
| Compactação | DEFLATE, LZF, shuffle, Fletcher32 e codecs opcionais distribuídos em pacotes e suportados |
| Estrutura do arquivo | Grupos, atributos, links, tipos de dados nomeados, referências, escalas de dimensões e cabeçalhos de aplicativos disponíveis |
| Dependências | Armazenamento externo, conjuntos de dados virtuais, links externos e arquivos Family/Split com arquivos auxiliares ou manifestos fornecidos explicitamente |

Para aquisições protegidas antes do dano, réplicas e dados de paridade preservados também podem reconstruir dados ausentes. A descoberta de arquivos auxiliares, a recuperação retomável e os limites de recursos para processamento em fluxo atendem a fluxos de trabalho científicos maiores.

O **benchmark de recuperação automática da versão 1.0.0rc1** abrange 23 famílias geradas de dados e estruturas. Seus **109 testes controlados com arquivos danificados** produziram 77 recuperações totalmente exatas, 27 parciais e 5 recusas: **104 resultados úteis (95,4%)**. Ele recuperou 83,1% dos elementos originais em suas coordenadas exatas, sem valores incorretos aceitos e sem alterar os arquivos de origem. Todas as 15 falhas declaradas de dimensões de blocos produziram resultados úteis, incluindo matrizes de cinco dimensões, scale-offset, strings variáveis, matrizes irregulares e campos compostos variáveis. [Consulte o relatório completo dos casos](benchmarks/results/v100rc1-release-coverage.json) ou a [análise da cobertura](docs/coverage.md).

As avaliações registradas da versão v0.14.0 incluem:

| Avaliação | Resultado registrado |
| --- | --- |
| [Quatro arquivos científicos intactos](benchmarks/results/v014-scientific-whole.json) | 251 conjuntos de dados e 253 atributos comparados exatamente com os originais preservados |
| [Ligação de índice GWOSC quebrada em teste controlado](benchmarks/results/v014-gwosc-controlled.json) | 128/128 blocos recuperados em suas coordenadas exatas; nenhum bloco incorreto |
| [MATLAB 7.3, netCDF4 e NWB](benchmarks/results/v014-application-readers.json) | Leitores independentes abriram seis arquivos produzidos a partir de dados intactos ou danificados de forma controlada; nenhum elemento incorreto aceito, com as regiões danificadas mantidas como desconhecidas |

O [guia de benchmarks](benchmarks/README.md) explica como reproduzir as avaliações. As [notas do corpus](corpus/README.md) fornecem as atribuições dos arquivos científicos.

## Como contribuir e relatar problemas

Abra uma [issue](https://github.com/xShadyy/H5Reclaim/issues) com o comando, as versões da ferramenta e do Python, o erro observado e o resultado esperado. Um arquivo pequeno que reproduza o problema, acompanhado do relatório, ajuda a distinguir armazenamento não suportado de um defeito na recuperação.

Para relatar uma vulnerabilidade, siga a [política de comunicação de segurança](SECURITY.md); antes de compartilhar relatórios, verifique se eles contêm caminhos e metadados sensíveis.

Para desenvolver o projeto, instale `python -m pip install -e ".[filters]"` e execute:

```sh
python -m unittest discover -s tests -q
```

Os testes verificam a correção da recuperação, a preservação do arquivo de origem e o tratamento de dados não resolvidos. Os benchmarks comparam os valores aceitos com originais separados. O [guia do repositório](docs/file-guide.md) mapeia a implementação e explica a finalidade desses arquivos.
O [guia de lançamento](docs/releasing.md) registra a compilação da versão candidata, a verificação da tag e as checagens restantes para a versão estável 1.0.

## Licença

O código-fonte está disponível sob a [Licença Apache 2.0](LICENSE). Os arquivos científicos incluídos têm [atribuições e licenças separadas](corpus/README.md).

<sub>Criado por Tymoteusz Netter.</sub>
