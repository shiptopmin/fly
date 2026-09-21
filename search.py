"""
search.py - 조건 기반 동적 항공권 검색 (터미널 + web.py 가 공유하는 검색 로직)

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

핵심 로직은 run_search() 에 있고, 터미널(main)과 웹(web.py)은 이 함수를 호출해 결과를 보여주기만 합니다.
트래커(main.py, data/history/, docs/)는 읽지도 쓰지도 않습니다.
"""
import argparse
import dataclasses
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import config
import storage
from analyzer import deals, stats
from analyzer.combinations import Trip, includes_weekend
from analyzer.history import RouteHistory, thresholds_from_config
from collectors.google_flights import GoogleFlightsCollector
from destinations import resolve
from search_conditions import SearchQuery

KST = timezone(timedelta(hours=9))
MEDALS = ["🥇", "🥈", "🥉", "4위", "5위"]


# ----------------------------------------------------------------------
# 입력 해석
# ----------------------------------------------------------------------
def parse_range(text, what):
    """'2026-11-01..2026-11-30' 또는 '2026-11-10' -> (date, date). 잘못되면 ValueError."""
    try:
        if ".." in text:
            a, b = text.split("..", 1)
            return date.fromisoformat(a.strip()), date.fromisoformat(b.strip())
        d = date.fromisoformat(text.strip())
        return d, d
    except ValueError:
        raise ValueError(f"{what} 형식 오류: '{text}' (예: 2026-11-01..2026-11-30 또는 2026-11-10)")


def parse_nights(text):
    """'3' 또는 '2..4' -> (min, max). 잘못되면 ValueError."""
    try:
        if ".." in text:
            a, b = text.split("..", 1)
            return int(a), int(b)
        n = int(text)
        return n, n
    except ValueError:
        raise ValueError(f"숙박일수 형식 오류: '{text}' (예: 3 또는 2..4)")


def make_query(origin, depart, nights, ret=None, nonstop=False, destination="XXX") -> SearchQuery:
    """문자열 입력 -> SearchQuery (검증 포함). destination 은 목적지마다 바꿔 씁니다."""
    dep_from, dep_to = parse_range(depart, "출발 가능 기간")
    ret_from = ret_to = None
    if ret:
        ret_from, ret_to = parse_range(ret, "귀국 가능 기간")
    min_n, max_n = parse_nights(nights)
    q = SearchQuery(origin=origin.strip().upper(), destination=destination,
                    depart_from=dep_from, depart_to=dep_to,
                    min_nights=min_n, max_nights=max_n,
                    return_from=ret_from, return_to=ret_to, nonstop=nonstop)
    q.validate()
    return q


# ----------------------------------------------------------------------
# 검색 실행 (터미널/웹 공용)
# ----------------------------------------------------------------------
@dataclass
class SearchOutcome:
    base: SearchQuery                 # 목적지만 비운 기준 조건
    airports: list                    # 검색 대상 Airport 목록 (순서대로)
    collected: list = field(default_factory=list)   # [(Airport, CollectResult)] 실제 시도한 것
    trips: list = field(default_factory=list)       # 가격 확인된 Trip 전부
    blocked: bool = False
    elapsed_sec: int = 0
    started_at: datetime | None = None

    @property
    def ranked(self):
        return stats.sort_trips(self.trips)

    def top(self, n):
        return stats.top_n(self.trips, n)

    @property
    def best_by_destination(self):
        """목적지별 최저 Trip, 가격 순."""
        by = {}
        for t in self.ranked:
            by.setdefault(t.destination, t)
        return sorted(by.values(), key=lambda t: t.price)

    @property
    def ok_airports(self):
        return [a for a, r in self.collected if r.records]

    @property
    def failed_airports(self):
        tried_fail = [a for a, r in self.collected if not r.records]
        not_tried = self.airports[len(self.collected):]
        return tried_fail + not_tried

    @property
    def page_loads(self):
        return sum(r.page_loads for _, r in self.collected)

    @property
    def missing_total(self):
        return sum(len(r.missing_pairs) for _, r in self.collected)

    @property
    def multi(self):
        return len(self.airports) > 1


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


def run_search(base: SearchQuery, airports, headless=True, on_progress=None) -> SearchOutcome:
    """목적지 목록을 차례로 검색해 SearchOutcome 으로 돌려줍니다.

    on_progress(i, n, airport, result, seconds) 를 주면 목적지 하나 끝날 때마다 호출합니다.
    차단/CAPTCHA 가 감지되면 남은 목적지는 검색하지 않습니다 (우회하지 않음).
    """
    outcome = SearchOutcome(base=base, airports=list(airports), started_at=datetime.now(KST))
    collector = GoogleFlightsCollector(currency=config.CURRENCY, language=config.LANGUAGE,
                                       page_load_delay=config.PAGE_LOAD_DELAY_SEC)
    t_start = time.time()
    for i, airport in enumerate(outcome.airports):
        if i > 0:
            time.sleep(config.PAGE_LOAD_DELAY_SEC)
        query = dataclasses.replace(base, destination=airport.code)
        t0 = time.time()
        result = collector.collect_query(query, headless=headless)
        outcome.collected.append((airport, result))
        outcome.trips.extend(records_to_trips(result.records, base.origin, airport))
        if on_progress:
            on_progress(i, len(outcome.airports), airport, result, time.time() - t0)
        if result.blocked:
            outcome.blocked = True
            break
    outcome.elapsed_sec = int(time.time() - t_start)
    return outcome


def save_outcome(outcome: SearchOutcome, to_text, nights_text):
    """검색 결과를 data/searches/ 에 CSV 하나로 저장 (트래커 CSV 와 분리). (경로, 저장 건수)"""
    os.makedirs(config.SEARCH_DIR, exist_ok=True)
    airports, base = outcome.airports, outcome.base
    codes = "+".join(a.code for a in airports) if len(airports) <= 3 else \
        "".join(c for c in to_text if c.isalnum()) or f"{len(airports)}dest"
    name = (f"{outcome.started_at.strftime('%Y%m%d_%H%M%S')}_{base.origin}-{codes}"
            f"_{base.depart_from}_{base.depart_to}_{nights_text.replace('..', '-')}n"
            f"{'_nonstop' if base.nonstop else ''}.csv")
    path = os.path.join(config.SEARCH_DIR, name)
    total = 0
    for airport, result in outcome.collected:
        if result.records:
            added, _ = storage.append_records(path, result.records, base.origin, airport.code)
            total += added
    return path, total


def build_histories(origin, airports, today=None):
    """목적지별 트래커 이력(RouteHistory)을 모읍니다.

    Tracker 가 쌓은 data/history/<노선>.csv 만 읽습니다. 검색 결과를 이력에 섞지 않습니다.
    이력 파일이 없는 목적지는 비어 있는 이력이 되어 판정이 HOLD 로 나옵니다.
    """
    today = today or datetime.now(KST).date()
    th = thresholds_from_config(config)
    return {a.code: RouteHistory.from_file(
        os.path.join(config.HISTORY_DIR, f"{origin}-{a.code}.csv"), origin, a.code, today, th)
        for a in airports}


def judge_trip(trip, histories):
    """조합 하나를 과거 가격과 비교해 판정합니다. 이력이 없으면 HOLD 입니다."""
    h = histories.get(trip.destination)
    if h is None:
        return None
    return deals.judge(trip.price, h, nights=trip.nights, dep=trip.departure_date,
                       ret=trip.return_date, rules=config.DEAL_RULES)


def plan_summary(base: SearchQuery, airports):
    """(목적지당 조합 수, 목적지당 페이지 로드 수)"""
    pairs = base.needed_pairs()
    loads = len(GoogleFlightsCollector._plan_anchors(pairs))
    return len(pairs), loads


# ----------------------------------------------------------------------
# 터미널 출력
# ----------------------------------------------------------------------
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
    p.add_argument("--compare", action="store_true",
                   help="트래커 이력(data/history/)과 비교해 좋은 가격인지 함께 표시")
    p.add_argument("--save", action="store_true", help=f"결과를 {config.SEARCH_DIR}/ 에 CSV 저장")
    p.add_argument("--debug", action="store_true", help="브라우저 표시 + 상세 로그")
    args = p.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.WARNING,
                        format="[%(levelname)s] %(message)s", stream=sys.stdout)

    try:
        airports = resolve(args.destination)
        base = make_query(args.origin, args.depart, args.nights, args.ret, args.nonstop)
    except (ValueError, FileNotFoundError) as e:
        raise SystemExit(f"입력 오류: {e}")
    if not airports:
        raise SystemExit("목적지가 비어 있습니다.")

    pairs_per_dest, loads_per_dest = plan_summary(base, airports)
    print("=" * 60)
    print("🔎 항공권 검색")
    print(f"조건: {base.describe().replace(base.destination, '[목적지]', 1)}")
    print(f"목적지 {len(airports)}곳: " + ", ".join(a.label for a in airports))
    print(f"목적지당 날짜 조합 {pairs_per_dest}개, 페이지 로드 {loads_per_dest}회 "
          f"→ 총 약 {loads_per_dest * len(airports)}회 (1회 ≈ 10초)")
    if not pairs_per_dest:
        print("조건을 만족하는 날짜 조합이 없습니다.")
        return 1

    def progress(i, n, airport, result, seconds):
        status = "차단" if result.blocked else f"{len(result.records)}건 확인"
        if result.missing_pairs:
            status += f", {len(result.missing_pairs)}건 확인 불가"
        verified = "검증 OK" if result.semantics_verified else "검증 실패"
        print(f"  [{i + 1}/{n}] {airport.label:<12} {status:<22} {verified}  ({seconds:.0f}초)")
        for e in result.errors:
            print(f"        ! {e}")
        if result.blocked:
            print("  차단/CAPTCHA 감지 - 남은 목적지 검색을 중단합니다 (우회하지 않음).")

    outcome = run_search(base, airports, headless=not args.debug, on_progress=progress)

    print("-" * 60)
    fails = outcome.failed_airports
    print(f"수집 결과: {len(outcome.trips)}건 / 목적지 {len(outcome.ok_airports)}곳 성공"
          + (f", {len(fails)}곳 확인 불가 ({', '.join(a.label for a in fails)})" if fails else "")
          + f" / 총 {outcome.page_loads}회 로드, {outcome.elapsed_sec}초")
    print("가격 의미: Google Flights 날짜 표 셀 = 해당 출발/귀국 조합의 왕복 총액 (성인 1명, 세금 포함)")
    if not outcome.trips:
        print("가격을 확인한 조합이 없습니다.")
        return 1

    multi = outcome.multi
    ranked = outcome.ranked
    best = ranked[0]
    histories = build_histories(base.origin, airports) if args.compare else {}

    def dest(t):
        return f"{t.destination_label:<12} " if multi else ""

    def verdict_line(t, indent=" " * 6):
        if not args.compare:
            return None
        v = judge_trip(t, histories)
        return None if v is None else f"{indent}{v.label}: {v.headline}"

    print("=" * 60)
    print(f"최저가: {best.price:,}원  {dest(best)}{best.period_label}  {best.nights}박  {best.weekend_label}")
    print("-" * 60)
    print(f"🏆 Top {args.top}  (가격 낮은 순 → 숙박 짧은 순 → 출발일 빠른 순)")
    for i, t in enumerate(outcome.top(args.top)):
        mark = MEDALS[i] if i < len(MEDALS) else f"{i + 1}위"
        tag = f"  (Google 표시: {t.source_tag})" if t.source_tag else ""
        print(f"  {mark}  {dest(t)}{t.period_label}  {t.nights}박  {t.price:>9,}원  {t.weekend_label}{tag}")
        line = verdict_line(t)
        if line:
            print(line)
    if multi:
        print("-" * 60)
        print("목적지별 최저가")
        for t in outcome.best_by_destination:
            print(f"  {t.destination_label:<12} {t.price:>9,}원   {t.period_label}  {t.nights}박  {t.weekend_label}")
            line = verdict_line(t)
            if line:
                print(line)
        for a in fails:
            print(f"  {a.label:<12} 해당 날짜 가격 확인 불가")
    if base.min_nights != base.max_nights:
        print("-" * 60)
        print("숙박일수별 최저가")
        for n, t in stats.cheapest_by_nights(outcome.trips).items():
            print(f"  {n}박 → {t.price:>9,}원   {dest(t)}{t.period_label}  {t.weekend_label}")
    if outcome.missing_total:
        print("-" * 60)
        print(f"해당 날짜 가격 확인 불가: {outcome.missing_total}건")
        for a, r in outcome.collected:
            if r.missing_pairs:
                print(f"  {a.label}: " + ", ".join(f"{d.month}/{d.day}→{x.month}/{x.day}" for d, x in r.missing_pairs[:10])
                      + (" ..." if len(r.missing_pairs) > 10 else ""))
    if args.all:
        print("-" * 60)
        print("모든 조합 (가격순)")
        for t in ranked:
            print(f"  {dest(t)}{t.period_label}  {t.nights}박  {t.price:>9,}원  {t.weekend_label}")
    if args.save:
        path, added = save_outcome(outcome, args.destination, args.nights)
        print("-" * 60)
        print(f"저장: {path} ({added}건)")
    print("=" * 60)
    return 1 if outcome.blocked else 0


if __name__ == "__main__":
    sys.exit(main())
