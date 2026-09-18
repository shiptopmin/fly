"""
analyzer/history.py - 노선 이력(data/history/<노선>.csv)에서 비교용 시계열을 꺼내는 층 (Phase 7-1)

세 가지 층의 시계열을 제공합니다. (비교 우선순위 순서)
    1. pair   : 동일 노선 + 동일 출발일 + 동일 귀국일 조합의 일별 가격
    2. nights : 동일 노선 + 동일 숙박일수 조합들 중 그날 최저가
    3. route  : 동일 노선 전체 조합 중 그날 최저가 (전체적인 가격 흐름)

공통 규칙
- 하루에 여러 번 수집했으면 그날 "마지막 실행"만 씁니다 (stats.daily_min_series 와 동일).
- 서로 다른 여행 날짜를 같은 일정으로 취급하지 않습니다. pair 층은 정확히 같은 (출발일, 귀국일)만 모읍니다.
- 이 파일은 계산만 합니다. 사이트 접근이나 파일 쓰기는 하지 않습니다.
- 창 통계(30/90일, 수집 이후)와 전일 대비 변화는 기존 stats.window_stats / stats.price_change 를 그대로 씁니다.
"""
from dataclasses import dataclass, field
from datetime import date

from . import stats
from .combinations import load_rows

# 이력 등급
GRADE_NONE = "NONE"   # 이력 없음
GRADE_THIN = "THIN"   # 데이터 부족 (판단 보류)
GRADE_OK = "OK"       # 분석 가능 (30일 지표 사용 가능)
GRADE_RICH = "RICH"   # 충분한 이력 (30일 + 90일 지표 사용 가능)

LEVEL_PAIR, LEVEL_NIGHTS, LEVEL_ROUTE = "pair", "nights", "route"


@dataclass(frozen=True)
class Thresholds:
    """등급 판정에 쓰는 최소 수집일 수. 기본값은 config 에서 가져와 넘깁니다."""
    min_days_30: int = 7
    min_days_90: int = 14
    min_days_pair: int = 5
    min_days_rich: int = 30


@dataclass
class SeriesStats:
    """한 층(pair/nights/route)의 시계열과 그 요약."""
    level: str
    key: str                      # 예: "10/20->10/21", "3박", "전체"
    points: list = field(default_factory=list)   # stats.DayPoint 목록 (날짜 오름차순)
    grade: str = GRADE_NONE
    w30: stats.WindowStats = None
    w90: stats.WindowStats = None
    w_all: stats.WindowStats = None
    change: stats.PriceChange = None

    @property
    def days(self) -> int:
        return len(self.points)

    @property
    def first_day(self):
        return self.points[0].day if self.points else None

    @property
    def last_day(self):
        return self.points[-1].day if self.points else None


# ----------------------------------------------------------------------
# 행 -> 일별 시계열
# ----------------------------------------------------------------------
def last_run_per_day(rows):
    """{수집일(date): 그날 마지막 실행의 collected_at}"""
    last = {}
    for r in rows:
        day = date.fromisoformat(r["collected_at"][:10])
        if day not in last or r["collected_at"] > last[day]:
            last[day] = r["collected_at"]
    return last


def _daily_points(rows, keep):
    """rows 중 keep(row) 가 True 인 행으로, 날짜별 '마지막 실행'의 최저가 DayPoint 목록을 만듭니다."""
    last = last_run_per_day(rows)
    per_day = {}
    for r in rows:
        if not keep(r):
            continue
        day = date.fromisoformat(r["collected_at"][:10])
        if r["collected_at"] != last[day]:
            continue
        price = int(r["price"])
        cur = per_day.get(day)
        if cur is None or price < cur["price"]:
            per_day[day] = {"price": price, "count": (cur["count"] + 1 if cur else 1)}
        else:
            cur["count"] += 1
    return [stats.DayPoint(day=d, collected_at=last[d], min_price=v["price"], trips_count=v["count"])
            for d, v in sorted(per_day.items())]


def route_points(rows):
    """route 층: 그날 전체 조합 중 최저가."""
    return _daily_points(rows, lambda r: True)


def nights_points(rows, nights: int):
    """nights 층: 그날 같은 숙박일수 조합 중 최저가."""
    return _daily_points(rows, lambda r: int(r["nights"]) == nights)


def pair_points(rows, dep: date, ret: date):
    """pair 층: 정확히 같은 (출발일, 귀국일) 조합의 그날 가격."""
    d, t = dep.isoformat(), ret.isoformat()
    return _daily_points(rows, lambda r: r["departure_date"] == d and r["return_date"] == t)


# ----------------------------------------------------------------------
# 등급과 요약
# ----------------------------------------------------------------------
def grade_points(points, today: date, th: Thresholds, min_days_override=None) -> str:
    """수집일 수로 등급을 정합니다.

    - NONE : 점이 없음
    - RICH : 전체 수집일 >= min_days_rich, 90일 창 >= min_days_90, 30일 창 >= min_days_30
    - OK   : 30일 창 안 수집일 >= min_days_30 (pair 층은 min_days_pair 로 대체)
    - THIN : 그 외
    """
    if not points:
        return GRADE_NONE
    need30 = th.min_days_30 if min_days_override is None else min_days_override
    d30 = stats.window_stats(points, "30", 30, 1, today).days
    d90 = stats.window_stats(points, "90", 90, 1, today).days
    if len(points) >= th.min_days_rich and d90 >= th.min_days_90 and d30 >= need30:
        return GRADE_RICH
    if d30 >= need30:
        return GRADE_OK
    return GRADE_THIN


def summarize(level, key, points, today: date, th: Thresholds) -> SeriesStats:
    """시계열 하나를 등급 + 30/90일/수집 이후 창 통계 + 전일 대비 변화로 요약합니다."""
    need = th.min_days_pair if level == LEVEL_PAIR else th.min_days_30
    s = SeriesStats(level=level, key=key, points=list(points))
    s.grade = grade_points(points, today, th, min_days_override=need)
    s.w30 = stats.window_stats(points, "최근 30일", 30, need, today)
    s.w90 = stats.window_stats(points, "최근 90일", 90, th.min_days_90, today)
    s.w_all = stats.window_stats(points, "수집 이후", None, 1, today)
    s.change = stats.price_change(points)
    return s


class RouteHistory:
    """노선 하나의 이력 행을 들고 세 층의 요약을 만들어 줍니다.

    rows: data/history/<노선>.csv 의 행(dict) 목록. origin/destination 으로 한 번 더 걸러 혼합을 막습니다.
    today: 기준일 (보통 오늘). 창 통계의 끝점.
    """

    def __init__(self, rows, origin, destination, today: date, th: Thresholds = Thresholds()):
        self.origin, self.destination = origin, destination
        self.rows = [r for r in rows if r.get("origin") == origin and r.get("destination") == destination]
        self.today = today
        self.th = th

    @classmethod
    def from_file(cls, path, origin, destination, today: date, th: Thresholds = Thresholds()):
        return cls(load_rows(path), origin, destination, today, th)

    @property
    def days_collected(self) -> int:
        return len(last_run_per_day(self.rows))

    def route(self) -> SeriesStats:
        return summarize(LEVEL_ROUTE, "전체", route_points(self.rows), self.today, self.th)

    def nights(self, n: int) -> SeriesStats:
        return summarize(LEVEL_NIGHTS, f"{n}박", nights_points(self.rows, n), self.today, self.th)

    def pair(self, dep: date, ret: date) -> SeriesStats:
        key = f"{dep.month}/{dep.day:02d}→{ret.month}/{ret.day:02d}"
        return summarize(LEVEL_PAIR, key, pair_points(self.rows, dep, ret), self.today, self.th)


def thresholds_from_config(cfg) -> Thresholds:
    return Thresholds(min_days_30=cfg.MIN_DAYS_30, min_days_90=cfg.MIN_DAYS_90,
                      min_days_pair=cfg.MIN_DAYS_PAIR, min_days_rich=cfg.MIN_DAYS_RICH)
