<p align="center">
  <img src="assets/h5reclaim-banner.png" alt="H5Reclaim：科学データ用 HDF5 ファイルの復旧" width="760">
</p>

<p align="center">
  <a href="pyproject.toml"><img alt="Python 3.10+" src="https://img.shields.io/badge/Python-3.10%2B-3559F0?style=flat-square&amp;logo=python&amp;logoColor=white"></a>
  <a href="LICENSE"><img alt="ライセンス：Apache 2.0" src="https://img.shields.io/badge/License-Apache%202.0-263557?style=flat-square"></a>
  <a href="docs/coverage.md"><img alt="条件を制御したベンチマーク：95.4% で有用な復旧" src="https://img.shields.io/badge/Controlled%20benchmark-95.4%25-3559F0?style=flat-square"></a>
</p>

<p align="center">
  <strong>言語:</strong>
  <a href="README.md" lang="en">English</a> · <a href="README.pl.md" lang="pl">Polski</a> · <a href="README.de.md" lang="de">Deutsch</a> · <a href="README.fr.md" lang="fr">Français</a> · <a href="README.es.md" lang="es">Español</a> · <a href="README.pt-BR.md" lang="pt-BR">Português (Brasil)</a><br>
  <a href="README.zh-CN.md" lang="zh-CN">简体中文</a> · <a href="README.ja.md" lang="ja">日本語</a> · <a href="README.ko.md" lang="ko">한국어</a> · <a href="README.ru.md" lang="ru">Русский</a> · <a href="README.ar.md" lang="ar">العربية</a>
</p>

<!-- English README SHA-256: 5a2c704dd8e97c5bb668d1c2f9e856b9e21568e44477487b74e281f694a66646 -->

H5Reclaim は、破損した HDF5 ファイルから科学データを復旧します。書き込みの中断、インデックスやメタデータの破損、ファイルの切り詰めが起きた場合に、残存する測定データを利用可能な新しいファイルに取り出します。HDF5 は、多くの科学機器やアプリケーションが配列、測定データ、そのメタデータの保存に使用する形式です。

**1 つのコマンドでデータセットを検出し、復旧方法を選択して、明確な JSON レポートとともに新しい HDF5 ファイルを作成します。** 元のファイルは変更しません。H5Reclaim は一部が破損したデータセットから読み取り可能な測定値を保持し、ファイルの残りの部分も復旧し続けます。

リリース候補版 1.0.0rc1 は、**条件を制御して破損させたファイルによる試験 109 件のうち 95.4% で有用な復旧**を達成しました。内訳は完全かつ正確な復旧が 77 件、部分的な復旧が 27 件で、この試験群では採用された値に誤りはありませんでした。これらの生成された試験が測定するのは明示された障害の種類であり、ほかのファイルで期待できる成功率ではありません。[結果を見る](docs/coverage.md)。

[クイックスタート](#quick-start) · [使い方](docs/usage.md) · [復旧対象](docs/coverage.md) · [レポート形式](docs/report-schema.md) · [リリースノート](CHANGELOG.md)

<a id="quick-start"></a>
## クイックスタート

Python 3.10 以降が必要です。このリポジトリをクローンするか、GitHub から ZIP をダウンロードして展開してください。

```sh
git clone https://github.com/xShadyy/H5Reclaim.git
cd H5Reclaim
python -m venv .venv
```

仮想環境を有効にします。

| システム | コマンド |
| --- | --- |
| Windows PowerShell | `.venv\Scripts\Activate.ps1` |
| Windows コマンドプロンプト | `.venv\Scripts\activate.bat` |
| macOS / Linux | `. .venv/bin/activate` |

リポジトリのルートでインストールし、ファイルを復旧します。

```sh
python -m pip install .
python -m h5reclaim rescue "damaged.h5"
```

ソースファイルと同じ場所に `damaged.recovered.h5` と `damaged.recovered.report.json` が作成されます。既存の出力先は上書きされません。別の場所を指定する場合や再実行する場合は、パスを明示してください。

```sh
python -m h5reclaim rescue "damaged.h5" --output "rescued.h5" --report "evidence.json"
```

インストールされた `h5reclaim` コマンドも使用できます。コマンドの一覧は `python -m h5reclaim --help`、復旧オプションは `python -m h5reclaim rescue --help` で確認できます。

## 結果の読み方

端末の要約には、復旧が完全か部分的か、および出力先が表示されます。JSON レポートには、復旧したデータセット、使用した方法、未解決の位置、復元したメタデータが記録されます。

使用する前に、保存したレポートと公開済みの出力が一致しているか確認してください。

```sh
python -m h5reclaim verify-result "damaged.recovered.h5" "damaged.recovered.report.json" --source "damaged.h5"
```

このコマンドは、リソースに上限を設けたワーカープロセスで、レポート、データセットの形状、ソースのハッシュ、妥当性マップの整合性を確認します。復旧した測定値そのものは読み取らず、その正しさも認証しません。確認可能なマップがない復旧経路は、非対応という結果を返します。終了コードと制限については[使い方](docs/usage.md#verify-a-published-result)を参照してください。

復旧経路によって**ステータスマップ**が生成された場合、採用された測定値がどの位置にあるかを示します。解析時にはレポートに記載されたマップを使用して値を選択してください。不明な位置にはゼロなどの埋め値が表示されることがあります。一部の無傷の連続配置データセットと null データセットには確認可能なマップがないため、解析前に各復旧経路のレポートを確認してください。過去のベースラインや保護バンドルがあれば、以前の取得データとの比較もできます。

固定サイズのデータセットでは、不明な位置をマスクした状態で復旧値を読み取れます。

```python
from h5reclaim import read_masked

values = read_masked(
    "damaged.recovered.h5", "damaged.recovered.report.json",
    "/experiment/readings", selection=(slice(0, 1000), Ellipsis),
)
```

このリソースに上限を設けたリーダーは、レポートに記載されたデータセットのステータスマップを使用します。マスクが示すのは破損した入力から採用された値であり、その値が以前の取得データと一致することを証明するものではありません。対応する選択方法と制限については[使い方](docs/usage.md#read-results)を参照してください。

## よく使う操作

| 目的 | コマンド |
| --- | --- |
| 復旧前にファイルを調べる | `python -m h5reclaim diagnose damaged.h5` |
| 1 つのデータセットを復旧する | `python -m h5reclaim rescue damaged.h5 --dataset /experiment/readings` |
| 既存の結果を要約する | `python -m h5reclaim report damaged.recovered.report.json` |
| 出力とレポートの整合性を確認する | `python -m h5reclaim verify-result damaged.recovered.h5 damaged.recovered.report.json` |
| 時間のかかる復旧を再開する | `python -m h5reclaim rescue damaged.h5 --resume-dir recovery-progress` |
| 関連ファイルを指定する | `python -m h5reclaim rescue container.h5 --related-dir companion-files` |
| 名前が失われたデータセットを探す | `python -m h5reclaim discover damaged.h5 --json` |

ファイルによっては追加の圧縮コーデックが必要です。リポジトリのルートで `python -m pip install ".[filters]"` を実行してから再試行してください。[使い方](docs/usage.md)では、大きなファイル、リソース上限、依存ファイル、再開可能な処理、以前に作成した保護バンドルを説明しています。

## 復旧対象

H5Reclaim は各ファイル自身のデータセット記述を読み取るため、実験、データセット名、配列の形状が異なっても同じ手順で使用できます。

| 分野 | 対応済みの範囲 |
| --- | --- |
| 保存方式 | コンパクト、連続配置、チャンク形式のデータセット。従来型と新しいチャンクインデックス |
| 値 | 数値配列、複合レコード、固定長と可変長の文字列、長さが不揃いな配列、列挙型、参照、空のデータセットと null データセット |
| 破損 | インデックスリンクの破損からの復旧、チェックサムで根拠を確認したメタデータとチャンク次元の修正、新しい形式のシグネチャ修復、書き込み中断フラグ、残存するデータセットヘッダー、読み取り不能なチャンク、ファイル末尾の物理的な切り詰め |
| 圧縮 | DEFLATE、LZF、shuffle、Fletcher32、および対応する追加の同梱コーデック |
| ファイル構造 | 利用可能なグループ、属性、リンク、名前付きデータ型、参照、次元スケール、アプリケーションヘッダー |
| 依存関係 | 関連ファイルまたはマニフェストを明示的に指定した場合の外部ストレージ、仮想データセット、外部リンク、Family/Split ファイル |

破損前に保護した取得データでは、保持しておいた複製やパリティによって失われたデータを再構築することもできます。関連ファイルの検出、復旧処理の再開、ストリーミング時のリソース上限により、大規模な科学データの処理にも対応します。

**1.0.0rc1 の自動復旧ベンチマーク**は、生成されたデータとレイアウトの 23 ファミリーを対象としています。**条件を制御して破損させたファイルによる試験 109 件**の結果は、完全かつ正確な復旧が 77 件、部分的な復旧が 27 件、復旧の拒否が 5 件で、**有用な出力は 104 件（95.4%）**でした。元の要素の 83.1% を正確な座標で復旧し、採用された値の誤りはゼロ、ソースファイルの変更もゼロでした。明示されたチャンク次元の障害 15 件はすべて有用な出力につながりました。これには 5 次元配列、scale-offset、可変長文字列、長さが不揃いな配列、可変長フィールドを持つ複合データが含まれます。[全ケースのレポート](benchmarks/results/v100rc1-release-coverage.json)または[復旧範囲の内訳](docs/coverage.md)をご覧ください。

記録済みの v0.14.0 の評価には、以下が含まれます。

| 評価 | 記録された結果 |
| --- | --- |
| [無傷の科学データファイル 4 件](benchmarks/results/v014-scientific-whole.json) | 251 個のデータセットと 253 個の属性が、保持しておいた元ファイルと正確に一致 |
| [条件を制御して破損させた GWOSC インデックスリンク](benchmarks/results/v014-gwosc-controlled.json) | 128/128 チャンクを正確な座標で復旧。誤ったチャンクはゼロ |
| [MATLAB 7.3、netCDF4、NWB](benchmarks/results/v014-application-readers.json) | 独立したリーダーが、無傷のファイルまたは条件を制御して破損させたファイルの出力 6 件を開いた。採用された要素の誤りはゼロで、破損した領域は不明のまま |

[ベンチマークガイド](benchmarks/README.md)では評価の再現方法を説明しています。[コーパスの説明](corpus/README.md)には科学データファイルの出典と帰属を記載しています。

## 貢献と問題の報告

問題を報告する場合は、使用したコマンド、ツールと Python のバージョン、発生したエラー、期待した結果を添えて [issue](https://github.com/xShadyy/H5Reclaim/issues) を作成してください。再現可能な小さなファイルとそのレポートがあれば、非対応の保存方式と復旧処理の不具合を区別しやすくなります。

脆弱性は[セキュリティ報告ポリシー](SECURITY.md)に従って報告してください。レポートを共有する前に、機密性のあるパスやメタデータが含まれていないか確認してください。

開発に参加する場合は、`python -m pip install -e ".[filters]"` でインストールしてから、次を実行してください。

```sh
python -m unittest discover -s tests -q
```

テストでは、復旧の正確性、ソースファイルの保持、未解決データの扱いを確認します。ベンチマークでは、別途保持した元ファイルと採用された値を比較します。[リポジトリガイド](docs/file-guide.md)には実装の構成と各ファイルの役割を記載しています。
[リリース手順書](docs/releasing.md)には、候補版のビルド、タグに対する検証ゲート、安定版 1.0 に向けた残りの確認事項を記載しています。

## ライセンス

ソースコードは [Apache License 2.0](LICENSE) で提供しています。同梱の科学データファイルには[別個の帰属情報とライセンス](corpus/README.md)が適用されます。

<sub>作成者：Tymoteusz Netter。</sub>
