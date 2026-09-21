"""
probe.py - Broad Probe 실행기 (Phase 7-3)

흐름은 반드시 이 순서를 지킵니다.

    Broad Probe          목적지마다 매일 '같은 조건'으로 1회만 훑기
        ↓
    후보 선별            자기 자신의 과거 Probe 기록과 비교해서 내려간 곳만 고름
        ↓
    Precise Confirmation 후보만 넓은 날짜 범위로 다시 검색
        ↓
    최종 판정            정밀 검색 결과를 과거 기록과 비교

원칙
- Probe 가격 자체를 바로 DEAL 로 확정하지 않습니다. Probe 판정은 '후보 선별용'입니다.
- Deep Tracker(main.py, data/history/, tracker.yml)와 데이터도 실행 흐름도 분리되어 있습니다.
- 이력이 부족하면 판단을 보류합니다(HOLD). 가격을 추정하거나 보정하지 않습니다.

Probe 가 '최저가 찾기'가 아니라 '같은 자리 재기'인 이유
- 싼 조합은 189개 중 1~9개로 매우 희소해서, 얕게 훑으면 진짜 최저가는 거의 놓칩니다.
- 대신 매일 같은 자리를 재면 '그 자리가 평소보다 내려갔는지'는 정확히 알 수 있습니다.
- 그래서 기준일(anchor)을 달력에서 결정론적으로 정해 한 달 내내 같은 창을 재고,
  기준일이 바뀌는 시점이 데이터에 그대로 남도록 모든 관측에 날짜를 함께 저장합니다.

실행:
    python probe.py               # 수집 + 후보 선별 + 정밀 확인
    python probe.py --dry-run     # 접속하지 않고 이번에 무엇을 볼지만 출력
    python probe.py --no-confirm  # Probe 와 후보 선별까지만
"""
import argparse
import json
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone

import config
import storage
from analyzer import deals
from analyzer.combinations import Trip, includes_weekend, load_rows
from analyzer.history import RouteHistory, thresholds_from_config
from collectors.google_flights import GoogleFlightsCollector
from destinations import resolve
from search import make_query, run_search
from search_conditions import SearchQuery

KST = timezone(timedelta(hours=9))
GRID_HALF = 3   # 그리드가 기준일 앞뒤로 보여주는 날짜 수 (7x7 창)


# ----------------------------------------------------------------------
# 기준일(anchor) 정하기 - 달력에서 결정론적으로. 한 달 동안 같은 값이 나옵니다.
# ----------------------------------------------------------------------
def add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    return date(d.year + m // 12, m % 12 + 1, 1)


def nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """그 달의 n 번째 특정 요일. 예: 2026년 10월의 2번째 화요일."""
    first = date(year, month, 1)
    shift = (weekday - first.weekday()) % 7
    d = first + timedelta(days=shift + 7 * (n - 1))
    if d.month != month:
        raise ValueError(f"{year}-{month} 에는 {n}번째 요일이 없습니다")
    return d


def probe_anchor(today: date, cfg=config):
    """이번 Probe 의 기준 출발일/귀국일. 같은 달 안에서는 매일 같은 값입니다."""
    base = add_months(today, cfg.PROBE_MONTHS_AHEAD)
    dep = nth_weekday(base.year, base.month, cfg.PROBE_WEEKDAY, cfg.PROBE_WEEK_OF_MONTH)
    return dep, dep + timedelta(days=cfg.PROBE_NIGHTS)


def probe_query(origin, destination, dep, ret) -> SearchQuery:
    """기준일 앞뒤 3일씩(7x7 창)을 한 번의 페이지 로드로 덮는 검색 조건."""
    return SearchQuery(
        origin=origin, destination=destination,
        depart_from=dep - timedelta(days=GRID_HALF), depart_to=dep + timedelta(days=GRID_HALF),
        min_nights=1, max_nights=7,
        return_from=ret - timedelta(days=GRID_HALF), return_to=ret + timedelta(days=GRID_HALF))


def probe_file(origin, code):
    return os.path.join(config.PROBE_DIR, f"{origin}-{code}.csv")


def confirm_file(origin, code):
    return os.path.join(config.PROBE_DIR, "confirm", f"{origin}-{code}.csv")


# ----------------------------------------------------------------------
# 관측 -> Trip
# ----------------------------------------------------------------------
def records_to_trips(records, origin, airport):
    trips = []
    for r in records:
        d = date.fromisoformat(r.departure_date)
        t = date.fromisoformat(r.return_date)
        trips.append(Trip(departure_date=d, return_date=t, nights=(t - d).days,
                          price=r.price, currency=r.currency,
                          includes_weekend=includes_weekend(d, t), source_tag=r.source_tag,
                          origin=origin, destination=airport.code, destination_label=airport.label))
    return trips


def history_for(path, origin, code, today):
    return RouteHistory.from_file(path, origin, code, today, thresholds_from_config(config))


# ----------------------------------------------------------------------
# Deep Tracker 와 비교: Probe 가 무엇을 놓치는지 측정
# ----------------------------------------------------------------------
def tracker_comparison(origin, code, today, probe_pairs, probe_min):
    """같은 날 Deep Tracker 가 본 것과 Probe 가 본 것을 비교합니다.

    - window_min : 트래커 데이터 중 Probe 가 본 창 안의 최저가 (같아야 정상)
    - full_min   : 트래커가 본 그 달 전체 최저가
    - missed_pct : Probe 가 전체 최저가보다 몇 % 비싼 값을 보고 있는지 (= 놓치는 정도)
    """
    rows = load_rows(os.path.join(config.HISTORY_DIR, f"{origin}-{code}.csv"))
    rows = [r for r in rows if r.get("origin") == origin and r.get("destination") == code]
    same_day = [r for r in rows if r["collected_at"][:10] == today.isoformat()]
    if not same_day:
        return None
    last = max(r["collected_at"] for r in same_day)
    same_day = [r for r in same_day if r["collected_at"] == last]
    full_min = min(int(r["price"]) for r in same_day)
    in_window = [int(r["price"]) for r in same_day
                 if (date.fromisoformat(r["departure_date"]), date.fromisoformat(r["return_date"])) in probe_pairs]
    window_min = min(in_window) if in_window else None
    out = {"tracker_rows_today": len(same_day), "tracker_full_min": full_min,
           "tracker_window_min": window_min, "probe_min": probe_min,
           "pairs_in_window": len(in_window)}
    if probe_min and full_min:
        out["missed_pct"] = round((probe_min - full_min) / full_min * 100, 2)
    if probe_min and window_min:
        out["window_agreement_diff"] = probe_min - window_min
    return out


# ----------------------------------------------------------------------
# 실행
# ----------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(description="Broad Probe - 목적지별로 매일 같은 조건으로 가격을 재고 후보를 고릅니다")
    p.add_argument("--dry-run", action="store_true", help="접속하지 않고 이번 실행 계획만 출력")
    p.add_argument("--no-confirm", action="store_true", help="정밀 확인 단계를 건너뜀")
    p.add_argument("--debug", action="store_true", help="브라우저 표시 + 상세 로그")
    args = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.WARNING,
                        format="[%(levelname)s] %(message)s", stream=sys.stdout)

    today = datetime.now(KST).date()
    now_iso = datetime.now(KST).replace(microsecond=0).isoformat()
    env = "actions" if os.environ.get("GITHUB_ACTIONS") == "true" else "local"
    origin = config.PROBE_ORIGIN
    airports = resolve(config.PROBE_DESTINATIONS)
    anchor_dep, anchor_ret = probe_anchor(today)
    sample = probe_query(origin, airports[0].code, anchor_dep, anchor_ret)
    pairs = sample.needed_pairs()
    loads_each = len(GoogleFlightsCollector._plan_anchors(pairs))

    print("=" * 66)
    print("🔭 Broad Probe")
    print(f"출발지 {origin} / 목적지 {len(airports)}곳: " + ", ".join(a.label for a in airports))
    print(f"기준일(anchor): {anchor_dep} ~ {anchor_ret}  ({config.PROBE_NIGHTS}박 기준, "
          f"{config.PROBE_MONTHS_AHEAD}개월 뒤 {config.PROBE_WEEK_OF_MONTH}번째 요일)")
    print(f"목적지당 조합 {len(pairs)}개 / 페이지 로드 {loads_each}회 "
          f"→ Probe 합계 약 {loads_each * len(airports)}회")
    if loads_each != 1:
        print(f"  주의: 목적지당 로드가 {loads_each}회입니다. 1회가 되도록 설정을 확인하세요.")
    if args.dry_run:
        print("(--dry-run 이므로 접속하지 않고 종료합니다)")
        return 0

    collector = GoogleFlightsCollector(currency=config.CURRENCY, language=config.LANGUAGE,
                                       page_load_delay=config.PAGE_LOAD_DELAY_SEC)
    th_pairs = {(d, r) for d, r in pairs}
    started = time.time()
    probes, total_loads, blocked = [], 0, False

    print("-" * 66)
    for i, a in enumerate(airports):
        if i > 0:
            time.sleep(config.PAGE_LOAD_DELAY_SEC)
        q = probe_query(origin, a.code, anchor_dep, anchor_ret)
        t0 = time.time()
        res = collector.collect_query(q, headless=not args.debug)
        total_loads += res.page_loads

        path = probe_file(origin, a.code)
        added, _ = storage.append_records(path, res.records, origin, a.code) if res.records else (0, 0)
        storage.append_semantics_checks(config.SEMANTICS_LOG, res.semantics_checks,
                                        origin, a.code, now_iso, env, context="probe")

        trips = records_to_trips(res.records, origin, a)
        best = min(trips, key=lambda t: (t.price, t.nights, t.departure_date)) if trips else None
        mism = [c for c in res.semantics_checks if not c["ok"]]
        print(f"  [{i + 1}/{len(airports)}] {a.label:<12} "
              + (f"{len(trips)}건, 최저 {best.price:>9,}원 {best.period_label} {best.nights}박"
                 if best else "가격 확인 불가")
              + f"  (저장 {added}건, 검증 불일치 {len(mism)}건, {time.time() - t0:.0f}초)")
        for e in res.errors:
            print(f"        ! {e}")
        probes.append({"airport": a, "result": res, "trips": trips, "best": best,
                       "saved": added, "mismatches": len(mism)})
        if res.blocked:
            blocked = True
            print("  차단/CAPTCHA 감지 - 남은 목적지를 중단합니다 (우회하지 않음).")
            break

    # ---- 후보 선별: 자기 자신의 과거 Probe 기록과 비교 (판정은 후보 선별용) ----
    print("-" * 66)
    print("후보 선별 (Probe 기록 대비). 여기서 나온 판정은 후보를 고르기 위한 것이며 최종 결론이 아닙니다.")
    for pr in probes:
        best = pr["best"]
        if best is None:
            pr["verdict"] = None
            continue
        hist = history_for(probe_file(origin, pr["airport"].code), origin, pr["airport"].code, today)
        v = deals.judge(best.price, hist, nights=best.nights, dep=best.departure_date,
                        ret=best.return_date, rules=config.DEAL_RULES)
        pr["verdict"] = v
        pr["probe_days"] = hist.days_collected
        print(f"  {pr['airport'].label:<12} {best.price:>9,}원  [{v.label}] {v.headline}"
              f"  (Probe 이력 {hist.days_collected}일치)")

    candidates = [pr for pr in probes
                  if pr.get("verdict") and pr["verdict"].label in (deals.LABEL_DEAL, deals.LABEL_WATCH)]
    candidates.sort(key=lambda pr: pr["best"].price)
    candidates = candidates[:config.PROBE_MAX_CANDIDATES]
    print(f"  → 후보 {len(candidates)}곳"
          + (": " + ", ".join(c["airport"].label for c in candidates) if candidates else " (없음)"))

    # ---- 정밀 확인: 후보만 넓은 날짜 범위로 재검색 ----
    confirmations = []
    if candidates and not args.no_confirm and not blocked:
        print("-" * 66)
        print("정밀 확인 (후보만 넓은 범위로 재검색)")
        span = config.PROBE_CONFIRM_SPAN_DAYS
        for c in candidates:
            a, best = c["airport"], c["best"]
            d1, d2 = best.departure_date - timedelta(days=span), best.departure_date + timedelta(days=span)
            base = make_query(origin, f"{d1}..{d2}", "1..7")
            t0 = time.time()
            outcome = run_search(base, [a], headless=not args.debug)
            total_loads += outcome.page_loads
            for airport, res in outcome.collected:
                storage.append_semantics_checks(config.SEMANTICS_LOG, res.semantics_checks,
                                                origin, airport.code, now_iso, env, context="confirm")
                if res.records:
                    storage.append_records(confirm_file(origin, airport.code), res.records, origin, airport.code)
            if not outcome.trips:
                print(f"  {a.label:<12} 가격 확인 불가")
                confirmations.append({"airport": a, "verdict": None, "best": None})
                continue
            cbest = outcome.ranked[0]
            hist = history_for(probe_file(origin, a.code), origin, a.code, today)
            v = deals.judge(cbest.price, hist, nights=cbest.nights, dep=cbest.departure_date,
                            ret=cbest.return_date, rules=config.DEAL_RULES)
            print(f"  {a.label:<12} {cbest.price:>9,}원 {cbest.period_label} {cbest.nights}박 "
                  f"→ 최종 [{v.label}] {v.headline}  ({outcome.page_loads}회 로드, {time.time() - t0:.0f}초)")
            confirmations.append({"airport": a, "verdict": v, "best": cbest,
                                  "loads": outcome.page_loads})

    # ---- Deep Tracker 와 비교: Probe 가 놓치는 정도 ----
    print("-" * 66)
    print("Deep Tracker 대비 Probe 가 보는 범위 (트래커가 있는 노선만)")
    comparisons = {}
    any_cmp = False
    for pr in probes:
        code = pr["airport"].code
        cmp_ = tracker_comparison(origin, code, today, th_pairs,
                                  pr["best"].price if pr["best"] else None)
        if cmp_ is None:
            continue
        any_cmp = True
        comparisons[code] = cmp_
        label = pr["airport"].label
        pmin, wmin = cmp_["probe_min"], cmp_["tracker_window_min"]
        agree = cmp_.get("window_agreement_diff")
        line = f"  {label:<12} Probe {pmin:>9,}원" if pmin else f"  {label:<12} Probe 값 없음"
        if wmin is not None and agree is not None:
            line += f" | 트래커의 같은 창 {wmin:>9,}원 (차이 {agree:+,}원, 같아야 정상)"
        else:
            line += f" | 같은 창 비교 불가 (겹치는 조합 {cmp_['pairs_in_window']}개)"
        print(line)
        missed = cmp_.get("missed_pct")
        print(f"  {'':<12} 트래커가 본 그 달 전체 최저 {cmp_['tracker_full_min']:>9,}원"
              + (f" → Probe 는 {missed}% 비싼 값을 보고 있음 (= 놓치는 정도)" if missed is not None else ""))
    if not any_cmp:
        print("  (오늘 같은 날짜의 Deep Tracker 데이터가 없어 비교하지 못했습니다)")

    # ---- 결과 저장 ----
    elapsed = int(time.time() - started)
    feed = {
        "date": today.isoformat(),
        "run_at": now_iso,
        "env": env,
        "origin": origin,
        "anchor": {"dep": anchor_dep.isoformat(), "ret": anchor_ret.isoformat(),
                   "nights": config.PROBE_NIGHTS, "months_ahead": config.PROBE_MONTHS_AHEAD,
                   "pairs": len(pairs)},
        "page_loads": total_loads,
        "elapsed_sec": elapsed,
        "blocked": blocked,
        "probes": [{
            "code": pr["airport"].code, "label": pr["airport"].label,
            "records": len(pr["trips"]), "saved": pr["saved"], "mismatches": pr["mismatches"],
            "probe_days": pr.get("probe_days"),
            "best_price": pr["best"].price if pr["best"] else None,
            "best_dep": pr["best"].departure_date.isoformat() if pr["best"] else None,
            "best_ret": pr["best"].return_date.isoformat() if pr["best"] else None,
            "best_nights": pr["best"].nights if pr["best"] else None,
            "source_tag": pr["best"].source_tag if pr["best"] else "",
            "candidate_label": pr["verdict"].label if pr.get("verdict") else None,
            "candidate_reason": pr["verdict"].headline if pr.get("verdict") else None,
        } for pr in probes],
        "candidates": [c["airport"].code for c in candidates],
        "confirmations": [{
            "code": c["airport"].code, "label": c["airport"].label,
            "price": c["best"].price if c["best"] else None,
            "dep": c["best"].departure_date.isoformat() if c["best"] else None,
            "ret": c["best"].return_date.isoformat() if c["best"] else None,
            "nights": c["best"].nights if c["best"] else None,
            "final_label": c["verdict"].label if c["verdict"] else None,
            "final_reason": c["verdict"].headline if c["verdict"] else None,
            "final_reasons": c["verdict"].reasons if c["verdict"] else [],
        } for c in confirmations],
        "tracker_comparison": comparisons,
    }
    os.makedirs(config.PROBE_FEED_DIR, exist_ok=True)
    for name in (f"{today.isoformat()}.json", "latest.json"):
        with open(os.path.join(config.PROBE_FEED_DIR, name), "w", encoding="utf-8") as f:
            json.dump(feed, f, ensure_ascii=False, indent=2)

    print("=" * 66)
    print(f"완료: 목적지 {len(probes)}곳, 페이지 로드 {total_loads}회, {elapsed}초, "
          f"후보 {len(candidates)}곳, 정밀 확인 {len(confirmations)}건")
    print(f"저장: {config.PROBE_DIR}/  요약: {config.PROBE_FEED_DIR}/latest.json")
    return 1 if blocked else 0


if __name__ == "__main__":
    sys.exit(main())
