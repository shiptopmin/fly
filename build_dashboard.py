"""
build_dashboard.py - 분석 결과로 HTML Dashboard 생성 (Phase 4)

실행:
    python build_dashboard.py     -> docs/index.html 생성 (GitHub Pages 용)

사이트에 접속하지 않고 data/flights_raw.csv 만 읽습니다.
외부 라이브러리/JS 없이 순수 HTML+CSS 만 사용합니다 (가독성 우선).
"""
import html
import os
import sys
from datetime import datetime, timedelta, timezone

import config
from analyzer.report import build_report
from analyzer import charts, deals, stats
from analyzer.history import RouteHistory, thresholds_from_config
from routes import load_routes

KST = timezone(timedelta(hours=9))
MEDALS = ["🥇", "🥈", "🥉", "4", "5"]


def esc(s):
    return html.escape(str(s))


def won(n):
    return f"{n:,}원"


def fmt_kst(iso):
    """'2026-09-18T15:56:12+09:00' -> '2026-09-18 15:56 KST'"""
    try:
        return datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M KST")
    except ValueError:
        return iso


def stat_card(label, value, note=""):
    return (f'<div class="card"><div class="label">{esc(label)}</div>'
            f'<div class="value">{esc(value)}</div>'
            f'<div class="note">{esc(note)}</div></div>')


def window_cards(w: stats.WindowStats, min_days):
    """최근 30일/90일 카드 두 장(평균, 최저). 데이터 부족이면 값 대신 문구."""
    if w.days == 0:
        return stat_card(f"{w.label} 평균", "데이터 없음") + stat_card(f"{w.label} 최저", "데이터 없음")
    if not w.enough:
        note = f"{w.days}일치 (최소 {min_days}일 필요)"
        return (stat_card(f"{w.label} 평균", "데이터 부족", f"참고 {w.avg:,.0f}원 · {note}")
                + stat_card(f"{w.label} 최저", "데이터 부족", f"참고 {won(w.low)} · {note}"))
    return (stat_card(f"{w.label} 평균", f"{w.avg:,.0f}원", f"{w.days}일치")
            + stat_card(f"{w.label} 최저", won(w.low), f"최고 {won(w.high)}"))


def trip_row(rank, t):
    tag = f'<span class="ext">Google 표시: {esc(t.source_tag)}</span>' if t.source_tag else ""
    wk = '<span class="badge wk">주말 포함</span>' if t.includes_weekend else '<span class="badge wd">평일</span>'
    return (f"<tr><td class='rank'>{rank}</td><td>{esc(t.period_label)}</td>"
            f"<td>{t.nights}박</td><td class='price'>{won(t.price)}</td><td>{wk} {tag}</td></tr>")


LABEL_STYLE = {"DEAL": ("deal", "🟢 좋은 가격"), "WATCH": ("watch", "🟡 지켜볼 만함"),
               "NORMAL": ("normal", "⚪ 보통"), "HOLD": ("hold", "⚫ 판단 보류")}


def verdict_block(rp, hist):
    """최저가 조합 하나에 대한 판정을 설명과 함께 보여줍니다. 가격을 보정하지 않습니다."""
    if not rp.top:
        return ""
    t = rp.top[0]
    v = deals.judge(t.price, hist, nights=t.nights, dep=t.departure_date,
                    ret=t.return_date, rules=config.DEAL_RULES)
    cls, title = LABEL_STYLE.get(v.label, ("normal", v.label))
    reasons = "".join(f"<li>{esc(r)}</li>" for r in v.reasons)
    return (f'<div class="verdict {cls}">'
            f'<div class="vhead">{esc(title)} <span class="muted">'
            f'{esc(t.period_label)} · {t.nights}박 · {won(t.price)}</span></div>'
            f'<ul class="vreasons">{reasons}</ul></div>')


def history_block(hist, rp):
    """가격 이력 그래프. 노선 전체 흐름 + 숙박일수별."""
    route_s = hist.route()
    main = charts.line_chart(route_s.points, caption="그날 전체 조합 중 최저가")
    small = ""
    for n in range(rp.min_nights, rp.max_nights + 1):
        s = hist.nights(n)
        if not s.points:
            continue
        small += (f'<div class="chart-cell"><div class="chart-title">{n}박</div>'
                  + charts.line_chart(s.points, width=320, height=120) + "</div>")
    if small:
        small = (f'<details><summary>숙박일수별 가격 이력</summary>'
                 f'<div class="chart-grid">{small}</div></details>')
    return main + small


def render(rp, hist, generated_at):
    change_html = "비교할 이전 수집일 없음 (수집일 2일 이상 필요)"
    if rp.change is not None:
        c = rp.change
        sign = "+" if c.diff > 0 else ""
        cls = "up" if c.diff > 0 else ("down" if c.diff < 0 else "")
        change_html = (f"{c.prev_day} {won(c.prev_price)} → {rp.series[-1].day} {won(c.today_price)} "
                       f"<span class='{cls}'>({sign}{c.diff:,}원, {sign}{c.pct:.1f}%)</span>")

    top_rows = "".join(trip_row(MEDALS[i] if i < len(MEDALS) else i + 1, t) for i, t in enumerate(rp.top))

    nights_rows = ""
    for n in range(rp.min_nights, rp.max_nights + 1):
        t = rp.best_by_nights.get(n)
        if t is None:
            nights_rows += f"<tr><td>{n}박</td><td colspan='3' class='muted'>해당 날짜 가격 확인 불가</td></tr>"
        else:
            nights_rows += trip_row(f"{n}박", t).replace(f"<td>{t.nights}박</td>", "", 1)

    # 전체 조합 (접어두기)
    all_sections = ""
    for n in range(rp.min_nights, rp.max_nights + 1):
        items = rp.groups.get(n, [])
        rows = "".join(
            f"<tr><td>{esc(t.period_label)}</td><td class='price'>{won(t.price)}</td>"
            f"<td>{'주말 포함' if t.includes_weekend else '평일'}</td></tr>" for t in items)
        all_sections += (f"<details><summary>{n}박 - {len(items)}개 조합</summary>"
                         f"<table><thead><tr><th>기간</th><th>왕복 총액</th><th>구분</th></tr></thead>"
                         f"<tbody>{rows or '<tr><td colspan=3 class=muted>해당 날짜 가격 확인 불가</td></tr>'}</tbody></table></details>")

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Flight Price Tracker</title>
<style>
  :root {{ --bg:#f6f7f9; --card:#fff; --text:#1f2937; --muted:#6b7280; --line:#e5e7eb; --accent:#2563eb; --good:#15803d; --bad:#b91c1c; }}
  @media (prefers-color-scheme: dark) {{ :root {{ --bg:#111827; --card:#1f2937; --text:#f3f4f6; --muted:#9ca3af; --line:#374151; --accent:#60a5fa; --good:#4ade80; --bad:#f87171; }} }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; padding:16px; background:var(--bg); color:var(--text); font-family:-apple-system,"Segoe UI","Malgun Gothic","Apple SD Gothic Neo",sans-serif; line-height:1.5; }}
  main {{ max-width:900px; margin:0 auto; }}
  h1 {{ font-size:1.6rem; margin:0 0 4px; }}
  h2 {{ font-size:1.15rem; margin:28px 0 10px; border-bottom:1px solid var(--line); padding-bottom:6px; }}
  .route {{ font-size:1.3rem; font-weight:600; }}
  .muted {{ color:var(--muted); }}
  .status {{ margin:10px 0 0; padding:10px 14px; background:var(--card); border-left:4px solid var(--accent); border-radius:6px; }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:12px; margin-top:12px; }}
  .card {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px; }}
  .card .label {{ font-size:.85rem; color:var(--muted); }}
  .card .value {{ font-size:1.45rem; font-weight:700; margin-top:2px; }}
  .card .note {{ font-size:.8rem; color:var(--muted); min-height:1.2em; }}
  .card.hero .value {{ color:var(--accent); font-size:1.8rem; }}
  table {{ width:100%; border-collapse:collapse; background:var(--card); border:1px solid var(--line); border-radius:10px; overflow:hidden; }}
  th, td {{ padding:9px 10px; text-align:left; border-bottom:1px solid var(--line); font-size:.95rem; }}
  th {{ color:var(--muted); font-weight:600; font-size:.85rem; }}
  tr:last-child td {{ border-bottom:none; }}
  td.price {{ font-weight:700; white-space:nowrap; }}
  td.rank {{ width:2.5em; text-align:center; }}
  .badge {{ display:inline-block; padding:1px 8px; border-radius:999px; font-size:.78rem; }}
  .badge.wk {{ background:#fee2e2; color:#991b1b; }} .badge.wd {{ background:#dcfce7; color:#166534; }}
  .ext {{ font-size:.75rem; color:var(--muted); margin-left:6px; }}
  .up {{ color:var(--bad); }} .down {{ color:var(--good); }}
  details {{ margin:8px 0; }} summary {{ cursor:pointer; font-weight:600; }}
  footer {{ margin-top:32px; font-size:.85rem; color:var(--muted); border-top:1px solid var(--line); padding-top:12px; }}
  footer dl {{ display:grid; grid-template-columns:max-content 1fr; gap:4px 12px; margin:0; }}
  footer dt {{ font-weight:600; }} footer dd {{ margin:0; }}
  .verdict {{ background:var(--card); border:1px solid var(--line); border-left-width:4px; border-radius:8px; padding:12px 14px; }}
  .verdict.deal {{ border-left-color:var(--good); }}
  .verdict.watch {{ border-left-color:#ca8a04; }}
  .verdict.normal {{ border-left-color:var(--muted); }}
  .verdict.hold {{ border-left-color:var(--line); }}
  .vhead {{ font-size:1.05rem; font-weight:700; }}
  .vreasons {{ margin:8px 0 0; padding-left:18px; font-size:.9rem; }}
  .vreasons li {{ margin:2px 0; }}
  .chart {{ display:block; max-width:100%; }}
  .chart-caption {{ font-size:.8rem; margin-top:2px; }}
  .chart-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); gap:10px; margin-top:8px; }}
  .chart-cell {{ background:var(--card); border:1px solid var(--line); border-radius:8px; padding:8px; }}
  .chart-title {{ font-size:.85rem; font-weight:600; color:var(--muted); }}
</style>
</head>
<body>
<main>
  <div class="muted"><a href="../index.html">← 전체 노선</a></div>
  <h1>✈️ Flight Price Tracker</h1>
  <div class="route">{esc(rp.origin)} → {esc(rp.destination)}</div>
  <div class="muted">{rp.year}년 {rp.month}월 · 숙박 {rp.min_nights}~{rp.max_nights}박 · 왕복 총액(성인 1명, 세금 포함)</div>
  <div class="muted">마지막 업데이트 {esc(fmt_kst(rp.latest_collected_at))}</div>
  <div class="status">상태: {esc(rp.status)}</div>

  <div class="grid">
    {stat_card("현재 최저가", won(rp.current_min), "최신 수집분 전체 조합 중 최저").replace('class="card"', 'class="card hero"')}
    {stat_card("수집 이후 최저가", won(rp.s_all.low), f"{rp.first_day} 부터 {rp.s_all.days}일치")}
    {window_cards(rp.s30, config.MIN_DAYS_FOR_STATS)}
    {window_cards(rp.s90, config.MIN_DAYS_FOR_STATS)}
  </div>
  <p class="muted">가격 변화: {change_html}</p>

  <h2>가격 판정</h2>
  {verdict_block(rp, hist)}
  <p class="muted" style="font-size:.8rem">이력이 부족하면 판단을 보류합니다. 수집되지 않은 기간의 가격은 추정하지 않습니다.</p>

  <h2>가격 이력</h2>
  {history_block(hist, rp)}

  <h2>🏆 Top {config.TOP_N}</h2>
  <table><thead><tr><th></th><th>기간</th><th>숙박</th><th>왕복 총액</th><th>구분</th></tr></thead>
  <tbody>{top_rows}</tbody></table>
  <p class="muted" style="font-size:.8rem">정렬: 가격 낮은 순 → 숙박일수 짧은 순 → 출발일 빠른 순. "Google 표시"는 Google Flights 가 자체 기준으로 붙인 저가/최저가 표시이며, 이 시스템의 수집 이력과는 별개의 외부 기준입니다.</p>

  <h2>숙박일수별 최저가</h2>
  <table><thead><tr><th>숙박</th><th>기간</th><th>왕복 총액</th><th>구분</th></tr></thead>
  <tbody>{nights_rows}</tbody></table>

  <h2>전체 조합</h2>
  {all_sections}

  <footer>
    <dl>
      <dt>데이터 출처</dt><dd>Google Flights 날짜 표(Date Grid) · 출발일/귀국일 조합의 왕복 최저 총액 (성인 1명, 필수 세금·수수료 포함)</dd>
      <dt>데이터 수집 건수</dt><dd>최신 수집분 {rp.latest_rows}건 (분석 조합 {len(rp.trips)}개) · 누적 {rp.total_rows}행 · 수집일 {rp.days_collected}일</dd>
      <dt>마지막 수집 시간</dt><dd>{esc(fmt_kst(rp.latest_collected_at))}</dd>
      <dt>페이지 생성 시간</dt><dd>{esc(generated_at)}</dd>
      <dt>안내</dt><dd>"수집 이후 최저가"는 이 시스템이 수집을 시작한 이후의 범위에서만 최저입니다. 수집되지 않은 날짜/기간의 가격은 추정하지 않습니다.</dd>
    </dl>
  </footer>
</main>
</body>
</html>
"""


def route_card(route, rp):
    """홈 화면의 노선 카드 하나. rp 가 None 이면 아직 수집 전."""
    href = f"routes/{route.slug}.html"
    if rp is None:
        return (f'<a class="rcard" href="{href}"><div class="rtitle">{esc(route.label)}</div>'
                f'<div class="muted">{esc(route.month_label)} · 숙박 {route.min_nights}~{route.max_nights}박</div>'
                f'<div class="muted">아직 수집된 데이터 없음</div></a>')
    s30 = rp.s30
    low30 = won(s30.low) if s30.enough else f"데이터 부족 ({s30.days}일치)"
    avg30 = f"{s30.avg:,.0f}원" if s30.enough else f"데이터 부족 ({s30.days}일치)"
    return (f'<a class="rcard" href="{href}">'
            f'<div class="rtitle">{esc(route.label)}</div>'
            f'<div class="muted">{esc(route.month_label)} · 숙박 {rp.min_nights}~{rp.max_nights}박 · '
            f'마지막 수집 {esc(fmt_kst(rp.latest_collected_at))}</div>'
            f'<dl class="rstats">'
            f'<dt>현재 최저가</dt><dd class="big">{won(rp.current_min)}</dd>'
            f'<dt>최근 30일 최저</dt><dd>{esc(low30)}</dd>'
            f'<dt>최근 30일 평균</dt><dd>{esc(avg30)}</dd>'
            f'<dt>수집 이후 최저</dt><dd>{won(rp.s_all.low)} <span class="muted">({rp.s_all.days}일치)</span></dd>'
            f'</dl><div class="status-line">{esc(rp.status)}</div></a>')


def render_home(items, generated_at):
    """items = [(route, report 또는 None), ...]"""
    cards = "".join(route_card(route, rp) for route, rp in items)
    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Flight Price Tracker</title>
<style>
  :root {{ --bg:#f6f7f9; --card:#fff; --text:#1f2937; --muted:#6b7280; --line:#e5e7eb; --accent:#2563eb; }}
  @media (prefers-color-scheme: dark) {{ :root {{ --bg:#111827; --card:#1f2937; --text:#f3f4f6; --muted:#9ca3af; --line:#374151; --accent:#60a5fa; }} }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; padding:16px; background:var(--bg); color:var(--text); font-family:-apple-system,"Segoe UI","Malgun Gothic","Apple SD Gothic Neo",sans-serif; line-height:1.5; }}
  main {{ max-width:900px; margin:0 auto; }}
  h1 {{ font-size:1.6rem; margin:0 0 4px; }}
  h2 {{ font-size:1.15rem; margin:24px 0 10px; border-bottom:1px solid var(--line); padding-bottom:6px; }}
  .muted {{ color:var(--muted); font-size:.9rem; }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); gap:12px; }}
  .rcard {{ display:block; background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px; color:inherit; text-decoration:none; }}
  .rcard:hover {{ border-color:var(--accent); }}
  .rtitle {{ font-size:1.2rem; font-weight:700; }}
  .rstats {{ display:grid; grid-template-columns:max-content 1fr; gap:2px 12px; margin:10px 0 6px; }}
  .rstats dt {{ color:var(--muted); font-size:.85rem; }} .rstats dd {{ margin:0; font-weight:600; }}
  .rstats dd.big {{ color:var(--accent); font-size:1.3rem; }}
  .status-line {{ font-size:.85rem; }}
  footer {{ margin-top:32px; font-size:.85rem; color:var(--muted); border-top:1px solid var(--line); padding-top:12px; }}
</style>
</head>
<body>
<main>
  <h1>✈️ Flight Price Tracker</h1>
  <div class="muted">왕복 총액(성인 1명, 세금 포함) · 매일 자동 수집 · 노선을 누르면 상세 보기</div>
  <h2>Tracked Routes ({len(items)})</h2>
  <div class="grid">{cards}</div>
  <footer>
    데이터 출처: Google Flights 날짜 표(Date Grid) · 페이지 생성 {esc(generated_at)}<br>
    "수집 이후 최저"는 이 시스템이 수집을 시작한 이후의 범위에서만 최저입니다. 수집되지 않은 기간의 가격은 추정하지 않습니다.
  </footer>
</main>
</body>
</html>
"""


def main():
    generated_at = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    routes = load_routes()
    items = []
    for route in routes:
        rp = build_report(route, config)   # 노선별 이력 파일만 읽음 (노선 간 혼합 없음)
        items.append((route, rp))
        if rp is None:
            print(f"{route.slug}: 데이터 없음 ({route.history_file}) - 상세 페이지 생략")
            continue
        os.makedirs(os.path.dirname(route.page_file), exist_ok=True)
        hist = RouteHistory.from_file(route.history_file, route.origin, route.destination,
                                      rp.series[-1].day, thresholds_from_config(config))
        page = render(rp, hist, generated_at)
        with open(route.page_file, "w", encoding="utf-8") as f:
            f.write(page)
        print(f"{route.slug}: {route.page_file} ({len(page):,} bytes) 현재 최저가 {rp.current_min:,}원 / 조합 {len(rp.trips)}개")

    os.makedirs(os.path.dirname(config.DASHBOARD_FILE) or ".", exist_ok=True)
    home = render_home(items, generated_at)
    with open(config.DASHBOARD_FILE, "w", encoding="utf-8") as f:
        f.write(home)
    print(f"홈 생성: {config.DASHBOARD_FILE} ({len(home):,} bytes, 노선 {len(items)}개)")
    return 0 if any(rp is not None for _, rp in items) else 1


if __name__ == "__main__":
    sys.exit(main())
