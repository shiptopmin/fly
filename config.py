# ============================================================
# config.py - 항공권 가격 추적기 설정값
# ------------------------------------------------------------
# 코드 안에 값을 직접 쓰지 않고(하드코딩 금지) 여기서만 바꿉니다.
# 노선이나 대상 월을 바꾸고 싶으면 이 파일만 수정하면 됩니다.
# ============================================================

# --- 노선 (IATA 공항 코드) ---
ORIGIN = "ICN"          # 출발 공항 (예: ICN 인천, GMP 김포)
DESTINATION = "KIX"     # 도착 공항 (예: KIX 오사카 간사이, NRT 도쿄 나리타)

# --- 대상 여행월 ---
TARGET_YEAR = 2026
TARGET_MONTH = 10

# --- 숙박일수 범위 (Phase 2 분석에서 사용, Phase 1에서는 수집 범위 결정에 사용) ---
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
RAW_FILE = "data/flights_raw.csv"       # 가격 이력이 계속 누적되는 파일
LAST_RUN_FILE = "data/last_run.json"    # 가장 최근 실행 요약 (덮어씀)
DASHBOARD_FILE = "docs/index.html"      # HTML Dashboard (GitHub Pages 가 docs/ 를 서비스)
SEARCH_DIR = "data/searches"            # 동적 검색(search.py) 결과 저장 폴더. 트래커 CSV 와 분리

# --- 사이트 부하 최소화 ---
# 페이지를 연속으로 열 때 사이에 두는 대기 시간(초). 봇 탐지 우회 목적이 아니라
# 같은 사이트에 너무 빠르게 반복 요청하지 않기 위한 값입니다.
PAGE_LOAD_DELAY_SEC = 2.0
