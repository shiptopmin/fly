# ✈️ Flight Price Tracker

Google Flights 날짜 표(Date Grid)에서 **실제로 표시된 왕복 가격만** 매일 수집하고,
대상 월의 1~7박 여행 조합 중 저렴한 날짜를 찾아 GitHub Pages 에 보여주는 개인용 도구입니다.

- 가격은 절대 임의로 만들거나 추정하지 않습니다. 확인 못 한 날짜는 "확인 불가" 로 표시합니다.
- 셀 가격 의미: 해당 출발일/귀국일 조합의 **왕복 총액(성인 1명, 필수 세금·수수료 포함)**.
- CAPTCHA/차단이 발생하면 기록하고 멈춥니다. 우회하지 않습니다.

## 폴더 구조

```
config.py             설정 (추적 노선 목록 ROUTES, 숙박일수, 파일 경로)
routes.py             노선 목록 로더 (config.ROUTES -> Route)
main.py               가격 수집 - 노선마다 반복 (python main.py / --debug)
analyze.py            터미널 분석 출력 - 노선별 (python analyze.py / --route ICN-KIX / --all)
build_dashboard.py    docs/index.html(홈) + docs/routes/<노선>.html 생성
search.py             조건 검색 (--compare 를 붙이면 트래커 이력과 비교한 판정도 함께 표시)
                      --to 에 도시명/국가명/쉼표 목록 가능: --to 도쿄 / --to 일본 / --to KIX,FUK
search_conditions.py  검색 조건(SearchQuery)
destinations.py       목적지 이름 해석 (data/destinations.json 마스터: 공항/도시/국가/지역)
web.py                로컬 웹 검색 폼 (python web.py -> http://127.0.0.1:5000). search.py 의 run_search() 를 그대로 사용
collectors/           수집 계층 (google_flights.py) - 교체 가능
analyzer/             분석 계층 (combinations, stats, report, history, deals, charts) - 노선별로 따로 계산
data/diagnostics/     가격 의미 검증 기록 (append-only). 불일치 사실만 남기고 가격은 보정하지 않음
storage.py            CSV 누적 저장
data/history/<노선>[-연-월].csv   (노선, 월) 별 가격 이력 (커밋됨)  예: data/history/ICN-KIX.csv(10월), ICN-KIX-2026-11.csv
data/searches/        일회성 검색 결과 (로컬 전용, 커밋 안 함)
docs/index.html       홈: 추적 노선 카드 (GitHub Pages)
docs/routes/          노선별 상세 대시보드
.github/workflows/tracker.yml   매일 09:10 KST 자동 실행
```

## 노선 추가하기

`config.py` 의 `ROUTES` 목록에 한 줄 추가하고 push 하면 다음 실행부터 수집됩니다.

```python
ROUTES = [
    {"origin": "ICN", "destination": "KIX"},
    {"origin": "ICN", "destination": "FUK"},
    {"origin": "GMP", "destination": "HND", "min_nights": 2, "max_nights": 4},
]
```

- 월을 적지 않으면 **이번 달 + 다음 달**을 매일 함께 수집합니다 (`TRACK_MONTH_OFFSETS = (0, 1)`).
  다음 달을 미리 수집하므로 그 달이 이번 달이 되었을 때 자체 이력이 이미 쌓여 있습니다. 매달 1일이 되면 새 '다음 달'이 자동으로 추가됩니다.
- 한 달만 추적하고 싶으면 `"year": 2026, "month": 12` 를 적습니다.
- (노선, 월) 마다 이력 파일과 상세 페이지가 따로 만들어집니다: `data/history/ICN-KIX-2026-11.csv`, `docs/routes/ICN-KIX-2026-11.html`.
  10월 분만 이전부터 쓰던 이름(`ICN-KIX.csv`, `ICN-KIX.html`)을 그대로 씁니다(`LEGACY_SLUGS`).
  한 파일에 두 달을 쌓으면 이력의 '하루'(그날 마지막 실행)가 한 달만 남아 다른 달의 하루가 사라지므로 파일을 나눕니다.
- 달이 완전히 지나면 수집을 멈추고 화면에는 마지막 데이터로 '종료'로 남습니다. 남은 출발일이 없는 날은 접속하지 않고 건너뜁니다(실패 아님).
- 새 달은 수집만 하고 화면에서는 숨깁니다(`PUBLIC_THROUGH`). 판정 규칙 검증이 끝난 뒤 올립니다.

## 로컬 실행 (Anaconda Prompt)

```bash
pip install -r requirements.txt
python -m playwright install chromium
python main.py            # 수집 (headless).  --debug 를 붙이면 브라우저가 보임
python analyze.py         # 분석 결과 출력
python build_dashboard.py # docs/index.html 생성
```

## 좋은 가격 판정 기준 (DEAL / WATCH / NORMAL / HOLD)

판정할 가격(현재 관측)은 **판정 시점보다 먼저 수집된 모든 출처의 관측**과만 비교합니다. 현재 관측은 이력에 저장되어
있어도 비교 대상에서 빠집니다.
①③ 은 현재 관측과 **출발일이 같은 달**인 관측끼리만 비교합니다(출발 날짜의 달이 다르면 시즌/연휴가 달라 요금 수준이 다름).
같은 출발월 이력이 7일 미만이면 다른 달로 대신하지 않고 HOLD 입니다.

| 조건 | 질문 |
|---|---|
| ① 신저가 | 같은 숙박일수 모든 일정의 과거 최저보다 3% 이상 낮은가 |
| ② 동일 일정 하락 | 정확히 같은 일정의 과거 최저보다 3% 이상 낮은가 (같은 일정 이력 5일 이상) |
| ③ 평소 대비 저가 | 같은 숙박일수 모든 일정의 최근 30일 일별 최저가 평균보다 15% 이상 낮은가 |

- **DEAL** = ③ 그리고 (① 또는 ②). ①이 성립하면 ②는 같은 사실이라 따로 세지 않습니다.
- **WATCH** = ①②③ 중 하나만 성립(강한 신호), 또는 평균보다 5% 이상 낮음 / 같은 일정이 전일보다 10% 이상 하락(약한 신호).
- **HOLD** = 판정할 이력이 부족함(같은 숙박일수의 30일 창 수집일 7일 미만). 다른 뜻은 없습니다.
- 기준값은 `config.py` 의 `DEAL_RULES` 에 있습니다. `deal_requires_pair_low=True` 로 두면 DEAL 에 ② 도 요구합니다(기본 꺼짐).

### Probe 사전 측정 (월이 바뀔 때 HOLD 를 없애기 위한 기록)

Probe 의 기준 창은 매달 1일에 다음 달로 넘어갑니다. 새 창의 같은 출발월 이력은 비어 있으므로 아무 준비가 없으면 7일간 HOLD 입니다.
그래서 **다음 달 1일의 8일 전부터**(`config.PROBE_PREROLL_DAYS`, 0 이면 끔) 다음에 쓸 창을 미리 재서 `data/probes/preroll/` 에 **기록만** 합니다.

- 판정, 후보 선정, 정밀 검색에는 쓰지 않습니다. 창이 넘어간 날부터 그 달 이력으로만 쓰입니다 (출발월 한정 비교 원칙 그대로).
- 본 측정 파일(`data/probes/ICN-XXX.csv`)과 섞지 않습니다. 이력의 '하루'는 출처 파일마다 그날 마지막 실행 하나로 정해지기 때문에, 같은 파일에 쌓으면 한쪽 창의 하루가 사라집니다.
- 완료 여부는 `data/probes/preroll/status.csv` 에 목적지별로 기록합니다(append-only). 같은 날 재실행할 때는 정상 완료된 목적지만 건너뜁니다.
  정상 완료 = 조합·날짜·숙박일수별 건수가 기대와 정확히 같고, 수집 오류/차단/가격 의미 불일치가 없고, 오늘의 마지막 측정일 때만입니다.
- 차단이 감지되면 우회하지 않고 멈추며 워크플로를 실패로 표시합니다. 이때도 본 측정 결과와 데이터는 그대로 저장됩니다.
- 실행 요약(`data/probes/feed/*.json`)에는 `preroll` 키로 따로 기록합니다. 기존 키와 값은 바뀌지 않습니다.
- 계획만 확인: `python probe.py --dry-run --today 2026-10-24`

테스트: `python -m unittest discover -s tests -t .`

## 웹에서 검색하기 (로컬)

```bash
python web.py
```

브라우저에서 http://127.0.0.1:5000 을 열고 출발 공항, 목적지(KIX / 도쿄 / 일본 / KIX,FUK), 출발 가능 기간,
숙박일수, (선택) 귀국 가능 기간, 직항 여부를 입력한 뒤 "검색" 을 누릅니다.
목적지 1곳당 페이지 로드 1~9회(1회 약 10초)가 걸리므로 결과가 나올 때까지 1~5분 기다립니다.
서버를 켠 PC 안에서만 접속되며, 트래커 데이터(data/history/)에는 영향을 주지 않습니다.

## GitHub 에 올리고 자동 실행하기

1. GitHub 에서 새 저장소를 만듭니다 (예: `flight-tracker`). README 등은 추가하지 않습니다.
2. 이 폴더에서:
   ```bash
   git remote add origin https://github.com/<계정>/<저장소>.git
   git push -u origin main
   ```
3. 저장소 **Settings → Actions → General → Workflow permissions** 에서
   "Read and write permissions" 를 선택하고 저장합니다. (결과를 커밋하기 위해 필요)
4. **Settings → Pages → Build and deployment** 에서
   Source: *Deploy from a branch*, Branch: `main`, Folder: `/docs` 를 선택하고 저장합니다.
5. **Actions 탭 → Flight Price Tracker → Run workflow** 로 한 번 수동 실행해 봅니다.
   - 성공하면 `data/`, `docs/` 가 커밋되고 잠시 후 `https://<계정>.github.io/<저장소>/` 에서 볼 수 있습니다.
   - 실패하면 Actions 로그와 `data/last_run.json` 의 `errors` 를 확인합니다.
     GitHub Actions 환경(해외 IP)에서 Google 이 차단하면 로컬과 결과가 다를 수 있으며,
     그 경우 수집 방식을 재검토합니다 (우회하지 않음).

이후에는 매일 09:10 KST 에 자동 실행됩니다. GitHub 예약 실행은 지연될 수 있으므로
실제 실행 시각은 페이지의 "마지막 업데이트" 를 기준으로 보세요.

## 설정 바꾸기

`config.py` 의 `ORIGIN`, `DESTINATION`, `TARGET_YEAR`, `TARGET_MONTH`, `MIN_NIGHTS`, `MAX_NIGHTS` 만 수정하면 됩니다.
