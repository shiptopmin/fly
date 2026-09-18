# ✈️ Flight Price Tracker

Google Flights 날짜 표(Date Grid)에서 **실제로 표시된 왕복 가격만** 매일 수집하고,
대상 월의 1~7박 여행 조합 중 저렴한 날짜를 찾아 GitHub Pages 에 보여주는 개인용 도구입니다.

- 가격은 절대 임의로 만들거나 추정하지 않습니다. 확인 못 한 날짜는 "확인 불가" 로 표시합니다.
- 셀 가격 의미: 해당 출발일/귀국일 조합의 **왕복 총액(성인 1명, 필수 세금·수수료 포함)**.
- CAPTCHA/차단이 발생하면 기록하고 멈춥니다. 우회하지 않습니다.

## 폴더 구조

```
config.py             설정 (노선, 대상 월, 숙박일수, 파일 경로)
main.py               가격 수집  (python main.py / --debug)
analyze.py            터미널 분석 출력 (python analyze.py / --all)
build_dashboard.py    docs/index.html 생성
collectors/           수집 계층 (google_flights.py) - 교체 가능
analyzer/             분석 계층 (combinations, stats, report)
storage.py            CSV 누적 저장
data/flights_raw.csv  가격 이력 (커밋됨)
docs/index.html       Dashboard (GitHub Pages)
.github/workflows/tracker.yml   매일 09:10 KST 자동 실행
```

## 로컬 실행 (Anaconda Prompt)

```bash
pip install -r requirements.txt
python -m playwright install chromium
python main.py            # 수집 (headless).  --debug 를 붙이면 브라우저가 보임
python analyze.py         # 분석 결과 출력
python build_dashboard.py # docs/index.html 생성
```

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
