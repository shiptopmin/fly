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

# --- 좋은 가격 판정 (Phase 7-1) ---
# 이력이 이만큼 쌓이기 전에는 "좋은 가격" 판단을 하지 않습니다 (HOLD / 판단 보류).
MIN_DAYS_30 = 7      # 최근 30일 창 안에 수집일이 7일 이상이어야 30일 지표 사용 (등급 OK)
MIN_DAYS_90 = 14     # 최근 90일 창 안에 14일 이상이어야 90일 지표 사용
MIN_DAYS_PAIR = 5    # 동일 출발일/귀국일 조합의 이력이 5일 이상이어야 "동일 일정" 비교
MIN_DAYS_RICH = 30   # 전체 수집일 30일 이상(+90일 조건)이면 등급 RICH

# 판정 규칙. 값은 절대 기준이 아니라 운영하며 조정하는 설정값입니다.
DEAL_RULES = {
    "below_avg30_pct": 15,        # 비교 기준의 30일 평균보다 이 % 이상 낮으면 DEAL
    "watch_below_avg30_pct": 5,   # 이 % 이상 ~ 위 값 미만 낮으면 WATCH
    "below_low30": True,          # 30일 최저 이하(동률 포함)이면 DEAL
    "below_low_all": True,        # 수집 이후 최저 이하이면 DEAL (수집일 MIN_DAYS_30 이상일 때만)
    "below_pair_low": True,       # 동일 일정의 과거 최저보다 낮으면 DEAL (조합 이력 MIN_DAYS_PAIR 이상일 때만)
    "drop_1d_pct": 10,            # 직전 수집일 대비 이 % 이상 하락이면 WATCH
    "noise_pct": 3,               # 이 % 미만의 차이는 '변화 없음(잡음 범위)'으로 취급
}

# --- 저장 위치 ---
DATA_DIR = "data"
HISTORY_DIR = "data/history"            # 노선별 가격 이력: data/history/ICN-KIX.csv 처럼 노선당 파일 1개
LAST_RUN_FILE = "data/last_run.json"    # 가장 최근 실행 요약 (덮어씀)
DASHBOARD_FILE = "docs/index.html"      # HTML Dashboard (GitHub Pages 가 docs/ 를 서비스)
SEARCH_DIR = "data/searches"            # 동적 검색(search.py) 결과 저장 폴더. 트래커 CSV 와 분리
# 가격 의미 검증 기록(진단용, append-only). 가격 보정은 하지 않고 불일치 사실만 누적합니다.
SEMANTICS_LOG = "data/diagnostics/semantics_checks.csv"

# --- 사이트 부하 최소화 ---
# 페이지를 연속으로 열 때 사이에 두는 대기 시간(초). 봇 탐지 우회 목적이 아니라
# 같은 사이트에 너무 빠르게 반복 요청하지 않기 위한 값입니다.
PAGE_LOAD_DELAY_SEC = 2.0
