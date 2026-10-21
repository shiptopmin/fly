# ============================================================
# config.py - 항공권 가격 추적기 설정값
# ------------------------------------------------------------
# 코드 안에 값을 직접 쓰지 않고(하드코딩 금지) 여기서만 바꿉니다.
# 노선이나 대상 월을 바꾸고 싶으면 이 파일만 수정하면 됩니다.
# ============================================================

# --- 추적 노선 목록 (Tracker 가 매일 수집하는 노선) ---
# 한 줄 = 노선 하나. 추가하려면 같은 형식으로 한 줄 넣으면 됩니다.
#   origin/destination : IATA 공항 코드 (ICN 인천, GMP 김포, KIX 오사카, FUK 후쿠오카, NRT 도쿄 ...)
#   year/month         : (선택) 적으면 그 달만 추적. 적지 않으면 아래 TRACK_MONTH_OFFSETS 로 매달 자동 추적
#   min_nights/max_nights : 생략하면 아래 MIN_NIGHTS / MAX_NIGHTS 기본값 사용
ROUTES = [
    {"origin": "ICN", "destination": "KIX"},
    {"origin": "ICN", "destination": "FUK"},
]
# 자동 추적 월: 0 = 이번 달, 1 = 다음 달. (0, 1) 이면 항상 이번 달과 다음 달을 함께 수집합니다.
# 다음 달을 미리 수집하므로 그 달이 '이번 달'이 되었을 때 이미 자체 이력이 쌓여 있습니다.
TRACK_MONTH_OFFSETS = (0, 1)
TRACK_FIRST_MONTH = (2026, 10)      # 이 달부터 화면에 남깁니다 (지난 달은 수집을 멈추고 '종료'로 표시)
# 월별 파일이 생기기 전부터 쓰던 식별자(이력 CSV / 상세 페이지 주소)를 그대로 이어받는 달.
# 여기 없는 달의 식별자는 '노선-연-월' 입니다 (예: ICN-KIX-2026-11).
LEGACY_SLUGS = {
    ("ICN", "KIX", 2026, 10): "ICN-KIX",
    ("ICN", "FUK", 2026, 10): "ICN-FUK",
}
# 화면(홈/상세 페이지)에 공개할 마지막 달. 이 달 이후는 수집만 하고 판정 규칙 검증이 끝날 때까지 숨깁니다.
# 검증이 끝나면 올리거나(예: (2026, 11)) None 으로 바꿉니다 (None = 모두 공개).
PUBLIC_THROUGH = (2026, 10)

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
#   DEAL = ③ 평소 대비 저가  그리고  (① 신저가  또는  ② 동일 일정 하락)
#   ① 같은 숙박일수 모든 일정의 과거 최저보다 noise_pct 이상 낮음
#   ② 정확히 같은 일정의 과거 최저보다 noise_pct 이상 낮음 (같은 일정 이력 MIN_DAYS_PAIR 일 이상일 때만)
#   ③ 같은 숙박일수 모든 일정의 최근 30일 '일별 최저가 평균'보다 below_avg30_pct 이상 낮음
DEAL_RULES = {
    "below_avg30_pct": 15,         # ③ 기준 (%). 12 와 비교해 볼 수 있도록 설정값으로 둡니다
    "watch_below_avg30_pct": 5,    # 약한 신호(WATCH): 평균보다 이 % 이상 낮음
    "below_low_all": True,         # ① 신저가 조건 사용
    "below_pair_low": True,        # ② 동일 일정 하락 조건 사용
    "drop_1d_pct": 10,             # 약한 신호(WATCH): 같은 일정이 직전 수집일보다 이 % 이상 하락
    "noise_pct": 3,                # ①② 에서 이 % 미만의 차이는 '변화 없음(잡음)'
    "deal_requires_pair_low": False,   # B 정책: DEAL 이려면 ② 도 필요. 기본 꺼짐
}

# --- Broad Probe (Phase 7-3) ---
# 목적을 분명히 합니다. Probe 는 "최저가를 찾는 것"이 아니라
# "목적지마다 매일 같은 자리를 재서 어제와 비교할 수 있는 시계열을 만드는 것"입니다.
# 그래서 기준일(anchor)이 달마다 한 번만 바뀌도록 달력에서 결정론적으로 정합니다.
PROBE_ORIGIN = "ICN"
PROBE_DESTINATIONS = "일본"    # destinations.py 가 해석 (공항코드 / 도시 / 국가 / 쉼표목록)
PROBE_MONTHS_AHEAD = 1         # 몇 달 뒤를 볼지. 검증 단계에서는 1 (Deep Tracker 와 같은 달이라 비교 가능)
PROBE_WEEKDAY = 1              # 기준 출발 요일 (0=월, 1=화). 실측상 화/수 출발이 저렴했음
PROBE_WEEK_OF_MONTH = 2        # 그 달의 몇 번째 해당 요일
PROBE_NIGHTS = 3               # 기준 숙박일수 (그리드가 앞뒤 날짜와 1~7박을 함께 보여줌)
PROBE_MAX_CANDIDATES = 3       # 정밀 확인으로 넘길 후보 상한 (하루 검색량 상한)
PROBE_CONFIRM_SPAN_DAYS = 10   # 정밀 확인 시 후보 출발일 앞뒤로 볼 날짜 폭
PROBE_DIR = "data/probes"                  # 목적지별 Probe 시계열 (Deep Tracker 이력과 분리)
PROBE_FEED_DIR = "data/probes/feed"        # 실행 결과 요약 (후보/정밀확인/최종판정)
# 사전 측정(preroll): 다음 달 1일의 N일 전부터, '다다음 달' 창을 미리 재서 기록만 합니다 (0 이면 끔).
# 월이 바뀌는 날 새 창의 같은 출발월 이력이 비어 HOLD 가 되는 일주일을 없애기 위한 것입니다.
# 판정/후보 선정/정밀 검색에는 쓰지 않고, 창이 넘어간 뒤 그 창의 이력으로만 쓰입니다.
PROBE_PREROLL_DAYS = 10
PROBE_PREROLL_DIR = "data/probes/preroll"                   # 본 측정 파일과 섞지 않는 별도 폴더
PROBE_PREROLL_STATUS = "data/probes/preroll/status.csv"     # 사전 측정 완료 여부 기록 (append-only)

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
