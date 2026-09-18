"""
search_conditions.py - 검색 조건(SearchQuery) 정의

트래커(main.py)와 동적 검색(search.py) 모두 이 자료형으로 조건을 표현합니다.
- 트래커: "대상 월 전체 출발, 1~7박, 귀국도 그 달 안" 을 SearchQuery 로 감싸서 사용
- 검색기: 사용자가 준 기간/숙박일수/직항 조건을 그대로 SearchQuery 로 사용
"""
from dataclasses import dataclass
from datetime import date, timedelta


@dataclass
class SearchQuery:
    origin: str                      # 출발 공항 (예: "ICN")
    destination: str                 # 목적지 공항 (예: "KIX")  * MVP: 단일 목적지
    depart_from: date                # 출발 가능 기간 시작
    depart_to: date                  # 출발 가능 기간 끝 (포함)
    min_nights: int                  # 숙박일수 최소
    max_nights: int                  # 숙박일수 최대 (고정이면 min 과 같게)
    return_from: date | None = None  # 귀국 가능 기간 (None 이면 제한 없음)
    return_to: date | None = None
    nonstop: bool = False            # 직항만
    trip_type: str = "round_trip"    # MVP 는 왕복만 지원
    adults: int = 1                  # MVP 는 1명만 지원 (URL 문구 미확인)

    def validate(self):
        if self.depart_from > self.depart_to:
            raise ValueError("출발 가능 기간이 뒤집혀 있습니다 (depart_from > depart_to)")
        if self.min_nights < 1 or self.max_nights < self.min_nights:
            raise ValueError("숙박일수 범위가 잘못되었습니다 (1 <= min <= max)")
        if self.return_from and self.return_to and self.return_from > self.return_to:
            raise ValueError("귀국 가능 기간이 뒤집혀 있습니다")
        if self.trip_type != "round_trip":
            raise ValueError("MVP 는 왕복(round_trip)만 지원합니다")
        if self.adults != 1:
            raise ValueError("MVP 는 성인 1명만 지원합니다")

    def needed_pairs(self):
        """조건을 만족하는 (출발일, 귀국일) 조합 목록. 출발일 순 -> 숙박일수 순."""
        pairs = []
        d = self.depart_from
        while d <= self.depart_to:
            for n in range(self.min_nights, self.max_nights + 1):
                r = d + timedelta(days=n)
                if self.return_from and r < self.return_from:
                    continue
                if self.return_to and r > self.return_to:
                    continue
                pairs.append((d, r))
            d += timedelta(days=1)
        return pairs

    def describe(self) -> str:
        nights = f"{self.min_nights}박" if self.min_nights == self.max_nights \
            else f"{self.min_nights}~{self.max_nights}박"
        ret = ""
        if self.return_from or self.return_to:
            ret = f", 귀국 {self.return_from or '-'}..{self.return_to or '-'}"
        return (f"{self.origin} → {self.destination}, 출발 {self.depart_from}..{self.depart_to}{ret}, "
                f"{nights}, 왕복{', 직항' if self.nonstop else ''}")

    @classmethod
    def for_month(cls, origin, destination, year, month, min_nights, max_nights,
                  allow_next_month_return=False):
        """트래커용: 대상 월 전체 출발. allow_next_month_return=False 면 귀국도 그 달 안."""
        first = date(year, month, 1)
        last = (date(year + (month == 12), (month % 12) + 1, 1) - timedelta(days=1))
        return cls(origin=origin, destination=destination,
                   depart_from=first, depart_to=last,
                   min_nights=min_nights, max_nights=max_nights,
                   return_to=None if allow_next_month_return else last)
