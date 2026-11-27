"""
analyzer/outliers.py - '이상값으로 표시한 관측' 목록 (data/outliers.csv)

- 원본 CSV 는 고치거나 지우지 않습니다. 어떤 수집 실행을 믿지 않는지만 이 목록에 따로 적습니다.
- 목록의 한 줄 = (출발지, 목적지, 수집 시각) 한 번의 실행. 그 실행에서 저장된 행 전체가 이상값 표시 대상입니다.
- 화면의 추천 / 최저가 / 가격 흐름 계산에서만 뺍니다. 기존 판정 엔진(analyzer/deals.py)의 입력은 바꾸지 않습니다.
- 표시를 취소하려면 목록에서 그 줄을 지우면 됩니다.
"""
import csv
import os

DEFAULT_FILE = os.path.join("data", "outliers.csv")


def load(cfg) -> set:
    """{(origin, destination, collected_at)}. 파일이 없으면 빈 집합."""
    path = getattr(cfg, "OUTLIERS_FILE", DEFAULT_FILE)
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            return {(r["origin"], r["destination"], r["collected_at"]) for r in csv.DictReader(f)}
    except FileNotFoundError:
        return set()


def is_flagged(row, flagged) -> bool:
    return (row.get("origin"), row.get("destination"), row.get("collected_at")) in flagged
