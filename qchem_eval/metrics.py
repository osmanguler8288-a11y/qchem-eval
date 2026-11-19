"""批量运行的汇总指标。

单份报告回答「这一份能不能用」，汇总回答「这一批到底怎么样」。后端真正会看的
是后者：跑 300 份之后，需要知道的不是第 147 份命中哪条规则，而是

  · 多少份不该用（FAIL/UNKNOWN），多少份该转人工
  · 哪条规则老在报 UNKNOWN —— 这几乎总是解析器的缺口，而不是用户的错
  · 哪条规则命中最多 —— 如果 C-08 占了八成的命中，说明这批计算的基组整体偏小
  · 解析耗时分布 —— 平均值会被一份超大日志完全带偏，所以给 p95
  · 有几个文件直接处理失败 —— 它们不进 results，不汇总就会被静默丢掉

这里全是纯函数：不读文件、不打日志，方便单测，也方便从别处（服务端）复用。
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from .schema import EvalResult, Severity

# 分数分档。0-59 基本等于「不可用」，100 是「一条问题都没有」
_SCORE_BUCKETS = [(0, 59), (60, 69), (70, 79), (80, 89), (90, 99), (100, 100)]


def _p95(values: Sequence[float]) -> float:
    """95 分位。n 小的时候退化成最大值，不会因为样本少而虚低。"""
    if not values:
        return 0.0
    s = sorted(values)
    idx = max(0, min(len(s) - 1, int(round(0.95 * (len(s) - 1)))))
    return s[idx]


def summarize(
    results: Sequence[EvalResult],
    errors: Iterable[dict[str, Any]] = (),
    wall_ms: float = 0.0,
    trace_id: str = "-",
) -> dict[str, Any]:
    """把一批 EvalResult 收成一个 dict（可直接 json.dumps）。

    errors 是「压根没跑出结果」的文件，必须单独统计：把它们混进 results 会
    变成一次 PASS，静默丢掉又会让「12 个文件只有 11 个跑成功」这件事消失。
    """
    results = list(results)
    errors = list(errors)
    n = len(results)

    verdicts = {"pass": 0, "pass_with_warning": 0, "fail": 0, "unknown": 0}
    actions: dict[str, int] = {}
    scores: list[int] = []
    parse_ms: list[float] = []
    rules_ms: list[float] = []
    coverages: list[float] = []
    n_lines = 0

    hit_by_rule: dict[str, int] = {}
    hit_by_severity = {s.value: 0 for s in Severity}
    unknown_by_rule: dict[str, int] = {}
    n_unknown = 0
    n_signature_hits = 0
    n_parse_warnings = 0
    files_with_parse_warnings = 0

    for r in results:
        verdicts[r.verdict.value] = verdicts.get(r.verdict.value, 0) + 1
        actions[r.action.value] = actions.get(r.action.value, 0) + 1
        scores.append(r.score)
        parse_ms.append(float(r.timing.get("parse", 0.0)))
        rules_ms.append(float(r.timing.get("rules", 0.0)))

        m = r.metrics
        if m.get("coverage") is not None:
            coverages.append(float(m["coverage"]))
        n_lines += int(m.get("n_lines") or 0)

        for f in r.findings:
            hit_by_rule[f.rule_id] = hit_by_rule.get(f.rule_id, 0) + 1
            hit_by_severity[f.severity.value] += 1

        for d in m.get("unknown_detail") or []:
            rid = d.get("rule_id", "?")
            unknown_by_rule[rid] = unknown_by_rule.get(rid, 0) + 1
            n_unknown += 1

        n_signature_hits += len(m.get("signature_hits") or [])

        pw = m.get("parse_warnings") or []
        if pw:
            files_with_parse_warnings += 1
            n_parse_warnings += len(pw)

    total_parse_s = sum(parse_ms) / 1000.0

    summary: dict[str, Any] = {
        "trace_id": trace_id,
        "n_files": n + len(errors),
        "n_ok": n,
        "n_error": len(errors),
        "verdicts": verdicts,
        "actions": actions,
        "score": {
            "mean": round(sum(scores) / n, 1) if n else 0.0,
            "min": min(scores) if n else 0,
            "median": sorted(scores)[n // 2] if n else 0,
            "histogram": _histogram(scores),
        },
        "timing_ms": {
            "wall": round(wall_ms, 1),
            "parse_total": round(sum(parse_ms), 1),
            "parse_mean": round(sum(parse_ms) / n, 1) if n else 0.0,
            "parse_p95": round(_p95(parse_ms), 1),
            "parse_max": round(max(parse_ms), 1) if parse_ms else 0.0,
            "rules_total": round(sum(rules_ms), 1),
        },
        "throughput": {
            "n_lines": n_lines,
            "mlines_per_s": round(n_lines / total_parse_s / 1e6, 2) if total_parse_s > 0 else 0.0,
        },
        "coverage": {
            "mean": round(sum(coverages) / len(coverages), 3) if coverages else 0.0,
            "min": round(min(coverages), 3) if coverages else 0.0,
        },
        "unknown": {"total": n_unknown, "by_rule": _top(unknown_by_rule)},
        "hits": {
            "total": sum(hit_by_rule.values()),
            "by_rule": _top(hit_by_rule),
            "by_severity": hit_by_severity,
            "signature_hits": n_signature_hits,
        },
        "parse_warnings": {
            "total": n_parse_warnings,
            "files": files_with_parse_warnings,
        },
        "errors": errors,
    }
    return summary


def _histogram(scores: Sequence[int]) -> dict[str, int]:
    out: dict[str, int] = {}
    for lo, hi in _SCORE_BUCKETS:
        label = f"{lo}" if lo == hi else f"{lo}-{hi}"
        out[label] = sum(1 for s in scores if lo <= s <= hi)
    return out


def _top(counts: dict[str, int], k: int = 15) -> dict[str, int]:
    """按次数降序取前 k。全列出来在 300 份的批量里会长到几十行，没人看。"""
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:k])


# ---------------------------------------------------------------- 人读渲染

def render_summary(s: dict[str, Any]) -> str:
    """给终端看的汇总块。刻意比单份报告短，一眼能扫完。"""
    v = s["verdicts"]
    lines = [
        "",
        "─" * 78,
        f" 批量汇总   trace {s['trace_id']}    {s['n_files']} 个文件"
        f"（成功 {s['n_ok']}，处理失败 {s['n_error']}）",
        "─" * 78,
        f" 结论   通过 {v.get('pass', 0)}   有警告 {v.get('pass_with_warning', 0)}"
        f"   不通过 {v.get('fail', 0)}   判不了 {v.get('unknown', 0)}",
        " 动作   " + "   ".join(f"{k} {n}" for k, n in sorted(s["actions"].items())),
        f" 评分   均值 {s['score']['mean']}   中位 {s['score']['median']}"
        f"   最低 {s['score']['min']}",
        f" 耗时   wall {s['timing_ms']['wall']} ms"
        f"   解析合计 {s['timing_ms']['parse_total']} ms"
        f"（均值 {s['timing_ms']['parse_mean']} / p95 {s['timing_ms']['parse_p95']}"
        f" / 最慢 {s['timing_ms']['parse_max']}）",
        f" 吞吐   {s['throughput']['n_lines']} 行"
        f"   {s['throughput']['mlines_per_s']} M 行/秒",
        f" 覆盖率 均值 {s['coverage']['mean']:.0%}   最低 {s['coverage']['min']:.0%}",
    ]

    hist = s["score"]["histogram"]
    lines.append(" 分布   " + "   ".join(f"{k} 分 {n} 份" for k, n in hist.items() if n))

    if s["hits"]["total"]:
        sev = s["hits"]["by_severity"]
        lines.append(
            f" 命中   共 {s['hits']['total']} 条"
            f"（critical {sev['critical']} / major {sev['major']}"
            f" / minor {sev['minor']} / info {sev['info']}）"
            f"   签名层 {s['hits']['signature_hits']} 条"
        )
        for rid, c in list(s["hits"]["by_rule"].items())[:8]:
            lines.append(f"          {rid:<8}{c:>4} 次")

    if s["unknown"]["total"]:
        lines.append(
            f" 判不了 共 {s['unknown']['total']} 处（缺数据，按设计不算通过）"
        )
        for rid, c in list(s["unknown"]["by_rule"].items())[:8]:
            lines.append(f"          {rid:<8}{c:>4} 次")

    if s["parse_warnings"]["total"]:
        lines.append(
            f" 解析告警 {s['parse_warnings']['total']} 条，涉及"
            f" {s['parse_warnings']['files']} 个文件（多半是解析器缺口，不是日志缺数据）"
        )

    if s["errors"]:
        lines.append(f" 处理失败 {s['n_error']} 个：")
        for e in s["errors"][:8]:
            lines.append(f"          {e.get('source')}  {e.get('error')}")

    lines.append("─" * 78)
    return "\n".join(lines)


__all__ = ["render_summary", "summarize"]
