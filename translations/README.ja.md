<p align="center">
  <img src="../assets/h5reclaim-banner.png" alt="H5Reclaim：科学データ用 HDF5 ファイルの復旧" width="760">
</p>

<p align="center">
  <a href="../pyproject.toml"><img alt="Python 3.10+" src="https://img.shields.io/badge/Python-3.10%2B-3559F0?style=flat-square&amp;logo=python&amp;logoColor=white"></a>
  <a href="../LICENSE"><img alt="ライセンス：Apache 2.0" src="https://img.shields.io/badge/License-Apache%202.0-263557?style=flat-square"></a>
  <a href="https://github.com/xShadyy/H5Reclaim/blob/main/benchmarks/results/v100rc1-release-coverage.json"><img alt="条件を制御したベンチマーク：95.4% で有用な復旧" src="https://img.shields.io/badge/Controlled%20benchmark-95.4%25-3559F0?style=flat-square"></a>
</p>

<p align="center">
  <strong>言語:</strong>
  <a href="../README.md" lang="en">English</a> · <a href="README.pl.md" lang="pl">Polski</a> · <a href="README.de.md" lang="de">Deutsch</a> · <a href="README.fr.md" lang="fr">Français</a> · <a href="README.es.md" lang="es">Español</a> · <a href="README.pt-BR.md" lang="pt-BR">Português (Brasil)</a><br>
  <a href="README.zh-CN.md" lang="zh-CN">简体中文</a> · <a href="README.ja.md" lang="ja">日本語</a> · <a href="README.ko.md" lang="ko">한국어</a> · <a href="README.ru.md" lang="ru">Русский</a> · <a href="README.ar.md" lang="ar">العربية</a>
</p>

H5Reclaim は、破損した HDF5 ファイルから科学データを復旧します。書き込みの中断、インデックスやメタデータの破損、ファイルの切り詰めが起きた場合に、残存する測定データを利用可能な新しいファイルに取り出します。HDF5 は、多くの科学機器やアプリケーションが配列、測定データ、そのメタデータの保存に使用する形式です。

**1 つのコマンドでデータセットを検出し、復旧方法を選択して、明確な JSON レポートとともに新しい HDF5 ファイルを作成します。** 元のファイルは変更しません。H5Reclaim は一部が破損したデータセットから読み取り可能な測定値を保持し、ファイルの残りの部分も復旧し続けます。

破損ファイルを用いた管理された試験 109 件で、バージョン 1.2.0 は有用な結果を 104 件得ました。完全に正確な復旧は 77 件、部分的な復旧は 27 件で、5 件は復旧を見送り、誤った値は採用しませんでした。この結果は従来の 1.0.0 の同じ試験と同一です。[1.2.0 の結果](../benchmarks/results/v120-release-coverage.json)。

バージョン 1.2.0 は、各修復に独立した根拠がある複数の破損した索引リンクや、より大きな固定長レコードの索引にも対応します。2 つのリンクが破損した例を含む[別の圧縮データ試験 8 件](../benchmarks/results/v120-streaming-recovery.json)では、誤った値は採用されませんでした。

検査・修復ツール、対応形式、検証、一括処理については[使用ガイド](../USAGE.md)を参照してください。

<a id="quick-start"></a>
## クイックスタート

Python 3.10 以降が必要です。このリポジトリをクローンするか、GitHub から ZIP をダウンロードして展開してください。

```sh
git clone https://github.com/xShadyy/H5Reclaim.git
cd H5Reclaim
```

リポジトリのルートでインストールし、ファイルを復旧します。

```sh
python -m pip install .
python -m h5reclaim rescue "damaged.h5"
```

## 結果の読み方

端末の要約には、復旧が完全か部分的か、および出力先が表示されます。JSON レポートには、復旧したデータセット、使用した方法、未解決の位置、復元したメタデータが記録されます。

復旧経路によって**ステータスマップ**が生成された場合、採用された測定値がどの位置にあるかを示します。解析時にはレポートに記載されたマップを使用して値を選択してください。不明な位置にはゼロなどの埋め値が表示されることがあります。一部の無傷の連続配置データセットと null データセットには確認可能なマップがないため、解析前に各復旧経路のレポートを確認してください。過去のベースラインや保護バンドルがあれば、以前の取得データとの比較もできます。

## ライセンス

ソースコードは [Apache License 2.0](../LICENSE) で提供しています。同梱の科学データファイルには[別個の帰属情報とライセンス](https://github.com/xShadyy/H5Reclaim/blob/main/corpus/README.md)が適用されます。

<sub>作成者：Tymoteusz Netter。</sub>
