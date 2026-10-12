"""
routes.py - 추적 노선(Route) 정의를 한 곳에서 읽어 오는 도우미

노선 정보는 config.ROUTES 에만 적습니다. main.py / analyze.py / build_dashboard.py 는
전부 이 파일의 load_routes() 를 통해 같은 목록을 사용합니다.

하나의 Route 는 (노선, 출발 월) 한 쌍입니다. 같은 노선이라도 월이 다르면 이력 파일/상세 페이지가 따로입니다.
(한 파일에 두 달을 같이 쌓으면 '그날 마지막 실행'만 인정되는 이력 규칙 때문에 한 달의 하루가 사라지고,
 보고서의 '최신 수집분'도 다른 달 실행으로 바뀌기 때문입니다.)
"""
import calendar
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import config

KST = timezone(timedelta(hours=9))


@dataclass(frozen=True)
class Route:
    origin: str
    destination: str
    year: int
    month: int
    min_nights: int
    max_nights: int
    slug_override: str = ""      # 이 달이 기존 식별자를 이어받아야 할 때만 지정 (config.LEGACY_SLUGS)

    @property
    def od(self) -> str:
        """노선(월 무관) 식별자. Probe/정밀검색 파일 이름이 이것을 씁니다. 예: ICN-KIX"""
        return f"{self.origin}-{self.destination}"

    @property
    def slug(self) -> str:
        """이 (노선, 월) 의 식별자. 이력 파일/상세 페이지/실행 기록 이름. 예: ICN-KIX (10월, 기존) / ICN-KIX-2026-11"""
        return self.slug_override or f"{self.od}-{self.year:04d}-{self.month:02d}"

    @property
    def label(self) -> str:
        return f"{self.origin} → {self.destination}"

    @property
    def month_label(self) -> str:
        return f"{self.year}년 {self.month}월"

    @property
    def last_day(self) -> date:
        return date(self.year, self.month, calendar.monthrange(self.year, self.month)[1])

    def ended(self, today: date) -> bool:
        """그 달이 완전히 지났는가 (더 이상 수집하지 않고, 마지막 데이터로 '종료' 표시)."""
        return self.last_day < today

    @property
    def public(self) -> bool:
        """화면(GitHub Pages)에 공개할 달인가. config.PUBLIC_THROUGH 이후의 달은 검증 전까지 숨깁니다."""
        through = getattr(config, "PUBLIC_THROUGH", None)
        return through is None or (self.year, self.month) <= tuple(through)

    @property
    def history_file(self) -> str:
        """이 (노선, 월) 의 가격 이력 CSV 경로. 예: data/history/ICN-KIX.csv, data/history/ICN-KIX-2026-11.csv"""
        return os.path.join(config.HISTORY_DIR, f"{self.slug}.csv")

    @property
    def page_file(self) -> str:
        """이 (노선, 월) 의 상세 대시보드 경로 (docs/ 기준)."""
        return os.path.join(os.path.dirname(config.DASHBOARD_FILE), "routes", f"{self.slug}.html")


def observation_sources(origin, destination, tracker_file=None):
    """이 노선에 대해 우리가 보유한 모든 관측 파일. [(출처이름, 경로), ...]

    판정은 항상 이 목록 전체를 봅니다. 한 곳만 보면 "수집 이후 최저" 같은 주장이
    우리가 이미 가진 다른 기록에 반박당할 수 있기 때문입니다. 없는 파일은 그냥 비어 있습니다.

    tracker_file: 트래커 이력 파일. 주지 않으면 기존 파일(노선 하나당 하나, 10월분)입니다.
    월별로 파일이 나뉜 뒤에도 Probe 쪽 이력(history_for)은 이 기본값을 그대로 씁니다.
    """
    slug = f"{origin}-{destination}"
    return [
        ("tracker", tracker_file or os.path.join(config.HISTORY_DIR, f"{slug}.csv")),
        ("probe", os.path.join(config.PROBE_DIR, f"{slug}.csv")),
        ("confirm", os.path.join(config.PROBE_DIR, "confirm", f"{slug}.csv")),
    ]


def _add_months(ym, n):
    y, m = ym
    k = m - 1 + n
    return (y + k // 12, k % 12 + 1)


def tracked_months(today: date):
    """오늘 기준으로 화면에 나올 (연, 월) 목록. 시작 달부터 '이번 달 + 다음 달 ...' 까지, 지난 달은 종료 상태로 남습니다."""
    first = tuple(getattr(config, "TRACK_FIRST_MONTH", (today.year, today.month)))
    offsets = tuple(getattr(config, "TRACK_MONTH_OFFSETS", (0, 1)))
    now = (today.year, today.month)
    active = {_add_months(now, o) for o in offsets}
    months, cur = [], first
    last = max(active)
    while cur <= last:
        if cur < now or cur in active:
            months.append(cur)
        cur = _add_months(cur, 1)
    return months


def load_routes(today=None, include_ended=False):
    """config.ROUTES -> [Route, ...] (월 오름차순, 같은 달 안에서는 config 순서).

    - 항목에 year/month 가 있으면 그 달 하나(고정). 없으면 '이번 달 + 다음 달'을 매달 자동으로 따라갑니다.
    - include_ended=False(기본, 수집용)이면 이미 끝난 달은 뺍니다. 화면/분석은 True 로 불러 '종료' 상태로 남깁니다.
    - 같은 (노선, 월) 이 두 번이면 오류입니다. 같은 노선의 다른 달은 정상입니다.
    """
    today = today or datetime.now(KST).date()
    legacy = dict(getattr(config, "LEGACY_SLUGS", {}))
    found = []
    for idx, r in enumerate(config.ROUTES):
        o, d = r["origin"].upper(), r["destination"].upper()
        if "year" in r and "month" in r:
            months = [(int(r["year"]), int(r["month"]))]
        else:
            months = tracked_months(today)
        for (y, m) in months:
            found.append((y, m, idx, Route(
                origin=o, destination=d, year=y, month=m,
                min_nights=int(r.get("min_nights", config.MIN_NIGHTS)),
                max_nights=int(r.get("max_nights", config.MAX_NIGHTS)),
                slug_override=r.get("slug") or legacy.get((o, d, y, m), ""))))
    found.sort(key=lambda t: t[:3])
    routes, seen = [], set()
    for _, _, _, route in found:
        if route.slug in seen:
            raise ValueError(f"config.ROUTES 에 같은 노선/월이 두 번 있습니다: {route.slug}")
        seen.add(route.slug)
        if include_ended or not route.ended(today):
            routes.append(route)
    return routes
