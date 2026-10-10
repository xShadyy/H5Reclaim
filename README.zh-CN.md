<p align="center">
  <img src="assets/h5reclaim-banner.png" alt="H5Reclaim：科学 HDF5 数据恢复工具" width="760">
</p>

<p align="center">
  <a href="pyproject.toml"><img alt="Python 3.10+" src="https://img.shields.io/badge/Python-3.10%2B-3559F0?style=flat-square&amp;logo=python&amp;logoColor=white"></a>
  <a href="LICENSE"><img alt="许可证：Apache 2.0" src="https://img.shields.io/badge/License-Apache%202.0-263557?style=flat-square"></a>
  <a href="docs/coverage.md"><img alt="受控基准测试：95.4% 的恢复结果可用" src="https://img.shields.io/badge/Controlled%20benchmark-95.4%25-3559F0?style=flat-square"></a>
</p>

<p align="center">
  <strong>语言:</strong>
  <a href="README.md" lang="en">English</a> · <a href="README.pl.md" lang="pl">Polski</a> · <a href="README.de.md" lang="de">Deutsch</a> · <a href="README.fr.md" lang="fr">Français</a> · <a href="README.es.md" lang="es">Español</a> · <a href="README.pt-BR.md" lang="pt-BR">Português (Brasil)</a><br>
  <a href="README.zh-CN.md" lang="zh-CN">简体中文</a> · <a href="README.ja.md" lang="ja">日本語</a> · <a href="README.ko.md" lang="ko">한국어</a> · <a href="README.ru.md" lang="ru">Русский</a> · <a href="README.ar.md" lang="ar">العربية</a>
</p>

<!-- English README SHA-256: 5a2c704dd8e97c5bb668d1c2f9e856b9e21568e44477487b74e281f694a66646 -->

H5Reclaim 可从损坏的 HDF5 文件中恢复科学数据。对于写入中断、索引损坏、元数据损坏和文件截断等情况，它会将仍然存活的测量数据写入一个可用的新文件。HDF5 是许多科学仪器和应用程序用于存储数组、测量数据及其元数据的格式。

**一条命令即可发现数据集、选择恢复方法，并创建新的 HDF5 文件及清晰的 JSON 报告。** 原始文件保持不变。H5Reclaim 可以保留部分损坏的数据集中仍可读取的测量值，并继续恢复文件的其他部分。

1.0.0rc1 候选发布版在 **109 次受控损坏文件试验中实现了 95.4% 的可用恢复率**：其中 77 次完全精确恢复，27 次部分恢复；在这组试验中，已接受的值没有错误。这些生成的试验只衡量所声明的故障类别，不能作为其他文件的预期成功率。[查看结果](docs/coverage.md)。

[快速开始](#quick-start) · [使用指南](docs/usage.md) · [恢复范围](docs/coverage.md) · [报告格式](docs/report-schema.md) · [发布说明](CHANGELOG.md)

<a id="quick-start"></a>
## 快速开始

需要 Python 3.10 或更高版本。克隆此仓库，或从 GitHub 下载 ZIP 文件并解压：

```sh
git clone https://github.com/xShadyy/H5Reclaim.git
cd H5Reclaim
python -m venv .venv
```

激活虚拟环境：

| 系统 | 命令 |
| --- | --- |
| Windows PowerShell | `.venv\Scripts\Activate.ps1` |
| Windows 命令提示符 | `.venv\Scripts\activate.bat` |
| macOS / Linux | `. .venv/bin/activate` |

在仓库根目录安装，然后恢复文件：

```sh
python -m pip install .
python -m h5reclaim rescue "damaged.h5"
```

这会在源文件旁创建 `damaged.recovered.h5` 和 `damaged.recovered.report.json`。现有的目标文件绝不会被覆盖。如需指定其他位置或重复运行，请显式指定路径：

```sh
python -m h5reclaim rescue "damaged.h5" --output "rescued.h5" --report "evidence.json"
```

安装后也可以使用 `h5reclaim` 命令。运行 `python -m h5reclaim --help` 查看命令，或运行 `python -m h5reclaim rescue --help` 查看恢复选项。

## 理解恢复结果

终端摘要会显示恢复是完整还是部分完成，并列出输出位置。JSON 报告标明已恢复的数据集、恢复方法、未解决的位置和恢复的元数据。

使用结果前，请根据保存的报告检查已发布的输出：

```sh
python -m h5reclaim verify-result "damaged.recovered.h5" "damaged.recovered.report.json" --source "damaged.h5"
```

此命令会在有资源限制的工作进程中检查报告、数据集形状、源文件哈希值和有效性映射的一致性。它不会读取或鉴证恢复后的测量值；没有可检查映射的恢复路径会返回“不支持”结果。退出码和限制请参阅[使用指南](docs/usage.md#verify-a-published-result)。

如果某条恢复路径生成了**状态映射**，该映射会指示哪些位置包含已接受的测量值。分析时请使用报告中列出的映射来选择数值；未知位置可能显示零等填充值。某些完整的连续存储数据集和空数据集没有可检查的映射，因此分析前应查看相应恢复路径的报告。先前的基线和保护包可用于与更早的采集结果进行比较。

对于固定大小的数据集，可以在读取恢复值时直接屏蔽未知位置：

```python
from h5reclaim import read_masked

values = read_masked(
    "damaged.recovered.h5", "damaged.recovered.report.json",
    "/experiment/readings", selection=(slice(0, 1000), Ellipsis),
)
```

这个受资源限制的读取器使用报告中的数据集状态映射。掩码标识从损坏输入中接受的值；它不能证明这些值与更早的采集结果相同。有关支持的选择方式和限制，请参阅[使用指南](docs/usage.md#read-results)。

## 常见工作流程

| 目标 | 命令 |
| --- | --- |
| 恢复前检查文件 | `python -m h5reclaim diagnose damaged.h5` |
| 恢复一个数据集 | `python -m h5reclaim rescue damaged.h5 --dataset /experiment/readings` |
| 汇总已有结果 | `python -m h5reclaim report damaged.recovered.report.json` |
| 检查输出与报告的一致性 | `python -m h5reclaim verify-result damaged.recovered.h5 damaged.recovered.report.json` |
| 继续较长的恢复任务 | `python -m h5reclaim rescue damaged.h5 --resume-dir recovery-progress` |
| 提供配套文件 | `python -m h5reclaim rescue container.h5 --related-dir companion-files` |
| 数据集名称丢失后查找数据集 | `python -m h5reclaim discover damaged.h5 --json` |

有些文件需要额外的压缩编解码器。在仓库根目录运行 `python -m pip install ".[filters]"` 安装后重试。[使用指南](docs/usage.md)介绍较大的文件、资源预算、依赖文件、可从中断处继续的运行任务和先前创建的保护包。

## 恢复范围

H5Reclaim 会读取每个文件自身的数据集描述，因此同一工作流程适用于不同的实验、数据集名称和数组形状。

| 类别 | 已实现的支持范围 |
| --- | --- |
| 存储 | 紧凑、连续和分块数据集；旧版和现代分块索引 |
| 数据值 | 数值数组、复合记录、定长和变长字符串、不规则数组、枚举、引用、空数据集和 null 数据集 |
| 损坏 | 损坏的索引链接恢复、由校验和支持的元数据及块维度修正、现代签名修复、写入中断标志、幸存的数据集头、不可读取的块以及文件尾部的物理截断 |
| 压缩 | DEFLATE、LZF、shuffle、Fletcher32，以及受支持的可选打包编解码器 |
| 文件结构 | 可用的组、属性、链接、命名数据类型、引用、维度标尺和应用程序头 |
| 依赖项 | 在明确提供配套文件或清单时支持外部存储、虚拟数据集、外部链接以及 Family/Split 文件 |

对于损坏前已受保护的采集数据，保留的副本和奇偶校验数据还可以重建丢失的数据。配套文件发现、可继续运行的恢复任务和流式资源预算有助于处理更大的科学数据工作流程。

**1.0.0rc1 自动恢复基准测试**覆盖 23 类生成的数据与布局。其 **109 次受控损坏文件试验**得到 77 次完全精确恢复、27 次部分恢复和 5 次拒绝恢复，即 **104 个可用输出（95.4%）**。原始元素中有 83.1% 在其精确坐标上得到恢复，已接受的值没有错误，源文件也没有被修改。声明的 15 个块维度故障全部产生了可用输出，包括五维数组、scale-offset、变长字符串、不规则数组及带变长字段的复合记录。[查看完整案例报告](benchmarks/results/v100rc1-release-coverage.json)或[恢复范围细目](docs/coverage.md)。

已记录的 v0.14.0 评估包括：

| 评估 | 记录的结果 |
| --- | --- |
| [四个完整的科学数据文件](benchmarks/results/v014-scientific-whole.json) | 将 251 个数据集和 253 个属性与保留的原始文件逐一精确比较 |
| [受控损坏的 GWOSC 索引链接](benchmarks/results/v014-gwosc-controlled.json) | 128/128 个块在其精确坐标上恢复；错误块数量为零 |
| [MATLAB 7.3、netCDF4 和 NWB](benchmarks/results/v014-application-readers.json) | 独立读取器打开了六个完整文件或受控损坏文件的输出；已接受的元素没有错误，损坏区域仍标记为未知 |

[基准测试指南](benchmarks/README.md)介绍如何复现评估。[语料说明](corpus/README.md)提供科学数据文件的来源与署名信息。

## 贡献与问题报告

提交 [issue](https://github.com/xShadyy/H5Reclaim/issues) 时，请附上命令、工具和 Python 的版本、观察到的错误以及预期结果。提供小型可复现文件和对应报告，有助于区分不支持的存储方式与恢复程序缺陷。

如需报告漏洞，请遵循[安全问题报告政策](SECURITY.md)；分享报告前，请检查其中是否含有敏感路径和元数据。

参与开发时，请运行 `python -m pip install -e ".[filters]"` 安装，然后运行：

```sh
python -m unittest discover -s tests -q
```

测试会检查恢复正确性、源文件保持不变以及未解决数据的处理方式。基准测试通过独立保存的原始文件评估已接受的值。[仓库指南](docs/file-guide.md)列出了实现文件，并解释其存在的原因。
[发布操作手册](docs/releasing.md)记录候选版本构建、标签校验门槛以及稳定版 1.0 仍需完成的检查。

## 许可证

源代码采用 [Apache License 2.0](LICENSE)。随附的科学数据文件有[单独的署名信息和许可证](corpus/README.md)。

<sub>由 Tymoteusz Netter 创建。</sub>
