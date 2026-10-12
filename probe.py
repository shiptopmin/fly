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
import csv
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
from routes import observation_sources
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


def probe_anchor(today: date, cfg=config, extra_months: int = 0):
    """이번 Probe 의 기준 출발일/귀국일. 같은 달 안에서는 매일 같은 값입니다.

    extra_months=1 이면 한 달 더 뒤의 창(사전 측정용)입니다. 기본값 0 은 기존 동작 그대로입니다.
    """
    base = add_months(today, cfg.PROBE_MONTHS_AHEAD + extra_months)
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


def history_for(origin, code, today):
    """판정에 쓸 이력. 이 노선에 대해 '보유한 모든 관측'을 함께 봅니다.

    - probe   : 매일 같은 창을 잰 기록 (비교 가능한 시계열)
    - confirm : 정밀 검색으로 본 기록 (창 밖 날짜가 많음)
    - tracker : Deep Tracker 가 있는 노선이면 그 이력까지
    - preroll : 창이 넘어가기 전에 미리 잰 다음 달 창 기록 (Probe 판정에만 쓰고, 트래커 화면/검색 비교에는 섞지 않음)
    이렇게 해야 "수집 이후 최저" 같은 주장이 우리가 이미 본 값에 반박당하지 않습니다.
    판정은 출발월로 한정해서 비교하므로, 사전 측정 기록은 그 달 창이 본 측정이 된 뒤에야 비교에 들어갑니다.
    """
    groups = [(label, load_rows(path)) for label, path in observation_sources(origin, code)]
    # 사전 측정분은 '지금 본 측정 중인 창의 출발월'인 행만 넣습니다. 아직 본 측정이 아닌 창(다음 달)은
    # 판정에도, 비교 근거 문구(관측 N건)에도 나타나지 않고, 창이 넘어간 날부터 그 달 이력으로 들어옵니다.
    ym = probe_anchor(today)[0].isoformat()[:7]
    groups.append(("preroll", [r for r in load_rows(preroll_file(origin, code))
                               if r["departure_date"][:7] == ym]))
    return RouteHistory.from_groups(groups, origin, code, today, thresholds_from_config(config))


# ----------------------------------------------------------------------
# 사전 측정(preroll): 다음 달 창을 미리 재서 '기록만' 합니다
# ----------------------------------------------------------------------
# 왜 별도 파일인가: 이력의 '하루'는 출처(파일)마다 그날 마지막 실행 하나로 정해집니다.
# 본 측정 파일에 같은 날 다른 창을 또 쌓으면 나중 실행만 인정되어 한쪽 창의 하루가 통째로 사라집니다.
PREROLL_STATUS_FIELDS = ["date", "run_at", "origin", "destination", "anchor_dep", "anchor_ret",
                         "collected_at", "expected_pairs", "records", "saved", "mismatches",
                         "errors", "blocked", "page_loads", "semantics_verified", "complete",
                         "nights_counts", "problems"]


def preroll_active(today: date, cfg=config) -> bool:
    """오늘이 '다음 달 1일의 PROBE_PREROLL_DAYS 일 전'부터 월말 사이인가."""
    days = cfg.PROBE_PREROLL_DAYS
    if not days:
        return False
    return (add_months(today, 1) - today).days <= days


def preroll_file(origin, code):
    return os.path.join(config.PROBE_PREROLL_DIR, f"{origin}-{code}.csv")


def check_preroll_rows(rows, expected_pairs, origin, code, today):
    """저장된 행(CSV dict)이 '오늘 완성된 한 번의 측정'인지 확인합니다.

    돌려주는 값: (문제 목록, {숙박일수: 건수}). 문제 목록이 비어 있어야만 정상 완료입니다.
    조합 수만 보지 않고 목적지/날짜/숙박일수/가격 의미/수집 시각을 하나하나 맞춰 봅니다.
    """
    problems = []
    if not rows:
        return ["저장된 행 없음"], {}
    stamps = {r["collected_at"] for r in rows}
    if len(stamps) != 1:
        problems.append(f"수집 시각이 {len(stamps)}종류 (한 번의 측정이 아님)")
    if any(s[:10] != today.isoformat() for s in stamps):
        problems.append("수집일이 오늘이 아님")
    pairs, counts = [], {}
    for r in rows:
        try:
            dep, ret = date.fromisoformat(r["departure_date"]), date.fromisoformat(r["return_date"])
            price = int(r["price"])
        except (ValueError, KeyError, TypeError):
            problems.append("날짜/가격을 읽을 수 없는 행")
            continue
        pairs.append((dep, ret))
        n = (ret - dep).days
        counts[n] = counts.get(n, 0) + 1
        if price <= 0:
            problems.append(f"{dep}→{ret} 가격이 0 이하")
        if int(r.get("nights") or -1) != n:
            problems.append(f"{dep}→{ret} 숙박일수 불일치")
        if r.get("currency") != config.CURRENCY or r.get("price_type") != "round_trip_total":
            problems.append(f"{dep}→{ret} 가격 의미 표시가 다름")
        if r.get("origin") != origin or r.get("destination") != code:
            problems.append(f"{dep}→{ret} 노선이 다름")
    if len(set(pairs)) != len(pairs):
        problems.append("같은 조합이 중복됨")
    expected = set(expected_pairs)
    if set(pairs) != expected:
        problems.append(f"조합 불일치 (부족 {len(expected - set(pairs))}, 초과 {len(set(pairs) - expected)})")
    want = {}
    for d, r in expected:
        want[(r - d).days] = want.get((r - d).days, 0) + 1
    for n in sorted(set(want) | set(counts)):
        if counts.get(n, 0) != want.get(n, 0):
            problems.append(f"{n}박 {counts.get(n, 0)}건 (기대 {want.get(n, 0)}건)")
    return problems, counts


def _fmt_counts(counts):
    return "|".join(f"{n}:{counts[n]}" for n in sorted(counts))


def _preroll_day_rows(path, origin, code, today):
    return [r for r in load_rows(path) if r.get("origin") == origin and r.get("destination") == code
            and r["collected_at"][:10] == today.isoformat()]


def preroll_done(origin, code, today, anchor_dep, expected_pairs, status_path=None, path=None):
    """오늘 이 목적지의 사전 측정이 '정상 완료'되어 있는가 (되어 있으면 재실행을 건너뜁니다).

    상태 기록의 complete 표시만 믿지 않고, 저장된 행을 다시 검사합니다.
    그리고 그 측정이 오늘의 '마지막 측정'이어야 합니다 (나중에 불완전한 측정이 있으면 하루 값이 그쪽으로 바뀜).
    """
    status_path = status_path or config.PROBE_PREROLL_STATUS
    path = path or preroll_file(origin, code)
    marks = [s for s in load_rows(status_path)
             if s["date"] == today.isoformat() and s["destination"] == code and s["origin"] == origin
             and s["anchor_dep"] == anchor_dep.isoformat() and s["complete"] == "1"]
    if not marks:
        return False
    day_rows = _preroll_day_rows(path, origin, code, today)
    if not day_rows:
        return False
    last_stamp = max(r["collected_at"] for r in day_rows)
    for s in reversed(marks):
        if s["collected_at"] != last_stamp:
            continue
        problems, _ = check_preroll_rows([r for r in day_rows if r["collected_at"] == last_stamp],
                                         expected_pairs, origin, code, today)
        if not problems:
            return True
    return False


def append_preroll_status(row, status_path=None):
    status_path = status_path or config.PROBE_PREROLL_STATUS
    os.makedirs(os.path.dirname(status_path) or ".", exist_ok=True)
    is_new = not os.path.exists(status_path) or os.path.getsize(status_path) == 0
    with open(status_path, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=PREROLL_STATUS_FIELDS)
        if is_new:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in PREROLL_STATUS_FIELDS})


def run_preroll(collector, airports, origin, today, now_iso, env, headless=True,
                sleep=time.sleep, say=print):
    """다음 달 창(한 달 더 뒤)을 목적지마다 1회 재서 data/probes/preroll/ 에만 저장합니다.

    판정, 후보 선정, 정밀 검색에는 연결하지 않습니다. 차단이 감지되면 우회하지 않고 거기서 멈춥니다.
    """
    dep, ret = probe_anchor(today, extra_months=1)
    pairs = set(probe_query(origin, airports[0].code, dep, ret).needed_pairs())
    loads_each = len(GoogleFlightsCollector._plan_anchors(sorted(pairs)))
    out = {"active": True,
           "anchor": {"dep": dep.isoformat(), "ret": ret.isoformat(), "pairs": len(pairs),
                      "months_ahead": config.PROBE_MONTHS_AHEAD + 1},
           "page_loads": 0, "blocked": False, "destinations": []}
    started = time.time()
    for a in airports:
        item = {"code": a.code, "status": "", "records": 0, "saved": 0, "mismatches": 0,
                "errors": 0, "problems": []}
        out["destinations"].append(item)
        if preroll_done(origin, a.code, today, dep, pairs):
            item["status"] = "skipped_complete"
            say(f"  사전 측정 {a.label:<12} 오늘 정상 완료분이 있어 건너뜀")
            continue
        sleep(config.PAGE_LOAD_DELAY_SEC)
        res = collector.collect_query(probe_query(origin, a.code, dep, ret), headless=headless)
        out["page_loads"] += res.page_loads
        path = preroll_file(origin, a.code)
        added = storage.append_records(path, res.records, origin, a.code)[0] if res.records else 0
        storage.append_semantics_checks(config.SEMANTICS_LOG, res.semantics_checks,
                                        origin, a.code, now_iso, env, context="preroll")
        stamp = res.records[0].collected_at if res.records else ""
        rows = [r for r in _preroll_day_rows(path, origin, a.code, today) if r["collected_at"] == stamp]
        problems, counts = check_preroll_rows(rows, pairs, origin, a.code, today)
        mism = sum(1 for c in res.semantics_checks if not c["ok"])
        if res.blocked:
            problems.append("차단/CAPTCHA 감지")
        if res.errors:
            problems.append(f"수집 오류 {len(res.errors)}건: {res.errors[0]}")
        if mism:
            problems.append(f"가격 의미 검증 불일치 {mism}건")
        if not res.semantics_verified:
            problems.append("가격 의미 교차검증 미완료")
        if res.page_loads != loads_each:
            problems.append(f"페이지 로드 {res.page_loads}회 (기대 {loads_each}회)")
        if res.records and added != len(res.records):
            problems.append(f"저장 {added}건 / 수집 {len(res.records)}건")
        complete = not problems
        item.update(status="complete" if complete else "incomplete", records=len(res.records),
                    saved=added, mismatches=mism, errors=len(res.errors), problems=problems,
                    nights_counts=_fmt_counts(counts))
        append_preroll_status({
            "date": today.isoformat(), "run_at": now_iso, "origin": origin, "destination": a.code,
            "anchor_dep": dep.isoformat(), "anchor_ret": ret.isoformat(), "collected_at": stamp,
            "expected_pairs": len(pairs), "records": len(res.records), "saved": added,
            "mismatches": mism, "errors": len(res.errors), "blocked": int(res.blocked),
            "page_loads": res.page_loads, "semantics_verified": int(bool(res.semantics_verified)),
            "complete": int(complete), "nights_counts": _fmt_counts(counts),
            "problems": " / ".join(problems)})
        say(f"  사전 측정 {a.label:<12} {len(res.records)}건 저장 {added}건 "
            + ("정상 완료" if complete else "불완전: " + "; ".join(problems)))
        if res.blocked:
            out["blocked"] = True
            say("  차단/CAPTCHA 감지 - 사전 측정을 중단합니다 (우회하지 않음).")
            break
    out["elapsed_sec"] = int(time.time() - started)
    out["all_complete"] = all(d["status"] in ("complete", "skipped_complete") for d in out["destinations"]) \
        and len(out["destinations"]) == len(airports)
    return out


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
    p.add_argument("--today", help="(--dry-run 전용) 이 날짜인 것처럼 계획만 출력. 예: 2026-10-24")
    args = p.parse_args()
    if args.today and not args.dry_run:
        p.error("--today 는 --dry-run 과 함께만 쓸 수 있습니다 (실제 수집 날짜를 속이지 않기 위해)")
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.WARNING,
                        format="[%(levelname)s] %(message)s", stream=sys.stdout)

    today = date.fromisoformat(args.today) if args.today else datetime.now(KST).date()
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
    if preroll_active(today):
        pre_dep, pre_ret = probe_anchor(today, extra_months=1)
        print(f"사전 측정(기록만): 기준일 {pre_dep} ~ {pre_ret}  ({config.PROBE_MONTHS_AHEAD + 1}개월 뒤) / "
              f"목적지당 {loads_each}회 → 합계 약 {loads_each * len(airports)}회. 판정·후보·정밀검색에는 쓰지 않음")
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
        hist = history_for(origin, pr["airport"].code, today)
        # 현재 관측 = 이번 Probe 의 최저 조합, 판정 시점 = 이 목적지 Probe 의 수집 시각
        pr["as_of"] = datetime.fromisoformat(pr["result"].records[0].collected_at)
        v = deals.judge(best.price, hist, pr["as_of"], nights=best.nights, dep=best.departure_date,
                        ret=best.return_date, rules=config.DEAL_RULES)
        pr["verdict"] = v
        pr["probe_days"] = v.metrics.get("base_days", 0)   # 같은 출발월·같은 숙박일수 이력 일수
        print(f"  {pr['airport'].label:<12} {best.price:>9,}원  [{v.label}] {v.headline}"
              f"  (같은 출발월 이력 {pr['probe_days']}일치)")

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
            hist = history_for(origin, a.code, today)
            # 정밀 확인은 Probe 가 연 하나의 발견 사건이므로, 판정 시점은 그 목적지 Probe 의 수집 시각입니다.
            # (같은 사건의 Probe 기록이 '과거'로 들어가 자기 발견과 비교되는 것을 막습니다)
            v = deals.judge(cbest.price, hist, c["as_of"], nights=cbest.nights, dep=cbest.departure_date,
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
    def write_feed():
        os.makedirs(config.PROBE_FEED_DIR, exist_ok=True)
        for name in (f"{today.isoformat()}.json", "latest.json"):
            with open(os.path.join(config.PROBE_FEED_DIR, name), "w", encoding="utf-8") as f:
                json.dump(feed, f, ensure_ascii=False, indent=2)

    write_feed()   # 본 측정 결과는 사전 측정보다 먼저 확정해 둡니다

    # ---- 사전 측정: 본 측정/후보/정밀검색이 모두 끝난 뒤, 기록만 합니다 ----
    preroll_failed = False
    if preroll_active(today):
        print("-" * 66)
        print("사전 측정 (다음 달 창을 미리 재서 기록만 합니다. 판정에는 쓰지 않음)")
        if blocked:
            feed["preroll"] = {"active": True, "skipped": "본 측정에서 차단이 감지되어 사전 측정을 하지 않음"}
        else:
            try:
                feed["preroll"] = run_preroll(collector, airports, origin, today, now_iso, env,
                                              headless=not args.debug)
                preroll_failed = feed["preroll"]["blocked"]
            except Exception as e:   # 사전 측정의 오류가 본 측정 결과를 해치지 않도록 격리
                feed["preroll"] = {"active": True, "error": f"{type(e).__name__}: {e}"}
                preroll_failed = True
                print(f"  ! 사전 측정 오류: {type(e).__name__}: {e}")
        write_feed()

    print("=" * 66)
    print(f"완료: 목적지 {len(probes)}곳, 페이지 로드 {total_loads}회, {elapsed}초, "
          f"후보 {len(candidates)}곳, 정밀 확인 {len(confirmations)}건")
    print(f"저장: {config.PROBE_DIR}/  요약: {config.PROBE_FEED_DIR}/latest.json")
    return 1 if (blocked or preroll_failed) else 0


if __name__ == "__main__":
    sys.exit(main())
