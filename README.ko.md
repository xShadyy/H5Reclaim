<p align="center">
  <img src="assets/h5reclaim-banner.png" alt="H5Reclaim: 과학 HDF5 데이터 복구" width="760">
</p>

<p align="center">
  <a href="pyproject.toml"><img alt="Python 3.10+" src="https://img.shields.io/badge/Python-3.10%2B-3559F0?style=flat-square&amp;logo=python&amp;logoColor=white"></a>
  <a href="LICENSE"><img alt="라이선스: Apache 2.0" src="https://img.shields.io/badge/License-Apache%202.0-263557?style=flat-square"></a>
  <a href="docs/coverage.md"><img alt="통제된 벤치마크: 95.4% 유용한 복구" src="https://img.shields.io/badge/Controlled%20benchmark-95.4%25-3559F0?style=flat-square"></a>
</p>

<p align="center">
  <strong>언어:</strong>
  <a href="README.md" lang="en">English</a> · <a href="README.pl.md" lang="pl">Polski</a> · <a href="README.de.md" lang="de">Deutsch</a> · <a href="README.fr.md" lang="fr">Français</a> · <a href="README.es.md" lang="es">Español</a> · <a href="README.pt-BR.md" lang="pt-BR">Português (Brasil)</a><br>
  <a href="README.zh-CN.md" lang="zh-CN">简体中文</a> · <a href="README.ja.md" lang="ja">日本語</a> · <a href="README.ko.md" lang="ko">한국어</a> · <a href="README.ru.md" lang="ru">Русский</a> · <a href="README.ar.md" lang="ar">العربية</a>
</p>

<!-- English README SHA-256: 5a2c704dd8e97c5bb668d1c2f9e856b9e21568e44477487b74e281f694a66646 -->

H5Reclaim은 손상된 HDF5 파일에서 과학 데이터를 복구합니다. 쓰기가 중단되거나, 인덱스 및 메타데이터가 손상되거나, 파일이 잘린 뒤에도 남아 있는 측정값을 사용할 수 있는 새 파일로 옮깁니다. HDF5는 많은 과학 기기와 응용 프로그램에서 배열, 측정값 및 관련 메타데이터를 저장하는 형식입니다.

**명령 하나로 데이터셋을 찾아 복구 방법을 선택하고, 명확한 JSON 보고서와 함께 새 HDF5 파일을 생성합니다.** 원본은 그대로 유지됩니다. H5Reclaim은 일부가 손상된 데이터셋에서 읽을 수 있는 측정값을 보존하고 파일의 나머지 부분도 계속 복구합니다.

1.0.0rc1 출시 후보 버전은 **통제된 손상 파일 실험 109건에서 유용한 복구율 95.4%**를 기록했습니다. 완전히 정확한 복구 77건과 부분 복구 27건이 있었으며, 이 실험군에서 잘못된 값을 받아들인 사례는 없었습니다. 생성된 이 실험은 명시된 손상 유형을 측정하며, 다른 파일에서의 예상 성공률을 뜻하지는 않습니다. [결과 살펴보기](docs/coverage.md).

[빠른 시작](#quick-start) · [사용 안내](docs/usage.md) · [복구 범위](docs/coverage.md) · [보고서 형식](docs/report-schema.md) · [출시 기록](CHANGELOG.md)

<a id="quick-start"></a>
## 빠른 시작

Python 3.10 이상이 필요합니다. 이 저장소를 복제하거나 GitHub에서 ZIP 파일을 내려받아 압축을 푸세요.

```sh
git clone https://github.com/xShadyy/H5Reclaim.git
cd H5Reclaim
python -m venv .venv
```

가상 환경을 활성화하세요.

| 시스템 | 명령 |
| --- | --- |
| Windows PowerShell | `.venv\Scripts\Activate.ps1` |
| Windows Command Prompt | `.venv\Scripts\activate.bat` |
| macOS / Linux | `. .venv/bin/activate` |

저장소 루트에서 설치하고 파일을 복구하세요.

```sh
python -m pip install .
python -m h5reclaim rescue "damaged.h5"
```

원본 옆에 `damaged.recovered.h5`와 `damaged.recovered.report.json`이 생성됩니다. 기존 대상 파일은 덮어쓰지 않습니다. 다른 위치를 선택하거나 복구를 다시 실행하려면 경로를 명시하세요.

```sh
python -m h5reclaim rescue "damaged.h5" --output "rescued.h5" --report "evidence.json"
```

설치된 `h5reclaim` 명령도 사용할 수 있습니다. 명령 목록은 `python -m h5reclaim --help`로, 복구 옵션은 `python -m h5reclaim rescue --help`로 확인하세요.

## 결과 이해하기

터미널 요약에는 완전 복구인지 부분 복구인지와 출력 파일의 위치가 표시됩니다. JSON 보고서에는 복구된 데이터셋, 복구 방법, 아직 해결되지 않은 위치, 복원된 메타데이터가 기록됩니다.

결과를 사용하기 전에 저장된 보고서와 게시된 출력의 일치 여부를 확인하세요.

```sh
python -m h5reclaim verify-result "damaged.recovered.h5" "damaged.recovered.report.json" --source "damaged.h5"
```

이 명령은 제한된 작업 프로세스에서 보고서, 데이터셋 형태, 원본 해시, 유효성 맵의 일관성을 확인합니다. 복구된 측정값을 읽거나 그 값을 인증하지는 않습니다. 확인 가능한 맵이 없는 복구 경로에서는 지원되지 않음 결과를 반환합니다. 종료 코드와 제한 사항은 [사용 안내](docs/usage.md#verify-a-published-result)를 참고하세요.

복구 경로에서 **상태 맵**을 제공하는 경우, 어떤 위치에 허용된 측정값이 있는지 표시합니다. 분석에 사용할 값을 고를 때 보고서에 명시된 맵을 이용하세요. 알 수 없는 위치에는 0과 같은 채움 값이 표시될 수 있습니다. 일부 온전한 연속형 데이터셋과 null 데이터셋에는 확인 가능한 맵이 없으므로, 분석 전에 해당 복구 경로의 보고서를 확인하세요. 이전 기준본과 보호 번들은 과거에 저장한 사본과 비교하는 기능을 추가합니다.

크기가 고정된 데이터셋에서는 알 수 없는 위치가 미리 마스킹된 복구값을 읽을 수 있습니다.

```python
from h5reclaim import read_masked

values = read_masked(
    "damaged.recovered.h5", "damaged.recovered.report.json",
    "/experiment/readings", selection=(slice(0, 1000), Ellipsis),
)
```

이 제한된 읽기 기능은 보고서에 기록된 데이터셋의 상태 맵을 사용합니다. 마스크는 손상된 입력에서 받아들인 값을 가리키며, 그 값이 과거에 저장한 사본과 일치함을 증명하지는 않습니다. 지원되는 선택 방식과 제한 사항은 [사용 안내](docs/usage.md#read-results)를 참고하세요.

## 주요 작업

| 목적 | 명령 |
| --- | --- |
| 복구 전에 파일 살펴보기 | `python -m h5reclaim diagnose damaged.h5` |
| 데이터셋 하나 복구하기 | `python -m h5reclaim rescue damaged.h5 --dataset /experiment/readings` |
| 기존 결과 요약하기 | `python -m h5reclaim report damaged.recovered.report.json` |
| 출력과 보고서의 일관성 확인하기 | `python -m h5reclaim verify-result damaged.recovered.h5 damaged.recovered.report.json` |
| 오래 걸리는 복구 이어서 하기 | `python -m h5reclaim rescue damaged.h5 --resume-dir recovery-progress` |
| 관련 파일 제공하기 | `python -m h5reclaim rescue container.h5 --related-dir companion-files` |
| 이름이 사라진 데이터셋 찾기 | `python -m h5reclaim discover damaged.h5 --json` |

일부 파일은 추가 압축 코덱이 필요합니다. 저장소 루트에서 `python -m pip install ".[filters]"`를 실행한 다음 다시 시도하세요. 큰 파일, 자원 사용 한도, 의존 파일, 이어서 실행하는 복구, 이전 보호 번들은 [사용 안내](docs/usage.md)에서 설명합니다.

## 복구 범위

H5Reclaim은 각 파일에 담긴 데이터셋 설명을 읽으므로 실험 종류, 데이터셋 이름, 배열 형태가 달라도 같은 절차를 사용할 수 있습니다.

| 영역 | 구현된 범위 |
| --- | --- |
| 저장 방식 | Compact, contiguous, chunked 데이터셋; 구형 및 최신 청크 인덱스 |
| 값 | 숫자 배열, 복합 레코드, 고정 길이 및 가변 길이 문자열, 가변 길이 배열, 열거형, 참조, 빈 데이터셋 및 null 데이터셋 |
| 손상 | 끊어진 인덱스 연결 복구, 체크섬으로 정당화되는 메타데이터 및 청크 차원 수정, 최신 시그니처 복구, 쓰기 중단 플래그, 남아 있는 데이터셋 헤더, 읽을 수 없는 청크, 물리적으로 잘린 파일 끝부분 |
| 압축 | DEFLATE, LZF, shuffle, Fletcher32 및 지원되는 선택적 패키지 코덱 |
| 파일 구조 | 이용 가능한 그룹, 속성, 링크, 이름이 있는 데이터형, 참조, 차원 스케일, 애플리케이션 헤더 |
| 의존성 | 명시적으로 제공된 관련 파일 또는 매니페스트가 있는 외부 저장소, 가상 데이터셋, 외부 링크, Family/Split 파일 |

손상 전에 보호해 둔 수집 데이터는 보관된 복제본과 패리티로 누락된 데이터를 재구성할 수도 있습니다. 관련 파일 탐색, 이어서 하는 복구, 스트리밍 자원 한도는 더 큰 규모의 과학 작업을 지원합니다.

**1.0.0rc1 자동 복구 벤치마크**는 생성된 데이터 및 레이아웃 계열 23개를 다룹니다. **통제된 손상 파일 실험 109건**에서 완전히 정확한 복구 77건, 부분 복구 27건, 복구 거부 5건으로 **유용한 출력 104건(95.4%)**을 기록했습니다. 원본 요소의 83.1%를 정확한 좌표에서 복구했으며, 잘못 받아들인 값과 변경된 원본 파일은 모두 0건이었습니다. 명시된 청크 차원 손상 15건은 모두 유용한 출력을 얻었고, 여기에는 5차원 배열, scale-offset, 가변 길이 문자열, 가변 길이 배열, 가변 길이 필드가 있는 복합 데이터가 포함됩니다. [전체 사례 보고서](benchmarks/results/v100rc1-release-coverage.json) 또는 [복구 범위 상세](docs/coverage.md)를 확인하세요.

기록된 v0.14.0 평가에는 다음이 포함됩니다.

| 평가 | 기록된 결과 |
| --- | --- |
| [온전한 과학 파일 4개](benchmarks/results/v014-scientific-whole.json) | 보관된 원본과 대조해 데이터셋 251개와 속성 253개가 정확히 일치 |
| [통제된 GWOSC 인덱스 연결 손상](benchmarks/results/v014-gwosc-controlled.json) | 청크 128/128개를 정확한 좌표에서 복구; 잘못된 청크 0개 |
| [MATLAB 7.3, netCDF4, NWB](benchmarks/results/v014-application-readers.json) | 독립적인 리더가 온전하거나 통제된 손상이 있는 출력 6개를 열었음; 잘못 받아들인 요소 0개, 손상된 영역은 알 수 없음으로 유지 |

[벤치마크 안내](benchmarks/README.md)에는 평가 재현 방법이 설명되어 있습니다. [코퍼스 안내](corpus/README.md)에는 과학 파일의 출처와 라이선스 정보가 있습니다.

## 기여 및 문제 보고

명령, 도구와 Python 버전, 발생한 오류, 예상 결과를 적어 [이슈](https://github.com/xShadyy/H5Reclaim/issues)를 등록하세요. 재현 가능한 작은 파일과 해당 보고서가 있으면 지원되지 않는 저장 방식인지 복구 버그인지 구별하는 데 도움이 됩니다.

보안 취약점은 [보안 신고 정책](SECURITY.md)을 따르세요. 보고서를 공유하기 전에 민감한 경로와 메타데이터가 들어 있는지 확인하세요.

개발 환경에서는 `python -m pip install -e ".[filters]"`로 설치하고 다음을 실행하세요.

```sh
python -m unittest discover -s tests -q
```

테스트는 복구의 정확성, 원본 보존, 해결되지 않은 데이터의 처리를 확인합니다. 벤치마크는 별도로 보관된 원본과 비교하여 받아들인 값의 정확도를 평가합니다. [저장소 안내](docs/file-guide.md)에는 구현 파일의 위치와 각 파일이 필요한 이유가 설명되어 있습니다.
[출시 절차](docs/releasing.md)에는 후보 버전 빌드, 태그 검사, 안정판 1.0 출시 전에 남은 확인 항목이 기록되어 있습니다.

## 라이선스

소스 코드는 [Apache License 2.0](LICENSE)에 따라 제공됩니다. 포함된 과학 파일에는 [별도의 출처 표기와 라이선스](corpus/README.md)가 적용됩니다.

<sub>제작: Tymoteusz Netter.</sub>
