"""
main.py - 항공권 가격 추적기 (Phase 1: 실제 가격 수집 검증)

실행:
    python main.py            -> 브라우저를 숨긴 채(headless) 실행
    python main.py --debug    -> 브라우저가 보이는 상태 + 상세 로그

흐름:
    config.py 설정 읽기
      -> Collector (Google Flights 날짜 표) 로 실제 가격 수집
      -> data/flights_raw.csv 에 누적 저장
      -> data/last_run.json 에 실행 요약 저장
      -> 화면에 결과 요약 출력
"""
import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone

import config
import storage
from collectors.google_flights import GoogleFlightsCollector

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


def main():
    parser = argparse.ArgumentParser(description="Flight Price Tracker - Phase 1 collector")
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

    log.debug("Flight Tracker started")
    log.debug("Source: %s", config.SOURCE)
    log.debug("Route: %s -> %s", config.ORIGIN, config.DESTINATION)
    log.debug("Target month: %04d-%02d", config.TARGET_YEAR, config.TARGET_MONTH)
    log.debug("Nights: %d..%d (next-month return allowed: %s)",
              config.MIN_NIGHTS, config.MAX_NIGHTS, config.ALLOW_NEXT_MONTH_RETURN)
    log.debug("Headless: %s", headless)

    started = datetime.now(KST).replace(microsecond=0)
    collector = build_collector(config.SOURCE)
    result = collector.collect(
        origin=config.ORIGIN,
        destination=config.DESTINATION,
        year=config.TARGET_YEAR,
        month=config.TARGET_MONTH,
        min_nights=config.MIN_NIGHTS,
        max_nights=config.MAX_NIGHTS,
        allow_next_month_return=config.ALLOW_NEXT_MONTH_RETURN,
        headless=headless,
    )
    finished = datetime.now(KST).replace(microsecond=0)

    log.debug("Data collection completed")
    log.debug("Records collected: %d", len(result.records))
    if result.missing_pairs:
        log.debug("Missing pairs: %s", ", ".join(
            f"{d.month}/{d.day}->{r.month}/{r.day}" for d, r in result.missing_pairs))

    # ---- 저장 (실제 수집된 데이터가 있을 때만) ----
    added = skipped = 0
    if result.records:
        added, skipped = storage.append_records(
            config.RAW_FILE, result.records, config.ORIGIN, config.DESTINATION)
        log.debug("Saved file: %s (added=%d, duplicates skipped=%d)", config.RAW_FILE, added, skipped)
    else:
        log.warning("No records collected - nothing saved to %s", config.RAW_FILE)

    summary = {
        "source": config.SOURCE,
        "origin": config.ORIGIN,
        "destination": config.DESTINATION,
        "target_month": f"{config.TARGET_YEAR:04d}-{config.TARGET_MONTH:02d}",
        "nights_range": [config.MIN_NIGHTS, config.MAX_NIGHTS],
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "headless": headless,
        "records_collected": len(result.records),
        "records_added_to_csv": added,
        "duplicates_skipped": skipped,
        "missing_pairs": [f"{d.isoformat()}->{r.isoformat()}" for d, r in result.missing_pairs],
        "price_semantics": result.price_semantics,
        "semantics_verified": result.semantics_verified,
        "blocked": result.blocked,
        "errors": result.errors,
        "page_loads": result.page_loads,
        "grid_steps": result.grid_steps,
        "total_rows_in_csv": storage.count_rows(config.RAW_FILE),
    }
    storage.save_last_run(config.LAST_RUN_FILE, summary)

    # ---- 사람이 읽는 요약 ----
    print()
    print("=" * 60)
    print(f"✈️  Flight Price Tracker - Phase 1 수집 결과")
    print(f"노선: {config.ORIGIN} -> {config.DESTINATION}   대상: {summary['target_month']}")
    print(f"수집 시각: {started.strftime('%Y-%m-%d %H:%M KST')}  (소요 {(finished - started).seconds}초)")
    print(f"수집원: {config.SOURCE}   페이지 로드 {result.page_loads}회")
    print("-" * 60)
    if result.blocked:
        print("상태: 가격 수집 실패 (차단/CAPTCHA)")
    print(f"Records collected: {len(result.records)}")
    print(f"CSV 추가: {added}건 (중복 제외 {skipped}건) / 누적 행 수: {summary['total_rows_in_csv']}")
    if result.missing_pairs:
        print(f"해당 날짜 가격 확인 불가: {len(result.missing_pairs)}건")
        for d, r in result.missing_pairs[:20]:
            print(f"  {d.month:02d}/{d.day:02d} -> {r.month:02d}/{r.day:02d}")
        if len(result.missing_pairs) > 20:
            print(f"  ... 외 {len(result.missing_pairs) - 20}건 (data/last_run.json 참고)")
    else:
        print("누락 조합: 없음")
    print("-" * 60)
    print(f"가격 의미: {result.price_semantics}")
    print(f"가격 의미 교차검증: {'성공' if result.semantics_verified else '실패/미확인'}")
    if result.errors:
        print("-" * 60)
        print("발생한 문제:")
        for e in result.errors:
            print(f"  - {e}")
    if result.records:
        cheapest = min(result.records, key=lambda r: r.price)
        print("-" * 60)
        print(f"이번 수집에서 가장 저렴한 조합: {cheapest.departure_date} -> {cheapest.return_date} "
              f"{cheapest.price:,}원 (왕복 총액)")
    print("=" * 60)

    return 0 if result.records and not result.blocked else 1


if __name__ == "__main__":
    sys.exit(main())
