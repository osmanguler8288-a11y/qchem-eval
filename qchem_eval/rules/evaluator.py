"""规则求值器。

设计要点：
1. 规则全部声明在 catalog.yaml 里，代码里不写死任何阈值 —— 阈值要按体系调档，
   埋进 Python 里就调不动了。
2. `requires` 里任一字段缺失 => UNKNOWN。这是与 QMCert 最关键的分歧：
   QMCert 的 assess_qm_quality(scf_converged: bool = True) 在解析失败时静默判通过。
3. 表达式用 compile() 预编译一次，之后每份日志只是 eval 一个 AST，微秒级。
"""

from __future__ import annotations

import difflib
import dis
from dataclasses import dataclass
from dataclasses import fields as dc_fields
from functools import cached_property, lru_cache
from pathlib import Path
from typing import Any, Callable

import yaml

from ..schema import (
    TM_ATOMIC_NUMBERS,
    Action,
    CalcData,
    Finding,
    Severity,
    Verdict,
)

CATALOG_PATH = Path(__file__).with_name("catalog.yaml")

HARTREE_TO_KCAL = 627.509474

# 严重级别 -> 扣分权重（score 从 100 起扣）
SEVERITY_PENALTY = {
    Severity.CRITICAL: 40,
    Severity.MAJOR: 15,
    Severity.MINOR: 5,
    Severity.INFO: 0,
}

# 严重级别 -> 结论
SEVERITY_VERDICT = {
    Severity.CRITICAL: Verdict.FAIL,
    Severity.MAJOR: Verdict.WARNING,
    Severity.MINOR: Verdict.WARNING,
    Severity.INFO: Verdict.PASS,
}

ACTION_PRIORITY = {
    Action.ACCEPT: 0,
    Action.HUMAN: 1,
    Action.ESCALATE: 2,
    Action.RERUN: 3,
}

# 判定表达式里允许用的内建函数（白名单，不给 open/eval/import）
SAFE_BUILTINS = {
    "len": len,
    "sum": sum,
    "min": min,
    "max": max,
    "abs": abs,
    "any": any,
    "all": all,
    "round": round,
    "sorted": sorted,
    "float": float,
    "int": int,
    "bool": bool,
    "str": str,
}


class RuleCatalogError(ValueError):
    """规则清单本身有错。

    加载期就抛，绝不留给运行期 —— 因为运行期的表现形式是
    「这条规则状态 unknown，但没有任何告警」，比崩掉难查一个数量级：
      · `requires` 拼错字段名 -> missing_fields() 永远报缺失 -> 永久 UNKNOWN
      · `applies` 写错名字   -> eval 抛 NameError -> 被按 na 吞掉 -> 规则等于不存在，
                                而且 na **不计入覆盖率分母**，指标上看不出来
    两种都是"评测系统自己瞎了但报告显示一切正常"，属于最坏的一类失效。
    """


def _suggest(name: str, pool: Any, n: int = 1) -> str:
    """给拼错的标识符一个候选。"你是不是想写 xxx" 能省掉半小时的排查。"""
    close = difflib.get_close_matches(name, sorted(pool), n=n)
    return f"你是不是想写 {close[0]!r}？" if close else ""


# ---- 判定表达式允许引用的名字 ----------------------------------------------
# 白名单 = CalcData 字段（ctx 里由 vars(d) 带进来的）∪ DERIVED_CTX_KEYS ∪
#          {"d", "kcal"} ∪ SAFE_BUILTINS
#
# 维护约定：在 build_context() 里新增一个派生 ctx 键，**必须**同步加到这里，
# 否则引用它的规则会在加载期被误判为"未知名字"。tests/test_rules.py 里有断言
# 拿真实解析结果反查这个集合，漏加会直接让测试红掉。
DERIVED_CTX_KEYS = frozenset({
    "err", "last_cycles", "min_freq", "stages_s", "n_freqs", "n_elems", "n_heavy",
    "gap_ev", "s2_dev", "n_electrons", "tm_zs", "heavy_zs", "n_err", "n_trace",
    "de_kcal", "e_prev", "e_last", "de_up_kcal", "gibbs_minus_zpe_kcal", "opt_table",
    "conv_tol", "conv_tol_num", "conv_tol_src", "conv_last", "conv_ratio",
    "vt_dev", "min_dist", "n_rises", "basis_n", "route_basis_n",
    "n_frag", "frag_gap", "n_route_methods", "route_methods_s",
})

CALC_FIELDS = frozenset(f.name for f in dc_fields(CalcData))
# CalcData 上的 @property 也是合法的 requires 对象，因为 missing_fields() 走的是
# getattr()，属性一样能被取到（例如 min_atom_distance 在原子数不足时返回 None，
# 正好满足"缺数据"语义）。只认 dataclass fields 会把 B-05 误判成拼写错误。
CALC_PROPS = frozenset(
    n for n, v in vars(CalcData).items()
    # cached_property 也是合法的 requires 对象（比 property 多一层缓存），
    # 但它不是 property 的子类，只判 property 会把 B-10 的 n_fragments
    # 当成拼写错误挡在加载期。
    if isinstance(v, (property, cached_property)) and not n.startswith("_")
)
CALC_ATTRS = CALC_FIELDS | CALC_PROPS

# when / applies 里能用裸名读到的，只有 ctx 里真的存在的键 —— 即 vars(d)（字段）
# 与 build_context 显式塞进去的派生键。属性**不在**其中：要读属性必须写 d.xxx，
# 写成裸 min_atom_distance 在运行期会 NameError（build_context 里叫 min_dist）。
_ALLOWED_NAMES = (
    CALC_FIELDS | DERIVED_CTX_KEYS | frozenset(SAFE_BUILTINS) | {"d", "kcal"}
)


def _expr_names(code: Any) -> set[str]:
    """表达式里从**外层作用域读取**的名字，含推导式等嵌套 code object。

    只收 LOAD_NAME / LOAD_GLOBAL，两个原因：
      · 不能用 code.co_names：属性名也在里面（`d.stages` 的 co_names 是
        {'d', 'stages'}，但 'stages' 是 LOAD_ATTR 的操作数，不是变量），
        直接拿它校验会造出一堆假阳。
      · 不收 LOAD_FAST / LOAD_DEREF：那是**表达式内部自己绑定**的名字
        （`any(f != f for f in d.freqs)` 里的 f 就是 LOAD_FAST），
        它们不可能是"外部名字写错"，收进来只会误报。
    所以需要校验的恰好就是"这个名字如果不是我自己定义的，就一定来自作用域"。
    """
    out: set[str] = set()
    for ins in dis.get_instructions(code):
        if ins.opname in ("LOAD_NAME", "LOAD_GLOBAL"):
            name = ins.argval
            # `.0` 是推导式隐式第一个参数的内部名，不是用户写的变量
            if isinstance(name, str) and not name.startswith("."):
                out.add(name)
    for const in code.co_consts:
        if hasattr(const, "co_names"):     # 嵌套 code object：生成器 / 推导式
            out |= _expr_names(const)
    return out


def _check_names(rule_id: str, label: str, code: Any) -> None:
    unknown = _expr_names(code) - _ALLOWED_NAMES
    if not unknown:
        return
    parts = []
    for n in sorted(unknown):
        hint = _suggest(n, _ALLOWED_NAMES)
        parts.append(f"{n!r}" + (f"（{hint}）" if hint else ""))
    raise RuleCatalogError(
        f"规则 {rule_id} 的 {label} 引用了未知名字：{', '.join(parts)}。\n"
        f"  可用名字 = CalcData 字段 / 派生键（DERIVED_CTX_KEYS）/ d / kcal / 白名单内建函数。\n"
        f"  这类拼写错误在运行期的表现是「规则静默 UNKNOWN 或静默 na」——"
        f"报告照样输出，只是这条规则永远不生效，所以必须在这里拦住。"
    )


@dataclass
class Rule:
    id: str
    category: str
    severity: Severity
    title: str
    tag: str
    requires: list[str]
    code: Any            # 编译后的判定表达式
    source: str
    evidence: str
    fix: str
    action: Action
    applies_code: Any = None   # 适用条件；为 False 时状态是 na（不适用，不计入覆盖率）

    def missing_fields(self, d: CalcData) -> list[str]:
        """哪些前置字段没拿到。"""
        out = []
        for f in self.requires:
            v = getattr(d, f, None)
            if v is None or (isinstance(v, list) and not v):
                out.append(f)
        return out


@dataclass
class RuleSet:
    rules: list[Rule]
    pair_rules: list[dict]
    version: str


@lru_cache(maxsize=1)
def load_ruleset(path: str | Path = CATALOG_PATH) -> RuleSet:
    """加载、编译并**校验**规则清单。进程内只做一次。

    除编译表达式，这里还做三件拒绝静默的事（任一不通过直接抛 RuleCatalogError）：
      1. requires 里的字段名必须存在于 CalcData；
      2. when / applies 引用的名字必须在白名单内；
      3. severity / action 写了但取值非法。
    这三件事如果在运行期才发现，症状都是"这条规则永远不生效，但报告一切正常"。
    """
    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)

    rules: list[Rule] = []
    for r in raw.get("rules", []):
        rid = r["id"]

        # ---- 1) requires 字段名必须真实存在 ----
        req = list(r.get("requires", []))
        bad_req = [f for f in req if f not in CALC_ATTRS]
        if bad_req:
            detail = "；".join(
                f"{f!r}（{_suggest(f, CALC_ATTRS)}）" if _suggest(f, CALC_ATTRS) else f"{f!r}"
                for f in bad_req
            )
            raise RuleCatalogError(
                f"规则 {rid} 的 requires 里有 CalcData 不存在的字段：{detail}\n"
                f"  missing_fields() 只会 getattr 到 None，于是这条规则**永久 UNKNOWN**，"
                f"而且报告上看不出任何异常。"
            )

        # ---- 2) severity / action 写了就得合法，不许静默降级 ----
        try:
            severity = Severity(r["severity"])
        except (ValueError, KeyError):
            raise RuleCatalogError(
                f"规则 {rid} 的 severity={r.get('severity')!r} 非法，"
                f"可选：{[s.value for s in Severity]}"
            ) from None
        # action 缺省 -> HUMAN（保守：宁可交给人看）；但写了写错必须报错，
        # 否则一条 ESCALATE 会悄悄退化成 HUMAN，人工升级通道静默消失。
        if r.get("action") is None:
            action = Action.HUMAN
        else:
            try:
                action = Action(r["action"])
            except (ValueError, TypeError):
                raise RuleCatalogError(
                    f"规则 {rid} 的 action={r['action']!r} 非法，"
                    f"可选：{[a.value for a in Action]}"
                ) from None

        # ---- 3) 表达式引用的名字必须在白名单内 ----
        code = compile(r["when"], f"<rule {rid}>", "eval")
        _check_names(rid, "when", code)
        applies_code = None
        if r.get("applies"):
            applies_code = compile(r["applies"], f"<applies {rid}>", "eval")
            _check_names(rid, "applies", applies_code)

        rules.append(
            Rule(
                id=rid,
                category=r["category"],
                severity=severity,
                title=r["title"],
                tag=r.get("tag", ""),
                requires=req,
                code=code,
                source=r["when"],
                evidence=r.get("evidence", ""),
                fix=r.get("fix", ""),
                action=action,
                applies_code=applies_code,
            )
        )

    return RuleSet(
        rules=rules,
        pair_rules=raw.get("pair_rules", []),
        version=str(raw.get("meta", {}).get("version", "?")),
    )


class _SafeDict(dict):
    """format_map 用：缺的键显示成 <xx 缺失> 而不是抛 KeyError。"""

    def __missing__(self, key: str) -> str:
        return f"<{key} 缺失>"


def build_context(d: CalcData) -> _SafeDict:
    """给 evidence 模板和判定表达式准备的变量表。"""
    ctx: dict[str, Any] = dict(vars(d))

    ctx["err"] = "；".join(d.error_terminations) if d.error_terminations else "（无）"
    ctx["last_cycles"] = d.scf_cycles[-1] if d.scf_cycles else "?"
    ctx["min_freq"] = round(min(d.freqs), 2) if d.freqs else "?"
    ctx["stages_s"] = "+".join(d.stages) if d.stages else "?"
    ctx["n_freqs"] = len(d.freqs)
    ctx["n_elems"] = len(d.atomic_numbers)
    ctx["n_heavy"] = d.n_heavy

    if d.homo_ev is not None and d.lumo_ev is not None:
        ctx["gap_ev"] = round(d.lumo_ev - d.homo_ev, 3)

    if d.s2 is not None and d.s2_expected is not None:
        ctx["s2_dev"] = round(d.s2 - d.s2_expected, 4)

    if d.atomic_numbers and d.charge is not None:
        ctx["n_electrons"] = sum(d.atomic_numbers) - d.charge

    tms = [z for z in d.atomic_numbers if z in TM_ATOMIC_NUMBERS]
    ctx["tm_zs"] = ",".join(str(z) for z in sorted(set(tms))) if tms else "?"

    heavy = [z for z in d.atomic_numbers if z >= 37]
    ctx["heavy_zs"] = ",".join(str(z) for z in sorted(set(heavy))) if heavy else "?"

    ctx["n_err"] = len(d.error_terminations)
    ctx["n_trace"] = len(d.scf_trace)

    # 归一化基组名，给「route 声明 vs 实际使用」比对用。
    # Gaussian 输出里大小写并不统一（route 写 aug-cc-pvqz，Standard basis 印
    # Aug-CC-pVQZ），空格也可能有，不归一化会产生大量假阳。
    ctx["basis_n"] = (d.basis or "").lower().replace(" ", "")
    ctx["route_basis_n"] = (d.route_basis or "").lower().replace(" ", "")

    # 逐段 route 的方法/基组。两个用法，语义不同，别混：
    #   · n_route_methods —— 去重后有几种。**无条件**给值（没有 route 就是 0）：
    #     C-07 拿它当 applies 判据（只在「全文只声明了一种方法」时才比声明 vs 实际），
    #     若给成缺失，applies 会在求值时炸掉、降级为 unknown，把一条本该 na 的规则
    #     拖进覆盖率分母。
    #   · route_methods_s —— 只给 evidence 用，没有内容就不给（显示 <缺失>）。
    # E-08 的 requires 仍是列表 route_methods：空列表算缺失 => UNKNOWN，
    # 那才对应"解析器没取到 route，判不了"。
    ctx["n_route_methods"] = len(set(d.route_methods))
    if d.route_methods:
        ctx["route_methods_s"] = " → ".join(d.route_methods)

    if len(d.scf_energies) >= 2:
        ctx["de_kcal"] = round(
            abs(d.scf_energies[-1] - d.scf_energies[-2]) * HARTREE_TO_KCAL, 1
        )
        ctx["e_prev"] = round(d.scf_energies[-2], 8)
        ctx["e_last"] = round(d.scf_energies[-1], 8)
        ctx["de_up_kcal"] = round(
            (d.scf_energies[-1] - d.scf_energies[-2]) * HARTREE_TO_KCAL, 2
        )

    if d.zpe_energy is not None and d.gibbs_energy is not None:
        ctx["gibbs_minus_zpe_kcal"] = round(
            (d.gibbs_energy - d.zpe_energy) * HARTREE_TO_KCAL, 2
        )

    if d.opt_last_table:
        ctx["opt_table"] = "  ".join(
            f"{k}={v}" for k, v in sorted(d.opt_last_table.items())
        )

    # ---- SCF 收敛判据 ----
    # 优先用日志自己声明的判据；老版本没有那行时退回 Gaussian 默认 1e-8，并标注来源，
    # 这样用户能看出「严格程度是谁定的」。
    tol = d.scf_conv_target if d.scf_conv_target else 1e-8
    ctx["conv_tol"] = f"{tol:.2e}"          # 给人看的
    ctx["conv_tol_num"] = tol               # 给 when 表达式用的数值
    ctx["conv_tol_src"] = "日志声明" if d.scf_conv_target else "Gaussian 默认值"
    if d.scf_conv:
        ctx["conv_last"] = f"{d.scf_conv[-1]:.2e}"
        ctx["conv_ratio"] = round(d.scf_conv[-1] / tol, 1)

    if d.virial_ratio is not None:
        ctx["vt_dev"] = round(d.virial_ratio - 2.0, 4)

    # 最小原子间距算一次就够了（O(n^2)），别让每条规则各算一遍
    md = d.min_atom_distance
    if md is not None:
        ctx["min_dist"] = round(md, 3)

    # 片段切分同理：也是 O(n^2)，而且两条规则（数量、间距）共用同一次切分
    nf = d.n_fragments
    if nf is not None:
        ctx["n_frag"] = nf
        gap = d.fragment_gap
        if gap is not None:
            ctx["frag_gap"] = round(gap, 3)

    if d.scf_trace:
        ctx["n_rises"] = sum(1 for t in d.scf_trace if t.get("rises"))

    return _SafeDict(ctx)


@dataclass
class RuleOutcome:
    """一条规则的执行结果。

    status 四态，这个区分是评测系统可信度的关键：
      hit     命中（有 Finding）
      miss    判定为不触发
      unknown 数据缺失，判定不了（**不等于通过**）
      na      本条规则不适用于这类计算（如 opt-only 任务去问有没有虚频）
    """

    rule: Rule
    status: str                  # hit / miss / unknown / na
    finding: Finding | None = None
    missing: list[str] = None    # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.missing is None:
            self.missing = []


def run_rules(d: CalcData, ruleset: RuleSet | None = None) -> list[RuleOutcome]:
    """逐条跑规则。判定表达式抛异常时降级为 UNKNOWN，绝不让一条坏规则拖垮整份报告。"""
    rs = ruleset or load_ruleset()
    ctx = build_context(d)
    # 必须全部塞进 globals：eval 的 locals 对推导式/生成器表达式不可见，
    # 只放 locals 的话 "any(k in d.basis for k in (...))" 会报 d is not defined
    # （C-03 就是这么静默失效过一轮）。
    scope = {**ctx, "__builtins__": SAFE_BUILTINS}
    scope["d"] = d
    scope["kcal"] = HARTREE_TO_KCAL
    outcomes: list[RuleOutcome] = []

    for rule in rs.rules:
        # 1) 先看适用不适用。不适用是「这类任务没有这个检查项」，不是缺数据。
        if rule.applies_code is not None:
            try:
                if not bool(eval(rule.applies_code, scope)):
                    outcomes.append(RuleOutcome(rule, "na"))
                    continue
            except Exception as exc:  # noqa: BLE001 —— 规则写错不能拖垮评测
                # 不能按 na 处理！na 不计入覆盖率分母，一条 applies 写崩的规则
                # 会从所有指标里彻底消失，报告上看不出任何异常。归到 unknown：
                # 语义是"判定不了"，会计入分母并出现在未解决清单里。
                outcomes.append(
                    RuleOutcome(rule, "unknown", missing=[f"适用条件求值异常: {exc}"])
                )
                continue

        # 2) 再看前置字段在不在。缺一个就判 UNKNOWN，不许静默通过。
        missing = rule.missing_fields(d)
        if missing:
            outcomes.append(RuleOutcome(rule, "unknown", missing=missing))
            continue

        try:
            hit = bool(eval(rule.code, scope))
        except Exception as exc:  # noqa: BLE001 —— 规则写错不能拖垮评测
            outcomes.append(
                RuleOutcome(rule, "unknown", missing=[f"规则求值异常: {exc}"])
            )
            continue

        if not hit:
            outcomes.append(RuleOutcome(rule, "miss"))
            continue

        outcomes.append(
            RuleOutcome(
                rule,
                "hit",
                finding=Finding(
                    rule_id=rule.id,
                    category=rule.category,
                    severity=rule.severity,
                    title=rule.title,
                    evidence=rule.evidence.format_map(ctx).strip(),
                    fix_hint=rule.fix.strip(),
                    action=rule.action,
                ),
            )
        )

    return outcomes


def aggregate(
    outcomes: list[RuleOutcome],
    extra_findings: tuple | list = (),
) -> tuple[Verdict, int, Action, bool]:
    """最坏优先聚合（学 QMCert 的 worst-of），但把 UNKNOWN 单独拎出来。

    extra_findings 用来并入签名层（signatures.py）的命中项：签名不是「规则」，
    但它判定的严重性必须参与最终结论，否则崩了的计算可能因为规则层全 UNKNOWN
    而被判成「无法判定」而不是「不通过」。

    返回 (verdict, score, action, abstain)。覆盖率的分母是「适用规则数」，不含 na。
    """
    unknown = [o for o in outcomes if o.status == "unknown"]
    n_na = sum(1 for o in outcomes if o.status == "na")
    applicable = len(outcomes) - n_na
    decided = applicable - len(unknown)

    findings = [o.finding for o in outcomes if o.finding is not None]
    findings.extend(extra_findings)

    score = 100
    worst = Verdict.PASS
    action = Action.ACCEPT

    for f in findings:
        score -= SEVERITY_PENALTY[f.severity]
        v = SEVERITY_VERDICT[f.severity]
        if v is Verdict.FAIL:
            worst = Verdict.FAIL
        elif v is Verdict.WARNING and worst is not Verdict.FAIL:
            worst = Verdict.WARNING
        if ACTION_PRIORITY[f.action] > ACTION_PRIORITY[action]:
            action = f.action

    score = max(0, score)

    # FAIL 绝不能对应「直接采用」：说明有规则判了「不可用」但没给出补救动作
    if worst is Verdict.FAIL and action is Action.ACCEPT:
        action = Action.HUMAN

    # 一条规则都没能判定 => 不是「通过」，是「不知道」
    # （签名层若已确证 FAIL，仍按 FAIL 走——那是「知道」而不是「不知道」）
    if applicable == 0 or decided == 0:
        if worst is Verdict.FAIL:
            return Verdict.FAIL, score, action, False
        return Verdict.UNKNOWN, 0, Action.HUMAN, True

    abstain = bool(unknown) and decided < applicable / 2
    return worst, score, action, abstain


def describe_missing(outcomes: list[RuleOutcome]) -> list[dict[str, Any]]:
    """UNKNOWN 的明细，给用户看「为什么这条判不了」。"""
    return [
        {"rule_id": o.rule.id, "title": o.rule.title, "missing": o.missing}
        for o in outcomes
        if o.status == "unknown"
    ]


__all__ = [
    "Rule",
    "RuleOutcome",
    "RuleSet",
    "aggregate",
    "build_context",
    "describe_missing",
    "load_ruleset",
    "run_rules",
    "HARTREE_TO_KCAL",
    "SEVERITY_PENALTY",
]
