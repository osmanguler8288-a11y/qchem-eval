"""L4 决策层：把 findings 收束成唯一的 EvalResult。

做三件事：
  1. 计时（解析 vs 判定分开算，性能优化时才知道该优化哪一段）
  2. 聚合（最坏优先，UNKNOWN 不参与「通过」的判定）
  3. 定 family —— 阈值要按体系调档，family 就是档位的 key
"""

from __future__ import annotations

import time
from pathlib import Path

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
        },
    )


def evaluate_file(path: str | Path) -> EvalResult:
    """评测一份输出文件。"""
    t0 = time.perf_counter()
    d, n_lines = parse(path)
    t_parse_ms = (time.perf_counter() - t0) * 1000
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
        provenance={"parser": "gaussian/single-pass", "catalog_version": rs.version},
    )


__all__ = ["derive_family", "evaluate_file", "evaluate_pair"]
