"""
destinations.py - 목적지 마스터(data/destinations.json) 읽기와 이름 해석

사용자가 --to 에 적은 값을 공항 목록으로 바꿉니다.
    "KIX"        -> [KIX]                (공항 코드)
    "도쿄"        -> [NRT, HND]           (도시: 공항이 여럿이면 모두)
    "일본" / "JP" / "japan" -> 일본의 enabled 공항 전부   (국가 또는 지역)
    "KIX,FUK,NRT" -> [KIX, FUK, NRT]     (쉼표 목록, 각각 위 규칙으로 해석)
목록에 없는 3글자 코드는 그대로 공항 코드로 취급합니다 (예: TPE). 그 외 모르는 이름은 오류.
"""
import json
from dataclasses import dataclass

DESTINATIONS_FILE = "data/destinations.json"


@dataclass(frozen=True)
class Airport:
    code: str
    city_ko: str
    city_en: str
    country: str
    country_ko: str
    region: str
    enabled: bool = True
    priority: int = 9

    @property
    def label(self) -> str:
        """예: 오사카(KIX)"""
        return f"{self.city_ko}({self.code})"


def load_airports(path=DESTINATIONS_FILE):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return [Airport(**{k: v for k, v in a.items() if k in Airport.__dataclass_fields__})
            for a in data["airports"]]


def resolve(text, airports=None):
    """--to 문자열 -> [Airport, ...]. 해석 불가 시 ValueError."""
    airports = airports if airports is not None else load_airports()
    result = []
    for token in (t.strip() for t in text.split(",")):
        if not token:
            continue
        found = _resolve_one(token, airports)
        for a in found:
            if a.code not in [r.code for r in result]:
                result.append(a)
    return result


def _resolve_one(token, airports):
    low = token.lower()
    by_code = [a for a in airports if a.code.lower() == low]
    if by_code:
        return by_code
    by_city = [a for a in airports if a.city_ko == token or a.city_en.lower() == low]
    if by_city:
        return by_city
    by_country = [a for a in airports if a.enabled and
                  (a.country.lower() == low or a.country_ko == token or a.region.lower() == low)]
    if by_country:
        return sorted(by_country, key=lambda a: (a.priority, a.code))
    if len(token) == 3 and token.isalpha():
        # 마스터에 없는 공항 코드: 그대로 사용 (도시명은 코드로 표시됨)
        return [Airport(code=token.upper(), city_ko=token.upper(), city_en=token.upper(),
                        country="", country_ko="", region="")]
    raise ValueError(f"목적지를 해석할 수 없습니다: '{token}' "
                     f"(공항 코드, 도시명, 국가명 중 하나. 예: KIX, 도쿄, 일본, KIX,FUK)")
