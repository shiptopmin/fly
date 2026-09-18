"""
analyzer/stats.py - 가격 정렬 / Top N / 숙박일수별 최저가 / 가격 이력 통계 (Phase 3)

원칙
- 모든 숫자는 CSV 에 실제로 저장된 가격에서만 계산합니다.
- 이력 통계는 "수집한 날" 단위로 계산합니다. 하루에 여러 번 수집했으면 그날의 마지막 실행만 사용합니다.
- 데이터가 없는 날을 추정해서 채우지 않습니다. 날 수가 부족하면 "데이터 부족" 으로 표시합니다.
- "역대 최저가" 가 아니라 "수집 이후 최저가" 입니다. 우리가 수집한 범위 안에서만 최저입니다.
"""
from dataclasses import dataclass
from datetime import date, timedelta

from .combinations import build_trips


# ----------------------------------------------------------------------
# 정렬 / Top N / 숙박일수별 최저가  (최신 수집분의 Trip 목록 사용)
# ----------------------------------------------------------------------
def sort_trips(trips):
    """1) 가격 낮은 순 2) 숙박일수 짧은 순 3) 출발일 빠른 순"""
    return sorted(trips, key=lambda t: (t.price, t.nights, t.departure_date))


def top_n(trips, n=5):
    return sort_trips(trips)[:n]


def cheapest_by_nights(trips):
    """{숙박일수: 가장 저렴한 Trip}. 같은 가격이면 출발일 빠른 순."""
    best = {}
    for t in sort_trips(trips):
        best.setdefault(t.nights, t)
    return dict(sorted(best.items()))


# ----------------------------------------------------------------------
# 가격 이력 (날짜별 "그날의 최저 조합 가격")
# ----------------------------------------------------------------------
@dataclass
class DayPoint:
    day: date          # 수집한 날 (KST)
    collected_at: str  # 그날 마지막 실행의 수집 시각
    min_price: int     # 그날 전체 조합 중 최저가
    trips_count: int   # 그날 분석에 포함된 조합 수


def daily_min_series(rows, year, month, min_nights, max_nights, allow_next_month_return=False):
    """CSV 전체 행 -> 날짜별 최저가 목록(날짜 오름차순).

    같은 날 여러 번 수집했으면 collected_at 이 가장 늦은 실행만 씁니다.
    """
    by_run = {}
    for r in rows:
        by_run.setdefault(r["collected_at"], []).append(r)

    last_run_of_day = {}
    for collected_at in by_run:
        day = date.fromisoformat(collected_at[:10])
        if day not in last_run_of_day or collected_at > last_run_of_day[day]:
            last_run_of_day[day] = collected_at

    series = []
    for day in sorted(last_run_of_day):
        run_id = last_run_of_day[day]
        trips = build_trips(by_run[run_id], year, month, min_nights, max_nights, allow_next_month_return)
        if not trips:
            continue  # 그날 실제 데이터가 없으면 점을 만들지 않음
        series.append(DayPoint(day=day, collected_at=run_id,
                               min_price=min(t.price for t in trips), trips_count=len(trips)))
    return series


@dataclass
class WindowStats:
    label: str        # "최근 30일" 등
    days: int         # 실제로 데이터가 있는 날 수
    enough: bool      # 최소 필요 일수를 채웠는지
    avg: float = 0.0
    low: int = 0
    high: int = 0


def window_stats(series, label, window_days, min_days, today=None):
    """최근 window_days 일 안의 DayPoint 로 평균/최저/최고. window_days=None 이면 전체(수집 이후)."""
    today = today or (series[-1].day if series else date.today())
    if window_days is None:
        pts = list(series)
    else:
        start = today - timedelta(days=window_days - 1)
        pts = [p for p in series if start <= p.day <= today]
    if not pts:
        return WindowStats(label=label, days=0, enough=False)
    prices = [p.min_price for p in pts]
    return WindowStats(label=label, days=len(pts), enough=len(pts) >= min_days,
                       avg=sum(prices) / len(prices), low=min(prices), high=max(prices))


@dataclass
class PriceChange:
    prev_day: date
    prev_price: int
    today_price: int

    @property
    def diff(self):
        return self.today_price - self.prev_price

    @property
    def pct(self):
        return self.diff / self.prev_price * 100 if self.prev_price else 0.0


def price_change(series):
    """직전 수집일 대비 변화. 수집일이 2일 미만이면 None (계산하지 않음)."""
    if len(series) < 2:
        return None
    prev, cur = series[-2], series[-1]
    return PriceChange(prev_day=prev.day, prev_price=prev.min_price, today_price=cur.min_price)


def price_status(current, since_start: WindowStats, recent30: WindowStats):
    """단순 비교로만 상태 문장을 만듭니다. 예측/점수 없음."""
    notes = []
    if since_start.days > 0 and current <= since_start.low:
        notes.append("🟢 수집 이후 최저가")
    elif since_start.days > 0:
        notes.append(f"수집 이후 최저가 대비 +{current - since_start.low:,}원")
    if recent30.enough:
        if current < recent30.avg:
            notes.append("최근 30일 평균보다 저렴")
        elif current > recent30.avg:
            notes.append("최근 30일 평균보다 비쌈")
        else:
            notes.append("최근 30일 평균과 동일")
    else:
        notes.append(f"최근 30일 데이터 부족 ({recent30.days}일치)")
    return " / ".join(notes)
