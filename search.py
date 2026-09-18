"""
search.py - 조건 기반 동적 항공권 검색 (Phase 6 MVP, 터미널)

예:
    python search.py --from ICN --to KIX --depart 2026-11-01..2026-11-30 --nights 3
    python search.py --from ICN --to FUK --depart 2026-12-10..2026-12-12 --return 2026-12-13..2026-12-15 --nights 1..5 --nonstop
    python search.py --from ICN --to KIX --depart 2026-11-01..2026-11-30 --nights 3 --save

동작:
    조건(SearchQuery) -> (출발일, 귀국일) 조합 생성 -> Google Flights 날짜 표 수집 (기존 수집기 재사용)
    -> 가격순 정렬 -> 최저가 / Top 5 / 숙박일수별 최저가 출력 -> --save 면 data/searches/ 에 CSV 저장

트래커(main.py, data/flights_raw.csv, docs/index.html)는 읽지도 쓰지도 않습니다.
"""
import argparse
import logging
import os
import sys
from datetime import date, datetime, timedelta, timezone

import config
import storage
from analyzer import stats
from analyzer.combinations import Trip, includes_weekend
from collectors.google_flights import GoogleFlightsCollector
from search_conditions import SearchQuery

KST = timezone(timedelta(hours=9))
MEDALS = ["🥇", "🥈", "🥉", "4위", "5위"]


def parse_range(text, what):
    """'2026-11-01..2026-11-30' 또는 '2026-11-10' -> (date, date)"""
    try:
        if ".." in text:
            a, b = text.split("..", 1)
            return date.fromisoformat(a), date.fromisoformat(b)
        d = date.fromisoformat(text)
        return d, d
    except ValueError:
        raise SystemExit(f"{what} 형식 오류: '{text}' (예: 2026-11-01..2026-11-30 또는 2026-11-10)")


def parse_nights(text):
    """'3' 또는 '2..4' -> (min, max)"""
    try:
        if ".." in text:
            a, b = text.split("..", 1)
            return int(a), int(b)
        n = int(text)
        return n, n
    except ValueError:
        raise SystemExit(f"--nights 형식 오류: '{text}' (예: 3 또는 2..4)")


def build_query(args) -> SearchQuery:
    dep_from, dep_to = parse_range(args.depart, "--depart")
    ret_from = ret_to = None
    if args.ret:
        ret_from, ret_to = parse_range(args.ret, "--return")
    min_n, max_n = parse_nights(args.nights)
    q = SearchQuery(origin=args.origin.upper(), destination=args.destination.upper(),
                    depart_from=dep_from, depart_to=dep_to,
                    min_nights=min_n, max_nights=max_n,
                    return_from=ret_from, return_to=ret_to, nonstop=args.nonstop)
    try:
        q.validate()
    except ValueError as e:
        raise SystemExit(f"조건 오류: {e}")
    return q


def records_to_trips(records):
    """수집된 PriceRecord -> Trip (기존 analyzer 자료형 재사용). 가격은 그대로, 계산 없음."""
    trips = []
    for r in records:
        dep = date.fromisoformat(r.departure_date)
        ret = date.fromisoformat(r.return_date)
        trips.append(Trip(departure_date=dep, return_date=ret, nights=(ret - dep).days,
                          price=r.price, currency=r.currency,
                          includes_weekend=includes_weekend(dep, ret), source_tag=r.source_tag))
    return trips


def save_results(query, records, started):
    """검색 결과를 data/searches/ 에 별도 CSV 로 저장 (트래커 CSV 와 분리)."""
    os.makedirs(config.SEARCH_DIR, exist_ok=True)
    nights = f"{query.min_nights}n" if query.min_nights == query.max_nights \
        else f"{query.min_nights}-{query.max_nights}n"
    name = (f"{started.strftime('%Y%m%d_%H%M%S')}_{query.origin}-{query.destination}"
            f"_{query.depart_from}_{query.depart_to}_{nights}{'_nonstop' if query.nonstop else ''}.csv")
    path = os.path.join(config.SEARCH_DIR, name)
    added, skipped = storage.append_records(path, records, query.origin, query.destination)
    return path, added


def main():
    p = argparse.ArgumentParser(description="조건 기반 항공권 최저가 검색 (Google Flights 날짜 표)")
    p.add_argument("--from", dest="origin", required=True, help="출발 공항 코드 (예: ICN)")
    p.add_argument("--to", dest="destination", required=True, help="목적지 공항 코드 (예: KIX)")
    p.add_argument("--depart", required=True, help="출발 가능 기간 (예: 2026-11-01..2026-11-30)")
    p.add_argument("--return", dest="ret", help="귀국 가능 기간 (예: 2026-12-13..2026-12-15). 생략 시 제한 없음")
    p.add_argument("--nights", required=True, help="숙박일수 (예: 3 또는 2..4)")
    p.add_argument("--nonstop", action="store_true", help="직항만")
    p.add_argument("--top", type=int, default=config.TOP_N, help="상위 몇 개 (기본 5)")
    p.add_argument("--all", action="store_true", help="모든 조합 출력")
    p.add_argument("--save", action="store_true", help=f"결과를 {config.SEARCH_DIR}/ 에 CSV 저장")
    p.add_argument("--debug", action="store_true", help="브라우저 표시 + 상세 로그")
    args = p.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.WARNING,
                        format="[%(levelname)s] %(message)s", stream=sys.stdout)

    query = build_query(args)
    needed = query.needed_pairs()
    print("=" * 60)
    print("🔎 항공권 검색")
    print(f"조건: {query.describe()}")
    print(f"확인할 날짜 조합: {len(needed)}개  (수집 시작, 조합 수에 따라 1~3분)")
    if not needed:
        print("조건을 만족하는 날짜 조합이 없습니다.")
        return 1

    started = datetime.now(KST)
    collector = GoogleFlightsCollector(currency=config.CURRENCY, language=config.LANGUAGE,
                                       page_load_delay=config.PAGE_LOAD_DELAY_SEC)
    result = collector.collect_query(query, headless=not args.debug)
    elapsed = (datetime.now(KST) - started).seconds

    print("-" * 60)
    print(f"수집 결과: {len(result.records)}건 확인 / {len(result.missing_pairs)}건 확인 불가 "
          f"(페이지 로드 {result.page_loads}회, {elapsed}초)")
    print(f"가격 의미 교차검증: {'성공' if result.semantics_verified else '실패/미확인'}  "
          f"- {result.price_semantics}")
    if result.blocked:
        print("상태: 가격 수집 실패 (차단/CAPTCHA). 우회하지 않습니다.")
    for e in result.errors:
        print(f"  ! {e}")
    if not result.records:
        print("가격을 확인한 조합이 없습니다.")
        return 1

    trips = records_to_trips(result.records)
    ranked = stats.sort_trips(trips)
    print("=" * 60)
    print(f"최저가: {ranked[0].price:,}원  {ranked[0].period_label}  {ranked[0].nights}박  {ranked[0].weekend_label}")
    print("-" * 60)
    print(f"🏆 Top {args.top}  (가격 낮은 순 → 숙박 짧은 순 → 출발일 빠른 순)")
    for i, t in enumerate(stats.top_n(trips, args.top)):
        mark = MEDALS[i] if i < len(MEDALS) else f"{i + 1}위"
        tag = f"  (Google 표시: {t.source_tag})" if t.source_tag else ""
        print(f"  {mark}  {t.period_label}  {t.nights}박  {t.price:>9,}원  {t.weekend_label}{tag}")
    if query.min_nights != query.max_nights:
        print("-" * 60)
        print("숙박일수별 최저가")
        for n, t in stats.cheapest_by_nights(trips).items():
            print(f"  {n}박 → {t.price:>9,}원   {t.period_label}  {t.weekend_label}")
    if result.missing_pairs:
        print("-" * 60)
        print(f"해당 날짜 가격 확인 불가 ({len(result.missing_pairs)}건): " + ", ".join(
            f"{d.month}/{d.day}→{r.month}/{r.day}" for d, r in result.missing_pairs[:15])
              + (" ..." if len(result.missing_pairs) > 15 else ""))
    if args.all:
        print("-" * 60)
        print("모든 조합 (가격순)")
        for t in ranked:
            print(f"  {t.period_label}  {t.nights}박  {t.price:>9,}원  {t.weekend_label}")
    if args.save:
        path, added = save_results(query, result.records, started)
        print("-" * 60)
        print(f"저장: {path} ({added}건)")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
