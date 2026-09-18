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
