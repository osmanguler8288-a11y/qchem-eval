"""规则引擎的 risk-coverage 曲线。

架构方案 §6 的 Phase 0 出口标准之一是「规则引擎单独跑出第一条 risk-coverage
曲线」，§5 又要求「必设四组对照：纯规则 / 纯模型 / 规则+模型 / 随机选样」。
这个脚本负责前两个里的「纯规则」和「随机选样」——模型层还不存在。

## 曲线怎么读

横轴 coverage = 被系统「接受」的计算占多大比例（按置信度从高到低接受）。
纵轴 risk     = 被接受的那批里，实际有问题的比例（漏放率）。

把最有把握的排在前面，理想曲线应当从原点出发、随覆盖率上升才上翘。
如果曲线贴近随机基线（一条水平线 = 全体坏样本比例），说明排序能力为零。

## 标签从哪来（这段最重要，别跳过）

我们没有「这份计算在科学上是否合格」的客观标签 —— 那需要高精度参考值，
而这正是全项目最硬的缺口。这里用的是**合成样本的构造意图**当弱标签：

| 样本类别 | 标签 | 理由 |
|---|---|---|
| 规则变异正样本 | 坏 | 定点改坏了某个字段，构造上就该被检出 |
| 签名嫁接正样本 | 坏 | 把官方错误原文嫁接进去，构造上就该被检出 |
| 扰动负样本 | 好 | 真实日志 + 一个字符扰动，本不该命中任何签名 |
| 真实日志（REAL_OK 里那几份） | 好 | 都正常终止、结果可用（多数用 STO-3G，见下方警告） |
| 真实日志（不在 REAL_OK 里） | 坏 | 正常终止但结果不能用，如 ts_no_imag.log |

最后一行是必须单列的：`ts_no_imag.log` 的 route 声明了 `opt=ts`，Gaussian 却
收敛到极小点、频率全正，用户会以为自己拿到了过渡态。它「没崩」不等于「能用」。
把它按「好」标会让曲线在高分段凭空多出一个漏放，看着像排序失效，其实是标错了。

所以这条曲线上「好／坏」不按样本身份（负样本 / 正样本）划分，而按**内容**判定：
只要内容与某份已知不可用的真实日志相同，无论它先以 `realpos:*` 还是 `real:*`
的身份进来，都记为不可用。

所以这条曲线只能证明「**排序机制**是有效的」，不能证明「阈值对你们的体系合理」。
阈值合理性只能用真实失败日志校 —— 那是另一件事，公开渠道基本没有这种数据。

## 一个必须说清的口径问题

`realpos:C-08` 用的是 dvb_sp.out 原文，与负样本 `real:dvb_sp.out` 内容完全相同。
我们对「建议类规则在真实好计算上触发」的定义是：**这份计算本身是好的**，
只是方法选择有瑕疵。所以 realpos:* 一律按「好」标注并按内容去重，
否则同一份文件会同时被算成好和坏，曲线就没有意义了。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from qchem_eval import evaluate_file                      # noqa: E402
from qchem_eval.rules import load_ruleset                  # noqa: E402
from qchem_eval.rules.signatures import load_signatures    # noqa: E402
from qchem_eval.schema import Verdict                      # noqa: E402
from tools.synth_failures import DATA, REAL_BAD, build_samples  # noqa: E402

VERDICT_RANK = {
    Verdict.PASS: 3,
    Verdict.WARNING: 2,
    Verdict.UNKNOWN: 1,
    Verdict.FAIL: 0,
}


def _unusable_hashes() -> set[str]:
    """已知「正常终止但结果不可用」的真实日志，按内容哈希登记。

    为什么按哈希而不按文件名：`realpos:B-09` 和 `real:ts_no_imag.log` 是同一份
    文本的两种身份，去重时谁先到谁当代表。按文件名判会漏掉先到的那个。
    内容才是它不可用的原因，身份只是它进样本集的路径。
    """
    out = set()
    for p in DATA.glob("*"):
        if not p.is_file() or p.name not in REAL_BAD:
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        out.add(hashlib.sha1(text.encode("utf-8")).hexdigest()[:12])
    return out


def _severity_maps() -> tuple[dict[str, str], dict[str, str]]:
    """构造意图的严重性：expect 是规则 ID / 签名 ID，反查它声明了什么级别。

    这一步是整套评估的口径基础，必须说清楚：
      · 「这份计算不可用」不由人脸标注，而由**规则/签名作者声明的 severity**
        定义（critical = 结果不能用；major/minor = 可用但不可信）。
      · 所以这条曲线测的是**决策层的排序能力**（严重的能不能排到后面），
        不是检测能力（那由 eval_rules.py 的 P/R 负责）。
    """
    rule_sev = {r.id: r.severity.value for r in load_ruleset().rules}
    sig_sev = {s.id: s.severity.value for s in load_signatures()}
    return rule_sev, sig_sev


def collect() -> tuple[list[dict], list[tuple[str, list[str]]], list[tuple[str, list[str]]]]:
    """把带标签样本跑一遍。

    返回 (rows, dups, ambiguous)：
      dups      内容完全相同、已合并的样本
      ambiguous 标为负样本、却仍被**兄弟签名**命中的样本（见下）
    """
    samples = build_samples()
    rule_sev, sig_sev = _severity_maps()
    bad_content = _unusable_hashes()
    seen: dict[str, dict] = {}
    dups: list[tuple[str, str, bool, bool]] = []
    ambiguous: list[tuple[str, list[str]]] = []
    tmp = Path(tempfile.mkdtemp(prefix="qchem-rc-"))

    for s in samples:
        h = hashlib.sha1(s.text.encode("utf-8")).hexdigest()[:12]

        if s.kind == "negative":
            sev = "none"
        elif s.kind == "signature":
            sev = sig_sev.get(s.expect, "critical")
        else:
            sev = rule_sev.get(s.expect, "major")

        if h in seen:
            dups.append((s.name, seen[h]["name"], seen[h]["severity"], sev))
            continue

        p = tmp / f"{s.name.replace(':', '_')}.log"
        p.write_text(s.text, encoding="utf-8")
        r = evaluate_file(p)

        sig_hits = r.metrics.get("signature_hits") or []
        # 负样本只 forbid 自己那一条签名，所以「被兄弟签名接管」在 P/R 里不算误报。
        # 但它的"好"标签已经不干净了（文本本身确实像错误），留着会把曲线搅浑。
        # 单独报出来，而不是静默丢弃 —— 这同时暴露了签名库内部的文本重叠。
        if s.kind == "negative" and sig_hits:
            ambiguous.append((s.name, [x.get("id") if isinstance(x, dict) else str(x)
                                       for x in sig_hits]))
            continue

        seen[h] = {
            "name": s.name,
            "kind": s.kind,
            "severity": sev,
            # 「不可用」两个来源：
            #   ① 构造上引入了 critical 缺陷（崩溃、原子重叠、电子数矛盾…）
            #   ② 内容与某份已知不可用的真实日志相同（正常终止但答案是错的）
            # ② 不能由 severity 推出来 —— ts_no_imag.log 对应的 B-09 是 major，
            # 但它对用户而言就是没用。按 identity 判会把它算成「可用」。
            "unusable": sev == "critical" or h in bad_content,
            "verdict": r.verdict.value,
            "score": r.score,
            "coverage": r.metrics.get("coverage", 0.0),
            "abstain": r.abstain,
            "findings": [f.rule_id for f in r.findings],
        }

    return list(seen.values()), dups, ambiguous


def curve(rows: list[dict]) -> list[tuple[float, float]]:
    """按置信度降序接受，返回 [(coverage, risk), ...]。"""
    order = sorted(
        rows,
        key=lambda r: (VERDICT_RANK[Verdict(r["verdict"])], r["score"], r["coverage"]),
        reverse=True,
    )
    n = len(order)
    out = []
    cum_bad = 0
    for k, r in enumerate(order, start=1):
        cum_bad += 1 if r["unusable"] else 0
        out.append((k / n, cum_bad / k))
    return out


def aurc(pts: list[tuple[float, float]]) -> float:
    """曲线下面积（越低越好）。梯形积分。"""
    area = 0.0
    prev_c, prev_r = 0.0, pts[0][1] if pts else 0.0
    for c, r in pts:
        area += (c - prev_c) * (prev_r + r) / 2
        prev_c, prev_r = c, r
    return area


def at_coverage(pts: list[tuple[float, float]], target: float) -> float | None:
    """覆盖率恰为 target 时的 risk（取最接近且不超过 target 的点）。"""
    best = None
    for c, r in pts:
        if c <= target + 1e-9:
            best = r
    return best


def svg(pts, base_rate, path: Path, n_usable: int, n_unusable: int) -> None:
    """手写 SVG，零绘图依赖。风格与 README 里的表一致，浅色。"""
    W, H = 680, 420
    L, R, T, B = 72, 624, 56, 344          # 绘图区
    pw, ph = R - L, B - T

    def x(c):
        return L + c * pw

    def y(r):
        return B - (r / max(1e-9, ymax)) * ph

    ymax = max(base_rate * 1.35, max(r for _, r in pts) * 1.1, 0.1)
    ymax = min(ymax, 1.0)

    def ticks(lo, hi, step):
        v, out = lo, []
        while v <= hi + 1e-9:
            out.append(v)
            v += step
        return out

    yt = ticks(0, ymax, 0.1 if ymax <= 0.5 else 0.2)
    xt = ticks(0, 1.0, 0.2)

    parts = [
        f'<svg viewBox="0 0 {W} {H}" width="100%" role="img" '
        f'xmlns="http://www.w3.org/2000/svg">',
        "<title>规则引擎 risk-coverage 曲线</title>",
        f"<desc>{n_usable} 个可用 / {n_unusable} 个不可用样本，曲线越低说明排序越有效。</desc>",
        f'<rect x="0" y="0" width="{W}" height="{H}" fill="#FFFFFF"/>',
        f'<text x="{L}" y="30" font-family="system-ui,sans-serif" font-size="15" '
        f'font-weight="500" fill="#2C2C2A">纯规则引擎 · risk-coverage</text>',
        f'<text x="{L}" y="48" font-family="system-ui,sans-serif" font-size="12" '
        f'fill="#5F5E5A">{n_usable} 个可用 : {n_unusable} 个不可用 · 按 (结论, 评分, 覆盖率) 降序接受</text>',
    ]

    # 网格
    for v in yt:
        yy = y(v)
        parts.append(
            f'<line x1="{L}" y1="{yy:.1f}" x2="{R}" y2="{yy:.1f}" '
            f'stroke="#E8E6DF" stroke-width="0.5"/>'
        )
        parts.append(
            f'<text x="{L - 10}" y="{yy + 4:.1f}" text-anchor="end" '
            f'font-family="system-ui,sans-serif" font-size="11" fill="#888780">'
            f"{v:.0%}</text>"
        )
    for v in xt:
        xx = x(v)
        parts.append(
            f'<line x1="{xx:.1f}" y1="{T}" x2="{xx:.1f}" y2="{B}" '
            f'stroke="#E8E6DF" stroke-width="0.5"/>'
        )
        parts.append(
            f'<text x="{xx:.1f}" y="{B + 18}" text-anchor="middle" '
            f'font-family="system-ui,sans-serif" font-size="11" fill="#888780">'
            f"{v:.0%}</text>"
        )

    # 轴
    parts.append(
        f'<line x1="{L}" y1="{B}" x2="{R}" y2="{B}" stroke="#B4B2A9" stroke-width="1"/>'
    )
    parts.append(
        f'<line x1="{L}" y1="{T}" x2="{L}" y2="{B}" stroke="#B4B2A9" stroke-width="1"/>'
    )
    parts.append(
        f'<text x="{(L + R) / 2}" y="{B + 40}" text-anchor="middle" '
        f'font-family="system-ui,sans-serif" font-size="12" fill="#444441">'
        f"覆盖率 coverage（接受的计算比例）</text>"
    )
    parts.append(
        f'<text x="18" y="{(T + B) / 2}" text-anchor="middle" '
        f'font-family="system-ui,sans-serif" font-size="12" fill="#444441" '
        f'transform="rotate(-90 18 {(T + B) / 2})">漏放率 risk</text>'
    )

    # 随机基线（水平线 = 全体坏样本比例）
    by = y(base_rate)
    parts.append(
        f'<line x1="{L}" y1="{by:.1f}" x2="{R}" y2="{by:.1f}" '
        f'stroke="#B4B2A9" stroke-width="1" stroke-dasharray="5 4"/>'
    )
    parts.append(
        f'<text x="{R - 6}" y="{by - 8:.1f}" text-anchor="end" '
        f'font-family="system-ui,sans-serif" font-size="11" fill="#5F5E5A">'
        f"随机基线 {base_rate:.0%}</text>"
    )

    # 曲线（阶梯，符合"接受前 k 个"的离散语义）
    d = [f"M{x(pts[0][0]):.1f} {y(pts[0][1]):.1f}"]
    prev = pts[0][1]
    for c, r in pts[1:]:
        d.append(f"L{x(c):.1f} {y(prev):.1f}")
        d.append(f"L{x(c):.1f} {y(r):.1f}")
        prev = r
    parts.append(
        f'<path d="{" ".join(d)}" fill="none" stroke="#1D9E75" stroke-width="2" '
        f'stroke-linejoin="round"/>'
    )

    # 关键点标注
    for target in (0.5, 0.8):
        r = at_coverage(pts, target)
        if r is None:
            continue
        cx, cy = x(target), y(r)
        parts.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="3.5" fill="#1D9E75"/>')
        parts.append(
            f'<text x="{cx + 8:.1f}" y="{cy - 8:.1f}" '
            f'font-family="system-ui,sans-serif" font-size="11" fill="#0F6E56">'
            f"{target:.0%} → {r:.0%}</text>"
        )

    parts.append("</svg>")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(parts), encoding="utf-8")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="规则引擎 risk-coverage")
    ap.add_argument("--out", default=str(ROOT / "docs" / "risk_coverage.svg"))
    ap.add_argument("--csv", default=str(ROOT / "docs" / "risk_coverage.csv"))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    rows, dups, ambiguous = collect()
    usable_n = sum(1 for r in rows if not r["unusable"])
    unusable_n = len(rows) - usable_n
    base_rate = unusable_n / len(rows)
    pts = curve(rows)

    if args.json:
        print(json.dumps(
            {"n": len(rows), "usable": usable_n, "unusable": unusable_n,
             "base_rate": base_rate, "aurc": aurc(pts),
             "ideal_aurc": base_rate ** 2 / 2,
             "curve": [{"coverage": c, "risk": r} for c, r in pts]},
            ensure_ascii=False, indent=2))
        return 0

    print("qchem-eval 规则引擎 risk-coverage")
    print(f"样本 {len(rows)}（可用 {usable_n} / 不可用 {unusable_n}）" +
          (f"，去重合并 {len(dups)} 组" if dups else ""))
    if ambiguous:
        print(f"剔除口径不干净的负样本 {len(ambiguous)} 个（扰动后被兄弟签名接管）：")
        for name, hits in ambiguous:
            print(f"  {name:<20} 被 {','.join(hits)} 命中 —— 签名库内部有文本重叠")
    print()
    print("「不可用」= 构造上引入 critical 缺陷（崩溃、原子重叠、电子数矛盾…）；")
    print("major/minor 被接受是**正确**行为，不计入漏放。")
    print()
    print(f"随机基线（全体不可用占比） {base_rate:.1%}")
    print()
    print("  覆盖率    规则引擎      随机基线    相对")
    print("  " + "-" * 44)
    for t in (0.3, 0.5, 0.7, 0.8, 0.9, 1.0):
        r = at_coverage(pts, t)
        if r is None:
            continue
        delta = r - base_rate
        mark = "更低" if delta < -1e-9 else ("相同" if abs(delta) <= 1e-9 else "更高")
        print(f"  {t:>5.0%}    {r:>8.1%}     {base_rate:>8.1%}    {mark}")

    ideal = base_rate ** 2 / 2
    print()
    print(f"  AURC（曲线下面积，越低越好） {aurc(pts):.4f}")
    print(f"  理想 AURC（不可用全排最后）  {ideal:.4f}")

    # 尾巴：被接受但实际不可用的样本，最有诊断价值
    order = sorted(
        rows,
        key=lambda r: (VERDICT_RANK[Verdict(r["verdict"])], r["score"], r["coverage"]),
        reverse=True,
    )
    leaked = [r for r in order[: int(len(order) * 0.5)] if r["unusable"]]
    if leaked:
        print(f"\n  前 50% 里漏掉的不可用样本（{len(leaked)} 个）：")
        for r in leaked[:8]:
            print(f"    {r['name']:<28} {r['verdict']:<8} score={r['score']:<4} "
                  f"命中={','.join(r['findings']) or '无'}")

    svg(pts, base_rate, Path(args.out), usable_n, unusable_n)
    Path(args.csv).parent.mkdir(parents=True, exist_ok=True)
    with open(args.csv, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["coverage", "risk"])
        for c, r in pts:
            w.writerow([f"{c:.6f}", f"{r:.6f}"])
    print(f"\n  曲线图 {args.out}")
    print(f"  数据   {args.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
