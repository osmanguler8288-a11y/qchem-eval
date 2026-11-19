"""失败样本生成器：给「规则层」和「签名层」分别造带标签的测试输入。

为什么非造不可
--------------
公开渠道拿不到失败日志。实测 cclib 仓库 data/ 下 2046 份输出文件，文件名含
error / fail / trunc 的为 **0**；VQM24 论文统计了 262542 次失败计算，但对外发布的
835947 条全是成功的。也就是说「失败的量子化学计算」在公开数据里是系统性缺失的。

所以本文件用两种方法合成带标签样本：

1. 签名层（signatures.yaml）—— **嫁接**
   把官方错误目录里的 cause_text 原文接进真实日志的终止位置，并把官方路径
   /disc30/g98/ 换成本机 /g16/。这样检验的是「指纹匹配器能否在真实日志里找到
   官方文本」，而不是「官方文本能否覆盖所有真实崩溃」。

2. 规则层（catalog.yaml）—— **定点变异**
   在真实日志上改一个字段（能量、坐标、原子序数、频率……），检验阈值判据的实现
   是否正确。

诚实边界（这三条必须跟着报告一起看）
------------------------------------
  · 嫁接样本检验的是「识别能力」，不是「覆盖率」。官方目录里有 27 条指纹，
    未知的崩溃形态它一条都测不到。
  · 变异样本检验的是「判据实现对不对」，不是「阈值对你们的体系合不合适」。
    阈值合理性只能靠真实失败计算回归，这需要你们自己的失败 log。
  · 负样本只有 8 份真实日志（其中 7 份结果可用）+ 27 份扰动文本。样本量小，
    precision 的置信区间很宽，别把 100% 当成「不会误报」。

用法
----
    python tools/synth_failures.py            # 生成到 demo/synth/ 并打印清单
    python tools/synth_failures.py --check    # 只做自检：每个变异锚点是否存在
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "tests" / "data"
SIG_YAML = ROOT / "qchem_eval" / "rules" / "signatures.yaml"

NORMAL_LINE = "Normal termination of Gaussian 16"
# 官方目录里的示例路径是 g98 时代的 /disc30/g98/，实际环境是 /g16/。
# 这一步是刻意的：如果 match 串依赖官方路径，换环境就失效。
OFFICIAL_PREFIX = "/disc30/g98/"
ENV_PREFIX = "/g16/"


# ---------------------------------------------------------------------------
# 变异原语
# ---------------------------------------------------------------------------

def sub(anchor: str, new: str, count: int = -1) -> Callable[[str], str]:
    """把 anchor 换成 new。anchor 必须存在，否则直接报错（防止静默空变异）。"""

    def _apply(text: str) -> str:
        if anchor not in text:
            raise AssertionError(f"变异锚点不存在: {anchor!r}")
        return text.replace(anchor, new, count)

    return _apply


def drop_lines(pattern: str) -> Callable[[str], str]:
    """删掉所有匹配 pattern 的整行。"""
    rx = re.compile(pattern, re.M)

    def _apply(text: str) -> str:
        out, n = rx.subn(lambda m: "", text)
        if n == 0:
            raise AssertionError(f"没删到任何行: {pattern!r}")
        return out

    return _apply


def chain(*fns: Callable[[str], str]) -> Callable[[str], str]:
    def _apply(text: str) -> str:
        for fn in fns:
            text = fn(text)
        return text

    return _apply


# ---------------------------------------------------------------------------
# 规则层（catalog.yaml）正样本
# ---------------------------------------------------------------------------

@dataclass
class Case:
    name: str
    base: str                  # tests/data 下的真实日志文件名
    expect: str                # 期望命中的 rule_id / sig_id
    apply: Callable[[str], str]
    kind: str = "rule"         # rule / signature / negative
    note: str = ""
    forbid: list[str] = field(default_factory=list)   # 负样本要禁止出现的 id


RULE_CASES: list[Case] = [
    # ---- A 收敛性 ----
    Case("A-01_abnormal_term", "dvb_sp.out", "A-01",
         sub(NORMAL_LINE, "Error termination via Lnk1e in /g16/l502.exe."),
         note="把正常终止改成异常终止"),
    Case("A-02_scf_fail", "dvb_sp.out", "A-02",
         sub(" SCF Done:  E(RB3LYP) =  -382.308266602",
             " Convergence failure -- run terminated.\n"
             " SCF Done:  E(RB3LYP) =  -382.308266602"),
         note="SCF Done 之前插一条 Convergence failure（官方 #10 的真实结构）"),
    Case("A-03_near_maxcycle", "dvb_sp.out", "A-03",
         sub("A.U. after    1 cycles", "A.U. after  120 cycles"),
         note="120/128 步 = 93%，逼近上限"),
    Case("A-04_slow_scf", "dvb_sp.out", "A-04",
         sub("A.U. after    1 cycles", "A.U. after   60 cycles"),
         note="60 步收敛，慢但没到 80% 上限"),
    Case("A-05_opt_not_conv", "dvb_gopt.out", "A-05",
         sub("Optimization completed", "Optimization stopped."),
         note="优化停住但四判据未全 YES"),
    Case("A-06_conv_too_loose", "dvb_sp.out", "A-06",
         sub("Conv=0.19D-08", "Conv=0.19D-05"),
         note="Conv 0.19D-05 是判据 1e-8 的 1900 倍——G16 在跑满 maxcycle 后"
              "仍会打印 SCF Done，这是最隐蔽的一类废结果"),
    Case("A-07_scf_rises", "dvb_gopt.out", "A-07",
         sub("Rises=F Damp=T", "Rises=T Damp=T"),
         note="SCF 迭代能量上升"),

    # ---- B 结构合理性 ----
    Case("B-01_two_imag", "dvb_ir.out", "B-01",
         sub("Frequencies ---    53.1981   84.7415",
             "Frequencies ---   -53.1981  -84.7415"),
         note="两个虚频 = 停在鞍点"),
    Case("B-02_one_imag", "dvb_ir.out", "B-02",
         sub("Frequencies ---    53.1981", "Frequencies ---   -53.1981"),
         note="单个虚频"),
    Case("B-03_low_freq", "dvb_ir.out", "B-03",
         sub("Frequencies ---    53.1981   84.7415",
             "Frequencies ---    23.1981   84.7415"),
         note="最低模 23 cm^-1，需准谐振校正"),
    Case("B-04_tiny_imag", "dvb_ir.out", "B-04",
         sub("Frequencies ---    53.1981", "Frequencies ---   -30.0000"),
         note="虚频只有 -30 cm^-1，多半是网格噪声"),
    Case("B-05_atom_overlap", "dvb_sp.out", "B-05",
         sub("      2          6           0       -1.064785    0.920662   -0.000000",
             "      2          6           0        0.269445    1.410118    0.000000"),
         note="把 2 号原子挪到 1 号原子上（距离 0）"),
    Case("B-06_dummy_atom", "dvb_sp.out", "B-06",
         sub("     20          1           0        2.364759    0.813041   -0.000000",
             "     20          0           0        2.364759    0.813041   -0.000000"),
         note="末位原子改成虚原子（Z=0）"),
    Case("B-07_nan_freq", "dvb_ir.out", "B-07",
         sub("Frequencies ---    53.1981", "Frequencies ---        NaN"),
         note="频率里出现 NaN"),
    Case("B-08_point_group", "dvb_sp.out", "B-08",
         sub(" Standard basis: STO-3G (5D, 7F)",
             " Change in point group or standard orientation.\n"
             " Standard basis: STO-3G (5D, 7F)"),
         note="插入点群改变提示"),

    # ---- C 方法匹配 ----
    Case("C-01_tm_low_basis", "dvb_sp.out", "C-01",
         sub("     19          6           0        1.325389    0.457058   -0.000000",
             "     19         42           0        1.325389    0.457058   -0.000000"),
         note="把一个 C 换成 Mo（Z=42），基组仍是 STO-3G"),
    Case("C-02_spin_pollution", "dvb_un_sp_b.log", "C-02",
         sub("S**2 before annihilation     0.7778,   after     0.7504",
             "S**2 before annihilation     2.7778,   after     0.7504"),
         note="<S**2> 2.78 vs 理论 0.75，严重自旋污染"),
    Case("C-03_anion_no_diffuse", "dvb_sp.out", "C-03",
         sub("Charge =  0 Multiplicity = 1", "Charge = -1 Multiplicity = 2"),
         note="阴离子 + STO-3G 无弥散"),
    Case("C-04_tm_open_shell", "dvb_un_sp_b.log", "C-04",
         sub("      1          6           0        0.269481   -1.410087    0.000000",
             "      1         42           0        0.269481   -1.410087    0.000000"),
         note="开壳层体系里塞一个 Mo"),
    Case("C-05_small_gap", "dvb_sp.out", "C-05",
         sub("-0.19505  -0.15308", "-0.19505   0.02272"),
         note="HOMO 抬到 0.62 eV，间隙只剩 0.4 eV"),
    Case("C-06_heavy_no_ecp", "dvb_sp.out", "C-06",
         sub("     19          6           0        1.325389    0.457058   -0.000000",
             "     19         53           0        1.325389    0.457058   -0.000000"),
         note="换成 I（Z=53），STO-3G 无 ECP"),
    # C-07 要的是「声明 ≠ 实际」，所以只改 route 那一行，Standard basis 保持原样。
    # 真实场景里这几乎总是 geom=allcheck / guess=read 从 chk 读了旧基组。
    Case("C-07_route_basis_mismatch", "dvb_sp.out", "C-07",
         sub(" #p b3lyp/sto-3g guess=read geom=allcheck pop=full gfinput iop(3/33=1,3",
             " #p b3lyp/6-31g(d) guess=read geom=allcheck pop=full gfinput iop(3/33=1,3"),
         note="route 声明 6-31G(d)，实际 Standard basis 仍是 STO-3G"),

    # ---- D 数值一致性 ----
    Case("D-01_energy_jump", "dvb_gopt.out", "D-01",
         sub(" SCF Done:  E(RB3LYP) =  -382.308266602",
             " SCF Done:  E(RB3LYP) =  -381.308266602"),
         note="末次 SCF 能量跳 1 Ha（627 kcal/mol）"),
    Case("D-02_no_thermal", "dvb_ir.out", "D-02",
         sub("thermal Free Energies=", "thermal Free EnergyXX="),
         note="破坏自由能字段，模拟热化学段缺失"),
    Case("D-03_lumo_below_homo", "dvb_sp.out", "D-03",
         sub("Alpha virt. eigenvalues --    0.03742",
             "Alpha virt. eigenvalues --   -0.20000"),
         note="LUMO 压到 HOMO 之下，轨道能级倒错"),
    Case("D-07_virial", "dvb_sp.out", "D-07",
         sub("V/T= 2.0172", "V/T= 2.5500"),
         note="-V/T 偏离 2 达 0.55"),
    Case("D-08_energy_up", "dvb_gopt.out", "D-08",
         sub(" SCF Done:  E(RB3LYP) =  -382.308266602",
             " SCF Done:  E(RB3LYP) =  -382.298266602"),
         note="优化末步能量回升 6.3 kcal/mol"),
    Case("D-09_gibbs_inverted", "dvb_ir.out", "D-09",
         sub("zero-point Energies=           -382.131135",
             "zero-point Energies=           -382.200000"),
         note="E+ZPE 被压到 G 以下，热化学量倒挂"),

    # ---- E 流程完整性 ----
    Case("E-02_no_energy", "dvb_sp.out", "E-02",
         drop_lines(r"^.*SCF Done:.*\n"),
         note="宣告正常终止但删光所有 SCF Done"),
    Case("E-03_natoms_mismatch", "dvb_sp.out", "E-03",
         sub("     20          1           0        2.364759    0.813041   -0.000000",
             "     20          1           0        2.364759    0.813041   -0.000000\n"
             "     21          1           0        9.000000    9.000000    0.000000"),
         note="坐标块多一个原子，与 NAtoms 不符"),
    Case("E-04_parity", "dvb_sp.out", "E-04",
         sub("Charge =  0 Multiplicity = 1", "Charge =  0 Multiplicity = 2"),
         note="偶数电子配偶数多重度"),
    Case("E-06_err_then_normal", "dvb_sp.out", "E-06",
         sub(" Standard basis: STO-3G (5D, 7F)",
             " Error termination via Lnk1e in /g16/l101.exe.\n"
             " Standard basis: STO-3G (5D, 7F)"),
         note="中途崩了一段，最后还是宣告 Normal termination"),
    Case("E-07_no_freqs", "dvb_ir.out", "E-07",
         # 注意有两种破折号：hpmodes 段打 "Frequencies ---"，普通段打 "Frequencies --"
         drop_lines(r"^.*Frequencies\s+-+.*\n"),
         note="route 有 freq 但一条频率都没有"),
    Case("E-08_multi_job", "dvb_sp.out", "E-08",
         # 只把 Normal termination 复制一行**不再算正样本**了：v0.6 起 E-08 判的是
         # 「段与段之间方法/基组变了没」，而不是「文件里有没有多段」（后者每份
         # opt freq 都会响，是噪音）。所以这里必须真换一个基组：
         # 首段 b3lyp/sto-3g（用户写的），第二段自动生成头换成 6-31G(d)。
         sub(NORMAL_LINE,
             " #P Geom=AllCheck Guess=TCheck SCRF=Check GenChk RB3LYP/6-31G(d) Freq\n"
             + NORMAL_LINE + "\n " + NORMAL_LINE),
         note="两段 link1，第二段基组从 STO-3G 换成 6-31G(d)"),
]

# 这几条没法靠文本变异造正样本，只能等真实失败日志。列出来是为了如实交代覆盖缺口。
NO_SYNTHETIC_POSITIVE = {
    "A-01": "真实日志里 A-01 与 SIG-NOTERM 同时命中，单用它区分度低（已由 A-01_abnormal_term 覆盖）",
}


def _identity(text: str) -> str:
    """不改动：某些规则本来就该在「成功的」真实计算上触发。"""
    return text


# 「建议类」规则（info / minor）的设计意图就是在正常计算上也给提示，
# 所以真实日志本身就是它们的正样本——不需要造假。
IDENTITY_CASES: list[Case] = [
    Case("realpos:B-03", "dvb_ir.out", "B-03", _identity,
         note="dvb_ir 最低模 53 cm^-1 < 100，本就该提示准谐振校正"),
    Case("realpos:D-07", "Mo4OCl4-sp.log", "D-07", _identity,
         note="真实 Mo4OCl4 簇 -V/T=2.678，含 ECP 的重元素体系天然偏大"),
    Case("realpos:E-01", "dvb_gopt.out", "E-01", _identity,
         note="纯 opt 任务确实缺频率验证"),
    Case("realpos:E-05", "dvb_gopt.out", "E-05", _identity,
         note="B3LYP 未加 D3，重原子 ≥ 6"),
    # cclib 的这批测试文件为了跑得快全用 STO-3G —— 它们「正常终止」但科学上
    # 不能出结论，正好是 C-08 的天然正样本。这也说明了本系统的定位：
    # 崩溃检测只是下限，「算完了但不可用」才是价值所在。
    Case("realpos:C-08", "dvb_sp.out", "C-08", _identity,
         note="STO-3G 无极化函数，真实日志本身就该提示"),
    # B-09 的正样本来自 cu04 上真跑的 Gaussian 16：用
    # opt=(ts,calcfc,noeigentest) 优化 HCN，Gaussian 收敛到**极小点**、
    # 频率全为正（767/767/2213/3478）。它正常终止、SCF 收敛、能量正常，
    # 旧规则集给它 100 分；而用户以为自己拿到了过渡态。
    Case("realpos:B-09", "ts_no_imag.log", "B-09", _identity,
         note="声明 opt=ts 却无虚频，真实 G16 产物，不是构造的"),
    # B-10 的正样本同样来自 cu04 真跑：H2 的键长写成 5.0 Å，Gaussian 认得这个
    # 输入（语法合法）、正常终止、SCF 收敛，能量 -0.599 看着像个数字。它算的是
    # 两个相距 5 Å、互相没有化学作用的氢原子。旧规则集给 90 分，用户会照着这个
    # 结果往下做。这条也是「Gaussian 不报错 ≠ 结果可用」的第二个实例。
    Case("realpos:B-10", "h2_dissociated.log", "B-10", _identity,
         note="H2 键长 5 Å，正常终止但体系已断成两个孤立氢"),
]


# ---------------------------------------------------------------------------
# 「真实日志」到底哪几份算「结果可用」
# ---------------------------------------------------------------------------
# 这里必须显式声明，不能靠 glob("tests/data/*") 哄自己。原因是 tests/data 里
# 躺着两类东西，它们长得一模一样（都是 Gaussian 正常终止的输出）：
#
#   ① 结果可用的真实日志 —— 用来验「规则不许在好结果上报 CRITICAL/MAJOR」，
#      允许出 WARNING 级提示（E-01 缺频率、E-05 没上 D3 都属于「能用但可以更好」）
#   ② 结果不可用的真实日志 —— ts_no_imag.log：route 声明 opt=ts，Gaussian
#      却收敛到极小点、频率全正（767/767/2213/3478），用户以为拿到了过渡态。
#      它没崩、SCF 收敛、能量正常，但答案是错的。
#
# 把 ② 混进「成功日志」里，就等于用「它没有语法错误」去证明「它语义正确」。
# 上一版正是这么干的：B-09 在「真实成功日志」上命中，被记成高严重性误报。
#
# 所以维度要拆开——同一份日志可以同时是「签名层干净」（没崩）和「规则层有问题」：
REAL_OK: frozenset[str] = frozenset({
    "C_bigbasis.out",
    "Mo4OCl4-sp.log",
    "dvb_gopt.out",
    "dvb_ir.out",
    "dvb_sp.out",
    "dvb_un_sp_b.log",
    "water_ccsd.log",
    # 两份真实复合物：它们天然就是多个片段，B-10 不该响。
    # 这两份是 B-10 阈值 4.5 Å 的护栏 —— 谁把手一抖改成 3.0，
    # 苯二聚体（3.46 Å）立刻就红。
    "water_dimer.log",
    "benzene_dimer.log",
})
# 判据：结果本身能否拿去用。WARNING 级提示（基组偏小、缺频率验证）不影响这条。

# 正常终止、但结果是错的。必须显式登记，不能靠「不在 REAL_OK 里」推断 ——
# 那样新增一份日志忘了登记，就会**静默**被排除在检查之外，检查范围悄悄变窄，
# 而报告上一切正常。宁可让 build_samples() 直接报错。
REAL_BAD: frozenset[str] = frozenset({
    "ts_no_imag.log",       # 声明 opt=ts 却收敛到极小点，频率全正
    "h2_dissociated.log",   # H2 键长 5 Å，算完但体系已断成两个孤立氢
})


def assert_real_logs_classified() -> None:
    """每份真实日志都必须被显式判过「可用 / 不可用」，否则报错。

    这不是洁癖：漏登记的后果是那几份日志不再参与「高严重性误报」统计，
    通过率照样 100%，但检查其实少测了东西 —— 这种静默降级比测试失败危险。
    """
    on_disk = {p.name for p in DATA.glob("*") if p.is_file()}
    unknown = on_disk - REAL_OK - REAL_BAD
    both = REAL_OK & REAL_BAD
    missing = (REAL_OK | REAL_BAD) - on_disk
    if unknown or both or missing:
        raise AssertionError(
            "tests/data 下的真实日志标注不全："
            f"未分类={sorted(unknown)}｜同时标了可用与不可用={sorted(both)}"
            f"｜标注了但文件不存在={sorted(missing)}")


# ---------------------------------------------------------------------------
# 签名层（signatures.yaml）正样本：嫁接官方原文
# ---------------------------------------------------------------------------

def load_raw_signatures() -> list[dict]:
    raw = yaml.safe_load(SIG_YAML.read_text(encoding="utf-8"))
    return raw.get("signatures", [])


def graft_signature(sig: dict) -> Callable[[str], str]:
    """把官方 cause_text 嫁接到真实日志的终止位置。

    结构上要对齐真实崩溃：原因行在上、终止行在下。官方 cause_text 里已经带了
    "Error termination via Lnk1e in .../lNNN.exe."，所以直接整块替换 Normal termination
    那一行即可。没有终止行的条目（磁盘/内存/IO 类）就只把原文贴到末尾——真实环境里
    这些崩溃往往连终止标志都来不及打，正好对应「日志断掉」那条路径。
    """
    cause = (sig.get("cause_text") or "").strip("\n").replace(OFFICIAL_PREFIX, ENV_PREFIX)

    def _apply(text: str) -> str:
        if cause not in cause:            # pragma: no cover - 防御性
            raise AssertionError("empty")
        if "Normal termination of Gaussian 16" not in text:
            raise AssertionError("基准日志没有 Normal termination 行")
        return text.replace(NORMAL_LINE, cause)

    return _apply


def perturb_matches(sig: dict) -> Callable[[str], str]:
    """负样本：官方原文扰动一个字符，插在正常终止行**之前**。

    这里**不能复用 graft_signature()**。graft 为了模拟真实崩溃，是把
    "Normal termination" 整行替换成错误块；照搬到负样本上，这份"负样本"
    本身就是一份异常终止的日志 —— 规则层的 A-01 会正确地判它 FAIL，
    而那会被记成"误报"，把正确行为算成错误。

    负样本的语义必须是「正常计算 + 一段看起来像错误的文本」，所以：
      1. 终止行原样保留，扰动文本插在它前面；
      2. 只扰动 cause_text 里的 match 串，不碰日志正文。
    """
    matches = list(sig.get("match") or [])
    cause = (sig.get("cause_text") or "").strip("\n").replace(OFFICIAL_PREFIX, ENV_PREFIX)

    def _apply(text: str) -> str:
        if "Normal termination of Gaussian 16" not in text:
            raise AssertionError("基准日志没有 Normal termination 行")

        block = cause
        unmatched = []
        for m in matches:
            if m in block:
                mid = len(m) // 2
                block = block.replace(m, m[:mid] + "§" + m[mid:])
            else:
                unmatched.append(m)

        out = text.replace(NORMAL_LINE, "\n" + block + "\n" + NORMAL_LINE, 1)

        # match 串不在自己的 cause_text 里时，退而在全文找（少见，但别静默放过）
        for m in unmatched:
            if m in out:
                mid = len(m) // 2
                out = out.replace(m, m[:mid] + "§" + m[mid:])
        return out

    return _apply


# 有 terminating link 的签名嫁接到单点能日志；优化类嫁接到 opt 日志，
# 让 link 号与日志阶段对得上（l9999 出现在 sp 日志里很违和）。
def _base_for(sig: dict) -> str:
    if str(sig.get("link")) in ("9999", "103"):
        return "dvb_gopt.out"
    return "dvb_sp.out"


# ---------------------------------------------------------------------------
# 构建
# ---------------------------------------------------------------------------

@dataclass
class Sample:
    name: str
    text: str
    kind: str            # rule / signature / negative
    expect: str = ""     # 期望命中
    forbid: list[str] = field(default_factory=list)
    note: str = ""


def build_samples() -> list[Sample]:
    assert_real_logs_classified()
    bases = {p.name: p.read_text(encoding="utf-8", errors="replace")
             for p in DATA.glob("*") if p.is_file()}

    samples: list[Sample] = []

    # 1) 规则层正样本
    for c in RULE_CASES + IDENTITY_CASES:
        text = c.apply(bases[c.base])
        samples.append(Sample(c.name, text, "rule", c.expect, note=c.note))

    # 2) 签名层正样本 + 扰动负样本
    for sig in load_raw_signatures():
        if not sig.get("match") or sig.get("derived"):
            continue
        base = bases[_base_for(sig)]
        try:
            text = graft_signature(sig)(base)
        except AssertionError:
            continue
        samples.append(Sample(f"sig+{sig['id']}", text, "signature", sig["id"],
                              note=f"嫁接官方原文（{sig.get('ref', '')}）"))
        samples.append(Sample(f"sig-{sig['id']}", perturb_matches(sig)(base),
                              "negative", forbid=[sig["id"]],
                              note="官方原文被扰动一个字符，该签名不应命中"))

    # 3) 负样本：真实日志，任何签名都不许命中
    #
    # 注意这里收的是**全部**真实日志，不是只有 REAL_OK 那几份。因为「签名层」
    # 判的是「崩没崩」——ts_no_imag.log 确实没崩，它的日志里不该出现任何官方
    # 错误指纹，这个检查对它是成立的。至于它「结果不可用」，那是规则层的事，
    # 由 REAL_OK 单独管（见 eval_rules 的 high_fp_on_real）。
    all_sig_ids = [s["id"] for s in load_raw_signatures() if not s.get("derived")]
    for name, text in bases.items():
        ok = name in REAL_OK
        samples.append(Sample(
            f"real:{name}", text, "negative", forbid=all_sig_ids,
            note=("真实计算，签名层必须干净；且结果可用（允许 WARNING 级提示）" if ok
                  else "真实计算，签名层必须干净；但结果不可用，不参与「成功日志」统计")))

    return samples


def write_samples(samples: list[Sample], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    labels = []
    for s in samples:
        p = out_dir / f"{s.name.replace(':', '_')}.log"
        p.write_text(s.text, encoding="utf-8")
        labels.append({"file": p.name, "kind": s.kind, "expect": s.expect,
                       "forbid": s.forbid, "note": s.note})
    (out_dir / "labels.json").write_text(
        json.dumps(labels, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return out_dir / "labels.json"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成带标签的失败样本")
    ap.add_argument("--out", default=str(ROOT / "demo" / "synth"),
                    help="输出目录（默认 demo/synth）")
    ap.add_argument("--check", action="store_true", help="只做锚点自检，不写文件")
    args = ap.parse_args(argv)

    samples = build_samples()

    if args.check:
        for s in samples:
            assert s.text, f"{s.name} 生成为空"
        print(f"OK：{len(samples)} 个样本全部生成成功")
        return 0

    labels_path = write_samples(samples, Path(args.out))
    n_rule = sum(1 for s in samples if s.kind == "rule")
    n_sig = sum(1 for s in samples if s.kind == "signature")
    n_neg = sum(1 for s in samples if s.kind == "negative")
    print(f"已写入 {labels_path.parent}")
    print(f"  规则正样本 {n_rule} / 签名正样本 {n_sig} / 负样本 {n_neg}")
    print(f"  标签清单 {labels_path.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
