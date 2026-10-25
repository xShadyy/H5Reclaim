<p align="center">
  <img src="../assets/h5reclaim-banner.png" alt="H5Reclaim：科学 HDF5 数据恢复工具" width="760">
</p>

<p align="center">
  <a href="../pyproject.toml"><img alt="Python 3.10+" src="https://img.shields.io/badge/Python-3.10%2B-3559F0?style=flat-square&amp;logo=python&amp;logoColor=white"></a>
  <a href="../LICENSE"><img alt="许可证：Apache 2.0" src="https://img.shields.io/badge/License-Apache%202.0-263557?style=flat-square"></a>
  <a href="https://github.com/xShadyy/H5Reclaim/blob/main/benchmarks/results/v130-release-coverage.json"><img alt="受控基准测试：95.4% 的恢复结果可用" src="https://img.shields.io/badge/Controlled%20benchmark-95.4%25-3559F0?style=flat-square"></a>
</p>

<p align="center">
  <strong>语言:</strong>
  <a href="../README.md" lang="en">English</a> · <a href="README.pl.md" lang="pl">Polski</a> · <a href="README.de.md" lang="de">Deutsch</a> · <a href="README.fr.md" lang="fr">Français</a> · <a href="README.es.md" lang="es">Español</a> · <a href="README.pt-BR.md" lang="pt-BR">Português (Brasil)</a><br>
  <a href="README.zh-CN.md" lang="zh-CN">简体中文</a> · <a href="README.ja.md" lang="ja">日本語</a> · <a href="README.ko.md" lang="ko">한국어</a> · <a href="README.ru.md" lang="ru">Русский</a> · <a href="README.ar.md" lang="ar">العربية</a>
</p>

H5Reclaim 可从损坏的 HDF5 文件中恢复科学数据。对于写入中断、索引损坏、元数据损坏和文件截断等情况，它会将仍然存活的测量数据写入一个可用的新文件。HDF5 是许多科学仪器和应用程序用于存储数组、测量数据及其元数据的格式。

**一条命令即可发现数据集、选择恢复方法，并创建新的 HDF5 文件及清晰的 JSON 报告。** 原始文件保持不变。H5Reclaim 可以保留部分损坏的数据集中仍可读取的测量值，并继续恢复文件的其他部分。

在 109 次受控损坏文件试验中，1.3.0 版本获得了 104 个可用结果：77 次完全精确恢复和 27 次部分恢复。另有 5 次拒绝恢复，没有接受错误的数值。这与此前 1.0.0 版本的同一组试验结果相同。[查看 1.3.0 结果](../benchmarks/results/v130-release-coverage.json)。

1.3.0 版本可以修复同一个已验证索引节点中的两个损坏链接，通过原生 HDF5 迁移部分变长数据和引用，覆盖更多空闲空间结构，并保留经过验证的游离组硬链接别名。在[另外十个压缩数据受控案例](../benchmarks/results/v130-streaming-recovery.json)中，包括同一节点内的双链接故障，没有接受错误的数值。

有关检查与修复工具、支持的格式、验证和批量处理，请参阅[使用指南](../USAGE.md)。

<a id="quick-start"></a>
## 快速开始

需要 Python 3.10 或更高版本。克隆此仓库，或从 GitHub 下载 ZIP 文件并解压：

```sh
git clone https://github.com/xShadyy/H5Reclaim.git
cd H5Reclaim
```

在仓库根目录安装，然后恢复文件：

```sh
python -m pip install .
python -m h5reclaim rescue "damaged.h5"
```

## 理解恢复结果

终端摘要会显示恢复是完整还是部分完成，并列出输出位置。JSON 报告标明已恢复的数据集、恢复方法、未解决的位置和恢复的元数据。

如果某条恢复路径生成了**状态映射**，该映射会指示哪些位置包含已接受的测量值。分析时请使用报告中列出的映射来选择数值；未知位置可能显示零等填充值。某些完整的连续存储数据集和空数据集没有可检查的映射，因此分析前应查看相应恢复路径的报告。先前的基线和保护包可用于与更早的采集结果进行比较。

## 许可证

源代码采用 [Apache License 2.0](../LICENSE)。随附的科学数据文件有[单独的署名信息和许可证](https://github.com/xShadyy/H5Reclaim/blob/main/corpus/README.md)。

<sub>由 Tymoteusz Netter 创建。</sub>
