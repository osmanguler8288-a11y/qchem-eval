"""核心数据契约。

两个对象，全系统只认这两个：
  - CalcData   : 解析出来的事实（不含任何判断）
  - EvalResult : 判定结果（全系统唯一出口）

设计原则：**缺数据不等于通过**。所有字段默认 None，规则必须在
「拿到了数据」的前提下才给结论，否则报 UNKNOWN 而不是静默 PASS。
这是与 QMCert 最大的分歧点——它的 assess_qm_quality 把
scf_converged 默认成 True，一旦解析失败就会安静地判通过。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from functools import cached_property
import re
from typing import Any


class Severity(str, Enum):
    """严重级别。级别由规则决定，和「最终结论」分开，便于按体系调档。"""

    CRITICAL = "critical"  # 计算结果不可用
    MAJOR = "major"        # 结果可用但不可信
    MINOR = "minor"        # 需要注意，通常不影响结论
    INFO = "info"          # 提示信息


class Verdict(str, Enum):
    PASS = "pass"
    WARNING = "pass_with_warning"
    FAIL = "fail"
    UNKNOWN = "unknown"    # 数据不足以判定（不是通过）


class Action(str, Enum):
    ACCEPT = "accept"
    RERUN = "rerun"
    ESCALATE = "escalate"
    HUMAN = "human_review"


# 过渡金属原子序数（3d/4d/5d 第 3–12 族）
TM_ATOMIC_NUMBERS = frozenset(
    list(range(21, 31)) + list(range(39, 49)) + list(range(57, 81))
)

# 单键共价半径（Å），Cordero et al., Dalton Trans. 2008, 2832。
# 按原子序数索引，下标 0 占位 —— 让 COVALENT_RADII[6] 直接对应碳，
# 读代码的人不用每次心算偏移量。
COVALENT_RADII: tuple[float, ...] = (
    0.00,                                            # 0 占位（无此元素）
    0.31, 0.28,                                      # H  He
    1.28, 0.96, 0.84, 0.76, 0.71, 0.66, 0.57, 0.58,  # Li Be B  C  N  O  F  Ne
    1.66, 1.41, 1.21, 1.11, 1.07, 1.05, 1.02, 1.06,  # Na Mg Al Si P  S  Cl Ar
    2.03, 1.76, 1.70, 1.60, 1.53, 1.39, 1.39, 1.32,  # K  Ca Sc Ti V  Cr Mn Fe
    1.26, 1.24, 1.32, 1.22, 1.22, 1.20, 1.19, 1.20,  # Co Ni Cu Zn Ga Ge As Se
    1.20, 1.16, 2.20, 1.95, 1.90, 1.75, 1.64, 1.54,  # Br Kr Rb Sr Y  Zr Nb Mo
    1.47, 1.46, 1.42, 1.39, 1.45, 1.44, 1.42, 1.39,  # Tc Ru Rh Pd Ag Cd In Sn
    1.39, 1.38, 1.39, 1.40,                          # Sb Te I  Xe
)

# 判定「这两个原子成键」的容差系数：d < k*(r_i + r_j)。
# 1.3 的取值经过这几档校验（都是实测/文献几何）：
#   C–C 1.54 / 1.98 = 0.78 倍 → 成键 ✓      C=C 1.34 → 成键 ✓
#   Fe–O 2.00 / 2.57 = 0.78 倍 → 成键 ✓      Zn–N 2.10 / 2.51 → 成键 ✓
#   氢键 O···H 1.95 / 1.26 = 1.55 倍 → 不成键 ✓（不会把氢键当共价键）
#   水二聚体 O···O 2.98 / 1.72 → 不成键 ✓    苯堆积 C···C 3.4 / 1.98 → 不成键 ✓
BOND_TOLERANCE = 1.3

# 不含有效核势的低质量基组：套在过渡金属上会严重失真
BASIS_WITHOUT_ECP = (
    "sto-3g", "3-21g", "6-31g", "6-31g*", "6-31g**",
    "6-311g", "6-311g*", "6-311g**", "4-31g",
)

# 带有效核势的基组：可用于过渡金属
BASIS_WITH_ECP = (
    "lanl2dz", "lanl2mb", "lanl2tz", "sdd", "def2", "crenbl",
    "stuttgart", "dhf", "mdf10",
)

# 「没解析出来」的最多记录条数。一份格式异常的文件可能每行都触发，
# 不设上限会把内存吃光，而诊断信息超过 50 条后边际收益本来就是 0。
MAX_UNPARSED_HITS = 50


@dataclass
class CalcData:
    """一次量子化学计算的可观测事实。全部字段可为 None（= 没解析到）。"""

    source: str = ""
    software: str = "gaussian"

    # ---- 流程完整性 ----
    normal_termination: bool | None = None
    error_terminations: list[str] = field(default_factory=list)
    # link1 分段作业数。默认 None 而不是 0：解析器确实通读了全文时才会置 0，
    # 「通读了数出 0 次」和「根本没读」必须能区分开，否则垃圾输入会被判成 PASS。
    n_jobs: int | None = None

    # 异常终止处的上下文快照。每个元素 {link: int|None, lines: [str]}。
    # 为什么要存前置上下文：真正的原因行（比如 "End of file in GetChg."）出现在
    # "Error termination via Lnk1e" 的**上面**，只存终止行等于只有症状没有病因。
    error_blocks: list[dict] = field(default_factory=list)

    # ---- 体系 ----
    charge: int | None = None
    multiplicity: int | None = None
    natoms: int | None = None
    atomic_numbers: list[int] = field(default_factory=list)

    # ---- 方法 ----
    functional: str | None = None
    basis: str | None = None
    dispersion: str | None = None
    solvent: str | None = None
    route: str | None = None
    unrestricted: bool | None = None
    # route 行里**声明**的基组（小写原文）。
    # 单独留一份是因为 basis 会被后面 "Standard basis:" 行覆盖成实际值，
    # 而「你写了什么」和「实际跑的是什么」必须能分开比对。
    #
    # 实测澄清（2026-11-18，G16 Rev A.03，cu04）：即使 route 同时带
    # guess=read 与 geom=allcheck，Standard basis 仍等于 route 声明的基组，
    # **不会**从 chk 继承旧基组。所以这两者不一致时，原因通常是日志被拼接/
    # 手工编辑过、或混入了别的运行片段，而不是 allcheck。
    route_basis: str | None = None
    # 每个 link1 段 route 里声明的「泛函/基组」，已归一化（剥掉 R/U 自旋前缀），
    # 相邻重复的合并。opt freq 这类作业在 Gaussian 内部会拆成两段，第二段是它
    # 自动生成的 `RB3LYP/6-31G(d)` —— 归一化后与第一段相同，不该被当成"方法变了"。
    #
    # 只有**首段**会写进 functional / basis / route_basis，后续段一律只往这里追加：
    # 后段的值是 Gaussian 自己生成的，让它覆盖掉首段用户声明，E-05 就永远比不出东西。
    route_methods: list[str] = field(default_factory=list)

    # ---- 阶段 ----
    stages: list[str] = field(default_factory=list)   # opt / freq / sp / td / scan

    # ---- SCF ----
    scf_energies: list[float] = field(default_factory=list)   # Hartree
    scf_cycles: list[int] = field(default_factory=list)
    scf_trace: list[dict] = field(default_factory=list)       # {e, de, rises, damp}
    scf_maxcycle: int | None = None
    scf_converged: bool | None = None
    # Convg 值：收敛判据是 1e-8，但 Gaussian 会在未达标时也打印 SCF Done
    # （官方错误目录 #10 里就有 "Convg = 0.8831D-03" 配一条 Convergence failure）
    scf_conv: list[float] = field(default_factory=list)
    # 日志自己声明的收敛判据（"Requested convergence on RMS density matrix=1.00D-08"）。
    # 有了它，判「Conv 够不够」就不用把 1e-8 写死在规则里——用户改了 scf=conver=N
    # 也能正确比较。写死阈值是这类规则最常见的误报来源。
    scf_conv_target: float | None = None
    # -V/T 维里比，健康值 ≈ 2.0000；偏离说明波函数没收敛好
    virial_ratio: float | None = None

    # ---- 几何 ----
    # 同样是 None：0 表示「读完了，没发生」，None 表示「没读到日志」
    n_point_group_changes: int | None = None
    has_dummy_atoms: bool | None = None   # 输入含 X / Bq 虚原子
    coords_last: list[tuple[float, float, float]] = field(default_factory=list)

    # ---- 几何优化 ----
    opt_converged: bool | None = None
    opt_steps: int | None = None
    opt_stopped: bool | None = None
    opt_last_table: dict[str, str] = field(default_factory=dict)

    # ---- 频率 ----
    freqs: list[float] = field(default_factory=list)
    n_imag: int | None = None

    # ---- 电子结构 ----
    s2: float | None = None
    s2_expected: float | None = None
    homo_ev: float | None = None
    lumo_ev: float | None = None

    # ---- 热化学 ----
    zpe_energy: float | None = None     # Sum of electronic and zero-point Energies
    gibbs_energy: float | None = None    # Sum of electronic and thermal Free Energies

    # ---- 解析诊断（不参与判定，供排查用）----
    # 两类分开存，这个区分是责任归属问题，不是洁癖：
    #   parse_warnings —— 面向用户：这份日志有什么是**客观查不到**的
    #   unparsed_hits  —— 面向开发者：日志里**明明有**，是我没提取出来
    # 混在一起的后果很具体：正则不支持 E 记法（我的 bug），会被报告成
    # 「缺 scf_conv，无法判定」（用户的数据问题）——线上甩锅方向直接错。
    parse_warnings: list[str] = field(default_factory=list)
    unparsed_hits: list[dict] = field(default_factory=list)

    def note_unparsed(self, field_name: str, line: str, reason: str = "") -> None:
        """记录一次「看到关键词但没取出值」。限流防止坏文件刷爆内存。"""
        if len(self.unparsed_hits) < MAX_UNPARSED_HITS:
            self.unparsed_hits.append(
                {"field": field_name, "line": line.strip()[:120], "reason": reason}
            )

    # ---------------- 派生属性 ----------------

    @property
    def elements_ok(self) -> bool:
        return len(self.atomic_numbers) > 0

    @property
    def has_transition_metal(self) -> bool:
        return any(z in TM_ATOMIC_NUMBERS for z in self.atomic_numbers)

    @property
    def is_open_shell(self) -> bool:
        return self.multiplicity is not None and self.multiplicity > 1

    @property
    def declares_ts(self) -> bool | None:
        """route 是否声明了过渡态优化（opt=ts / opt=(ts,...)）。

        为什么需要单独一个属性：`opt=ts` 的**意图**是「找一阶鞍点」，
        而 B-02 只说「检出一个虚频」。两者要连起来看才有意义 ——
        声明找 TS 却搜出全正频率，说明优化落到了极小点，出来的不是过渡态。
        这是实测中真实发生的（G16 + noeigentest 会允许它收敛到极小点）。

        返回 None 表示 route 没解析到，交给规则判 UNKNOWN 而不是当 False。
        """
        if self.route is None:
            return None
        # opt=(ts,calcfc,noeigentest) / opt=ts / opt=(ts) 都要认。
        # 不用 `"ts" in route`：ts 是极短的串，会撞上 ts 开头的其他关键词
        # （例如某些自定义 iop 注释），也可能出现在 title 残留里。
        return re.search(r"opt\s*=\s*\(?[^)\s]*\bts\b", self.route.lower()) is not None

    @property
    def n_heavy(self) -> int:
        return sum(1 for z in self.atomic_numbers if z > 1)

    @property
    def last_scf_energy(self) -> float | None:
        return self.scf_energies[-1] if self.scf_energies else None

    @property
    def basis_is_low_quality_for_tm(self) -> bool:
        if not self.basis:
            return False
        b = self.basis.lower().replace(" ", "")
        if any(k in b for k in BASIS_WITH_ECP):
            return False
        return any(k in b for k in BASIS_WITHOUT_ECP)

    @property
    def min_atom_distance(self) -> float | None:
        """最后一帧几何的最小原子间距（Å）。用于识别结构崩掉/原子重合。"""
        c = self.coords_last
        if len(c) < 2:
            return None
        best = float("inf")
        for i in range(len(c)):
            xi, yi, zi = c[i]
            for j in range(i + 1, len(c)):
                xj, yj, zj = c[j]
                d2 = (xi - xj) ** 2 + (yi - yj) ** 2 + (zi - zj) ** 2
                if d2 < best:
                    best = d2
        return best ** 0.5

    @cached_property
    def _fragment_partition(self) -> list[list[int]] | None:
        """按共价半径把末帧几何切成互不相连的片段（每个片段是原子下标列表）。

        为什么需要这个：Gaussian 只保证「我算完了」，不保证「这是个完整的分子」。
        把 H2 的键长写成 5 Å，它会认认真真算两个互不相干的氢原子，正常终止、
        能量也像个数字。这类错误只能在几何上查：原子之间该连的没连上。

        缺任何一样数据就返回 None，不做猜测：
          · 坐标 / 原子序数不足两个原子 → None
          · **任何一个**元素的共价半径不在表里（比如重元素）→ None
        后者尤其重要：如果简单跳过没有半径的原子，那个原子会被当成孤立片段，
        凭空多切出一片 —— 那是按缺数据造出来的误报，比不判更糟。
        """
        zs, coord = self.atomic_numbers, self.coords_last
        n = min(len(zs), len(coord))
        if n < 2:
            return None
        for z in zs[:n]:
            if not (0 < z < len(COVALENT_RADII)):
                return None

        parent = list(range(n))

        def find(a: int) -> int:
            while parent[a] != a:
                parent[a] = parent[parent[a]]      # 路径压缩
                a = parent[a]
            return a

        def union(a: int, b: int) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        for i in range(n):
            ri = COVALENT_RADII[zs[i]]
            xi, yi, zi = coord[i]
            for j in range(i + 1, n):
                limit = BOND_TOLERANCE * (ri + COVALENT_RADII[zs[j]])
                xj, yj, zj = coord[j]
                if (xi - xj) ** 2 + (yi - yj) ** 2 + (zi - zj) ** 2 <= limit * limit:
                    union(i, j)

        groups: dict[int, list[int]] = {}
        for i in range(n):
            groups.setdefault(find(i), []).append(i)
        return list(groups.values())

    @property
    def n_fragments(self) -> int | None:
        """末帧几何里互不相连的片段数。1 表示完整分子，>1 表示断了。"""
        parts = self._fragment_partition
        return None if parts is None else len(parts)

    @property
    def fragment_gap(self) -> float | None:
        """不同片段之间的最小原子间距（Å）。

        这个数字是判「散架」还是判「复合物」的关键：
          · 氢键 / π 堆积 / 范德华接触都落在 1.5–4.0 Å，属于**设计好的**分子间作用
          · 超过 4.5 Å 之后，两个片段之间已经没有任何化学作用，只是被摆在同一
            个输入里 —— 要么是结构写错了，要么是故意算「非相互作用极限」
        """
        parts = self._fragment_partition
        if parts is None or len(parts) < 2:
            return None
        coord = self.coords_last
        best = float("inf")
        for a in range(len(parts)):
            for b in range(a + 1, len(parts)):
                for i in parts[a]:
                    xi, yi, zi = coord[i]
                    for j in parts[b]:
                        xj, yj, zj = coord[j]
                        d2 = (xi - xj) ** 2 + (yi - yj) ** 2 + (zi - zj) ** 2
                        if d2 < best:
                            best = d2
        return best ** 0.5

    def summary(self) -> dict[str, Any]:
        """给人看的摘要。"""
        return {
            "source": self.source,
            "natoms": self.natoms,
            "charge": self.charge,
            "multiplicity": self.multiplicity,
            "functional": self.functional,
            "basis": self.basis,
            # 多段作业里 functional 取自首段、basis 取自末段的 Standard basis。
            # 直接拼成 "hf/6-31G(d)" 会拼出一个**这份文件里从未出现过**的组合
            # （首段是 HF/STO-3G，末段是 B3LYP/6-31G(d)）。报告第一行就开始编造
            # 数据是不能接受的，所以把逐段声明一起带出去，让渲染层改显链式。
            "route_methods": self.route_methods,
            "stages": self.stages,
            "scf_cycles": self.scf_cycles[-1] if self.scf_cycles else None,
            "n_freqs": len(self.freqs),
            "n_imag": self.n_imag,
            "s2": self.s2,
        }


@dataclass
class Finding:
    """一条判定。必须同时给出：依据（evidence）与怎么改（fix_hint）。"""

    rule_id: str
    category: str          # A收敛 / B结构 / C方法 / D数值 / E流程
    severity: Severity
    title: str
    evidence: str
    fix_hint: str
    action: Action = Action.HUMAN

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "category": self.category,
            "severity": self.severity.value,
            "title": self.title,
            "evidence": self.evidence,
            "fix_hint": self.fix_hint,
            "action": self.action.value,
        }


@dataclass
class EvalResult:
    """全系统唯一出口。对齐方案文档 3.2 节的 EvalResult。"""

    source: str
    verdict: Verdict
    score: int
    family: str
    action: Action
    findings: list[Finding] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    timing: dict[str, float] = field(default_factory=dict)
    abstain: bool = False
    provenance: dict[str, Any] = field(default_factory=dict)

    def reasons(self) -> list[dict[str, str]]:
        """reasons[] 是「系统能不能被信」的底线字段，这里由 findings 派生。"""
        return [
            {"source": f"rule:{f.rule_id}", "text": f"{f.title}｜{f.evidence}"}
            for f in self.findings
            if f.severity in (Severity.CRITICAL, Severity.MAJOR)
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "verdict": self.verdict.value,
            "score": self.score,
            "family": self.family,
            "action": self.action.value,
            "abstain": self.abstain,
            "findings": [f.to_dict() for f in self.findings],
            "reasons": self.reasons(),
            "metrics": self.metrics,
            "timing_ms": {k: round(v, 2) for k, v in self.timing.items()},
            "provenance": self.provenance,
        }
