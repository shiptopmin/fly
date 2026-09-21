"""
analyzer/charts.py - 가격 이력 그래프 (인라인 SVG, 외부 라이브러리 없음)

DayPoint 목록(analyzer/history.py, analyzer/stats.py)을 받아 선 그래프 SVG 문자열을 만듭니다.
- 자바스크립트나 차트 라이브러리를 쓰지 않습니다. GitHub Pages 에서 그대로 보입니다.
- 색은 대시보드 CSS 변수(--accent, --muted, --line)를 쓰므로 밝은/어두운 테마 모두 맞습니다.
- 점이 없으면 안내 문구, 하나면 점 하나를 그립니다. 없는 기간을 지어내지 않습니다.
"""
import html

__all__ = ["line_chart"]


def _esc(s):
    return html.escape(str(s))


def _won(n):
    return f"{n:,}원"


def line_chart(points, width=640, height=170, caption="", empty_text="아직 가격 이력이 없습니다."):
    """[DayPoint] -> SVG 문자열.

    DayPoint 는 .day(date) 와 .min_price(int) 를 가지면 됩니다.
    """
    if not points:
        return f'<p class="muted">{_esc(empty_text)}</p>'

    pad_l, pad_r, pad_t, pad_b = 68, 14, 14, 24
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    ys = [p.min_price for p in points]
    lo, hi = min(ys), max(ys)
    span = hi - lo
    n = len(points)

    def x_of(i):
        return pad_l if n == 1 else pad_l + plot_w * i / (n - 1)

    def y_of(v):
        if span == 0:
            return pad_t + plot_h / 2
        return pad_t + plot_h * (hi - v) / span

    parts = [
        f'<svg class="chart" viewBox="0 0 {width} {height}" width="100%" height="{height}" '
        f'role="img" aria-label="{_esc(caption or "가격 이력")}" xmlns="http://www.w3.org/2000/svg">'
    ]

    # 가로 기준선 두 개 (최고/최저). 값이 하나뿐이면 가운데 한 줄만.
    if span == 0:
        rules = [(y_of(lo), lo)]
    else:
        rules = [(y_of(hi), hi), (y_of(lo), lo)]
    for y, val in rules:
        parts.append(f'<line x1="{pad_l}" x2="{width - pad_r}" y1="{y:.1f}" y2="{y:.1f}" '
                     f'stroke="var(--line)" stroke-width="1"/>')
        parts.append(f'<text x="{pad_l - 8}" y="{y + 4:.1f}" text-anchor="end" '
                     f'font-size="11" fill="var(--muted)">{_esc(_won(val))}</text>')

    # 선과 점
    coords = [(x_of(i), y_of(v)) for i, v in enumerate(ys)]
    if n > 1:
        pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
        parts.append(f'<polyline points="{pts}" fill="none" stroke="var(--accent)" '
                     f'stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>')
    for (x, y), p in zip(coords, points):
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.5" fill="var(--accent)">'
                     f'<title>{_esc(p.day)} {_esc(_won(p.min_price))}</title></circle>')

    # 가로축: 첫 날짜와 마지막 날짜만 (점이 촘촘해도 읽히도록)
    first, last = points[0].day, points[-1].day
    parts.append(f'<text x="{pad_l}" y="{height - 6}" font-size="11" fill="var(--muted)">{_esc(first)}</text>')
    if n > 1:
        parts.append(f'<text x="{width - pad_r}" y="{height - 6}" text-anchor="end" '
                     f'font-size="11" fill="var(--muted)">{_esc(last)}</text>')
    parts.append("</svg>")

    svg = "".join(parts)
    if caption:
        svg += f'<div class="muted chart-caption">{_esc(caption)}</div>'
    return svg
