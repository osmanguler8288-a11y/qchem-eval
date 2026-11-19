"""命令行入口。

  python -m qchem_eval judge tests/data/*.out     逐份判定，出中文诊断报告
  python -m qchem_eval judge x.log --json         机器可读
  python -m qchem_eval compare opt.log sp.log     跨阶段一致性
  python -m qchem_eval rules                      打印规则清单
  python -m qchem_eval bench x.out                自研解析器 vs cclib 计时
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .engine import evaluate_file, evaluate_pair
from .rules import RuleCatalogError, load_ruleset
from .schema import EvalResult, Severity, Verdict

# ---------------------------------------------------------------- 终端着色

_USE_COLOR = sys.stdout.isatty() and sys.platform != "win32"


def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOR else text


VERDICT_STYLE = {
    Verdict.PASS: ("通过", "32"),
    Verdict.WARNING: ("通过但有警告", "33"),
    Verdict.FAIL: ("不通过", "31"),
    Verdict.UNKNOWN: ("无法判定", "35"),
}

SEV_STYLE = {
    Severity.CRITICAL: ("CRIT", "31;1"),
    Severity.MAJOR: ("MAJOR", "33;1"),
    Severity.MINOR: ("MINOR", "36"),
    Severity.INFO: ("INFO", "90"),
}

ACTION_LABEL = {
    "accept": "直接采用",
    "rerun": "必须重算",
    "escalate": "升级计算级别",
    "human_review": "人工复核",
}

RULE_W = 78


def _hr(ch: str = "─") -> str:
    return ch * RULE_W


# ---------------------------------------------------------------- 报告渲染

def _findings_block(findings, indent: str = "") -> list[str]:
    out: list[str] = []
    if not findings:
        out.append(" 未发现规则命中项。")
        return out
    out.append(_hr())
    out.append(f" 问题清单（{len(findings)}）")
    out.append(_hr())
    for f in findings:
        tag, code = SEV_STYLE[f.severity]
        out.append(f" [{_c(tag.ljust(5), code)}] {f.rule_id}  {f.title}")
        out.append(f"          依据   {f.evidence}")
        out.append(f"          建议   {f.fix_hint}")
        out.append(f"          动作   {ACTION_LABEL.get(f.action.value, f.action.value)}")
        out.append("")
    return out


def _verdict_line(result: EvalResult) -> str:
    v_text, v_code = VERDICT_STYLE[result.verdict]
    return (
        f" 结论   {_c(v_text, v_code)}      评分 {result.score}/100"
        f"      动作 {_c(ACTION_LABEL.get(result.action.value, result.action.value), '1')}"
    )


def _method_label(s: dict) -> str:
    """体系的方法标签。

    多段作业不能把「首段泛函 / 末段基组」拼成一个方法名：那个组合在文件里
    根本不存在。例如首段 HF/STO-3G、末段 B3LYP/6-31G(d)，拼出来是
    `hf/6-31G(d)` —— 报告第一行就开始编造一个没跑过的计算。
    段间真不一致时改显链式，一眼看出这是几段拼起来的。
    """
    methods = s.get("route_methods") or []
    if len(set(methods)) > 1:
        return " → ".join(methods)
    return f"{s.get('functional') or '?'}/{s.get('basis') or '?'}"


def render(result: EvalResult, show_unknown: bool = True) -> str:
    out: list[str] = []
    m = result.metrics
    s = m.get("summary", {})

    out.append(_hr("═"))
    out.append(f" qchem-eval  判定报告    {Path(result.source).name}")
    out.append(_hr("═"))
    out.append(_verdict_line(result))

    method = _method_label(s)
    out.append(
        f" 体系   {method}   {s.get('natoms') or '?'} 原子"
        f"   电荷 {s.get('charge')}   多重度 {s.get('multiplicity')}"
        f"   阶段 {'+'.join(s.get('stages') or []) or '?'}"
    )
    out.append(f" 档位   {result.family}")

    cov = m.get("coverage", 0.0)
    out.append(
        f" 规则   {m.get('rules_total')} 条 → 适用 {m.get('rules_applicable')} 条"
        f"、判定 {m.get('rules_decided')} 条（覆盖率 {cov:.0%}）"
        f"  命中 {m.get('rules_hit')} 条  无法判定 {m.get('rules_unknown')} 条"
    )

    t = result.timing
    n_lines = m.get("n_lines", 0)
    rate = n_lines / t["parse"] * 1000 / 1e6 if t.get("parse") else 0.0
    out.append(
        f" 耗时   解析 {t.get('parse', 0):.1f} ms / 规则 {t.get('rules', 0):.2f} ms"
        f"（{n_lines} 行，{rate:.2f} M 行/秒）"
    )

    if result.provenance.get("catalog_version"):
        out.append(f" 规则集 v{result.provenance['catalog_version']}")

    out.append("")
    out.extend(_findings_block(result.findings))

    # ---- 未能判定 ----
    unknowns = m.get("unknown_detail", [])
    if show_unknown and unknowns:
        out.append(_hr())
        out.append(f" 未能判定（{len(unknowns)}）—— 数据缺失，按设计不判通过")
        out.append(_hr())
        for u in unknowns:
            miss = "、".join(u["missing"])
            out.append(f" {u['rule_id']}  {u['title']}")
            out.append(f"          缺   {miss}")
        out.append("")

    if result.abstain:
        out.append(
            _c(" ⚠ 缺数据过半，本结论为「无法判定」而非通过。", "35")
        )

    warns = m.get("parse_warnings") or []
    if warns:
        out.append(f" 解析告警：{'；'.join(warns)}")

    out.append(_hr("═"))
    return "\n".join(out)


def render_pair(result: EvalResult) -> str:
    """跨阶段报告。字段结构和单文件不同，不能复用 render()。"""
    m = result.metrics
    checks = m.get("pair_checks", {})
    sa, sb = m.get("summary_a", {}), m.get("summary_b", {})
    out: list[str] = []

    out.append(_hr("═"))
    out.append(f" qchem-eval  跨阶段核对    {result.source}")
    out.append(_hr("═"))

    for tag, s in (("A", sa), ("B", sb)):
        method = _method_label(s)
        out.append(
            f" {tag}  {Path(s.get('source', '')).name:<22} {method:<22} "
            f"{s.get('natoms')} 原子  阶段 {'+'.join(s.get('stages') or []) or '?'}"
            f"  单文件结论 {checks.get(f'verdict_{tag.lower()}', '?')}"
        )

    out.append(_hr())
    out.append(" 核对项")
    out.append(_hr())
    label = {"ok": "一致", "mismatch": "不一致", "same": "一致", "unknown": "无法核对"}
    if "dE_kcal" in checks:
        verdict = checks.get("energy")
        out.append(
            f"   能量一致性   ΔE = {checks['dE_kcal']} kcal/mol"
            f"   → {label.get(verdict, verdict)}"
        )
    else:
        out.append("   能量一致性   → 无法核对（缺能量）")
    out.append(
        f"   方法一致性   → {label.get(checks.get('method'), checks.get('method'))}"
    )
    out.append("")
    out.append(_verdict_line(result))

    out.append("")
    out.extend(_findings_block(result.findings))

    t = result.timing
    out.append(
        f" 耗时   解析 {t.get('parse', 0):.1f} ms（合计 {m.get('n_lines', 0)} 行）"
    )
    out.append(_hr("═"))
    return "\n".join(out)


# ---------------------------------------------------------------- 子命令

def cmd_judge(args: argparse.Namespace) -> int:
    results = []
    for p in args.paths:
        if not Path(p).exists():
            print(f"文件不存在: {p}", file=sys.stderr)
            return 2
        results.append(evaluate_file(p))

    if args.json:
        payload = [r.to_dict() for r in results]
        print(json.dumps(payload if len(payload) > 1 else payload[0],
                         ensure_ascii=False, indent=2))
    else:
        for r in results:
            print(render(r, show_unknown=not args.quiet))

    # 有 FAIL 时返回非 0，方便挂到 CI / Agent 的检查点
    return 1 if any(r.verdict is Verdict.FAIL for r in results) else 0


def cmd_compare(args: argparse.Namespace) -> int:
    r = evaluate_pair(args.a, args.b)
    if args.json:
        print(json.dumps(r.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(render_pair(r))
    return 1 if r.verdict is Verdict.FAIL else 0


def cmd_rules(args: argparse.Namespace) -> int:
    rs = load_ruleset()
    print(f"规则集 v{rs.version}    共 {len(rs.rules)} 条单文件规则"
          f" + {len(rs.pair_rules)} 条跨文件规则\n")
    print(f"{'ID':<7}{'级别':<9}{'类别':<14}{'来源':<12}标题")
    print(_hr())
    for r in rs.rules:
        print(f"{r.id:<7}{r.severity.value:<9}{r.category:<14}{r.tag:<12}{r.title}")
        print(f"{'':<7}{'依赖':<9}{', '.join(r.requires)}")
    print()
    for r in rs.pair_rules:
        print(f"{r['id']:<7}{r.get('severity', 'major'):<9}"
              f"{r.get('category', ''):<14}{r.get('tag', ''):<12}{r.get('title', '')}"
              f"   [跨文件]")
    return 0


def cmd_bench(args: argparse.Namespace) -> int:
    """自研解析器 vs cclib：证明「性能好一点」不是口号。"""
    path = args.path
    if not Path(path).exists():
        print(f"文件不存在: {path}", file=sys.stderr)
        return 2

    from .parsers import parse

    # 预热（首次 import 不算）
    parse(path)

    t0 = time.perf_counter()
    d, n_lines = parse(path)
    t_self = (time.perf_counter() - t0) * 1000

    print(f"文件   {path}")
    print(f"规模   {n_lines} 行 / {Path(path).stat().st_size / 1024:.0f} KB")
    print(_hr())
    print(f" qchem-eval  单遍解析   {t_self:8.2f} ms"
          f"   {n_lines / t_self * 1000 / 1e6:.2f} M 行/秒")

    try:
        import cclib
    except ImportError:
        print(" cclib        未安装，跳过对比（pip install cclib）")
        return 0

    t0 = time.perf_counter()
    data = cclib.io.ccread(path)
    t_cclib = (time.perf_counter() - t0) * 1000
    print(f" cclib        ccopen+parse {t_cclib:8.2f} ms")
    if t_self:
        print(f" 加速比       {t_cclib / t_self:6.1f}x")

    print(_hr())
    print(" 交叉校验（自研 vs cclib；cclib 内部按 eV 存能量，回算会有 1e-5 Ha 级舍入）")
    if data is not None:
        pairs = [
            ("natoms", d.natoms, getattr(data, "natom", None), 0),
            ("charge", d.charge, _first(getattr(data, "charge", None)), 0),
            ("mult", d.multiplicity, _first(getattr(data, "mult", None)), 0),
        ]
        ce = getattr(data, "scfenergies", None)
        if ce is not None and len(ce):
            # 用 cclib 自己的换算因子，避免把两套 Hartree→eV 常数差算成解析错误
            try:
                from cclib.parser.utils import convertor

                factor = convertor(1.0, "eV", "hartree")
            except Exception:  # noqa: BLE001
                factor = 1.0 / 27.211386245988
            pairs.append(("末步能量/Ha", d.last_scf_energy, ce[-1] * factor, 1e-5))
        nfreq = len(getattr(data, "vibfreqs", []) or [])
        if nfreq:
            pairs.append(("频率个数", len(d.freqs), nfreq, 0))

        for name, mine, theirs, tol in pairs:
            if theirs is None or mine is None:
                continue
            same = (
                abs(mine - theirs) <= max(tol, 1e-9)
                if isinstance(mine, float) and isinstance(theirs, float)
                else mine == theirs
            )
            mark = "OK " if same else "!! "
            if isinstance(mine, float):
                detail = f"自研={mine:<18.9f} cclib={theirs:<18.9f}"
            else:
                detail = f"自研={mine!s:<20} cclib={theirs!s:<20}"
            print(f"  {mark}{name:<12} {detail}")
    return 0


def _first(v):
    if isinstance(v, (list, tuple)):
        return v[0] if v else None
    return v


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="qchem-eval",
        description="量子化学计算结果的质量判定（缺数据不等于通过）",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    j = sub.add_parser("judge", help="判定单个（或多个）输出文件")
    j.add_argument("paths", nargs="+")
    j.add_argument("--json", action="store_true", help="输出 JSON")
    j.add_argument("--quiet", action="store_true", help="不显示未判定明细")
    j.set_defaults(func=cmd_judge)

    c = sub.add_parser("compare", help="跨阶段一致性核对")
    c.add_argument("a")
    c.add_argument("b")
    c.add_argument("--json", action="store_true")
    c.set_defaults(func=cmd_compare)

    r = sub.add_parser("rules", help="打印规则清单")
    r.set_defaults(func=cmd_rules)

    b = sub.add_parser("bench", help="解析性能对比（自研 vs cclib）")
    b.add_argument("path")
    b.set_defaults(func=cmd_bench)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except RuleCatalogError as exc:
        # 规则清单本身有错属于**启动期配置错误**，不是评测结论。
        # 用独立的退出码 3 与「有 FAIL 所以返回 1」区分开，否则 CI 会把
        # 「配置写错了」误读成「这批计算质量不合格」。
        print(f"规则清单加载失败：\n{exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
