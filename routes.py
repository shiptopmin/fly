"""
routes.py - 추적 노선(Route) 정의를 한 곳에서 읽어 오는 도우미

노선 정보는 config.ROUTES 에만 적습니다. main.py / analyze.py / build_dashboard.py 는
전부 이 파일의 load_routes() 를 통해 같은 목록을 사용합니다.
"""
import os
from dataclasses import dataclass

import config


@dataclass(frozen=True)
class Route:
    origin: str
    destination: str
    year: int
    month: int
    min_nights: int
    max_nights: int

    @property
    def slug(self) -> str:
        """파일 이름/URL 에 쓰는 식별자. 예: ICN-KIX"""
        return f"{self.origin}-{self.destination}"

    @property
    def label(self) -> str:
        return f"{self.origin} → {self.destination}"

    @property
    def month_label(self) -> str:
        return f"{self.year}년 {self.month}월"

    @property
    def history_file(self) -> str:
        """이 노선의 가격 이력 CSV 경로. 예: data/history/ICN-KIX.csv"""
        return os.path.join(config.HISTORY_DIR, f"{self.slug}.csv")

    @property
    def page_file(self) -> str:
        """이 노선의 상세 대시보드 경로 (docs/ 기준). 예: docs/routes/ICN-KIX.html"""
        return os.path.join(os.path.dirname(config.DASHBOARD_FILE), "routes", f"{self.slug}.html")


def observation_sources(origin, destination):
    """이 노선에 대해 우리가 보유한 모든 관측 파일. [(출처이름, 경로), ...]

    판정은 항상 이 목록 전체를 봅니다. 한 곳만 보면 "수집 이후 최저" 같은 주장이
    우리가 이미 가진 다른 기록에 반박당할 수 있기 때문입니다. 없는 파일은 그냥 비어 있습니다.
    """
    slug = f"{origin}-{destination}"
    return [
        ("tracker", os.path.join(config.HISTORY_DIR, f"{slug}.csv")),
        ("probe", os.path.join(config.PROBE_DIR, f"{slug}.csv")),
        ("confirm", os.path.join(config.PROBE_DIR, "confirm", f"{slug}.csv")),
    ]


def load_routes():
    """config.ROUTES -> [Route, ...]. 같은 노선(출발-도착)이 두 번 적혀 있으면 오류."""
    routes = []
    seen = set()
    for r in config.ROUTES:
        route = Route(
            origin=r["origin"].upper(),
            destination=r["destination"].upper(),
            year=int(r["year"]),
            month=int(r["month"]),
            min_nights=int(r.get("min_nights", config.MIN_NIGHTS)),
            max_nights=int(r.get("max_nights", config.MAX_NIGHTS)),
        )
        if route.slug in seen:
            raise ValueError(f"config.ROUTES 에 같은 노선이 두 번 있습니다: {route.slug}")
        seen.add(route.slug)
        routes.append(route)
    return routes
