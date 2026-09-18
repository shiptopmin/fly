"""
analyzer/report.py - 분석 결과를 한 덩어리(Report)로 조립

analyze.py(터미널 출력)와 build_dashboard.py(HTML)가 같은 숫자를 쓰도록
계산은 여기서 한 번만 하고, 두 파일은 Report 를 "보여주기만" 합니다.
"""
from dataclasses import dataclass, field
from datetime import date

from . import stats
from .combinations import load_rows, latest_run_rows, build_trips, group_by_nights


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
    series: list                      # 날짜별 최저가 DayPoint 목록
    current_min: int
    s30: stats.WindowStats
    s90: stats.WindowStats
    s_all: stats.WindowStats
    status: str
    change: object                    # stats.PriceChange 또는 None
    top: list                         # Top N Trip
    best_by_nights: dict              # {nights: Trip}
    groups: dict = field(default_factory=dict)   # {nights: [Trip]} 전체 조합

    @property
    def first_day(self) -> date:
        return self.series[0].day if self.series else None

    @property
    def days_collected(self) -> int:
        return len(self.series)


def build_report(cfg) -> Report | None:
    """config 모듈을 받아 Report 를 만듭니다. 데이터가 없으면 None."""
    rows = load_rows(cfg.RAW_FILE)
    if not rows:
        return None
    latest, latest_rows = latest_run_rows(rows)
    trips = build_trips(latest_rows, cfg.TARGET_YEAR, cfg.TARGET_MONTH,
                        cfg.MIN_NIGHTS, cfg.MAX_NIGHTS, cfg.ALLOW_NEXT_MONTH_RETURN)
    if not trips:
        return None

    series = stats.daily_min_series(rows, cfg.TARGET_YEAR, cfg.TARGET_MONTH,
                                    cfg.MIN_NIGHTS, cfg.MAX_NIGHTS, cfg.ALLOW_NEXT_MONTH_RETURN)
    today = series[-1].day
    s30 = stats.window_stats(series, "최근 30일", 30, cfg.MIN_DAYS_FOR_STATS, today)
    s90 = stats.window_stats(series, "최근 90일", 90, cfg.MIN_DAYS_FOR_STATS, today)
    s_all = stats.window_stats(series, "수집 이후", None, 1, today)
    current_min = min(t.price for t in trips)

    return Report(
        origin=cfg.ORIGIN, destination=cfg.DESTINATION,
        year=cfg.TARGET_YEAR, month=cfg.TARGET_MONTH,
        min_nights=cfg.MIN_NIGHTS, max_nights=cfg.MAX_NIGHTS,
        latest_collected_at=latest, total_rows=len(rows), latest_rows=len(latest_rows),
        trips=trips, series=series, current_min=current_min,
        s30=s30, s90=s90, s_all=s_all,
        status=stats.price_status(current_min, s_all, s30),
        change=stats.price_change(series),
        top=stats.top_n(trips, cfg.TOP_N),
        best_by_nights=stats.cheapest_by_nights(trips),
        groups=group_by_nights(trips),
    )
