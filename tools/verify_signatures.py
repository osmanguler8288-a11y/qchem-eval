"""按官方原文核对 signatures.yaml。

用途：保证「失败指纹照抄官方目录、一字不改」这个说法有证据，而不是口头保证。
每次改 signatures.yaml 后跑一遍：

    python tools/verify_signatures.py

未命中即说明抄错了或原文改版了，两种情况都必须人工确认。
"""

from __future__ import annotations

import html
import re
import sys
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / ".cache" / "spydur.html"


def page_text(url: str) -> str:
    if not CACHE.exists():
        CACHE.parent.mkdir(exist_ok=True)
        CACHE.write_bytes(urllib.request.urlopen(url, timeout=60).read())
    raw = CACHE.read_text(encoding="utf-8", errors="replace")
    txt = re.sub(r"<[^>]+>", "", raw)
    txt = html.unescape(txt)
    # 源码里带 Markdown 转义（S\*\*2、BiAq7_3+\_C2.chk），去掉反斜杠再比
    txt = txt.replace("\\", "")
    return re.sub(r"[ \t]+", " ", txt)


def main() -> int:
    sig = yaml.safe_load((ROOT / "qchem_eval" / "rules" / "signatures.yaml").read_text(encoding="utf-8"))
    txt = page_text(sig["meta"]["url"])

    match_ok = match_bad = 0
    line_ok = line_bad = 0
    for s in sig["signatures"]:
        for m in s.get("match", []):
            if m in txt:
                match_ok += 1
            else:
                match_bad += 1
                print(f"  [match 未命中] {s['id']}: {m!r}")
        for line in (s.get("cause_text") or "").splitlines():
            line = line.strip()
            if not line or line == "---":
                continue
            if re.sub(r"\s+", " ", line) in txt:
                line_ok += 1
            else:
                line_bad += 1
                print(f"  [cause_text 未命中] {s['id']}: {line!r}")

    n_official = sum(1 for s in sig["signatures"] if not s.get("derived"))
    print(f"\n签名总数 {len(sig['signatures'])}（官方 {n_official} + 派生 {len(sig['signatures']) - n_official}）")
    print(f"match 串     {match_ok} 命中 / {match_bad} 未命中")
    print(f"cause_text 行 {line_ok} 命中 / {line_bad} 未命中")
    return 1 if (match_bad or line_bad) else 0


if __name__ == "__main__":
    sys.exit(main())
