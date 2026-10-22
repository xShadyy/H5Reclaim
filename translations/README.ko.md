<p align="center">
  <img src="../assets/h5reclaim-banner.png" alt="H5Reclaim: 과학 HDF5 데이터 복구" width="760">
</p>

<p align="center">
  <a href="../pyproject.toml"><img alt="Python 3.10+" src="https://img.shields.io/badge/Python-3.10%2B-3559F0?style=flat-square&amp;logo=python&amp;logoColor=white"></a>
  <a href="../LICENSE"><img alt="라이선스: Apache 2.0" src="https://img.shields.io/badge/License-Apache%202.0-263557?style=flat-square"></a>
  <a href="https://github.com/xShadyy/H5Reclaim/blob/main/benchmarks/results/v100rc1-release-coverage.json"><img alt="통제된 벤치마크: 95.4% 유용한 복구" src="https://img.shields.io/badge/Controlled%20benchmark-95.4%25-3559F0?style=flat-square"></a>
</p>

<p align="center">
  <strong>언어:</strong>
  <a href="../README.md" lang="en">English</a> · <a href="README.pl.md" lang="pl">Polski</a> · <a href="README.de.md" lang="de">Deutsch</a> · <a href="README.fr.md" lang="fr">Français</a> · <a href="README.es.md" lang="es">Español</a> · <a href="README.pt-BR.md" lang="pt-BR">Português (Brasil)</a><br>
  <a href="README.zh-CN.md" lang="zh-CN">简体中文</a> · <a href="README.ja.md" lang="ja">日本語</a> · <a href="README.ko.md" lang="ko">한국어</a> · <a href="README.ru.md" lang="ru">Русский</a> · <a href="README.ar.md" lang="ar">العربية</a>
</p>

H5Reclaim은 손상된 HDF5 파일에서 과학 데이터를 복구합니다. 쓰기가 중단되거나, 인덱스 및 메타데이터가 손상되거나, 파일이 잘린 뒤에도 남아 있는 측정값을 사용할 수 있는 새 파일로 옮깁니다. HDF5는 많은 과학 기기와 응용 프로그램에서 배열, 측정값 및 관련 메타데이터를 저장하는 형식입니다.

**명령 하나로 데이터셋을 찾아 복구 방법을 선택하고, 명확한 JSON 보고서와 함께 새 HDF5 파일을 생성합니다.** 원본은 그대로 유지됩니다. H5Reclaim은 일부가 손상된 데이터셋에서 읽을 수 있는 측정값을 보존하고 파일의 나머지 부분도 계속 복구합니다.

1.0.0rc1 출시 후보 버전은 **통제된 손상 파일 실험 109건에서 유용한 복구율 95.4%**를 기록했습니다. 완전히 정확한 복구 77건과 부분 복구 27건이 있었으며, 이 실험군에서 잘못된 값을 받아들인 사례는 없었습니다. 생성된 이 실험은 명시된 손상 유형을 측정하며, 다른 파일에서의 예상 성공률을 뜻하지는 않습니다. [결과 살펴보기](https://github.com/xShadyy/H5Reclaim/blob/main/benchmarks/results/v100rc1-release-coverage.json).

<a id="quick-start"></a>
## 빠른 시작

Python 3.10 이상이 필요합니다. 이 저장소를 복제하거나 GitHub에서 ZIP 파일을 내려받아 압축을 푸세요.

```sh
git clone https://github.com/xShadyy/H5Reclaim.git
cd H5Reclaim
```

저장소 루트에서 설치하고 파일을 복구하세요.

```sh
python -m pip install .
python -m h5reclaim rescue "damaged.h5"
```

## 결과 이해하기

터미널 요약에는 완전 복구인지 부분 복구인지와 출력 파일의 위치가 표시됩니다. JSON 보고서에는 복구된 데이터셋, 복구 방법, 아직 해결되지 않은 위치, 복원된 메타데이터가 기록됩니다.

복구 경로에서 **상태 맵**을 제공하는 경우, 어떤 위치에 허용된 측정값이 있는지 표시합니다. 분석에 사용할 값을 고를 때 보고서에 명시된 맵을 이용하세요. 알 수 없는 위치에는 0과 같은 채움 값이 표시될 수 있습니다. 일부 온전한 연속형 데이터셋과 null 데이터셋에는 확인 가능한 맵이 없으므로, 분석 전에 해당 복구 경로의 보고서를 확인하세요. 이전 기준본과 보호 번들은 과거에 저장한 사본과 비교하는 기능을 추가합니다.

## 라이선스

소스 코드는 [Apache License 2.0](../LICENSE)에 따라 제공됩니다. 포함된 과학 파일에는 [별도의 출처 표기와 라이선스](https://github.com/xShadyy/H5Reclaim/blob/main/corpus/README.md)가 적용됩니다.

<sub>제작: Tymoteusz Netter.</sub>
