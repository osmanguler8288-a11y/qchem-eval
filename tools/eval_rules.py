"""规则级 / 签名级的 precision & recall 评估。

做什么
------
对 tools/synth_failures.py 产出的带标签样本逐个跑评测，按「每条规则」「每条签名」
分别统计混淆矩阵：

    TP  有标签的正样本命中
    FN  有标签的正样本漏掉
    FP  负样本上误触发
    未验证 没有可用正样本（列出来，不藏）

同时输出两个「规则库健康度」指标，它们比 P/R 更能暴露问题：

    · 真实日志上的 UNKNOWN 次数 —— 一条规则若在所有真实日志上全是 UNKNOWN，
      说明它的 requires 字段解析器根本取不到。这种规则写在 YAML 里看着很专业，
      实际一条结论都给不出，是「空规则」。
    · 真实**可用**日志上的 CRITICAL/MAJOR 误报 —— 这是最伤信任的一类错误。
      注意「真实」不等于「可用」：tests/data 里既有结果可用的日志，也有
      ts_no_imag.log 这种「正常终止但答案是错的」，后者不在统计范围内（见 REAL_OK）。

用法
----
    python tools/eval_rules.py
    python tools/eval_rules.py --json report.json
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qchem_eval import evaluate_file                      # noqa: E402
from qchem_eval.rules import load_ruleset, run_rules       # noqa: E402
from qchem_eval.rules.signatures import load_signatures    # noqa: E402
from qchem_eval.parsers import parse                       # noqa: E402
from qchem_eval.schema import Severity                     # noqa: E402
from tools.synth_failures import DATA, REAL_BAD, REAL_OK, build_samples       # noqa: E402

HIGH = (Severity.CRITICAL, Severity.MAJOR)


def _evaluate_all(samples) -> dict:
    """样本落盘后逐个评测。落盘是因为 evaluate_file 走的是「输入一个路径」的真实入口。"""
    tmp = Path(tempfile.mkdtemp(prefix="qchem_eval_"))
    results = {}
    for s in samples:
        p = tmp / f"{s.name.replace(':', '_')}.log"
        p.write_text(s.text, encoding="utf-8")
        results[s.name] = evaluate_file(p)
    return results


def build_report() -> dict:
    samples = build_samples()
    results = _evaluate_all(samples)
    rs = load_ruleset()
    sigs = load_signatures()

    # ---- 每条规则 / 签名的计数 ----
    tp = defaultdict(int)
    fn = defaultdict(int)
    fp = defaultdict(int)
    positives = defaultdict(int)
    fn_names = defaultdict(list)
    fp_names = defaultdict(list)

    for s in samples:
        hits = {f.rule_id for f in results[s.name].findings}

        if s.kind in ("rule", "signature"):
            positives[s.expect] += 1
            if s.expect in hits:
                tp[s.expect] += 1
            else:
                fn[s.expect] += 1
                fn_names[s.expect].append(s.name)

        if s.kind == "negative":
            for rid in s.forbid:
                if rid in hits:
                    fp[rid] += 1
                    fp_names[rid].append(s.name)

    # ---- 规则在真实日志上的 UNKNOWN / 误报 ----
    #
    # 两个统计的样本范围不同，别合并：
    #   UNKNOWN  用**全部**真实日志。UNKNOWN 表示「解析器取不到这个字段」，
    #            跟结果对不对无关，样本越多越能判准一条规则是不是空规则。
    #   高严重性误报  只用 REAL_OK。在结果本身就不可用的日志上报 CRITICAL/MAJOR
    #            不是误报——那正是它该报的。用全集会把真阳性记成误报。
    real_unknown = defaultdict(int)
    real_high_fp = defaultdict(list)
    n_real = 0
    n_real_ok = 0
    for p in sorted(DATA.glob("*")):
        if not p.is_file():
            continue
        n_real += 1
        d, _ = parse(p)
        for o in run_rules(d, rs):
            if o.status == "unknown":
                real_unknown[o.rule.id] += 1
        if p.name in REAL_BAD:
            continue
        n_real_ok += 1
        for f in evaluate_file(p).findings:
            if f.severity in HIGH:
                real_high_fp[f.rule_id].append(p.name)

    rows = []
    for r in rs.rules:
        p, t, f_ = positives[r.id], tp[r.id], fn[r.id]
        rows.append({
            "id": r.id,
            "title": r.title,
            "severity": r.severity.value,
            "positives": p,
            "tp": t,
            "fn": f_,
            "fp": fp[r.id],
            "precision": round(t / (t + fp[r.id]), 3) if (t + fp[r.id]) else None,
            "recall": round(t / p, 3) if p else None,
            "unknown_on_real": real_unknown[r.id],
            "high_fp_on_real": real_high_fp[r.id],
            "fn_cases": fn_names[r.id],
            "fp_cases": fp_names[r.id],
        })

    sig_rows = []
    for s in sigs:
        p, t, f_ = positives[s.id], tp[s.id], fn[s.id]
        sig_rows.append({
            "id": s.id,
            "name": s.name,
            "severity": s.severity.value,
            "positives": p,
            "tp": t,
            "fn": f_,
            "fp": fp[s.id],
            "precision": round(t / (t + fp[s.id]), 3) if (t + fp[s.id]) else None,
            "recall": round(t / p, 3) if p else None,
            "fn_cases": fn_names[s.id],
            "fp_cases": fp_names[s.id],
        })

    n_neg = sum(1 for s in samples if s.kind == "negative")
    return {
        "n_samples": {
            "rule_positive": sum(1 for s in samples if s.kind == "rule"),
            "signature_positive": sum(1 for s in samples if s.kind == "signature"),
            "negative": n_neg,
            "real_logs": n_real,
            "real_ok_logs": n_real_ok,
        },
        "rules": rows,
        "signatures": sig_rows,
    }


# ---------------------------------------------------------------------------
# 打印
# ---------------------------------------------------------------------------

def _bar(v: float | None, width: int = 10) -> str:
    if v is None:
        return " " * width
    k = int(round(v * width))
    return "█" * k + "·" * (width - k)


def print_report(rep: dict) -> None:
    ns = rep["n_samples"]
    print("=" * 78)
    print("qchem-eval 规则库评估报告")
    print("=" * 78)
    print(f"样本：规则正样本 {ns['rule_positive']}｜签名正样本 {ns['signature_positive']}"
          f"｜负样本 {ns['negative']}"
          f"（含 {ns['real_logs']} 份真实日志，其中 {ns['real_ok_logs']} 份结果可用）")
    print()

    # ---- 签名层 ----
    sigs = rep["signatures"]
    sp = sum(r["positives"] for r in sigs)
    stp = sum(r["tp"] for r in sigs)
    sfp = sum(r["fp"] for r in sigs)
    print("── 签名层（signatures.yaml，官方错误目录照抄）" + "─" * 34)
    print(f"  召回 {stp}/{sp} = {stp / sp:.1%}    负样本误报 {sfp}/{ns['negative']}")
    bad = [r for r in sigs if r["positives"] and r["tp"] < r["positives"]]
    for r in bad:
        print(f"  ✗ 漏检 {r['id']:12s} {r['name']}  样本={r['fn_cases']}")
    bad_fp = [r for r in sigs if r["fp"]]
    for r in bad_fp:
        print(f"  ✗ 误报 {r['id']:12s} 出现在 {r['fp_cases'][:3]}")
    print()

    # ---- 规则层 ----
    rules = rep["rules"]
    covered = [r for r in rules if r["positives"]]
    uncovered = [r for r in rules if not r["positives"]]
    rp = sum(r["positives"] for r in covered)
    rtp = sum(r["tp"] for r in covered)
    # 条数从清单实测，不要写死 —— 上一版把 35 硬编码进标题，后来加了 C-07/C-08
    # 变成 37 条，标题却还印着 35，报告自己就在误报规则库规模。
    head = f"── 规则层（catalog.yaml，{len(rules)} 条判据）"
    print(head + "─" * max(1, 78 - len(head)))
    print(f"  有正样本的规则 {len(covered)}/{len(rules)}，召回 {rtp}/{rp} = {rtp / rp:.1%}")

    print(f"  {'规则':7s} {'严重性':9s} {'召回':22s} {'正样本':6s} 标题")
    for r in covered:
        flag = "  " if r["recall"] == 1.0 else "✗ "
        print(f"  {flag}{r['id']:6s} {r['severity']:9s} "
              f"{_bar(r['recall'])} {r['recall']:.0%}  {r['positives']:>2d}   {r['title'][:32]}")
    miss = [r for r in covered if r["recall"] < 1.0]
    if miss:
        print("  漏检明细：")
        for r in miss:
            print(f"    {r['id']}  {r['fn_cases']}")
    print()

    if uncovered:
        print(f"  ⚠ 没有正样本的规则 {len(uncovered)} 条（本报告不对它们做任何结论）：")
        for r in uncovered:
            print(f"    {r['id']:6s} {r['severity']:9s} {r['title'][:40]}")
        print()

    # ---- 真实日志健康度 ----
    n_all, n_ok = ns["real_logs"], ns["real_ok_logs"]
    print(f"── 真实日志健康度（{n_all} 份日志／{n_ok} 份结果可用）" + "─" * 26)
    dead = [r for r in rules if r["unknown_on_real"] == n_all]
    if dead:
        print(f"  ⚠ 全部 {n_all} 份日志都判 UNKNOWN 的「空规则」{len(dead)} 条"
              f"（requires 字段解析器取不到）：")
        for r in dead:
            print(f"    {r['id']:6s} requires 取不到 → {r['title'][:38]}")
    else:
        print("  ✓ 没有全 UNKNOWN 的空规则")
    partial = [r for r in rules if 0 < r["unknown_on_real"] < n_all]
    if partial:
        print(f"  部分 UNKNOWN 的规则 {len(partial)} 条：")
        for r in partial:
            print(f"    {r['id']:6s} {r['unknown_on_real']}/{n_all} 份判不了"
                  f" → {r['title'][:34]}")

    high_fp = [r for r in rules if r["high_fp_on_real"]]
    print()
    if high_fp:
        print(f"  ✗ 结果可用的日志上出现 CRITICAL/MAJOR（{n_ok} 份里）：")
        for r in high_fp:
            print(f"    {r['id']:6s} {r['severity']:9s} {r['high_fp_on_real']} {r['title'][:34]}")
    else:
        print(f"  ✓ {n_ok} 份结果可用的日志上零 CRITICAL/MAJOR 误报")
    print()

    print("── 诚实边界" + "─" * 66)
    print("  1. 签名层正样本是把官方错误原文嫁接进真实日志，检验的是「匹配器能否在真实")
    print("     日志里找到官方文本」，不是「官方目录能否覆盖所有真实崩溃」。")
    print("  2. 规则层正样本是定点变异，检验的是「判据实现正确」，不是「阈值适合你们体系」。")
    print("  3. 负样本共 "
          f"{ns['negative']} 个（{ns['real_logs']} 份真实日志 + 其余为扰动文本），"
          "样本量小，precision 的置信区间很宽。")
    print(f"  4. 「真实日志」不是「成功日志」：{n_all} 份里有 {n_all - n_ok} 份正常终止"
          "但结果不可用（如 ts_no_imag.log），它们只参与签名层检查与 UNKNOWN 统计。")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="规则级 P/R 评估")
    ap.add_argument("--json", default="", help="把完整报告写成 JSON")
    args = ap.parse_args(argv)

    rep = build_report()
    print_report(rep)

    if args.json:
        Path(args.json).write_text(
            json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\nJSON 已写入 {args.json}")

    # 退出码：签名层有漏检或误报时不静默通过
    sig_bad = any(r["positives"] and r["tp"] < r["positives"] for r in rep["signatures"])
    sig_fp = any(r["fp"] for r in rep["signatures"])
    return 1 if (sig_bad or sig_fp) else 0


if __name__ == "__main__":
    sys.exit(main())
