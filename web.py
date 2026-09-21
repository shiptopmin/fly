"""
web.py - 로컬 웹 검색 폼 (Phase 8-1)

실행:
    python web.py
    -> 브라우저에서 http://127.0.0.1:5000 열기

- 이 PC 안에서만 접속됩니다 (127.0.0.1). 서버를 켠 동안만 동작합니다.
- 검색 로직은 search.py 의 run_search() 를 그대로 사용합니다 (중복 구현 없음).
- 검색은 한 번에 하나만 실행됩니다 (동시에 여러 브라우저 조작을 하지 않기 위해).
- 트래커(main.py, data/history/, docs/)는 읽지도 쓰지도 않습니다.
"""
import threading
import traceback

from flask import Flask, render_template_string, request

import config
from destinations import resolve
from search import build_histories, judge_trip, make_query, plan_summary, run_search, save_outcome

app = Flask(__name__)
search_lock = threading.Lock()   # 검색 동시 실행 방지

PAGE = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Flight Finder</title>
<style>
  :root { --bg:#f6f7f9; --card:#fff; --text:#1f2937; --muted:#6b7280; --line:#e5e7eb; --accent:#2563eb; --bad:#b91c1c; }
  @media (prefers-color-scheme: dark) { :root { --bg:#111827; --card:#1f2937; --text:#f3f4f6; --muted:#9ca3af; --line:#374151; --accent:#60a5fa; --bad:#f87171; } }
  * { box-sizing:border-box; }
  body { margin:0; padding:16px; background:var(--bg); color:var(--text); font-family:-apple-system,"Segoe UI","Malgun Gothic","Apple SD Gothic Neo",sans-serif; line-height:1.5; }
  main { max-width:900px; margin:0 auto; }
  h1 { font-size:1.5rem; margin:0 0 12px; }
  h2 { font-size:1.1rem; margin:22px 0 8px; border-bottom:1px solid var(--line); padding-bottom:6px; }
  form { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px; display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:10px 14px; }
  label { display:flex; flex-direction:column; font-size:.85rem; color:var(--muted); gap:3px; }
  input[type=text], input[type=date], input[type=number] { padding:8px; border:1px solid var(--line); border-radius:6px; background:var(--bg); color:var(--text); font-size:1rem; }
  .check { flex-direction:row; align-items:center; gap:8px; color:var(--text); font-size:1rem; align-self:end; }
  button { grid-column:1/-1; padding:10px; border:0; border-radius:8px; background:var(--accent); color:#fff; font-size:1rem; cursor:pointer; }
  .hint { font-size:.8rem; color:var(--muted); grid-column:1/-1; }
  .error { color:var(--bad); background:var(--card); border:1px solid var(--bad); border-radius:8px; padding:10px; margin-top:12px; }
  .muted { color:var(--muted); }
  .best { font-size:1.4rem; font-weight:700; color:var(--accent); }
  table { width:100%; border-collapse:collapse; background:var(--card); border:1px solid var(--line); border-radius:10px; overflow:hidden; }
  th, td { padding:8px 10px; text-align:left; border-bottom:1px solid var(--line); font-size:.95rem; }
  th { color:var(--muted); font-size:.85rem; } tr:last-child td { border-bottom:none; }
  td.price { font-weight:700; white-space:nowrap; }
  .badge { display:inline-block; padding:1px 8px; border-radius:999px; font-size:.78rem; }
  .wk { background:#fee2e2; color:#991b1b; } .wd { background:#dcfce7; color:#166534; }
  .vlabel { display:inline-block; padding:1px 7px; border-radius:6px; font-size:.75rem; font-weight:700; }
  .vlabel.deal { background:#dcfce7; color:#166534; }
  .vlabel.watch { background:#fef9c3; color:#854d0e; }
  .vlabel.normal, .vlabel.hold { background:var(--bg); color:var(--muted); border:1px solid var(--line); }
  .ext { font-size:.75rem; color:var(--muted); margin-left:6px; }
  details { margin-top:8px; } summary { cursor:pointer; }
  #loading { display:none; margin-top:12px; padding:10px; background:var(--card); border:1px solid var(--line); border-radius:8px; }
</style>
</head>
<body>
<main>
  <h1>✈️ Flight Finder <span class="muted" style="font-size:.9rem">(로컬 검색)</span></h1>
  <form method="post" action="/search" onsubmit="document.getElementById('loading').style.display='block'">
    <label>출발 공항 <input type="text" name="origin" value="{{ f.origin }}" placeholder="ICN" required></label>
    <label>목적지 <input type="text" name="destination" value="{{ f.destination }}" placeholder="KIX / 도쿄 / 일본 / KIX,FUK" required></label>
    <label>출발 가능 시작일 <input type="date" name="depart_from" value="{{ f.depart_from }}" required></label>
    <label>출발 가능 종료일 <input type="date" name="depart_to" value="{{ f.depart_to }}"></label>
    <label>숙박일수 (최소) <input type="number" name="nights_min" min="1" value="{{ f.nights_min }}" required></label>
    <label>숙박일수 (최대, 비우면 최소와 동일) <input type="number" name="nights_max" min="1" value="{{ f.nights_max }}"></label>
    <label>귀국 가능 시작일 (선택) <input type="date" name="return_from" value="{{ f.return_from }}"></label>
    <label>귀국 가능 종료일 (선택) <input type="date" name="return_to" value="{{ f.return_to }}"></label>
    <label class="check"><input type="checkbox" name="nonstop" {% if f.nonstop %}checked{% endif %}> 직항만</label>
    <label class="check"><input type="checkbox" name="save" {% if f.save %}checked{% endif %}> 결과를 data/searches/ 에 저장</label>
    <div class="hint">출발 종료일을 비우면 시작일 하루만 검색합니다. 목적지는 공항 코드·도시명·국가명·쉼표 목록 모두 가능 (data/destinations.json 기준).
      가격 = Google Flights 날짜 표의 왕복 총액(성인 1명, 세금 포함). 목적지 1곳당 페이지 로드 1~9회, 1회 약 10초.</div>
    <button type="submit">검색</button>
  </form>
  <div id="loading">🔎 검색 중입니다… 목적지 수와 기간에 따라 1~5분 걸립니다. 이 창을 닫지 마세요.</div>

  {% if error %}<div class="error">{{ error }}</div>{% endif %}

  {% if o %}
  <h2>검색 결과</h2>
  <div class="muted">{{ cond }} · 목적지 {{ o.airports|length }}곳 · {{ o.trips|length }}건 확인 · 페이지 로드 {{ o.page_loads }}회 · {{ o.elapsed_sec }}초
    {% if o.blocked %}<br><b style="color:var(--bad)">차단/CAPTCHA 감지: 남은 목적지 검색을 중단했습니다 (우회하지 않음).</b>{% endif %}
  </div>
  <table style="margin-top:8px"><thead><tr><th>목적지</th><th>결과</th><th>가격 의미 검증</th></tr></thead><tbody>
  {% for a, r in o.collected %}
    <tr><td>{{ a.label }}</td>
        <td>{% if r.blocked %}차단{% else %}{{ r.records|length }}건 확인{% if r.missing_pairs %}, {{ r.missing_pairs|length }}건 확인 불가{% endif %}{% endif %}
            {% for e in r.errors %}<div class="muted">! {{ e }}</div>{% endfor %}</td>
        <td>{{ '성공' if r.semantics_verified else '실패/미확인' }}</td></tr>
  {% endfor %}
  {% for a in o.airports[o.collected|length:] %}
    <tr><td>{{ a.label }}</td><td class="muted">검색하지 않음 (앞에서 차단)</td><td>-</td></tr>
  {% endfor %}
  </tbody></table>

  {% if o.trips %}
    {% set best = o.ranked[0] %}
    <p>최저가: <span class="best">{{ "{:,}".format(best.price) }}원</span>
       {% if o.multi %}{{ best.destination_label }} {% endif %}{{ best.period_label }} {{ best.nights }}박
       <span class="badge {{ 'wk' if best.includes_weekend else 'wd' }}">{{ '주말 포함' if best.includes_weekend else '평일' }}</span></p>

    <h2>🏆 Top {{ top_n }}</h2>
    <table><thead><tr><th></th>{% if o.multi %}<th>목적지</th>{% endif %}<th>기간</th><th>숙박</th><th>왕복 총액</th><th>구분</th><th>이력 비교</th></tr></thead><tbody>
    {% for t in o.top(top_n) %}
      <tr><td>{{ medals[loop.index0] if loop.index0 < medals|length else loop.index }}</td>
          {% if o.multi %}<td>{{ t.destination_label }}</td>{% endif %}
          <td>{{ t.period_label }}</td><td>{{ t.nights }}박</td><td class="price">{{ "{:,}".format(t.price) }}원</td>
          <td><span class="badge {{ 'wk' if t.includes_weekend else 'wd' }}">{{ '주말 포함' if t.includes_weekend else '평일' }}</span>
              {% if t.source_tag %}<span class="ext">Google 표시: {{ t.source_tag }}</span>{% endif %}</td>
          <td>{% set v = verdict_of(t) %}{% if v %}<span class="vlabel {{ v.label|lower }}">{{ v.label }}</span>
              <span class="ext">{{ v.headline }}</span>{% else %}<span class="muted">-</span>{% endif %}</td></tr>
    {% endfor %}
    </tbody></table>
    <div class="muted" style="font-size:.8rem">정렬: 가격 낮은 순 → 숙박 짧은 순 → 출발일 빠른 순. "Google 표시"는 Google 자체 기준(외부 기준)입니다.</div>

    {% if o.multi %}
    <h2>목적지별 최저가</h2>
    <table><thead><tr><th>목적지</th><th>왕복 총액</th><th>기간</th><th>숙박</th><th>구분</th><th>이력 비교</th></tr></thead><tbody>
    {% for t in o.best_by_destination %}
      <tr><td>{{ t.destination_label }}</td><td class="price">{{ "{:,}".format(t.price) }}원</td><td>{{ t.period_label }}</td><td>{{ t.nights }}박</td>
          <td><span class="badge {{ 'wk' if t.includes_weekend else 'wd' }}">{{ '주말 포함' if t.includes_weekend else '평일' }}</span></td>
          <td>{% set v = verdict_of(t) %}{% if v %}<span class="vlabel {{ v.label|lower }}">{{ v.label }}</span>
              <span class="ext">{{ v.headline }}</span>{% else %}<span class="muted">-</span>{% endif %}</td></tr>
    {% endfor %}
    {% for a in o.failed_airports %}
      <tr><td>{{ a.label }}</td><td colspan="4" class="muted">해당 날짜 가격 확인 불가</td></tr>
    {% endfor %}
    </tbody></table>
    {% endif %}

    {% if o.base.min_nights != o.base.max_nights %}
    <h2>숙박일수별 최저가</h2>
    <table><thead><tr><th>숙박</th><th>왕복 총액</th>{% if o.multi %}<th>목적지</th>{% endif %}<th>기간</th><th>구분</th></tr></thead><tbody>
    {% for n, t in by_nights.items() %}
      <tr><td>{{ n }}박</td><td class="price">{{ "{:,}".format(t.price) }}원</td>{% if o.multi %}<td>{{ t.destination_label }}</td>{% endif %}
          <td>{{ t.period_label }}</td><td><span class="badge {{ 'wk' if t.includes_weekend else 'wd' }}">{{ '주말 포함' if t.includes_weekend else '평일' }}</span></td></tr>
    {% endfor %}
    </tbody></table>
    {% endif %}

    <details><summary>모든 조합 ({{ o.trips|length }}건, 가격순)</summary>
    <table><thead><tr>{% if o.multi %}<th>목적지</th>{% endif %}<th>기간</th><th>숙박</th><th>왕복 총액</th><th>구분</th></tr></thead><tbody>
    {% for t in o.ranked %}
      <tr>{% if o.multi %}<td>{{ t.destination_label }}</td>{% endif %}<td>{{ t.period_label }}</td><td>{{ t.nights }}박</td>
          <td class="price">{{ "{:,}".format(t.price) }}원</td><td>{{ '주말 포함' if t.includes_weekend else '평일' }}</td></tr>
    {% endfor %}
    </tbody></table></details>
    {% if saved %}<p class="muted">저장: {{ saved }}</p>{% endif %}
  {% else %}
    <p class="error">가격을 확인한 조합이 없습니다.</p>
  {% endif %}
  {% endif %}
</main>
</body>
</html>
"""

MEDALS = ["🥇", "🥈", "🥉", "4", "5"]
DEFAULT_FORM = {"origin": "ICN", "destination": "", "depart_from": "", "depart_to": "",
                "nights_min": "3", "nights_max": "", "return_from": "", "return_to": "",
                "nonstop": False, "save": False}


def form_values():
    f = dict(DEFAULT_FORM)
    for k in f:
        if k in ("nonstop", "save"):
            f[k] = request.form.get(k) == "on"
        else:
            f[k] = request.form.get(k, "").strip()
    return f


@app.get("/")
def index():
    return render_template_string(PAGE, f=DEFAULT_FORM, o=None, error=None)


@app.post("/search")
def search():
    f = form_values()
    try:
        depart = f["depart_from"] + (".." + f["depart_to"] if f["depart_to"] else "")
        nights = f["nights_min"] + (".." + f["nights_max"] if f["nights_max"] else "")
        ret = None
        if f["return_from"] or f["return_to"]:
            ret = (f["return_from"] or f["return_to"]) + ".." + (f["return_to"] or f["return_from"])
        airports = resolve(f["destination"])
        base = make_query(f["origin"], depart, nights, ret, f["nonstop"])
        if not airports:
            raise ValueError("목적지가 비어 있습니다.")
        pairs, _ = plan_summary(base, airports)
        if not pairs:
            raise ValueError("조건을 만족하는 날짜 조합이 없습니다.")
    except (ValueError, FileNotFoundError) as e:
        return render_template_string(PAGE, f=f, o=None, error=f"입력 오류: {e}")

    if not search_lock.acquire(blocking=False):
        return render_template_string(PAGE, f=f, o=None, error="다른 검색이 진행 중입니다. 끝난 뒤 다시 시도하세요.")
    try:
        outcome = run_search(base, airports, headless=True)
        saved = None
        if f["save"] and outcome.trips:
            saved, _ = save_outcome(outcome, f["destination"], nights)
    except Exception as e:  # 예상 못 한 오류도 화면에 보여줌 (숨기지 않음)
        traceback.print_exc()
        return render_template_string(PAGE, f=f, o=None, error=f"검색 실패: {e.__class__.__name__}: {e}")
    finally:
        search_lock.release()

    from analyzer import stats
    by_nights = stats.cheapest_by_nights(outcome.trips) if outcome.trips else {}
    cond = base.describe().replace(base.destination, f["destination"], 1)

    # 화면에 보이는 조합만 트래커 이력과 비교합니다 (이력이 없으면 HOLD 로 표시됨).
    histories = build_histories(base.origin, airports) if outcome.trips else {}
    verdicts = {}
    for t in list(outcome.top(config.TOP_N)) + list(outcome.best_by_destination):
        key = (t.destination, t.departure_date, t.return_date)
        if key not in verdicts:
            v = judge_trip(t, histories)
            if v is not None:
                verdicts[key] = v

    def verdict_of(t):
        return verdicts.get((t.destination, t.departure_date, t.return_date))

    return render_template_string(PAGE, f=f, o=outcome, error=None, cond=cond,
                                  top_n=config.TOP_N, medals=MEDALS, by_nights=by_nights,
                                  saved=saved, verdict_of=verdict_of)


if __name__ == "__main__":
    print("Flight Finder 로컬 웹: http://127.0.0.1:5000  (종료: Ctrl+C)")
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
