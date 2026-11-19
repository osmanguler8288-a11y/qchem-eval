"""L4 决策层：把 findings 收束成唯一的 EvalResult。

做三件事：
  1. 计时（解析 vs 判定分开算，性能优化时才知道该优化哪一段）
  2. 聚合（最坏优先，UNKNOWN 不参与「通过」的判定）
  3. 定 family —— 阈值要按体系调档，family 就是档位的 key
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from .obs import current_span, current_trace, get_logger, log_event
from .parsers import parse
from .rules import (
    ACTION_PRIORITY,
    HARTREE_TO_KCAL,
    aggregate,
    describe_missing,
    load_ruleset,
    run_rules,
)
from .rules.signatures import load_signatures, match as match_signatures
from .schema import Action, CalcData, EvalResult, Finding, Severity, Verdict

_log = get_logger("engine")

# 单份解析超过这个时长记一条 warning。
# 汇总里给的是 p95 —— 平均值会被一份超大日志完全带偏；但 p95 也只是个数字，
# 想定位到具体是哪份文件慢，还得靠这条逐文件的 warning。
SLOW_PARSE_MS = 1500.0


def derive_family(d: CalcData) -> str:
    """体系档位。用途：同一套规则在不同 family 上用不同阈值（v2 的档位表按这个 key 查）。"""
    sys_kind = "TM" if d.has_transition_metal else ("organic" if d.atomic_numbers else "unknown")
    spin = "open" if d.is_open_shell else "closed"
    stages = "+".join(d.stages) if d.stages else "unknown"
    method = f"{(d.functional or '?')}/{(d.basis or '?')}".lower()
    return f"{d.software}:{stages}:{sys_kind}:{spin}:{method}"


def _evaluate(d: CalcData, t_parse_ms: float, n_lines: int) -> EvalResult:
    # 规则集与签名库的加载/编译只做一次，不能算进单次判定耗时
    # （否则第一次跑总是十几毫秒，签名库的 YAML 解析占了 13ms）
    rs = load_ruleset()
    load_signatures()
    t0 = time.perf_counter()
    outcomes = run_rules(d, rs)
    sig_findings = match_signatures(d)
    verdict, score, action, abstain = aggregate(outcomes, sig_findings)
    t_eval_ms = (time.perf_counter() - t0) * 1000

    findings = [o.finding for o in outcomes if o.finding is not None]
    findings.extend(sig_findings)
    findings.sort(key=lambda f: list(Severity).index(f.severity))

    n_hit = sum(1 for o in outcomes if o.status == "hit")
    n_unknown = sum(1 for o in outcomes if o.status == "unknown")
    n_na = sum(1 for o in outcomes if o.status == "na")
    n_total = len(outcomes)
    applicable = n_total - n_na

    log_event(
        _log, "engine.rules.done",
        n_total=n_total, applicable=applicable, na=n_na,
        hit=n_hit, unknown=n_unknown,
        coverage=round((applicable - n_unknown) / applicable, 3) if applicable else 0.0,
        ms=round(t_eval_ms, 2),
        # 判不了的规则 ID 全列出来。它们不是"没问题"，是"没判" ——
        # 批量跑的时候这一行直接指出哪些检查项在这批数据上根本不生效。
        unknown_rules=[o.rule.id for o in outcomes if o.status == "unknown"],
    )
    if sig_findings:
        log_event(_log, "engine.signature.hits",
                  ids=[f.rule_id for f in sig_findings])

    log_event(
        _log, "engine.result",
        verdict=verdict.value, score=score, action=action.value,
        abstain=abstain, findings=[f.rule_id for f in findings],
        # 只有 FAIL 抬到 WARNING 级：默认级别下 stderr 应当只在「这份不能用」
        # 或者「本工具自己出问题」时才响。pass_with_warning 在报告里已经写了，
        # 日志再重复一遍，300 份批量就是 300 行噪音，用户会直接把日志关掉。
        level=logging.WARNING if verdict is Verdict.FAIL else logging.INFO,
    )

    return EvalResult(
        source=d.source,
        verdict=verdict,
        score=score,
        family=derive_family(d),
        action=action,
        findings=findings,
        metrics={
            "rules_total": n_total,
            "rules_applicable": applicable,
            "rules_na": n_na,
            "rules_decided": applicable - n_unknown,
            "rules_hit": n_hit,
            "rules_unknown": n_unknown,
            "coverage": round((applicable - n_unknown) / applicable, 3) if applicable else 0.0,
            "unknown_detail": describe_missing(outcomes),
            "na_rules": [o.rule.id for o in outcomes if o.status == "na"],
            "signature_hits": [f.rule_id for f in sig_findings],
            "error_blocks": len(d.error_blocks),
            "parse_warnings": d.parse_warnings,
            "n_lines": n_lines,
            "summary": d.summary(),
        },
        timing={"parse": t_parse_ms, "rules": t_eval_ms},
        abstain=abstain,
        provenance={
            "parser": f"{d.software}/single-pass",
            "catalog_version": rs.version,
            "n_rules": n_total,
            # 与日志里同一个 trace/span：报告落库之后，还能靠它把当初那几行
            # 日志捞出来。没有这两个 id 的日志只是一堆没主的时间戳。
            "trace_id": current_trace(),
            "span_id": current_span(),
        },
    )


def evaluate_file(path: str | Path) -> EvalResult:
    """评测一份输出文件。

    解析是整条链路里唯一接触文件的环节，也是最容易出问题的地方（格式不认识、
    编码坏掉、日志被 append 过）。所以这里把「解析器看到了什么」记全：
    行数、耗时、它自己承认没解析出来的字段。
    """
    t0 = time.perf_counter()
    d, n_lines = parse(path)
    t_parse_ms = (time.perf_counter() - t0) * 1000

    name = str(path)
    log_event(
        _log, "engine.parse.done",
        path=name, n_lines=n_lines, ms=round(t_parse_ms, 1),
        mlines_per_s=round(n_lines / t_parse_ms / 1000, 2) if t_parse_ms > 0 else 0.0,
        software=d.software,
    )

    if d.software == "unknown":
        # 不是「文件不存在」这种明确错误，而是「读到了但认不出来」——
        # 这条如果不记 warning，报告只会显示一堆 UNKNOWN，看起来像用户的日志有问题。
        log_event(_log, "engine.parse.unrecognised", level=logging.WARNING,
                  path=name, warnings=d.parse_warnings[:3])
    if d.unparsed_hits:
        # 解析器自己承认没吃下的字段。这是**我方**的缺陷信号，必须显式记出来，
        # 不能只躺在 metrics.parse_warnings 里等有人去翻。
        log_event(_log, "engine.parser.gap", level=logging.WARNING,
                  path=name, n=d.unparsed_hits,
                  fields=sorted({h["field"] for h in d.unparsed_hits}))
    if t_parse_ms > SLOW_PARSE_MS:
        log_event(_log, "engine.parse.slow", level=logging.WARNING,
                  path=name, ms=round(t_parse_ms, 1), n_lines=n_lines,
                  mlines_per_s=round(n_lines / t_parse_ms / 1000, 2))

    return _evaluate(d, t_parse_ms, n_lines)


# --------------------------------------------------------------------------
# 跨阶段一致性：所有开源工具（QMCert/atomate/AiiDA）都不做的事
# --------------------------------------------------------------------------

def evaluate_pair(path_a: str | Path, path_b: str | Path) -> EvalResult:
    """比较两份计算（典型：opt 末步 vs 单点，或 opt vs freq）。

    单文件自检只能发现「这次计算自己有问题」，发现不了「两次计算之间不自洽」。
    而真实工作流恰恰是串起来的——这是本系统相对现有工具的主要增量。
    """
    t0 = time.perf_counter()
    da, lines_a = parse(path_a)
    db, lines_b = parse(path_b)
    t_parse_ms = (time.perf_counter() - t0) * 1000

    rs = load_ruleset()
    pair_cfg = {r["id"]: r for r in rs.pair_rules}
    findings: list[Finding] = []
    checks: dict[str, str] = {}

    # --- D-06 方法一致性 ---
    cfg = pair_cfg.get("D-06", {})
    if da.functional and db.functional and da.basis and db.basis:
        same_method = (
            da.functional.lower() == db.functional.lower()
            and da.basis.lower() == db.basis.lower()
            and (da.dispersion or "") == (db.dispersion or "")
        )
        if not same_method:
            findings.append(
                Finding(
                    rule_id="D-06",
                    category=cfg.get("category", "D-数值一致性"),
                    severity=Severity(cfg.get("severity", "critical")),
                    title=cfg.get("title", "跨阶段方法不一致"),
                    evidence=(
                        f"A: {da.functional}/{da.basis}（色散={da.dispersion or '无'}）  "
                        f"B: {db.functional}/{db.basis}（色散={db.dispersion or '无'}）"
                    ),
                    fix_hint=cfg.get("fix", "").strip(),
                    action=Action.HUMAN,
                )
            )
            checks["method"] = "mismatch"
        else:
            checks["method"] = "same"
    else:
        checks["method"] = "unknown"

    # --- D-05 能量一致性 ---
    cfg = pair_cfg.get("D-05", {})
    tol = float(cfg.get("tol_kcal", 1.0))
    ea, eb = da.last_scf_energy, db.last_scf_energy
    if ea is not None and eb is not None:
        de_kcal = (ea - eb) * HARTREE_TO_KCAL
        checks["dE_kcal"] = f"{de_kcal:+.2f}"
        if abs(de_kcal) > tol:
            findings.append(
                Finding(
                    rule_id="D-05",
                    category=cfg.get("category", "D-数值一致性"),
                    severity=Severity(cfg.get("severity", "major")),
                    title=cfg.get("title", "跨阶段能量不一致"),
                    evidence=(
                        f"A = {ea:.8f} Ha，B = {eb:.8f} Ha，"
                        f"差值 {de_kcal:+.2f} kcal/mol（容差 {tol} kcal/mol）。"
                    ),
                    fix_hint=cfg.get("fix", "").strip(),
                    action=Action.HUMAN,
                )
            )
            checks["energy"] = "mismatch"
        else:
            checks["energy"] = "ok"
    else:
        checks["energy"] = "unknown"

    # --- 各自再跑一遍单文件规则，合起来给结论 ---
    outcome_a = run_rules(da, rs)
    outcome_b = run_rules(db, rs)
    base_verdict_a, _, _, _ = aggregate(outcome_a)
    base_verdict_b, _, _, _ = aggregate(outcome_b)
    checks["verdict_a"] = base_verdict_a.value
    checks["verdict_b"] = base_verdict_b.value

    for o in outcome_a + outcome_b:
        if o.finding is not None:
            findings.append(o.finding)
    findings.extend(match_signatures(da))
    findings.extend(match_signatures(db))

    # 两份文件都会有 E-05 这类通用提示，去重免得同一个问题报两遍
    seen: set[tuple[str, str]] = set()
    deduped: list[Finding] = []
    for f in findings:
        key = (f.rule_id, f.evidence)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(f)
    findings = deduped
    findings.sort(key=lambda f: list(Severity).index(f.severity))

    score = 100 - sum(
        {Severity.CRITICAL: 40, Severity.MAJOR: 15, Severity.MINOR: 5, Severity.INFO: 0}[f.severity]
        for f in findings
    )
    score = max(0, score)

    if any(f.severity is Severity.CRITICAL for f in findings):
        verdict = Verdict.FAIL
    elif any(f.severity is Severity.MAJOR for f in findings):
        verdict = Verdict.WARNING
    elif any(f.severity is Severity.MINOR for f in findings):
        verdict = Verdict.WARNING
    elif checks["energy"] == "ok" and checks["method"] == "same":
        verdict = Verdict.PASS
    else:
        verdict = Verdict.UNKNOWN

    action = Action.ACCEPT
    for f in findings:
        if ACTION_PRIORITY[f.action] > ACTION_PRIORITY[action]:
            action = f.action
    if verdict is Verdict.FAIL and action is Action.ACCEPT:
        action = Action.HUMAN

    return EvalResult(
        source=f"{Path(path_a).name} <-> {Path(path_b).name}",
        verdict=verdict,
        score=score,
        family="cross-stage",
        action=action,
        findings=findings,
        metrics={
            "pair_checks": checks,
            "n_lines": lines_a + lines_b,
            "summary_a": da.summary(),
            "summary_b": db.summary(),
        },
        timing={"parse": t_parse_ms, "rules": 0.0},
        abstain=verdict is Verdict.UNKNOWN,
        provenance={
            "parser": "gaussian/single-pass",
            "catalog_version": rs.version,
            "trace_id": current_trace(),
            "span_id": current_span(),
            "pair": [str(path_a), str(path_b)],
        },
    )


__all__ = ["derive_family", "evaluate_file", "evaluate_pair"]
