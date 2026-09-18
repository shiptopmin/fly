"""
storage.py - 수집한 가격을 CSV 로 누적 저장

- data/flights_raw.csv 에 매번 "추가(append)" 합니다. 기존 이력을 덮어쓰지 않습니다.
- 같은 실행(collected_at 동일) 안에서 같은 (출발일, 귀국일)이 두 번 들어오면 한 번만 저장합니다.
- 다른 날/다른 시각에 수집한 같은 날짜 조합은 "가격 이력" 이므로 모두 남깁니다.
- 실제 수집된 PriceRecord 만 저장합니다. 누락된 조합은 last_run.json 에 목록으로만 기록합니다.
"""
import csv
import json
import os
from dataclasses import asdict

# CSV 열 순서 (열이 바뀌면 기존 파일과 어긋나므로 함부로 바꾸지 않습니다)
FIELDS = [
    "collected_at",     # 수집 시각 (KST)
    "origin",
    "destination",
    "departure_date",
    "return_date",
    "nights",           # 귀국일 - 출발일 (편의용, 두 날짜에서 계산)
    "price",
    "currency",
    "price_type",       # 가격 의미 (round_trip_total)
    "source",
    "source_tag",       # 사이트가 붙인 표시 (Google: 저가/최저가). 우리 판단 아님
]


def _existing_keys(path):
    """이미 파일에 있는 (collected_at, departure_date, return_date) 집합을 읽습니다."""
    keys = set()
    if not os.path.exists(path):
        return keys
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            keys.add((row["collected_at"], row["departure_date"], row["return_date"]))
    return keys


def append_records(path, records, origin, destination):
    """PriceRecord 목록을 CSV 에 추가합니다. (추가된 건수, 중복으로 건너뛴 건수) 를 돌려줍니다."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    existing = _existing_keys(path)
    is_new_file = not os.path.exists(path) or os.path.getsize(path) == 0

    added = 0
    skipped = 0
    with open(path, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        if is_new_file:
            writer.writeheader()
        for r in records:
            key = (r.collected_at, r.departure_date, r.return_date)
            if key in existing:
                skipped += 1
                continue
            existing.add(key)
            nights = _nights(r.departure_date, r.return_date)
            writer.writerow({
                "collected_at": r.collected_at,
                "origin": origin,
                "destination": destination,
                "departure_date": r.departure_date,
                "return_date": r.return_date,
                "nights": nights,
                "price": r.price,
                "currency": r.currency,
                "price_type": r.price_type,
                "source": r.source,
                "source_tag": r.source_tag,
            })
            added += 1
    return added, skipped


def _nights(dep, ret):
    from datetime import date
    return (date.fromisoformat(ret) - date.fromisoformat(dep)).days


def count_rows(path):
    """CSV 의 전체 데이터 행 수 (헤더 제외)."""
    if not os.path.exists(path):
        return 0
    with open(path, newline="", encoding="utf-8-sig") as f:
        return sum(1 for _ in csv.DictReader(f))


def save_last_run(path, summary: dict):
    """가장 최근 실행 요약을 JSON 으로 저장합니다 (이 파일만 덮어씁니다)."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
