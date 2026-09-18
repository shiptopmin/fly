# ============================================================
# config.py - 항공권 가격 추적기 설정값
# ------------------------------------------------------------
# 코드 안에 값을 직접 쓰지 않고(하드코딩 금지) 여기서만 바꿉니다.
# 노선이나 대상 월을 바꾸고 싶으면 이 파일만 수정하면 됩니다.
# ============================================================

# --- 추적 노선 목록 (Tracker 가 매일 수집하는 노선) ---
# 한 줄 = 노선 하나. 추가하려면 같은 형식으로 한 줄 넣으면 됩니다.
#   origin/destination : IATA 공항 코드 (ICN 인천, GMP 김포, KIX 오사카, FUK 후쿠오카, NRT 도쿄 ...)
#   year/month         : 추적할 여행월
#   min_nights/max_nights : 생략하면 아래 MIN_NIGHTS / MAX_NIGHTS 기본값 사용
ROUTES = [
    {"origin": "ICN", "destination": "KIX", "year": 2026, "month": 10},
    {"origin": "ICN", "destination": "FUK", "year": 2026, "month": 10},
]

# --- 숙박일수 기본 범위 (노선에 따로 적지 않으면 이 값 사용) ---
MIN_NIGHTS = 1
MAX_NIGHTS = 7

# 귀국일이 다음 달로 넘어가는 조합을 허용할지 (기본: 허용 안 함)
ALLOW_NEXT_MONTH_RETURN = False

# --- 수집원 / 통화 ---
SOURCE = "google_flights"   # 현재 구현된 Collector 이름
CURRENCY = "KRW"            # Google Flights 에 요청할 표시 통화
LANGUAGE = "ko"             # 페이지 언어 (셀 라벨 파싱이 한국어 기준으로 작성됨)

# --- 분석 (Phase 3) ---
TOP_N = 5                 # 가장 저렴한 조합 몇 개를 보여줄지
MIN_DAYS_FOR_STATS = 7    # 최근 30/90일 통계를 "충분" 하다고 볼 최소 수집일 수 (그 미만이면 데이터 부족 표시)

# --- 저장 위치 ---
DATA_DIR = "data"
HISTORY_DIR = "data/history"            # 노선별 가격 이력: data/history/ICN-KIX.csv 처럼 노선당 파일 1개
LAST_RUN_FILE = "data/last_run.json"    # 가장 최근 실행 요약 (덮어씀)
DASHBOARD_FILE = "docs/index.html"      # HTML Dashboard (GitHub Pages 가 docs/ 를 서비스)
SEARCH_DIR = "data/searches"            # 동적 검색(search.py) 결과 저장 폴더. 트래커 CSV 와 분리

# --- 사이트 부하 최소화 ---
# 페이지를 연속으로 열 때 사이에 두는 대기 시간(초). 봇 탐지 우회 목적이 아니라
# 같은 사이트에 너무 빠르게 반복 요청하지 않기 위한 값입니다.
PAGE_LOAD_DELAY_SEC = 2.0
