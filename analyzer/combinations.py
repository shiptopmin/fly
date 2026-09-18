"""
analyzer/combinations.py - 출발일/귀국일 조합 -> 숙박일수 -> 평일/주말 판단 (Phase 2)

입력: storage 에 저장된 실제 가격 행(dict) 목록
출력: Trip 목록 (조합 하나당 하나)

원칙
- 가격은 CSV 에 있는 값을 그대로 씁니다. 없는 조합의 가격을 계산하거나 추정하지 않습니다.
- 숙박일수 = 귀국일 - 출발일 (날짜 차이)
- 주말 판단은 출발일부터 귀국일까지 "여행 기간 전체"를 보고, 금/토/일이 하루라도 끼면 [주말 포함]
"""
import csv
from dataclasses import dataclass
from datetime import date, timedelta

WEEKEND_DAYS = {4, 5, 6}          # date.weekday(): 월=0 ... 금=4, 토=5, 일=6
WEEKDAY_KO = ["월", "화", "수", "목", "금", "토", "일"]


@dataclass
class Trip:
    departure_date: date
    return_date: date
    nights: int
    price: int
    currency: str
    includes_weekend: bool
    source_tag: str = ""      # 사이트가 붙인 표시(참고용). 우리 판단 아님

    @property
    def weekend_label(self):
        return "[주말 포함]" if self.includes_weekend else "[평일]"

    @property
    def period_label(self):
        """예: 10/12(월) → 10/13(화)"""
        d, r = self.departure_date, self.return_date
        return (f"{d.month}/{d.day:02d}({WEEKDAY_KO[d.weekday()]}) → "
                f"{r.month}/{r.day:02d}({WEEKDAY_KO[r.weekday()]})")


def load_rows(csv_path):
    """CSV 전체 행을 dict 목록으로 읽습니다. 파일이 없으면 빈 목록."""
    try:
        with open(csv_path, newline="", encoding="utf-8-sig") as f:
            return list(csv.DictReader(f))
    except FileNotFoundError:
        return []


def latest_run_rows(rows):
    """가장 최근 실행(collected_at 최대값)의 행만 골라냅니다."""
    if not rows:
        return None, []
    latest = max(r["collected_at"] for r in rows)
    return latest, [r for r in rows if r["collected_at"] == latest]


def includes_weekend(dep: date, ret: date) -> bool:
    """출발일~귀국일(양 끝 포함)에 금/토/일이 하루라도 있으면 True."""
    d = dep
    while d <= ret:
        if d.weekday() in WEEKEND_DAYS:
            return True
        d += timedelta(days=1)
    return False


def build_trips(rows, year, month, min_nights, max_nights, allow_next_month_return=False):
    """실제 가격 행 -> Trip 목록.

    - 출발일이 대상 월이 아니면 제외
    - 숙박일수가 범위 밖이면 제외
    - 귀국일이 다음 달이면 allow_next_month_return 이 False 일 때 제외
    """
    trips = []
    for r in rows:
        dep = date.fromisoformat(r["departure_date"])
        ret = date.fromisoformat(r["return_date"])
        if (dep.year, dep.month) != (year, month):
            continue
        nights = (ret - dep).days
        if nights < min_nights or nights > max_nights:
            continue
        if (ret.year, ret.month) != (year, month) and not allow_next_month_return:
            continue
        trips.append(Trip(
            departure_date=dep,
            return_date=ret,
            nights=nights,
            price=int(r["price"]),
            currency=r["currency"],
            includes_weekend=includes_weekend(dep, ret),
            source_tag=r.get("source_tag", "") or "",
        ))
    return trips


def group_by_nights(trips):
    """{숙박일수: [Trip, ...]} (각 목록은 출발일 순)"""
    groups = {}
    for t in trips:
        groups.setdefault(t.nights, []).append(t)
    for n in groups:
        groups[n].sort(key=lambda t: t.departure_date)
    return dict(sorted(groups.items()))
