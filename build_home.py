"""
build_home.py - 첫 화면(오늘의 추천 / 목적지별 최저가 / 수집 상태) HTML 한 장을 만듭니다.

    python build_home.py --out <저장할 파일>

저장된 CSV / JSON 만 읽고, --out 으로 지정한 파일 하나만 씁니다. (data/ 는 바꾸지 않음)
"""
import argparse
import json
import os
import sys
from datetime import datetime

import config
from analyzer import offers

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(HERE, "dashboard_home")


def _read(name):
    with open(os.path.join(ASSETS, name), encoding="utf-8") as f:
        return f.read()


def render(data) -> str:
    """데이터 묶음 -> 스스로 동작하는 HTML 문서 하나."""
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    return _read("template.html").replace("__VIEW_JS__", _read("view.js")).replace("__DATA__", payload)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Flight Price Tracker - 첫 화면 생성")
    parser.add_argument("--out", required=True, help="만들 HTML 파일 경로")
    args = parser.parse_args(argv)

    docs_dir = os.path.dirname(os.path.abspath(args.out))
    data = offers.build_data(config, datetime.now(offers.KST), docs_dir=docs_dir)
    page = render(data)
    os.makedirs(docs_dir, exist_ok=True)
    with open(args.out, "w", encoding="utf-8", newline="\n") as f:
        f.write(page)
    print(f"첫 화면: {args.out} ({len(page.encode('utf-8')):,} bytes) 일정 {len(data['offers']):,}개, "
          f"목적지 {len(data['airports'])}곳, 건너뛴 행 {data['skipped_rows']}")
    return 0 if data["offers"] else 1


if __name__ == "__main__":
    sys.exit(main())
