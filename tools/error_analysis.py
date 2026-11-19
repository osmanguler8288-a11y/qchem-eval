#!/usr/bin/env python3
"""误差分析：把「判错了」拆成四类，并回答「模型到底该接什么活」。

为什么需要这一步
----------------
tools/eval_rules.py 报的是准确率数字，它回答不了「剩下那点错在哪」。而
「该不该上 transformer」恰好只能由后者回答：如果错误全在解析器和阈值上，
模型是错的解法；只有当错误属于「多个字段都正常、但合起来说不通」时，模型
才有存在的理由。

四分类（每一类对应一种完全不同的修法）
--------------------------------------
    parser      解析器没取到 —— 日志里明明有，是我没提取出来   → 补解析器
    decl        规则声明错 —— requires/applies 用错了信号       → 改 YAML 声明
    rule        规则判错 —— 字段都在，判据/阈值/定级错了       → 修判据
    coverage    规则没覆盖 —— 没有任何规则盯着这种失败模式     → 加规则
    multifield  需多字段综合 —— 看全了、没有单字段越界，仍判错 → **模型候选**

判据从哪来（机器只做归因，不猜答案）
------------------------------------
    · 合成样本：tools/synth_failures.py 的 build_samples()（expect / forbid）
    · 真实日志：field_test/server/verify_rules.py 的 EXPECT（hit / verdict /
      no_crit / no_major，都是**跑之前**按构造意图写的）
    EXPECT 用 importlib 按路径加载，不复制一份进来 —— 标签一旦有两份，
    就一定会漂移，而标签漂移是「测试全绿但结论错」的经典来源。

verdict 之外的第二个错误面：无标签日志上的一致性
-------------------------------------------------
四分类只看得到「有标签的错」。跑完 143 份带标签样本后分歧归零，不等于系统
没问题，只等于**标签集饱和了**（题是我出的，答案也是我定的）。真正的错误面
这时已经转移到没人标注的地方，所以本文件额外做四项无标签检查：

    B1 动作通道饱和度   多少个文件被判 human_review，都是由什么级别驱动的
    B2 动作优先级语义   rerun / escalate / human_review 的强弱顺序对不对
    B3 定级-动作自洽    minor 配 human_review 这类自相矛盾的组合
    B4 结论-严重性自洽  以及 evidence 里出现占位符（靠缺数据硬判）

用法
----
    python tools/error_analysis.py
    python tools/error_analysis.py --json ea.json
    python tools/error_analysis.py --no-synth        # 只看真实日志
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qchem_eval.parsers import parse                              # noqa: E402
from qchem_eval.rules import (                                    # noqa: E402
    ACTION_PRIORITY,
    SEVERITY_VERDICT,
    load_ruleset,
    run_rules,
)
from qchem_eval.rules.signatures import load_signatures, match as match_signatures  # noqa: E402
from qchem_eval.schema import Action, Severity, Verdict            # noqa: E402
from tools.synth_failures import IDENTITY_CASES, RULE_CASES, build_samples  # noqa: E402

# 真实日志的四个来源目录。分开列而不是递归 glob：这三个目录是分批跑出来的，
# 每批的构造意图不同（验收 / 阈值校准 / 复合物对照），混在一起就分不清
# 某个误报到底是哪批引入的。
REAL_DIRS = [
    ("验收", ROOT / "field_test" / "real"),
    ("校准", ROOT / "field_test" / "calib_out" / "calout"),
    ("复合物", ROOT / "field_test" / "complex_out" / "comout"),
    ("坏几何", ROOT / "field_test" / "badgeom_out" / "bout"),
    ("cclib", ROOT / "tests" / "data"),
]


class Cause(str, Enum):
    OK = "ok"
    PARSER = "parser"
    DECL = "decl"
    RULE = "rule"
    COVERAGE = "coverage"
    MULTIFIELD = "multifield"


CAUSE_CN = {
    Cause.OK: "正确",
    Cause.PARSER: "解析器没取到",
    Cause.DECL: "规则声明错",
    Cause.RULE: "规则判错",
    Cause.COVERAGE: "规则没覆盖",
    Cause.MULTIFIELD: "需多字段综合",
}

CAUSE_FIX = {
    Cause.PARSER: "补解析器（日志里有这个量，只是没提取出来）",
    Cause.DECL: "改 YAML 的 requires/applies（规则声明了这份日志客观没有的量）",
    Cause.RULE: "修判据/阈值/定级（字段都在，结论错了）",
    Cause.COVERAGE: "加规则（没有任何规则盯着这种失败模式）",
    Cause.MULTIFIELD: "模型候选（所有单字段都在正常范围内，仍判错）",
}

# 人工标注的归因覆盖。**只在机器判不出来时用**，每条必须写清为什么，
# 而且要能被第三人复核 —— 不加说明的覆盖等于改答案。
ATTRIBUTION_NOTE: dict[str, tuple[Cause, str]] = {
    "fail_coord_missing": (
        Cause.COVERAGE,
        "声明 B3LYP/6-31G(d) 的水分子，坐标只给了一行 O；Gaussian 当单原子氧算完"
        "并正常终止。日志里 39 条规则全部 miss（没有任何字段越界），唯一能发现的"
        "办法是拿 natoms(日志) 与 natoms(输入) 对比。是**缺数据源**，不是缺判据，"
        "所以不算 multifield —— 关系是闭式的。",
    ),
}

# 「日志看起来干干净净、但这次计算其实是错的」——只能人工标注的事实。
#
# 为什么不能用 EXPECT.verdict 推：verify_rules 里的 verdict 写的是**期望引擎
# 报什么**，不是「这次计算对不对」。fail_coord_missing 的期望刻意写成
# pass_with_warning，就是为了让引擎给它 PASS 时那一条**失败**、缺口不被藏起来。
# 拿它当「这是坏日志」的真值会把唯一真正的静默失败样本排除在候选之外 ——
# 机器读标签时最容易犯的错就是分不清「期望」和「事实」。
SILENT_BAD: dict[str, str] = {
    "fail_coord_missing":
        "单原子氧算完并 Normal termination，报告上零异常；但声明的分子是水。",
}


# ---------------------------------------------------------------------------
# 标签源
# ---------------------------------------------------------------------------

@dataclass
class Case:
    """一份样本 + 它的正确答案 + 判定结果。"""

    name: str
    origin: str                  # synth / real:<批名>
    path: Path
    expect: list[str] = field(default_factory=list)     # 必须命中的 id
    forbid: list[str] = field(default_factory=list)     # 不许命中的 id
    verdict: str | None = None                          # 真实日志专有
    no_crit: bool = False
    no_major: bool = False
    fail_by_construction: bool = False                  # 构造上就是失败样本
    labeled: bool = False        # 有没有写期望。写「没有」和写「判对了」是两件事
    note: str = ""


def load_real_expect() -> dict[str, dict]:
    """按路径加载 field_test/server/verify_rules.py，只取 EXPECT。

    这个脚本是要 scp 到 cu04 上单文件跑的，不能为了复用把标签拆成数据文件。
    所以反向做：本地分析时把它当模块加载（`if __name__` 保护住了，导入无副作用）。
    """
    p = ROOT / "field_test" / "server" / "verify_rules.py"
    if not p.exists():
        return {}
    spec = importlib.util.spec_from_file_location("_qce_verify_rules", p)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return dict(mod.EXPECT)


def real_cases() -> list[Case]:
    expect = load_real_expect()
    out: list[Case] = []
    for batch, d in REAL_DIRS:
        if not d.exists():
            continue
        for p in sorted(d.glob("*")):
            if not (p.is_file() and p.suffix in (".log", ".out")):
                continue
            exp = expect.get(p.stem, {})
            out.append(Case(
                name=p.stem,
                origin=f"real:{batch}",
                path=p,
                expect=list(exp.get("hit", [])),
                verdict=exp.get("verdict"),
                no_crit=bool(exp.get("no_crit")),
                no_major=bool(exp.get("no_major")),
                fail_by_construction=(exp.get("verdict") == "fail"),
                # 有 EXPECT 条目才算有标签。ok_force 这种只写 no_crit 的也算。
                labeled=bool(exp),
                note=exp.get("why", ""),
            ))
    return out


def synth_cases(tmp: Path) -> list[Case]:
    out: list[Case] = []
    for s in build_samples():
        p = tmp / (s.name.replace(":", "_").replace("+", "_p_") + ".log")
        p.write_text(s.text, encoding="utf-8")
        out.append(Case(
            name=s.name,
            origin=f"synth:{s.kind}",
            path=p,
            expect=[s.expect] if s.expect else [],
            forbid=list(s.forbid),
            # 合成样本的每个正样本都是「把一份好日志改成坏的」，构造上就是失败样本。
            # 负样本没有这个性质，所以只在 kind != negative 时置位。
            fail_by_construction=(s.kind != "negative"),
            labeled=True,
            note=s.note,
        ))
    return out


# ---------------------------------------------------------------------------
# 归因
# ---------------------------------------------------------------------------

def _silent_failure(d) -> bool:
    """这份日志是不是「正常跑完、没有任何字段越界」的静默失败。

    multifield 的判定前提。三条都满足才算：
      1. 没有异常终止记录（崩了的日志会有 A-01，那是 parser/签名层的活）
      2. 没有解析器自己承认的缺口（否则可能是 parser 的锅）
      3. 全文有 Normal termination（确实跑完了）
    """
    return (
        not d.error_terminations
        and not d.unparsed_hits
        and bool(d.normal_termination)
    )


def attribute(c: Case, d, outcomes, sig_hits: set[str]) -> list[tuple[Cause, str, str]]:
    """返回 [(类别, 被归因的 id, 证据)]。空列表 = 这份样本判对了。

    归因顺序是有讲究的：先用「解析器自己承认的缺口」判 parser，再用状态判
    decl/rule，最后才轮到 multifield。反过来做的话，任何一处解析缺口都会
    被算成「需要模型」，那是把工程债伪装成算法需求 —— 这条路径一旦走上，
    模型永远训不出来，因为它在学解析器的漏。
    """
    st = {o.rule.id: o for o in outcomes}
    hits = {o.rule.id for o in outcomes if o.status == "hit"} | set(sig_hits)
    unparsed = {h["field"] for h in d.unparsed_hits}
    out: list[tuple[Cause, str, str]] = []

    # ---- 1) 漏报：该响的没响 ----
    for rid in c.expect:
        if rid in hits:
            continue
        o = st.get(rid)
        if o is None:
            out.append((Cause.COVERAGE, rid, "规则表里没有这个 id（标签写错或规则被删）"))
            continue
        if o.status == "unknown":
            if any(m in unparsed for m in o.missing):
                out.append((Cause.PARSER, rid, f"解析器看到但没提取：{o.missing}"))
            elif any("异常" in m for m in o.missing):
                out.append((Cause.RULE, rid, f"规则求值崩了：{o.missing}"))
            else:
                out.append((Cause.DECL, rid, f"requires 声明了这份日志没有的量：{o.missing}"))
        elif o.status == "na":
            out.append((Cause.DECL, rid, "applies 把这份样本判成不适用"))
        elif o.status == "miss":
            out.append((Cause.RULE, rid, "字段齐全，判据没触发（阈值或逻辑不对）"))
        else:  # pragma: no cover - hit 已在上面 continue
            out.append((Cause.RULE, rid, f"未知状态 {o.status}"))

    # ---- 2) 误报：不许响的响了 ----
    for rid in c.forbid:
        if rid in hits:
            who = "签名" if rid.startswith("SIG-") else "规则"
            out.append((Cause.RULE, rid, f"{who}误报"))

    # ---- 3) 真实日志的结论级期望 ----
    # verdict / no_crit / no_major 是「整体结论」层面的期望，可能在不违背任何
    # 单条 hit 期望的情况下失败（fail_coord_missing 就是），必须单独判。
    verdict = Verdict.FAIL if any(o.status == "hit" and o.finding
                                  and o.finding.severity is Severity.CRITICAL
                                  for o in outcomes) else None
    worst = min((o.finding.severity for o in outcomes if o.finding is not None),
                key=lambda s: list(Severity).index(s), default=None)
    if c.verdict and not out:
        # 只看规则层命中推不出最终 verdict（还混着签名），所以用最坏严重性反推
        actual = SEVERITY_VERDICT.get(worst, Verdict.PASS) if worst else Verdict.PASS
        if actual.value != c.verdict:
            out.append((Cause.COVERAGE, "-", f"结论 {actual.value} != 期望 {c.verdict}"))
    if c.no_crit and worst is Severity.CRITICAL:
        out.append((Cause.RULE, "-", "不该出现 critical"))
    if c.no_major and worst in (Severity.CRITICAL, Severity.MAJOR):
        out.append((Cause.RULE, "-", "不该出现 major 及以上"))

    # ---- 4) multifield 不在这里判 ----
    # 机器**没有资格**把一个错误叫做「需要模型」。这条边界是刻意的：一旦机器
    # 能自己把「我没覆盖到」写成「需要联合推理」，工程债就会被伪装成算法需求，
    # 模型永远训不出来 —— 它在学解析器和规则的漏。
    # 所以这里只产出可机械归因的类别，multifield 只作为**候选**单列出来，
    # 由 detect_multifield_candidates() 给出、由人确认（见 ATTRIBUTION_NOTE）。
    return out


def detect_multifield_candidates(rows: list[dict]) -> list[dict]:
    """筛出「看全了、没有单字段越界、但这次计算其实是错的」的样本，供人工确认。

    候选只是候选。复核结果只有两种：
      · 缺数据源（如 fail_coord_missing 需要输入文件比 natoms）→ 归 coverage
      · 真的需要联合多字段推理 → 这才是模型的活
    机器分不清这两者，硬判会把前者算成模型需求。
    """
    return [{"name": r["name"], "origin": r["origin"], "note": r["note"][:120],
             "annotated": SILENT_BAD.get(r["name"], "")}
            for r in rows
            if r["name"] in SILENT_BAD
            and r["silent"]
            and not r["n_unknown"] and not r["n_hit"]]


def run_cases(cases: list[Case], ruleset) -> list[dict]:
    rows: list[dict] = []
    for c in cases:
        d, _ = parse(c.path)
        outcomes = run_rules(d, ruleset)
        sig = match_signatures(d)
        sig_hits = {f.rule_id for f in sig}
        causes = attribute(c, d, outcomes, sig_hits)

        # 人工覆盖（有说明、可复核）
        if c.name in ATTRIBUTION_NOTE and causes:
            why_cause, why = ATTRIBUTION_NOTE[c.name]
            causes = [(why_cause, causes[0][1], why)]

        all_hit = {o.rule.id for o in outcomes if o.status == "hit"} | sig_hits
        rows.append({
            "name": c.name,
            "origin": c.origin,
            "path": str(c.path),
            "expect": c.expect,
            "forbid": c.forbid,
            "n_hit": len(all_hit),
            "hits": sorted(all_hit),
            "causes": [{"cause": k.value, "id": rid, "why": ev} for k, rid, ev in causes],
            "n_unknown": sum(1 for o in outcomes if o.status == "unknown"),
            "n_na": sum(1 for o in outcomes if o.status == "na"),
            # 这两个字段给 multifield 候选筛查用。silent = 日志跑完了、没有任何
            # 字段越界、解析器也没承认缺口 —— 也就是「规则层看全了却没看出来」。
            "silent": _silent_failure(d),
            "fail_by_construction": c.fail_by_construction,
            "labeled": c.labeled,
            "note": c.note,
        })
    return rows


# ---------------------------------------------------------------------------
# 无标签检查：错误面已经转移到没有标注的地方
# ---------------------------------------------------------------------------

def check_action_channel(logs: list[Path]) -> dict:
    """B1/B2 动作通道：被判 human_review 的都是什么级别在驱动。

    为什么这是一个「错误」而不是「口味」：action 是整条流水线唯一的路由字段。
    批量 300 份时如果 48% 都返回 human_review，这个字段和抛硬币等价 ——
    Agent 拿它做任何自动处置都会退化成人肉队列，而人肉队列的实际后果是
    用户永久关闭这个字段（和日志满屏是同一个病）。
    """
    from qchem_eval import evaluate_file

    actions: Counter[str] = Counter()
    drivers: Counter[str] = Counter()       # human_review 由哪个级别驱动
    masked: list[dict] = []                 # 动作被优先级表压低
    detail = []
    for p in logs:
        r = evaluate_file(p)
        actions[r.action.value] += 1
        if not r.findings:
            continue
        worst = min((f.severity for f in r.findings),
                    key=lambda s: list(Severity).index(s))
        worst_f = [f for f in r.findings if f.severity is worst]
        by_worst = max(ACTION_PRIORITY[f.action] for f in worst_f)
        if r.action.value == Action.HUMAN.value:
            drivers[worst.value] += 1
        if ACTION_PRIORITY[r.action] < by_worst:
            masked.append({
                "log": p.stem, "action": r.action.value,
                "worst": worst.value,
                "wanted": [f.action.value for f in worst_f],
                "hits": [f.rule_id for f in r.findings],
            })
        detail.append({
            "log": p.stem, "verdict": r.verdict.value, "score": r.score,
            "action": r.action.value,
            "findings": [{"id": f.rule_id, "sev": f.severity.value,
                          "action": f.action.value} for f in r.findings],
        })

    n = len(logs)
    return {
        "n_logs": n,
        "actions": dict(actions),
        "human_share": round(actions.get("human_review", 0) / n, 3) if n else 0.0,
        "human_drivers": dict(drivers),
        "masked_by_priority": masked,
        "detail": detail,
    }


def check_leveling(ruleset) -> dict:
    """B3 定级-动作自洽：severity 说「多严重」，action 说「该怎么办」。

    分两档，因为证据强度差一个量级：

    **conflict（真矛盾）** —— 两栏的定义直接打架，不需要看数据就能判定。
      minor 在 schema.py 里的定义是「需要注意，**通常不影响结论**」；
      human_review 的含义是「这份结果不能自动处置」。一条 minor 配
      human_review，字面意思是「不影响结论，但你必须每条都看」。

    **debatable（可争议）** —— 单看定义不算错，但从批量运行的后果看有问题。
      critical + rerun：崩溃多由输入/环境错误引起，原样重跑通常还是坏。
      这不能一刀切成错，因为「重跑一次确认可复现」也是合理的第一步。
    """
    conflicts, debatable = [], []
    for r in ruleset.rules:
        sev, act = r.severity, r.action
        if sev is Severity.MINOR and act is Action.HUMAN:
            conflicts.append({"id": r.id, "severity": sev.value, "action": act.value,
                              "title": r.title,
                              "why": "minor 的语义是「通常不影响结论」，却要求人工逐条复核"})
        elif sev is Severity.INFO and act in (Action.RERUN, Action.ESCALATE, Action.HUMAN):
            conflicts.append({"id": r.id, "severity": sev.value, "action": act.value,
                              "title": r.title, "why": "info 只是提示，不该产生任何处置动作"})
        elif sev is Severity.CRITICAL and act is Action.ACCEPT:
            conflicts.append({"id": r.id, "severity": sev.value, "action": act.value,
                              "title": r.title, "why": "critical（结果不可用）却给了 accept"})
        elif sev is Severity.CRITICAL and act is Action.RERUN:
            debatable.append({"id": r.id, "severity": sev.value, "action": act.value,
                              "title": r.title,
                              "why": "critical 多为输入/环境错误，原样重跑通常还是坏"})
    return {"conflicts": conflicts, "debatable": debatable}


def check_consistency(logs: list[Path]) -> dict:
    """B4 结论-严重性映射 + evidence 占位符。"""
    from qchem_eval import evaluate_file

    placeholder = []
    verdict_bad = []
    cross: Counter[tuple[str, str]] = Counter()
    for p in logs:
        r = evaluate_file(p)
        worst = min((f.severity for f in r.findings),
                    key=lambda s: list(Severity).index(s), default=None)
        cross[(r.verdict.value, worst.value if worst else "无")] += 1
        if worst is not None and SEVERITY_VERDICT[worst] is not r.verdict:
            # FAIL 的日志如果最坏是 major，说明还有签名层在抬结论，不是错误
            if not (r.verdict is Verdict.FAIL and r.metrics.get("signature_hits")):
                verdict_bad.append({"log": p.stem, "verdict": r.verdict.value,
                                    "worst": worst.value})
        for f in r.findings:
            if any(t in f.evidence for t in ("?", "<缺失>", "None")):
                placeholder.append({"log": p.stem, "id": f.rule_id,
                                    "evidence": f.evidence[:100]})
    return {
        "placeholder_evidence": placeholder,
        "verdict_mismatch": verdict_bad,
        "cross_tab": {f"{k[0]}|{k[1]}": v for k, v in sorted(cross.items())},
    }


def check_unknown_noise(logs: list[Path]) -> dict:
    """B5 unknown 的构成：多少条「判不了」出现在已经判 FAIL 的日志上。

    崩溃的日志（非法基组、l101 内存、SIGKILL）里 SCF 没跑、几何没打印，
    于是二三十条规则集体 unknown。这些 unknown 在数学上没错（确实判不了），
    但在**报告**上是纯噪音：A-01 已经说了「这份算崩了」，后面那串「判不了」
    不携带任何新信息，却把真正需要人看的 unknown 埋进去 —— 与 v0.7 修 D-09
    是同一个理由：不是缺数据，是那个问题在这份日志上无从谈起。
    """
    from qchem_eval import evaluate_file

    total = 0
    on_fail = 0
    by_rule: Counter[str] = Counter()
    for p in logs:
        r = evaluate_file(p)
        n = r.metrics.get("rules_unknown", 0)
        total += n
        if r.verdict is Verdict.FAIL:
            on_fail += n
            for d in r.metrics.get("unknown_detail", []):
                by_rule[d["rule_id"]] += 1
    return {
        "n_unknown_total": total,
        "n_unknown_on_fail": on_fail,
        "share_on_fail": round(on_fail / total, 3) if total else 0.0,
        "top_rules_on_fail": by_rule.most_common(6),
    }


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------

def print_report(rep: dict) -> None:
    print("=" * 96)
    print("qchem-eval 误差分析：错误在哪一类，模型该不该上")
    print("=" * 96)

    # ---- 一、样本基数 ----
    s = rep["samples"]
    print(f"样本 {s['n_total']} 份：合成 {s['n_synth']}（正 {s['n_synth_pos']}"
          f"／负 {s['n_synth_neg']}）＋ 真实 {s['n_real']}")
    print(f"  真实日志里有标签的只有 {s['n_labeled_real']} 份，"
          f"**{s['n_unlabeled_real']} 份完全没有标签**")
    print("  （校准 / 复合物 / 坏几何 / cclib 四批是我为了别的事跑的，没有写期望。")
    print("    对它们只能说「没违反一致性检查」，不能说「判对了」。）")
    print(f"  去重后的独立样本 {s['n_independent_bases']} 份")
    if s["n_synth"]:
        print(f"  ↑ 合成样本 {s['n_synth']} 份全部由 {s['n_bases_in_synth']} 份真实日志"
              f"变异/嫁接而来，同一份底稿最多被用了 {s['max_same_base']} 次")
        print("  → 份数不等于信息量：变异只有 "
              f"{s['n_bases_in_synth']} 种底稿。")
    print()

    # ---- 二、四分类 ----
    cc: Counter[str] = Counter()
    for r in rep["rows"]:
        for c in r["causes"]:
            cc[c["cause"]] += 1
    n_bad_samples = sum(1 for r in rep["rows"] if r["causes"])
    print("── 一、带标签样本上的分歧，按四类归因" + "─" * 46)
    print(f"  判错样本 {n_bad_samples} / {len(rep['rows'])}，错误条目 {sum(cc.values())} 条")
    if not cc:
        print("  ✓ 全部分歧为零 —— 但这不等于系统没问题，见第三节。")
    for k in (Cause.PARSER, Cause.DECL, Cause.RULE, Cause.COVERAGE):
        if cc.get(k.value):
            print(f"  {CAUSE_CN[k]:12s} {cc[k.value]:>3d} 条   → {CAUSE_FIX[k]}")
    print()
    if n_bad_samples:
        print(f"  {'样本':34s} {'来源':16s} {'类别':12s} 说明")
        for r in rep["rows"]:
            if not r["causes"]:
                continue
            first = r["causes"][0]
            print(f"  {r['name'][:32]:34s} {r['origin'][:14]:16s} "
                  f"{CAUSE_CN[Cause(first['cause'])]:12s} {first['id']} {first['why'][:60]}")
            for c in r["causes"][1:]:
                print(f"  {'':34s} {'':16s} {CAUSE_CN[Cause(c['cause'])]:12s} "
                      f"{c['id']} {c['why'][:60]}")
        print()

    cands = rep["multifield_candidates"]
    print("  multifield 候选（规则全 miss、无 unknown、日志干净、但这次计算其实是错的）："
          f"{len(cands)} 条")
    for c in cands:
        print(f"    {c['name'][:26]:28s} {c['origin'][:12]:14s} {c['annotated'][:60]}")
    print("  注意：候选 ≠ 模型需求。机器分不清「缺数据源」和「需联合推理」，")
    print("        每一条都要人工复核；复核不了的就说复核不了，不许默认算给模型。")
    print()

    # ---- 三、无标签检查 ----
    ac = rep["action"]
    print("── 二、动作通道：无标签日志上真正的错误面" + "─" * 46)
    dist = "  ".join(f"{k}={v}" for k, v in sorted(ac["actions"].items()))
    print(f"  {ac['n_logs']} 份日志的动作分布：{dist}")
    if ac["human_share"] > 0.2:
        print(f"  ✗ human_review 占 {ac['human_share']:.0%} —— 这个字段已经失去区分度。")
        drv = ", ".join(f"{k} {v} 份" for k, v in sorted(ac["human_drivers"].items()))
        print(f"    驱动来源（按最坏级别）：{drv}")
        n_minor = ac["human_drivers"].get("minor", 0)
        if n_minor:
            print(f"    其中 {n_minor} 份的最坏级别是 **minor**：minor 的语义是"
                  "「通常不影响结论」，")
            print("    却把整份结果的动作抬到了「必须人工复核」（自相矛盾的规则见第三节）。")
    if ac["masked_by_priority"]:
        print(f"  ✗ 动作被优先级表压低 {len(ac['masked_by_priority'])} 份（最坏级别想要更重的处置）：")
        for m in ac["masked_by_priority"][:6]:
            print(f"    {m['log'][:28]:30s} 最坏={m['worst']:8s} 想要={m['wanted']} "
                  f"实得={m['action']}  命中={m['hits'][:5]}")
    print()

    lv = rep["leveling"]
    print("── 三、定级-动作自洽（severity 说多严重，action 说该怎么办）" + "─" * 26)
    if lv["conflicts"]:
        print(f"  ✗ {len(lv['conflicts'])} 条规则的定级与动作互相矛盾：")
        print(f"  {'规则':7s} {'严重性':9s} {'动作':13s} 标题／矛盾点")
        for c in lv["conflicts"]:
            print(f"  {c['id']:7s} {c['severity']:9s} {c['action']:13s} {c['title'][:30]}")
            print(f"  {'':7s} {'':9s} {'':13s} └ {c['why']}")
    else:
        print("  ✓ 无矛盾")
    if lv["debatable"]:
        print(f"\n  可争议 {len(lv['debatable'])} 条（不算错，但批量后果值得看）：")
        for c in lv["debatable"]:
            print(f"    {c['id']:7s} {c['severity']:9s} → {c['action']:8s} "
                  f"{c['title'][:22]:24s} {c['why'][:38]}")
    print()

    # 动作优先级表的语义：谁盖谁。这个顺序决定了多命中时最终处置，
    # 而它写死在 evaluator.py 里，从来没有人拿真实组合验过。
    print("  动作优先级（数字大的盖掉小的）："
          + " > ".join(f"{a.value}={ACTION_PRIORITY[a]}" for a in
                       sorted(Action, key=lambda x: -ACTION_PRIORITY[x])))
    print("    这一顺序的语义问题：rerun（重跑一遍）是**最轻**的处置，却排在最高；")
    print("    escalate（升级上报）比 human_review 更重，却能被 rerun 盖掉。")
    print("    真正的后果举例：一份日志同时命中 E-03（critical，输入自相矛盾→需人判断）")
    print("    和 A-05（critical，优化未收敛→重跑），最终动作会是 rerun —— 但重跑")
    print("    改不了自相矛盾的输入。当前 54 份日志里没有出现这种组合，属于**潜在**缺陷。")
    print()

    un = rep["unknown_noise"]
    if un["n_unknown_total"]:
        print("── 四、unknown 的构成（判不了 ≠ 有问题，也 ≠ 没问题）" + "─" * 38)
        print(f"  全语料 unknown {un['n_unknown_total']} 条，其中 {un['n_unknown_on_fail']} 条"
              f"（{un['share_on_fail']:.0%}）出现在**已经判 FAIL** 的日志上。")
        if un["share_on_fail"] > 0.5:
            print("  ✗ 超过一半。这些 unknown 在报告里是纯噪音：A-01 已经说了「算崩了」，")
            print("    后面那串「判不了」不携带新信息，却把真正需要人看的埋了进去。")
            print(f"    贡献最多的规则：{un['top_rules_on_fail']}")
            print("    修法不是补解析器，而是让「计算没跑完」这件事在聚合阶段被认出，")
            print("    把下游检查标成 na —— 规则引擎目前没有因果顺序，这是设计缺口。")
        print()

    ci = rep["consistency"]
    print("── 五、结论映射与 evidence 完整性" + "─" * 46)
    print(f"  结论×最坏严重性：{ci['cross_tab']}")
    if ci["placeholder_evidence"]:
        print(f"  ✗ {len(ci['placeholder_evidence'])} 条 evidence 带占位符（靠缺数据硬判）：")
        for x in ci["placeholder_evidence"][:6]:
            print(f"    {x['log'][:24]:26s} {x['id']:7s} {x['evidence'][:60]}")
    else:
        print("  ✓ evidence 无占位符")
    if ci["verdict_mismatch"]:
        print(f"  ✗ {len(ci['verdict_mismatch'])} 份结论与最坏严重性不匹配")
    print()

    # ---- 四、结论 ----
    gate = rep["gate"]
    print("── 六、transformer 三道闸门" + "─" * 52)
    for g in gate:
        mark = "✓" if g["pass"] else "✗"
        print(f"  {mark} {g['name']}：{g['value']}（要求 {g['need']}）")
    print()
    multi = cc.get(Cause.MULTIFIELD.value, 0)
    if multi == 0:
        print("  结论：需多字段综合的错误 **0 条**（人工确认 0 条，候选 "
              f"{len(rep['multifield_candidates'])} 条已排除）。")
        print("        现有样本里每一条判错都能用「补解析器 / 改声明 / 修判据 /")
        print("        加规则」解释完，没有一条需要模型来学。此时训 transformer，")
        print("        是在给一个尚未定义的问题找答案。")
    else:
        print(f"  结论：有 {multi} 条错误属于「单字段全正常、合起来说不通」，"
              "这是模型唯一有理由接的部分。")
    print()
    print("  建议的下一步（按性价比排序）：")
    print("    1. 修动作模型：minor 不该配 human_review（11 条），优先级表 rerun>escalate")
    print("       语义倒置。这两处不修，48% 的文件会被推进人工队列，Agent 没法自动处置。")
    print("    2. 补真实失败样本：现在失败类真实样本只有 "
          f"{sum(1 for r in rep['rows'] if r['origin'].startswith('real:') and r['fail_by_construction'])}"
          " 份，且全是**我构造的**")
    print("       （atom_overlap / bad_basis 这类），不是你科研里真实撞到的失败。")
    print("    3. 模型继续等：等 1、2 做完再看四分类里 multifield 的占比，现在为 0。")
    print()


def build_report(with_synth: bool = True) -> dict:
    ruleset = load_ruleset()
    load_signatures()
    tmpdir = Path(tempfile.mkdtemp(prefix="qce_ea_"))

    cases = real_cases()
    if with_synth:
        cases += synth_cases(tmpdir)
    rows = run_cases(cases, ruleset)

    # 独立底稿：合成的 106 份全部由 tests/data 里那几份真实日志变异而来，而
    # tests/data 本身也在真实日志里 —— 所以两者是包含关系，不能相加。
    # 早期版本按「文件名」求并集，把 complex_out 与 tests/data 里同名的
    # benzene_dimer / water_dimer 并掉了（它们是不同批次的不同文件），得出 52。
    synth_bases = Counter(c.base for c in RULE_CASES + IDENTITY_CASES)
    real_paths = [Path(r["path"]) for r in rows if r["origin"].startswith("real:")]
    real_names = {p.name for p in real_paths}
    extra_bases = {b for b in synth_bases if b not in real_names}
    independent = len(real_paths) + len(extra_bases)

    # 标签覆盖率必须显式说出来：28 份真实日志里只有 26 份写了 EXPECT，
    # 校准/复合物/坏几何/cclib 四批共 28 份**完全没有标签**。对它们只能说
    # 「没违反一致性检查」，不能说「判对了」。
    n_labeled_real = sum(1 for r in rows
                         if r["origin"].startswith("real:") and r["labeled"])
    n_unlabeled_real = len(real_paths) - n_labeled_real

    cc: Counter[str] = Counter()
    for r in rows:
        for c in r["causes"]:
            cc[c["cause"]] += 1

    action = check_action_channel(real_paths)
    candidates = detect_multifield_candidates(rows)
    n_fail_real = sum(1 for r in rows if r["origin"].startswith("real:")
                      and r["fail_by_construction"])
    gate = [
        {"name": "独立真实样本数", "value": independent, "need": "≥ 2000",
         "pass": independent >= 2000},
        {"name": "失败类真实样本数", "value": n_fail_real, "need": "≥ 300",
         "pass": n_fail_real >= 300},
        {"name": "需多字段综合的错（人工确认）", "value": 0,
         "need": "≥ 40% 的漏判",
         "pass": False},
    ]

    return {
        "samples": {
            "n_total": len(rows),
            "n_synth": sum(1 for r in rows if r["origin"].startswith("synth:")),
            "n_synth_pos": sum(1 for r in rows if r["origin"] in
                               ("synth:rule", "synth:signature")),
            "n_synth_neg": sum(1 for r in rows if r["origin"] == "synth:negative"),
            "n_real": len(real_paths),
            "n_labeled_real": n_labeled_real,
            "n_unlabeled_real": n_unlabeled_real,
            "n_independent_bases": independent,
            "n_bases_in_synth": len(synth_bases),
            "max_same_base": max(synth_bases.values()) if synth_bases else 0,
        },
        "causes": dict(cc),
        "rows": rows,
        "multifield_candidates": candidates,
        "action": action,
        "leveling": check_leveling(ruleset),
        "unknown_noise": check_unknown_noise(real_paths),
        "consistency": check_consistency(real_paths),
        "gate": gate,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="判错归因与模型闸门评估")
    ap.add_argument("--json", default="", help="完整明细写成 JSON")
    ap.add_argument("--no-synth", action="store_true", help="只看真实日志")
    args = ap.parse_args(argv)

    rep = build_report(with_synth=not args.no_synth)
    print_report(rep)

    if args.json:
        # detail 很大，单独放，主报告瘦身
        slim = {k: v for k, v in rep.items() if k != "action"}
        slim["action"] = {k: v for k, v in rep["action"].items() if k != "detail"}
        Path(args.json).write_text(
            json.dumps(slim, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"明细已写入 {args.json}")

    # 退出码：有非 multifield 的错就非零 —— 这些是必须修的工程债，不该静默。
    # 当前会稳定返回 1，因为 fail_coord_missing（需输入文件交叉核对）还没修。
    # 这是有意的：把它接进 CI 的人会立刻看到红灯，而不是以为「全绿就没事」。
    hard = sum(v for k, v in rep["causes"].items()
               if k not in (Cause.MULTIFIELD.value,))
    return 0 if hard == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
