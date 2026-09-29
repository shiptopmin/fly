"""
analyzer/report.py - 분석 결과를 한 덩어리(Report)로 조립

analyze.py(터미널 출력)와 build_dashboard.py(HTML)가 같은 숫자를 쓰도록
계산은 여기서 한 번만 하고, 두 파일은 Report 를 "보여주기만" 합니다.

"최저가" 에 관한 값은 판정 블록(analyzer/deals.py)과 똑같은 근거를 씁니다.
- 출처: routes.observation_sources() 가 돌려주는 Tracker / Probe / 정밀검색 전체
- 방법: RouteHistory.observed_min() (원본 관측 전체에서 최저가 탐색)
그래서 상태 문구, "수집 이후 최저가" 카드, 판정 블록이 서로 다른 숫자를 말하지 않습니다.
평균은 추세용이므로 기존대로 Tracker 의 날짜별 대표값(시계열)으로 계산합니다.
"""
from dataclasses import dataclass, field, replace
from datetime import date, timedelta

from . import stats
from .combinations import load_rows, latest_run_rows, build_trips, group_by_nights
from .history import RouteHistory, thresholds_from_config


@dataclass
class Report:
    origin: str
    destination: str
    year: int
    month: int
    min_nights: int
    max_nights: int
    latest_collected_at: str          # 마지막 수집 시각 (ISO, KST)
    total_rows: int                   # CSV 누적 행 수
    latest_rows: int                  # 최신 수집분 행 수
    trips: list                       # 최신 수집분 Trip 목록
    series: list                      # 날짜별 최저가 DayPoint 목록 (Tracker)
    current_min: int
    s30: stats.WindowStats
    s90: stats.WindowStats
    s_all: stats.WindowStats
    status: str
    change: object                    # stats.PriceChange 또는 None
    top: list                         # Top N Trip
    best_by_nights: dict              # {nights: Trip}
    groups: dict = field(default_factory=dict)   # {nights: [Trip]} 전체 조합
    hist: object = None               # 판정 블록과 공유하는 RouteHistory (모든 출처)
    observed_low_info: dict = None    # 수집 이후 관측 최저가를 언제/어느 출처에서 봤는지

    @property
    def first_day(self) -> date:
        return self.series[0].day if self.series else None

    @property
    def days_collected(self) -> int:
        return len(self.series)


def scope_filter(route, cfg):
    """이 Report 가 다루는 범위(대상 월 출발, 숙박일수 범위, 귀국 규칙)의 관측만 고르는 필터."""
    def keep(r):
        dep = date.fromisoformat(r["departure_date"])
        ret = date.fromisoformat(r["return_date"])
        n = int(r["nights"])
        if (dep.year, dep.month) != (route.year, route.month):
            return False
        if n < route.min_nights or n > route.max_nights:
            return False
        if (ret.year, ret.month) != (route.year, route.month) and not cfg.ALLOW_NEXT_MONTH_RETURN:
            return False
        return True
    return keep


def _with_observed_low(w: stats.WindowStats, obs):
    """창 통계의 최저값을 '보유한 모든 관측'의 최저값으로 바꿉니다. 평균/최고/일수는 그대로."""
    if obs is None or w.days == 0:
        return w
    return replace(w, low=obs["price"])


def build_report(route, cfg, sources=None) -> Report | None:
    """노선(routes.Route) 하나로 Report 를 만듭니다. 데이터가 없으면 None.

    sources: [(출처이름, 경로), ...]. 주지 않으면 routes.observation_sources() 를 씁니다.
    노선별로 파일이 따로 있지만, 혹시 다른 노선 행이 섞여 있어도 통계에 들어가지 않도록
    출발/도착 공항으로 한 번 더 걸러냅니다. (노선 간 통계 혼합 방지)
    """
    rows = [r for r in load_rows(route.history_file)
            if r.get("origin") == route.origin and r.get("destination") == route.destination]
    if not rows:
        return None
    latest, latest_rows = latest_run_rows(rows)
    trips = build_trips(latest_rows, route.year, route.month,
                        route.min_nights, route.max_nights, cfg.ALLOW_NEXT_MONTH_RETURN)
    if not trips:
        return None

    series = stats.daily_min_series(rows, route.year, route.month,
                                    route.min_nights, route.max_nights, cfg.ALLOW_NEXT_MONTH_RETURN)
    today = series[-1].day
    s30 = stats.window_stats(series, "최근 30일", 30, cfg.MIN_DAYS_FOR_STATS, today)
    s90 = stats.window_stats(series, "최근 90일", 90, cfg.MIN_DAYS_FOR_STATS, today)
    s_all = stats.window_stats(series, "수집 이후", None, 1, today)
    current_min = min(t.price for t in trips)

    # ---- 판정 블록과 같은 근거로 '최저가' 값을 다시 구합니다 ----
    if sources is None:
        from routes import observation_sources   # 순환 import 를 피하려고 여기서 불러옵니다
        sources = observation_sources(route.origin, route.destination)
    hist = RouteHistory.from_files(sources, route.origin, route.destination,
                                   today, thresholds_from_config(cfg))
    keep = scope_filter(route, cfg)
    obs_all = hist.observed_min(keep)
    obs_30 = hist.observed_min(keep, since=today - timedelta(days=29))
    obs_90 = hist.observed_min(keep, since=today - timedelta(days=89))
    s_all = _with_observed_low(s_all, obs_all)
    s30 = _with_observed_low(s30, obs_30)
    s90 = _with_observed_low(s90, obs_90)

    return Report(
        origin=route.origin, destination=route.destination,
        year=route.year, month=route.month,
        min_nights=route.min_nights, max_nights=route.max_nights,
        latest_collected_at=latest, total_rows=len(rows), latest_rows=len(latest_rows),
        trips=trips, series=series, current_min=current_min,
        s30=s30, s90=s90, s_all=s_all,
        status=stats.price_status(current_min, s_all, s30,
                                  observed_low=obs_all["price"] if obs_all else None),
        change=stats.price_change(series),
        top=stats.top_n(trips, cfg.TOP_N),
        best_by_nights=stats.cheapest_by_nights(trips),
        groups=group_by_nights(trips),
        hist=hist,
        observed_low_info=obs_all,
    )
