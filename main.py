"""
main.py - 항공권 가격 추적기 (Tracker) : config.ROUTES 의 노선을 차례로 수집

실행:
    python main.py            -> 브라우저를 숨긴 채(headless) 실행
    python main.py --debug    -> 브라우저가 보이는 상태 + 상세 로그

흐름 (노선마다 반복):
    config.ROUTES 읽기
      -> Collector (Google Flights 날짜 표) 로 실제 가격 수집
      -> data/history/<ORIGIN>-<DEST>.csv 에 누적 저장 (노선별 파일)
      -> data/last_run.json 에 실행 요약 저장
      -> 화면에 결과 요약 출력
"""
import argparse
import logging
import sys
import time
from datetime import datetime, timedelta, timezone

import config
import storage
from collectors.google_flights import GoogleFlightsCollector
from routes import load_routes

KST = timezone(timedelta(hours=9))


def build_collector(name: str):
    """config.SOURCE 이름으로 Collector 를 고릅니다. 새 Collector 를 만들면 여기만 추가하면 됩니다."""
    if name == "google_flights":
        return GoogleFlightsCollector(
            currency=config.CURRENCY,
            language=config.LANGUAGE,
            page_load_delay=config.PAGE_LOAD_DELAY_SEC,
        )
    raise ValueError(f"Unknown SOURCE in config.py: {name}")


def collect_route(collector, route, headless, log):
    """노선 하나를 수집하고 저장한 뒤 요약 dict 를 돌려줍니다."""
    log.debug("Route: %s  Target month: %04d-%02d  Nights: %d..%d",
              route.label, route.year, route.month, route.min_nights, route.max_nights)
    started = datetime.now(KST).replace(microsecond=0)
    result = collector.collect(
        origin=route.origin,
        destination=route.destination,
        year=route.year,
        month=route.month,
        min_nights=route.min_nights,
        max_nights=route.max_nights,
        allow_next_month_return=config.ALLOW_NEXT_MONTH_RETURN,
        headless=headless,
    )
    finished = datetime.now(KST).replace(microsecond=0)
    log.debug("Records collected: %d", len(result.records))
    if result.missing_pairs:
        log.debug("Missing pairs: %s", ", ".join(
            f"{d.month}/{d.day}->{r.month}/{r.day}" for d, r in result.missing_pairs))

    added = skipped = 0
    if result.records:
        added, skipped = storage.append_records(
            route.history_file, result.records, route.origin, route.destination)
        log.debug("Saved file: %s (added=%d, duplicates skipped=%d)", route.history_file, added, skipped)
    else:
        log.warning("No records collected for %s - nothing saved", route.label)

    summary = {
        "route": route.slug,
        "origin": route.origin,
        "destination": route.destination,
        "target_month": f"{route.year:04d}-{route.month:02d}",
        "nights_range": [route.min_nights, route.max_nights],
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "records_collected": len(result.records),
        "records_added_to_csv": added,
        "duplicates_skipped": skipped,
        "missing_pairs": [f"{d.isoformat()}->{r.isoformat()}" for d, r in result.missing_pairs],
        "price_semantics": result.price_semantics,
        "semantics_verified": result.semantics_verified,
        "blocked": result.blocked,
        "errors": result.errors,
        "page_loads": result.page_loads,
        "history_file": route.history_file,
        "total_rows_in_csv": storage.count_rows(route.history_file),
    }

    # ---- 사람이 읽는 요약 ----
    print()
    print("=" * 60)
    print(f"✈️  {route.label}   대상: {summary['target_month']}   ({route.history_file})")
    print(f"수집 시각: {started.strftime('%Y-%m-%d %H:%M KST')}  (소요 {(finished - started).seconds}초, "
          f"페이지 로드 {result.page_loads}회)")
    if result.blocked:
        print("상태: 가격 수집 실패 (차단/CAPTCHA)")
    print(f"Records collected: {len(result.records)}   CSV 추가 {added}건 (중복 제외 {skipped}건) "
          f"/ 누적 {summary['total_rows_in_csv']}행")
    if result.missing_pairs:
        print(f"해당 날짜 가격 확인 불가: {len(result.missing_pairs)}건 (data/last_run.json 참고)")
    print(f"가격 의미 교차검증: {'성공' if result.semantics_verified else '실패/미확인'}")
    for e in result.errors:
        print(f"  - {e}")
    if result.records:
        cheapest = min(result.records, key=lambda r: r.price)
        print(f"가장 저렴한 조합: {cheapest.departure_date} -> {cheapest.return_date} "
              f"{cheapest.price:,}원 (왕복 총액)")
    print("=" * 60)
    return summary, bool(result.records) and not result.blocked


def main():
    parser = argparse.ArgumentParser(description="Flight Price Tracker - collector")
    parser.add_argument("--debug", action="store_true",
                        help="브라우저를 화면에 표시하고 [DEBUG] 로그를 출력합니다")
    args = parser.parse_args()

    headless = not args.debug
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="[%(levelname)s] %(message)s",
        stream=sys.stdout,
    )
    log = logging.getLogger("main")

    routes = load_routes()
    log.debug("Flight Tracker started")
    log.debug("Source: %s", config.SOURCE)
    log.debug("Routes: %s", ", ".join(r.slug for r in routes))
    log.debug("Headless: %s", headless)

    collector = build_collector(config.SOURCE)
    run_started = datetime.now(KST).replace(microsecond=0)
    summaries = []
    all_ok = True
    for i, route in enumerate(routes):
        if i > 0:
            time.sleep(config.PAGE_LOAD_DELAY_SEC)  # 노선 사이에도 잠깐 대기
        summary, ok = collect_route(collector, route, headless, log)
        summaries.append(summary)
        all_ok = all_ok and ok
        if summary["blocked"]:
            log.error("Blocked while collecting %s - stopping remaining routes", route.label)
            break

    storage.save_last_run(config.LAST_RUN_FILE, {
        "source": config.SOURCE,
        "run_started_at": run_started.isoformat(),
        "run_finished_at": datetime.now(KST).replace(microsecond=0).isoformat(),
        "headless": headless,
        "routes": summaries,
    })
    print(f"\n총 {len(summaries)}/{len(routes)}개 노선 처리, "
          f"성공 {sum(1 for s in summaries if s['records_collected'] and not s['blocked'])}개")
    return 0 if all_ok and len(summaries) == len(routes) else 1


if __name__ == "__main__":
    sys.exit(main())
