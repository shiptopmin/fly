"""
search.py - 조건 기반 동적 항공권 검색 (터미널)

예:
    python search.py --from ICN --to KIX --depart 2026-11-01..2026-11-30 --nights 3
    python search.py --from ICN --to 일본 --depart 2026-12-10 --return 2026-12-15 --nights 5
    python search.py --from ICN --to 도쿄,FUK --depart 2026-12-10..2026-12-12 --nights 2..4 --nonstop --save

--to 에는 공항 코드(KIX), 도시명(도쿄 -> NRT+HND), 국가명(일본 -> data/destinations.json 의 일본 공항 전부),
쉼표 목록(KIX,FUK) 을 쓸 수 있습니다.

동작:
    조건(SearchQuery) -> 목적지마다: (출발일, 귀국일) 조합 생성 -> Google Flights 날짜 표 수집 (기존 수집기 재사용)
    -> 결과 합치기 -> 가격순 정렬 -> 최저가 / Top 5 / 목적지별 최저가 / 숙박일수별 최저가
    -> --save 면 data/searches/ 에 CSV 저장

트래커(main.py, data/history/, docs/)는 읽지도 쓰지도 않습니다.
"""
import argparse
import dataclasses
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone

import config
import storage
from analyzer import stats
from analyzer.combinations import Trip, includes_weekend
from collectors.google_flights import GoogleFlightsCollector
from destinations import resolve
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


def build_query(args, destination_code) -> SearchQuery:
    dep_from, dep_to = parse_range(args.depart, "--depart")
    ret_from = ret_to = None
    if args.ret:
        ret_from, ret_to = parse_range(args.ret, "--return")
    min_n, max_n = parse_nights(args.nights)
    q = SearchQuery(origin=args.origin.upper(), destination=destination_code,
                    depart_from=dep_from, depart_to=dep_to,
                    min_nights=min_n, max_nights=max_n,
                    return_from=ret_from, return_to=ret_to, nonstop=args.nonstop)
    try:
        q.validate()
    except ValueError as e:
        raise SystemExit(f"조건 오류: {e}")
    return q


def records_to_trips(records, origin, airport):
    """수집된 PriceRecord -> Trip (기존 analyzer 자료형 재사용). 가격은 그대로, 계산 없음."""
    trips = []
    for r in records:
        dep = date.fromisoformat(r.departure_date)
        ret = date.fromisoformat(r.return_date)
        trips.append(Trip(departure_date=dep, return_date=ret, nights=(ret - dep).days,
                          price=r.price, currency=r.currency,
                          includes_weekend=includes_weekend(dep, ret), source_tag=r.source_tag,
                          origin=origin, destination=airport.code, destination_label=airport.label))
    return trips


def save_results(args, airports, collected, started):
    """검색 결과를 data/searches/ 에 CSV 하나로 저장 (트래커 CSV 와 분리). 목적지별로 이어 붙임."""
    os.makedirs(config.SEARCH_DIR, exist_ok=True)
    codes = "+".join(a.code for a in airports) if len(airports) <= 3 else \
        "".join(c for c in args.destination if c.isalnum()) or f"{len(airports)}dest"
    dep_from, dep_to = parse_range(args.depart, "--depart")
    name = (f"{started.strftime('%Y%m%d_%H%M%S')}_{args.origin.upper()}-{codes}"
            f"_{dep_from}_{dep_to}_{args.nights.replace('..', '-')}n{'_nonstop' if args.nonstop else ''}.csv")
    path = os.path.join(config.SEARCH_DIR, name)
    total = 0
    for airport, result in collected:
        if result.records:
            added, _ = storage.append_records(path, result.records, args.origin.upper(), airport.code)
            total += added
    return path, total


def main():
    p = argparse.ArgumentParser(description="조건 기반 항공권 최저가 검색 (Google Flights 날짜 표)")
    p.add_argument("--from", dest="origin", required=True, help="출발 공항 코드 (예: ICN)")
    p.add_argument("--to", dest="destination", required=True,
                   help="목적지: 공항 코드 / 도시명 / 국가명 / 쉼표 목록 (예: KIX, 도쿄, 일본, KIX,FUK)")
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

    try:
        airports = resolve(args.destination)
    except (ValueError, FileNotFoundError) as e:
        raise SystemExit(f"목적지 오류: {e}")
    if not airports:
        raise SystemExit("목적지가 비어 있습니다.")

    base = build_query(args, airports[0].code)
    pairs_per_dest = len(base.needed_pairs())
    loads_per_dest = len(GoogleFlightsCollector._plan_anchors(base.needed_pairs()))
    print("=" * 60)
    print("🔎 항공권 검색")
    print(f"조건: {base.describe().replace(base.destination, '[목적지]', 1)}")
    print(f"목적지 {len(airports)}곳: " + ", ".join(a.label for a in airports))
    print(f"목적지당 날짜 조합 {pairs_per_dest}개, 페이지 로드 {loads_per_dest}회 "
          f"→ 총 약 {loads_per_dest * len(airports)}회 (1회 ≈ 10초)")
    if not pairs_per_dest:
        print("조건을 만족하는 날짜 조합이 없습니다.")
        return 1

    started = datetime.now(KST)
    collector = GoogleFlightsCollector(currency=config.CURRENCY, language=config.LANGUAGE,
                                       page_load_delay=config.PAGE_LOAD_DELAY_SEC)
    collected = []   # [(airport, CollectResult)]
    trips = []
    blocked = False
    for i, airport in enumerate(airports):
        if i > 0:
            time.sleep(config.PAGE_LOAD_DELAY_SEC)
        query = dataclasses.replace(base, destination=airport.code)
        t0 = time.time()
        result = collector.collect_query(query, headless=not args.debug)
        collected.append((airport, result))
        status = "차단" if result.blocked else f"{len(result.records)}건 확인"
        if result.missing_pairs:
            status += f", {len(result.missing_pairs)}건 확인 불가"
        verified = "검증 OK" if result.semantics_verified else "검증 실패"
        print(f"  [{i + 1}/{len(airports)}] {airport.label:<12} {status:<22} {verified}  ({time.time() - t0:.0f}초)")
        for e in result.errors:
            print(f"        ! {e}")
        trips.extend(records_to_trips(result.records, base.origin, airport))
        if result.blocked:
            blocked = True
            print("  차단/CAPTCHA 감지 - 남은 목적지 검색을 중단합니다 (우회하지 않음).")
            break

    elapsed = (datetime.now(KST) - started).seconds
    print("-" * 60)
    ok_dests = [a for a, r in collected if r.records]
    fail_dests = [a for a, r in collected if not r.records] + airports[len(collected):]
    print(f"수집 결과: {len(trips)}건 / 목적지 {len(ok_dests)}곳 성공"
          + (f", {len(fail_dests)}곳 확인 불가 ({', '.join(a.label for a in fail_dests)})" if fail_dests else "")
          + f" / 총 {sum(r.page_loads for _, r in collected)}회 로드, {elapsed}초")
    print("가격 의미: Google Flights 날짜 표 셀 = 해당 출발/귀국 조합의 왕복 총액 (성인 1명, 세금 포함)")
    if not trips:
        print("가격을 확인한 조합이 없습니다.")
        return 1

    multi = len(airports) > 1
    ranked = stats.sort_trips(trips)
    best = ranked[0]

    def dest(t):
        return f"{t.destination_label:<12} " if multi else ""

    print("=" * 60)
    print(f"최저가: {best.price:,}원  {dest(best)}{best.period_label}  {best.nights}박  {best.weekend_label}")
    print("-" * 60)
    print(f"🏆 Top {args.top}  (가격 낮은 순 → 숙박 짧은 순 → 출발일 빠른 순)")
    for i, t in enumerate(stats.top_n(trips, args.top)):
        mark = MEDALS[i] if i < len(MEDALS) else f"{i + 1}위"
        tag = f"  (Google 표시: {t.source_tag})" if t.source_tag else ""
        print(f"  {mark}  {dest(t)}{t.period_label}  {t.nights}박  {t.price:>9,}원  {t.weekend_label}{tag}")
    if multi:
        print("-" * 60)
        print("목적지별 최저가")
        by_dest = {}
        for t in ranked:
            by_dest.setdefault(t.destination, t)
        for t in sorted(by_dest.values(), key=lambda t: t.price):
            print(f"  {t.destination_label:<12} {t.price:>9,}원   {t.period_label}  {t.nights}박  {t.weekend_label}")
        for a in fail_dests:
            print(f"  {a.label:<12} 해당 날짜 가격 확인 불가")
    if base.min_nights != base.max_nights:
        print("-" * 60)
        print("숙박일수별 최저가")
        for n, t in stats.cheapest_by_nights(trips).items():
            print(f"  {n}박 → {t.price:>9,}원   {dest(t)}{t.period_label}  {t.weekend_label}")
    missing_total = sum(len(r.missing_pairs) for _, r in collected)
    if missing_total:
        print("-" * 60)
        print(f"해당 날짜 가격 확인 불가: {missing_total}건")
        for a, r in collected:
            if r.missing_pairs:
                print(f"  {a.label}: " + ", ".join(f"{d.month}/{d.day}→{x.month}/{x.day}" for d, x in r.missing_pairs[:10])
                      + (" ..." if len(r.missing_pairs) > 10 else ""))
    if args.all:
        print("-" * 60)
        print("모든 조합 (가격순)")
        for t in ranked:
            print(f"  {dest(t)}{t.period_label}  {t.nights}박  {t.price:>9,}원  {t.weekend_label}")
    if args.save:
        path, added = save_results(args, airports, collected, started)
        print("-" * 60)
        print(f"저장: {path} ({added}건)")
    print("=" * 60)
    return 1 if blocked else 0


if __name__ == "__main__":
    sys.exit(main())
