"""
collectors/base.py - 모든 Collector 가 공통으로 따르는 규칙(인터페이스)

Collector 를 교체(Google Flights -> 다른 사이트/공식 API)하더라도
main.py, storage.py, 이후 분석 코드는 이 파일의 자료형만 사용하므로
다시 작성할 필요가 없습니다.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class PriceRecord:
    """실제 사이트에서 확인한 가격 1건.

    사이트가 제공하지 않는 값은 만들지 않습니다.
    Google Flights 날짜 표(Date Grid)는 출발일+귀국일 조합의 왕복 총액을 제공하므로
    departure_date 와 return_date 를 모두 저장합니다.
    """
    departure_date: str   # "YYYY-MM-DD"
    return_date: str      # "YYYY-MM-DD"
    price: int            # 숫자만 (예: 356900)
    currency: str         # "KRW"
    source: str           # "google_flights"
    collected_at: str     # 수집 시각 (KST, ISO 형식)
    price_type: str       # 가격의 의미 (예: "round_trip_total")
    source_tag: str = ""  # 사이트가 붙인 부가 표시(예: Google 의 "저가"/"최저가"). 우리 판단이 아님.


@dataclass
class CollectResult:
    """한 번 수집 실행의 결과 묶음."""
    records: list = field(default_factory=list)        # PriceRecord 목록 (가격 확인된 것만)
    missing_pairs: list = field(default_factory=list)  # 가격을 확인하지 못한 (출발일, 귀국일) 목록
    price_semantics: str = ""                          # 가격이 무엇을 뜻하는지 (검증 결과 문장)
    semantics_verified: bool = False                   # 가격 의미를 실제 페이지에서 교차 확인했는지
    errors: list = field(default_factory=list)         # 발생한 오류 메시지
    blocked: bool = False                              # CAPTCHA / 차단으로 중단되었는지
    page_loads: int = 0                                # 페이지를 몇 번 열었는지 (부하 확인용)
    grid_steps: int = 0                                # 그리드 스크롤 버튼을 몇 번 눌렀는지
    # 페이지별 가격 의미 검증 기록 (진단용). 가격을 보정하지 않고 '불일치 사실'만 남깁니다.
    semantics_checks: list = field(default_factory=list)


class BaseCollector(ABC):
    """모든 Collector 의 부모 클래스."""

    name: str = "base"

    @abstractmethod
    def collect(
        self,
        origin: str,
        destination: str,
        year: int,
        month: int,
        min_nights: int,
        max_nights: int,
        allow_next_month_return: bool,
        headless: bool,
    ) -> CollectResult:
        """대상 월의 (출발일, 귀국일) 조합 가격을 수집해서 CollectResult 로 돌려줍니다. (트래커용)"""
        raise NotImplementedError

    def collect_query(self, query, headless: bool) -> CollectResult:
        """SearchQuery(search_conditions.py) 조건으로 수집합니다. (동적 검색용, Phase 6)

        구현하지 않은 Collector 는 동적 검색을 지원하지 않는 것으로 봅니다.
        """
        raise NotImplementedError(f"{self.name} collector does not support collect_query()")
