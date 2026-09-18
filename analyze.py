"""
analyze.py - 저장된 실제 가격 데이터 분석 (터미널 출력) - 노선별로 따로 계산

실행:
    python analyze.py                 -> 모든 추적 노선 요약
    python analyze.py --route ICN-KIX -> 한 노선만
    python analyze.py --all           -> 모든 조합 목록도 출력

사이트에 접속하지 않습니다. data/history/<노선>.csv 만 읽습니다.
계산은 analyzer/report.py 에서 하고, 여기서는 보여주기만 합니다.
"""
import argparse
import sys

import config
from analyzer.report import build_report
from analyzer import stats
from routes import load_routes

MEDALS = ["🥇", "🥈", "🥉", "4위", "5위"]


def fmt_stats(w: stats.WindowStats):
    if w.days == 0:
        return f"{w.label}: 데이터 없음"
    if not w.enough:
        return (f"{w.label}: 데이터 부족 ({w.days}일치, 최소 {config.MIN_DAYS_FOR_STATS}일 필요) "
                f"- 참고: 평균 {w.avg:,.0f}원 / 최저 {w.low:,}원 / 최고 {w.high:,}원")
    return f"{w.label}: 평균 {w.avg:,.0f}원 / 최저 {w.low:,}원 / 최고 {w.high:,}원 ({w.days}일치)"


def print_report(rp, show_all=False):
    print("=" * 60)
    print(f"✈️  {rp.origin} → {rp.destination}   {rp.year}년 {rp.month}월")
    print(f"마지막 수집: {rp.latest_collected_at}   (분석 조합 {len(rp.trips)}개, 수집일 수 {rp.days_collected}일, 누적 {rp.total_rows}행)")
    print("-" * 60)
    print(f"현재 최저가        {rp.current_min:>12,}원")
    print(f"수집 이후 최저가   {rp.s_all.low:>12,}원   (첫 수집 {rp.first_day} 부터, {rp.s_all.days}일치)")
    print(f"상태: {rp.status}")
    print("-" * 60)
    print(fmt_stats(rp.s30))
    print(fmt_stats(rp.s90))
    print(fmt_stats(rp.s_all))
    print("-" * 60)
    if rp.change is None:
        print("가격 변화: 비교할 이전 수집일이 없음 (수집일 2일 이상 필요)")
    else:
        c = rp.change
        sign = "+" if c.diff > 0 else ""
        print(f"가격 변화: {c.prev_day} {c.prev_price:,}원 → {rp.series[-1].day} {c.today_price:,}원 "
              f"({sign}{c.diff:,}원, {sign}{c.pct:.1f}%)")

    print("=" * 60)
    print(f"🏆 Top {config.TOP_N}  (가격 낮은 순 → 숙박 짧은 순 → 출발일 빠른 순)")
    for i, t in enumerate(rp.top):
        print(f"  {MEDALS[i] if i < len(MEDALS) else i + 1}  {t.period_label}  {t.nights}박  "
              f"{t.price:>9,}원  {t.weekend_label}")

    print("-" * 60)
    print("숙박일수별 최저가")
    for n in range(rp.min_nights, rp.max_nights + 1):
        t = rp.best_by_nights.get(n)
        if t is None:
            print(f"  {n}박 → 해당 날짜 가격 확인 불가")
        else:
            print(f"  {n}박 → {t.price:>9,}원   {t.period_label}  {t.weekend_label}")
    print("=" * 60)

    if show_all:
        for nights in range(rp.min_nights, rp.max_nights + 1):
            items = rp.groups.get(nights, [])
            weekend = sum(1 for t in items if t.includes_weekend)
            print(f"\n■ {nights}박  - {len(items)}개 조합 (주말 포함 {weekend}, 평일 {len(items) - weekend})")
            if not items:
                print("  해당 날짜 가격 확인 불가")
            for t in items:
                print(f"  {t.period_label}  {t.price:>9,}원  {t.weekend_label}")
        print()


def main():
    parser = argparse.ArgumentParser(description="Flight Price Tracker - analysis")
    parser.add_argument("--route", help="한 노선만 (예: ICN-KIX)")
    parser.add_argument("--all", action="store_true", help="모든 조합 목록도 출력")
    args = parser.parse_args()

    routes = load_routes()
    if args.route:
        routes = [r for r in routes if r.slug == args.route.upper()]
        if not routes:
            print(f"config.ROUTES 에 없는 노선: {args.route}")
            return 1

    shown = 0
    for route in routes:
        rp = build_report(route, config)
        if rp is None:
            print("=" * 60)
            print(f"✈️  {route.label}   {route.month_label}")
            print(f"데이터 없음: {route.history_file} 가 없거나 분석할 조합이 없습니다. (`python main.py` 로 수집)")
            print("=" * 60)
            continue
        print_report(rp, args.all)
        shown += 1
    return 0 if shown else 1


if __name__ == "__main__":
    sys.exit(main())
