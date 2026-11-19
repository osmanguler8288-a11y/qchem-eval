#!/usr/bin/env python3
"""用真实日志校准规则阈值。

## 为什么需要这个

39 条规则里的数值阈值全部来自文献或拍脑袋，没有一条被真实失败日志验证过。
「实现是对的」和「判得准」是两件事：`when: d.scf_cycles[-1] > 50` 这句话语法
没问题、测试也能过，但 50 这个数是不是合理，只有拿真实计算的实际步数分布
去比才知道。

这个工具不改判定、不生成样本，只做一件事：**把每条阈值在真实日志上的取值
分布摊开**，让「阈值离真实值域多远」变成可看的东西。

## 口径为什么必须复用 run_rules

第一版自己重算派生量、自己判「哪些样本适用」，结果统计出来的分布和规则
实际看到的样本不是同一批 —— 报出「B-04 正常样本已越线 3527%」这种荒谬结论，
真实原因是 `applies: 'freq' in d.stages` 把没有频率的样本全排除了，而我没排除。

所以这里直接跑 `run_rules` 拿 status：
  · 只有 hit / miss 的样本才计入该规则的分布（规则真的看过它）
  · na 是「这类任务没这个检查项」，不参与统计
  · unknown 单独计数 —— **一条 major 规则如果全是 unknown，等于形同虚设**，
    这个信号比阈值不准更严重

## 三种危险信号

  · **误伤风险**：本应放过的样本，实测值已经逼近阈值（余量 < 15%）
  · **形同虚设**：正常样本离阈值还有 3 倍以上余量，真出问题时抓不到
  · **跑不起来**：unknown 占比高 → 解析器缺字段，规则从未真正生效

## 用法

    python tools/calibrate.py field_test/real
    python tools/calibrate.py field_test/real --csv /tmp/cal.csv
"""

from __future__ import annotations

import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qchem_eval.parsers import parse                      # noqa: E402
from qchem_eval.rules.evaluator import build_context, run_rules, load_ruleset  # noqa: E402


@dataclass
class Probe:
    """一条阈值规则的取数口径。

    value 取 ctx 里的派生量而不是原始字段：派生量已经做过单位换算
    （`de_kcal` 是 kcal、`vt_dev` 已取绝对值），跟 when 表达式比较的是同一个数。
    自己拿原始字段再算一遍等于把换算逻辑写两份，迟早不一致 —— `conv_last`
    就是反例：它在 ctx 里是**格式化好的字符串**（给 evidence 用），
    数值版本叫 `conv_ratio`，拿错一个就永远取不到数。
    """

    rule: str
    expr: str                       # when 原文，留作对照
    value: Callable[[dict], Any]    # ctx -> 实测标量
    threshold: float
    unit: str = ""
    lower_is_bad: bool = False      # True = 小于阈值才算问题（B-03 / B-05 / C-05）
    subset: Callable[[dict], bool] | None = None
    semantic: bool = False          # 判符号翻转/存在性，不是"贴近正常值域"型阈值
    note: str = ""


def _g(key: str) -> Callable[[dict], Any]:
    return lambda c: c.get(key)


PROBES: list[Probe] = [
    # ---- A 收敛性 ----
    Probe("A-03", "scf_cycles[-1] >= 0.8*scf_maxcycle",
          lambda c: (c["scf_cycles"][-1] / c["scf_maxcycle"]
                     if c.get("scf_cycles") and c.get("scf_maxcycle") else None),
          0.8, "占比", note="正常计算多数在 0.05 以下"),
    Probe("A-04", "scf_cycles[-1] > 50", _g("last_cycles"), 50, "步"),
    # conv_ratio 而不是 conv_last：后者是 f'{x:.2e}' 出来的字符串
    Probe("A-06", "scf_conv[-1] > 10*conv_tol_num", _g("conv_ratio"), 10.0, "倍判据",
          note="打出 SCF Done 但密度没到判据"),

    # ---- B 结构合理性 ----
    # 用 min_real_freq 而不是 min_freq：B-03 判的是 any(0 < f < 100)，
    # 拿全部频率的最小值会把虚频（-356）当成"最低频"，算出"本应放过的样本
    # 已到 -356"这种和规则判定不一致的结论。口径必须和 when 完全对齐。
    Probe("B-03", "any(0 < f < 100)", _g("min_real_freq"), 100, "cm-1",
          lower_is_bad=True,
          note="弱相互作用体系最低实频天生几十 cm-1，这条最可能误伤"),
    # subset 是必需的：B-04 的 when 第一句就是 `d.n_imag and d.n_imag > 0`，
    # 即「有虚频时才评估这条」。没有虚频的样本走的是 miss（第一个条件就不满足），
    # 它们不是「B-04 放过」，而是「B-04 根本没被考到」。不加 subset 会把一堆
    # 正常高频算进来，算出「本应放过的样本已到 4460 cm-1、越线 9012%」这种废话。
    # status 分不出这两者 —— 因为 when 内部的前提条件不进 status。
    Probe("B-04", "n_imag>0 and min(freqs) > -50", _g("min_freq"), -50, "cm-1",
          subset=lambda c: (c.get("n_imag") or 0) > 0,
          note="虚频绝对值极小：数值噪声还是真鞍点，全靠这条线分"),
    Probe("B-05", "min_dist < 0.6", _g("min_dist"), 0.6, "Å", lower_is_bad=True),
    Probe("B-10", "n_frag>=2 and frag_gap > 4.5", _g("frag_gap"), 4.5, "Å",
          note="4.5 = 3 倍范德华碳半径，判「断了」而非「弱相互作用」"),

    # ---- C 方法匹配 ----
    Probe("C-02", "abs(s2-s2_expected) > max(0.1*s2_expected, 0.02)",
          lambda c: (abs(c["s2_dev"]) / c["s2_expected"]
                     if c.get("s2") is not None and c.get("s2_expected") else None),
          0.1, "相对偏差", note="<S²> 相对理想值的偏离比例"),
    Probe("C-05", "0 < lumo-homo < 0.5", _g("gap_ev"), 0.5, "eV", lower_is_bad=True),

    # ---- D 数值一致性 ----
    Probe("D-01", "|E(n)-E(n-1)|*627.5 > 500", _g("de_kcal"), 500, "kcal/mol"),
    Probe("D-07", "|virial-2| > 0.5", _g("vt_dev"), 0.5, "偏差"),
    Probe("D-08", "(E(n)-E(n-1))*627.5 > 1.0", _g("de_up_kcal"), 1.0, "kcal/mol"),
    # 用 Ha 原量纲，避免 0.001 Ha -> kcal 换算出第二种错误来源。
    # semantic=True：这条判的是**符号翻转**（正常为负，转正才是异常），
    # 阈值 0 附近的自然分界，不存在"该不该贴边"的问题。硬算余量会得出
    # 「12 倍余量、形同虚设」这种误导结论 —— 它压根不是那个意义上的阈值。
    Probe("D-09", "gibbs - zpe > 0.001 Ha",
          lambda c: (c["gibbs_energy"] - c["zpe_energy"]
                     if c.get("gibbs_energy") is not None and c.get("zpe_energy") is not None
                     else None),
          0.001, "Ha", semantic=True,
          note="正常为负（室温下 -TS 主导），转正才是异常"),
]


def collect(paths: list[Path]) -> tuple[list[tuple[str, dict]], dict[str, dict]]:
    """解析并跑规则，返回 ([(文件名, ctx)], {文件名: {规则: 状态}})。"""
    rs = load_ruleset()
    rows: list[tuple[str, dict]] = []
    status: dict[str, dict] = {}
    for p in paths:
        try:
            d, _ = parse(p)
        except Exception as exc:                     # noqa: BLE001
            rows.append((p.name, {"_error": f"{type(exc).__name__}: {exc}"}))
            continue
        ctx = build_context(d)
        ctx["_software"] = d.software
        status[p.name] = {o.rule.id: o.status for o in run_rules(d, rs)}
        rows.append((p.name, ctx))
    return rows, status


def _fmt(v: Any) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.3g}"
    return str(v)


def _safe_value(pr: Probe, ctx: dict) -> float | None:
    """取一个可比较的浮点数，取不到返回 None。

    三层防御，每层都对应真实踩过的形状：
      · 调用本身可能抛 —— 派生量缺失时除法会 TypeError（ctx 里缺失值用 "?" 占位）；
      · 取到的可能是字符串 "?" 或 None；
      · 可能是 nan/inf —— 让它进排序会跑到端点，把 min/max 彻底毁掉，
        而且不报错，只是分布看起来莫名其妙。
    """
    try:
        v = pr.value(ctx)
        if v is None:
            return None
        v = float(v)
    except (TypeError, ValueError, ZeroDivisionError, KeyError):
        return None
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return v


def report(rows: list[tuple[str, dict]], status: dict[str, dict],
           csv_out: Path | None = None) -> None:
    ok = [(n, c) for n, c in rows if "_error" not in c]
    errs = [(n, c["_error"]) for n, c in rows if "_error" in c]
    print(f"样本 {len(rows)} 份：可用 {len(ok)}，解析异常 {len(errs)}")
    for n, e in errs:
        print(f"  解析异常 {n}: {e}")
    print()

    csv_rows = [["rule", "expr", "threshold", "unit", "sample", "status", "value"]]
    print(f"{'规则':<6}{'阈值':>10}{'单位':<8}{'判定过(过/命中)':<16}"
          f"{'放过样本 min/中位/max':<26}{'余量':>7}  判定")
    print("─" * 100)

    alerts: list[str] = []
    for pr in PROBES:
        # 只统计规则**真的判定过**的样本：na 是「这类任务没这个检查项」，
        # unknown 是「缺数据判不了」——两者都不该混进阈值分布，
        # 否则算出来的"正常值域"和规则实际看到的值域不是一回事。
        passed: list[tuple[str, float]] = []
        hits: list[tuple[str, float]] = []
        n_na = n_unk = 0
        for name, ctx in ok:
            st = status.get(name, {}).get(pr.rule, "?")
            if st == "na":
                n_na += 1
                continue
            if st == "unknown":
                n_unk += 1
                continue
            # when 内部的前置条件不在 status 里体现，要单独排掉：
            # 否则「n_imag>0 才算」的 B-04 会把所有无虚频样本当成「放过」。
            if pr.subset is not None and not pr.subset(ctx):
                n_na += 1
                continue
            v = _safe_value(pr, ctx)
            if v is None:
                continue
            csv_rows.append([pr.rule, pr.expr, str(pr.threshold), pr.unit,
                             name, st, f"{v:.6g}"])
            (hits if st == "hit" else passed).append((name, v))

        judged = len(passed) + len(hits)
        if not judged:
            print(f"{pr.rule:<6}{pr.threshold:>10g}{pr.unit:<8}{'—':<16}"
                  f"{'（这条规则从未真正判定过）':<26}")
            alerts.append(f"{pr.rule}: 全部样本都是 na/unknown，规则从未生效"
                          f"（na {n_na} / unknown {n_unk}）")
            continue

        stat = f"{len(passed)}/{len(hits)}"
        if not passed:
            print(f"{pr.rule:<6}{pr.threshold:>10g}{pr.unit:<8}{stat:<16}"
                  f"{'（无「放过」样本，无法算余量）':<26}")
            continue

        vs = sorted(v for _, v in passed)
        lo, mid, hi = vs[0], vs[len(vs) // 2], vs[-1]
        # "/".join 不是 " ".join —— 后者作用在单个字符串上会把字符逐个拆开
        dist = "/".join(_fmt(v) for v in (lo, mid, hi))

        if pr.semantic:
            # 符号/存在性型阈值：正常值域天然在阈值一侧很远，算"余量"没意义。
            # 只报实际取值，让人自己看有没有翻到另一侧。
            flipped = sum(1 for _, v in passed if (v > pr.threshold) != pr.lower_is_bad)
            print(f"{pr.rule:<6}{pr.threshold:>10g}{pr.unit:<8}{stat:<16}"
                  f"{dist:<26}{'—':>6}  ✓ 语义边界（正常取值恒在单侧）")
            if flipped:
                alerts.append(f"{pr.rule}: 有 {flipped} 份正常样本越到阈值另一侧")
            continue

        if pr.lower_is_bad:
            worst = lo                       # 越小越危险
            margin = (worst - pr.threshold) / abs(pr.threshold) if pr.threshold else 0.0
        else:
            worst = hi                       # 越大越危险
            margin = (pr.threshold - worst) / abs(pr.threshold) if pr.threshold else 0.0

        if n_unk:
            # unknown 占比高说明解析器缺字段，规则在大部分样本上根本没开口
            unk_note = f"（另有 {n_unk} 份 unknown）"
        else:
            unk_note = ""

        if margin < 0.15:
            flag = "⚠ 误伤风险"
            alerts.append(
                f"{pr.rule}: 本应放过的样本已到 {_fmt(worst)}{pr.unit}，"
                f"阈值 {pr.threshold}{pr.unit}，余量仅 {margin:.0%}。{pr.note}"
            )
        elif margin > 3.0:
            flag = "○ 余量过大"
            alerts.append(
                f"{pr.rule}: 放过样本最远只到 {_fmt(worst)}{pr.unit}，阈值 "
                f"{pr.threshold}{pr.unit}（{margin:.0f} 倍余量），真出问题时抓不到。"
            )
        else:
            flag = "✓"
        print(f"{pr.rule:<6}{pr.threshold:>10g}{pr.unit:<8}{stat:<16}"
              f"{dist:<26}{margin:>6.0%}  {flag}{unk_note}")

    print("─" * 100)
    print(" 判定过 = 规则真的看过并给出结论的样本数（放过/命中）；"
          "na 与 unknown 不计入")
    print(" 余量   = 放过样本最靠近阈值的一端到阈值的相对距离，"
          "负数表示本应放过的样本已越线")
    print()

    if alerts:
        print("需要处理的：")
        for i, a in enumerate(alerts, 1):
            print(f"  {i}. {a}")
    else:
        print("所有阈值都在安全区。")

    if csv_out:
        csv_out.parent.mkdir(parents=True, exist_ok=True)
        with open(csv_out, "w", encoding="utf-8", newline="") as fh:
            for r in csv_rows:
                fh.write(",".join(str(x).replace(",", ";") for x in r) + "\n")
        print(f"\n明细已写入 {csv_out}")


def main(argv: list[str]) -> int:
    # 多个路径而不是一个：验收样本（field_test/real）和校准样本
    # （field_test/calib_out）语义不同、要分开存放，但统计时必须合起来看 ——
    # 否则「这个阈值放过了所有正常样本」只对着一半数据成立。
    targets: list[Path] = []
    csv_out: Path | None = None
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "--csv":
            if i + 1 >= len(argv):
                print("--csv 后面要跟文件名", file=sys.stderr)
                return 2
            csv_out = Path(argv[i + 1])
            i += 2
            continue
        targets.append(Path(a))
        i += 1

    if not targets:
        print(__doc__)
        return 2

    paths: list[Path] = []
    for t in targets:
        if t.is_file():
            paths.append(t)
        elif t.is_dir():
            paths.extend(sorted(p for p in t.rglob("*")
                                if p.is_file() and p.suffix in (".log", ".out")))
        else:
            print(f"路径不存在：{t}", file=sys.stderr)
            return 2
    if not paths:
        print(f"没有可统计的文件：{targets}", file=sys.stderr)
        return 2

    report(*collect(paths), csv_out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
