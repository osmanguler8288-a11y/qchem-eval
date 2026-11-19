"""Gaussian 输出的单遍流式解析器。

为什么不用 cclib 当主力：
  cclib 会把整个文件重建成对象模型（Molecule 数组、MO 系数……），信息全但慢。
  我们只需要十来个标量字段，用一次顺序扫描即可，实测比 cclib 快两个数量级。
  cclib 保留为**交叉校验**用途（bench 子命令），不参与主流程。

只做「取事实」，不做任何判断——判断全部在 rules/ 里。
"""

from __future__ import annotations

import re
from collections import deque
from pathlib import Path

from ..schema import MAX_UNPARSED_HITS, CalcData

HARTREE_TO_EV = 27.211386245988

# 收敛表四行。注意用 ^\s* —— 传入的是 strip() 之后的串，写成 ^\s+ 会一条都匹配不上
# （这个坑踩过一次：A-05 靠 Optimization completed 那行兜底才"看起来正常"）
_OPT_ITEM = re.compile(
    r"^\s*(Maximum Force|RMS\s+Force|Maximum Displacement|RMS\s+Displacement)"
    r"\s+(-?\d+\.\d+)\s+(-?\d+\.\d+)\s+(YES|NO)"
)
_CHARGE = re.compile(r"Charge\s*=\s*(-?\d+)\s+Multiplicity\s*=\s*(-?\d+)")
_SCF_DONE = re.compile(
    r"SCF Done:\s+E\(([^)]+)\)\s*=\s*(-?\d+\.\d+)\s+A\.U\. after\s+(\d+) cycles"
)
_MAXCYCLE = re.compile(r"within\s+(\d+)\s+cycles")
# 日志自己声明的 SCF 收敛判据。三种 "Requested convergence" 行里只有这条是 RMS 密度矩阵：
#   Requested convergence on             energy=1.00D-06.
#   Requested convergence on MAX density matrix=1.00D-06.
#   Requested convergence on RMS density matrix=1.00D-08 within 128 cycles.   <- 要这条
_CONV_TARGET = re.compile(r"on\s+RMS\s+density\s+matrix\s*=\s*(\S+)")
_DELTA_E = re.compile(r"E=\s*(-?\d+\.\d+)\s+Delta-E=\s*(-?\d+\.\d+)\s+Rises=(\w)\s+Damp=(\w)")
_FREQ = re.compile(r"Frequencies\s+-+\s+(.+)")
_S2_ANNIHIL = re.compile(r"S\*\*2\s+before annihilation\s+(-?\d+\.\d+)")
_S2_ARCHIVE = re.compile(r"<S\*\*2>=\s*(-?\d+\.\d+)")
_OCC = re.compile(r"Alpha\s+occ\. eigenvalues\s+-+\s+(.+)")
_VIRT = re.compile(r"Alpha\s+virt\. eigenvalues\s+-+\s+(.+)")
_ZPE = re.compile(r"Sum of electronic and zero-point Energies=\s*(-?\d+\.\d+)")
_GIBBS = re.compile(r"Sum of electronic and thermal Free Energies=\s*(-?\d+\.\d+)")
_NATOMS = re.compile(r"NAtoms=\s*(\d+)")
# "Standard basis: 6-31G(d) (5D, 7F)"
#                    ^^^^^^^^  ^^^^^^^^^
#                    基组名    球谐标记，不属于基组名
#
# 老写法 [^(]+ 会在**第一个** "(" 处切断，把 6-31G(d) 读成 6-31G。后果不是显示问题：
#   · 报告里印出 "b3lyp/6-31G"，而实际算的是 6-31G(d) —— 报告在误报方法信息
#   · 极化函数标记丢失 → 无法判断基组是否带极化，C 类规则失去判断依据
# 所以只剥掉结尾的 (nD, mF) 球谐标记，6-31G(d) / 6-311++G(2d,2p) 里的括号要留着。
_STD_BASIS = re.compile(
    r"Standard basis:\s*(.+?)\s*(?:\(\s*\d+D\s*,\s*\d+F\s*\))?\s*$"
)
_ROUTE = re.compile(r"^ *#+[pPnN]?\s*(.+)")
_LINK = re.compile(r"l(\d+)\.exe")
# 官方错误目录 #10 里这两个字段与 SCF Done 同屏出现，是判断波函数质量的直接依据。
# 两种写法都要吃下：
#   G16 实机 : "NFock= 12  Conv=0.13D-08     -V/T= 2.0160"      <- Conv，无 g
#   目录 #10 : "Convg  =    0.8831D-03      -V/T = 2.0048"      <- Convg，有 g
# 指数记法也必须两种都吃：Gaussian 自己打 D，但某些后处理/重排文件会变成 E。
# 只认 D 的话 "Conv=1.9E-09" 会被静默丢掉，下游看到的是「缺字段」——
# 把解析器的缺陷伪装成数据缺失，这是最坏的一类不可观测。
_NUM = r"[-+]?(?:\d+\.\d*|\.\d+)(?:[DdEe][-+]?\d+)?"
_NUM_RE = re.compile(_NUM)
# 捕获组一律用 (\S+) 先抓**整个 token**，再拿 _NUM_RE 做全文校验，理由见 _grab_num。
_CONVG = re.compile(r"Conv(?:g)?\s*=\s*(\S+)")
_VT = re.compile(r"-V/T\s*=\s*(\S+)")

# 所有关心的标记在 strip() 之后都位于行首，所以可以用一次 C 层的 startswith(元组)
# 把 99% 的行挡掉。实测比逐个子串 in 判断快 15 倍（7309 行：1.6 ms vs 23.6 ms）。
MARKERS = (
    "SCF Done:", "E=", "Converged?", "Maximum", "RMS", "Frequencies",
    "Normal termination", "Error termination", "Convergence failure",
    "Requested convergence", "Optimization ", "Number of steps exceeded",
    "Charge =", "NAtoms=", "Standard", "S**2", "Alpha", "Sum of electronic", "#",
    "Conv", "NFock=", "Change in point group",
    # 异常终止的其他入口：这些行之后往往就再没有输出了
    "Erroneous write", "writwa", "No space left", "Probably out of disk",
    "Write error in NtrExt1", "malloc failed", "galloc:", "PGFIO",
    "segmentation violation",
)

# 这几种行代表「任务在这里停了」，要抓上下文快照
ERROR_MARKS = (
    "Error termination", "Erroneous write", "writwa", "No space left",
    "Probably out of disk", "Write error in NtrExt1", "malloc failed",
    "galloc:", "PGFIO", "segmentation violation",
)

# 官方错误块是多行的，而且**病因行常在终止行之后**：
#   Error termination in NtrErr:        <- 触发抓块
#   NtrErr Called from FileIO.          <- 真正的原因，本身不匹配任何 ERROR_MARK
#   Operation on file out of range.
# 只收 ERROR_MARK 行会把病因丢掉，SIG-FIL-1 就是这么漏检过一轮。
ERROR_TAIL = 8          # 终止行之后最多再收几行
# 见到这些前缀说明已经回到正常输出（多段 link1 作业里尤其需要这个刹车）
ERROR_STOP = (
    "SCF Done:", "Standard basis", "Standard orientation", "Normal termination",
    "Leave Link", "(Enter", "NAtoms=", "Charge =", "Requested convergence",
)


# 以 r 开头、但那个 r 不是自旋前缀的泛函名（revPBE / revTPSS / r2SCAN / RPBE）。
# 这里的 r 是 "revised" 或名字本身的一部分，剥掉就成了另一个方法 —— 这类
# 「归一化把数据改错」比不归一化危险得多，所以用白名单排除而不是一概剥掉。
_NOT_SPIN_PREFIX_R = ("ro", "rev", "r2", "rpbe")


def _strip_spin_prefix(label: str) -> str:
    """E(UB3LYP) -> B3LYP，RB3LYP -> B3LYP；但 ROHF 不能变成 OHF。

    大小写都要吃，因为有两个来源：
      · `SCF Done:  E(RB3LYP) = ...`              —— 大写
      · route 段里 Gaussian 自动生成的第二段 `RB3LYP/6-31G(d)` —— 先小写再进来
    只判大写会让 route 那条路静默归一化失败，于是 `opt freq` 的第二段被当成
    「换了泛函」（E-08 就是这么在正常日志上误响的）。
    """
    if len(label) <= 2:
        return label
    head = label[0].lower()
    if head == "u":
        return label[1:]
    if head == "r" and not label.lower().startswith(_NOT_SPIN_PREFIX_R):
        return label[1:]
    return label


def _to_float(tok: str) -> float | None:
    """把 Gaussian 的数字转成 float。D/E 两种指数记法都吃，失败返回 None。

    返回 None 而不是抛异常，是为了让调用方**必须显式决定**失败时怎么办：
    静默跳过（并留痕）还是记诊断。这是把「静默 except: pass」堵死的关键。
    """
    try:
        return float(tok.replace("D", "E").replace("d", "e"))
    except (ValueError, AttributeError):
        return None


def _to_int(tok: str) -> int | None:
    """同 _to_float：失败返回 None，强制调用方显式处理。"""
    try:
        return int(tok)
    except (ValueError, TypeError):
        return None


def _grab_num(pat: re.Pattern[str], s: str) -> tuple[float | None, str | None]:
    """从一行里抓一个数值，返回 (值, 失败时的原始 token)。

    为什么不能直接吃正则捕获组：`_NUM` 允许 `\\d+\\.\\d*`（小数部分可省），
    于是一串垃圾 `Conv=0.1x9D-08` 会被**部分匹配**成 `0.1` —— 正则报"成功"，
    我们就心安理得地把一个错得离谱的数交给下游（A-06 会拿 0.1 和 1e-8 比，
    得出"超出 1e7 倍"的结论并判 CRITICAL）。

    这比解析失败更危险：解析失败至少是"没有数据"，而部分匹配是"有一个错误的数据"，
    下游无从分辨。所以这里先抓整个非空白 token，再 fullmatch 全文校验 ——
    只要 token 不是纯数字就一律算失败，把决定权交回调用方去留痕。
    """
    m = pat.search(s)
    if not m:
        return None, None
    tok = m.group(1)
    if _NUM_RE.fullmatch(tok) is None:
        return None, tok
    return _to_float(tok), None


def _floats(text: str) -> list[float]:
    """从一段文本里抽出所有能转成浮点的 token。

    这里**允许**静默跳过不可转的 token：调用方已经用正则切出了数值区
    （"Frequencies ---" / "Alpha occ. eigenvalues --" 之后），剩下的杂质
    （续行的 `**`、单位、空列）本来就不是数据。为它们报诊断只会制造噪音，
    真正需要报的是「整行都没抽出数」——那个由调用方判断。
    """
    out = []
    for tok in text.replace("D", "E").replace("d", "e").split():
        try:
            out.append(float(tok))
        except ValueError:
            pass
    return out


def parse_gaussian(path: str | Path) -> tuple[CalcData, int]:
    """解析一个 Gaussian 输出文件。

    返回 (CalcData, 行数)。行数用于性能报告。
    """
    path = Path(path)
    d = CalcData(source=str(path), software="gaussian")
    # 本解析器会逐行通读全文，所以这里的「0 次」是真实观测结果（而不是缺数据）。
    # 保持 None 的话，占位规则会被判 UNKNOWN；置 0 才能让 "从未出现" 成为可判定的结论。
    d.n_jobs = 0
    d.n_point_group_changes = 0

    dash_count = 0
    orient_state = 0          # 0=未进入 1=跳表头 2=读原子
    orient_zs: list[int] = []
    orient_xyz: list[tuple[float, float, float]] = []
    last_opt_table: dict[str, str] = {}
    cur_opt_table: dict[str, str] = {}
    n_lines = 0

    # 滚动缓冲：真正的原因行出现在 "Error termination" 的上面，只存终止行等于
    # 只有症状没有病因。18 行足够覆盖官方目录里最长的上下文（l9999 那张收敛表）。
    buf: deque[str] = deque(maxlen=18)
    in_error = False          # 同一次异常终止内的多行标记合并成一个 block
    pending = 0               # 终止行之后已追加的病因行数

    with open(path, "r", encoding="utf-8", errors="replace", buffering=1 << 20) as fh:
        for line in fh:
            n_lines += 1
            s = line.strip()
            if not s:
                continue
            buf.append(s)

            # ---- 异常终止之后的病因行（必须排在 MARKERS 闸门之前）----
            # 这些行（"fd = 3" / "NtrErr Called from FileIO." / "Operation on file
            # out of range."）都不以 MARKERS 开头，放在闸门后面就永远收不到。
            if in_error:
                if pending < ERROR_TAIL and not s.startswith(ERROR_STOP):
                    d.error_blocks[-1]["lines"].append(s)
                    pending += 1
                else:
                    in_error = False

            # ---- 原子坐标块（取元素 + 末帧坐标）----
            if orient_state == 1:
                if s.startswith("---"):
                    dash_count += 1
                    if dash_count >= 2:
                        orient_state = 2
                continue
            if orient_state == 2:
                if s.startswith("---"):
                    orient_state = 0
                    d.atomic_numbers = orient_zs
                    if orient_xyz:
                        d.coords_last = orient_xyz
                else:
                    parts = s.split()
                    # 列序是 Center | AtomicNumber | AtomicType | X | Y | Z —— 共 6 列。
                    # 老版本日志没有 AtomicType 列（5 列）。这里必须按列数分支：
                    # 一律取 parts[2:5] 会把 AtomicType(恒为 0) 当成 X，Z 坐标被丢掉，
                    # 结果是所有原子都挤在 x=0 平面上（B-05 因此误报过原子重叠）。
                    if len(parts) >= 6:
                        z_tok, xyz = parts[1], parts[3:6]
                    elif len(parts) >= 5:
                        z_tok, xyz = parts[1], parts[2:5]
                    else:
                        z_tok = xyz = None
                    if z_tok is not None:
                        z = _to_int(z_tok)
                        if z is None:
                            d.note_unparsed("atomic_numbers", s,
                                            f"原子序数无法转换: {z_tok!r}")
                        else:
                            orient_zs.append(z)
                            if z == 0 and d.has_dummy_atoms is None:
                                d.has_dummy_atoms = True   # 虚原子 X / Bq
                            x, y, zc = (_to_float(xyz[0]), _to_float(xyz[1]),
                                        _to_float(xyz[2]))
                            if None in (x, y, zc):
                                # 坐标取不到就不往 coords_last 里塞半条记录：
                                # 长度对不上 natoms 会被 E-03 判「原子数不一致」，
                                # 那是把解析失败说成输入错误，方向反了。
                                d.note_unparsed("coords_last", s,
                                                f"坐标无法转换: {xyz!r}")
                            else:
                                orient_xyz.append((x, y, zc))
                    else:
                        d.note_unparsed("coords_last", s, "列数不足（<5），疑似表格错位")
                continue

            # ---- 无关行直接跳过（一次 C 层前缀判定）----
            if not s.startswith(MARKERS):
                continue

            # ---- 异常终止：抓上下文快照（必须排在终止分支之前）----
            if s.startswith(ERROR_MARKS):
                m = _LINK.search(s)
                link = int(m.group(1)) if m else None
                if not in_error:
                    # buf 的末尾就是当前行，直接整块搬进来，不用再 append
                    d.error_blocks.append({"link": link, "lines": list(buf)})
                    in_error = True
                    pending = 0
                else:
                    # 同一段终止里的后续 ERROR_MARK 行（如 l9999 的两行、磁盘类的
                    # writwa 变体）要并进同一个 block；link 为空时也不能丢行。
                    d.error_blocks[-1]["lines"].append(s)
                    if link is not None:
                        d.error_blocks[-1]["link"] = d.error_blocks[-1]["link"] or link
                d.error_terminations.append(s[:120])
                d.normal_termination = False
                continue

            # ---- 高频标记：先用前缀确认，再走正则 ----
            if s.startswith("SCF Done:"):
                m = _SCF_DONE.search(s)
                if m:
                    label = m.group(1)
                    d.functional = d.functional or _strip_spin_prefix(label)
                    d.unrestricted = label.startswith("U")
                    d.scf_energies.append(float(m.group(2)))
                    d.scf_cycles.append(int(m.group(3)))
                continue

            if s.startswith("E="):
                m = _DELTA_E.search(s)
                if m:
                    d.scf_trace.append(
                        {
                            "e": float(m.group(1)),
                            "de": float(m.group(2)),
                            "rises": m.group(3) == "T",
                            "damp": m.group(4) == "T",
                        }
                    )
                continue

            if s.startswith("Converged?"):
                cur_opt_table = {}
                continue

            # SCF Done 的续行，两种格式：
            #   NFock= 12  Conv=0.13D-08     -V/T= 2.0160   (G16)
            #   Convg  =    0.8831D-03       -V/T = 2.0048   (老版本 / 目录 #10)
            # 注意别写成裸 startswith("Conv")：那样 "Convergence failure" 会在这里被
            # 吃掉并 continue，下面的 SCF 不收敛分支就永远进不去了。
            if s.startswith(("NFock=", "Convg", "Conv=")):
                v, bad = _grab_num(_CONVG, s)
                if bad is not None:
                    # 抓到了 token 但不是合法数字（如 Conv=0.1x9D-08）——
                    # 这是**解析器给了个错值**，比没解析更危险，必须留痕且不入库。
                    d.note_unparsed("scf_conv", s, f"数值格式非法: {bad!r}")
                elif v is not None:
                    d.scf_conv.append(v)
                elif "Conv" in s:
                    # 行里有 Conv 但正则没吃下 —— 这是**解析器的缺陷**，不是日志缺数据。
                    # 必须留痕，否则下游只会看到「scf_conv 为空」而误判为用户的问题。
                    d.note_unparsed("scf_conv", s, "行内有 Conv 但未匹配到数值")
                v, bad = _grab_num(_VT, s)
                if bad is not None:
                    d.note_unparsed("virial_ratio", s, f"数值格式非法: {bad!r}")
                elif v is not None:
                    d.virial_ratio = v
                elif "-V/T" in s:
                    d.note_unparsed("virial_ratio", s, "行内有 -V/T 但未匹配到数值")
                continue

            if s.startswith("Change in point group"):
                d.n_point_group_changes += 1
                continue
            # 收敛表的行都以 YES/NO 结尾；前缀 Maximum / RMS 与 Requested convergence 不冲突
            if s.startswith(("Maximum", "RMS")):
                m = _OPT_ITEM.match(s)
                if m:
                    # 表里 "RMS     Force" 有多个空格，归一成 "RMS Force"
                    key = " ".join(m.group(1).split())
                    cur_opt_table[key] = m.group(4)
                    if len(cur_opt_table) == 4:
                        last_opt_table = dict(cur_opt_table)
                        d.opt_steps = (d.opt_steps or 0) + 1
                continue

            if s.startswith("Frequencies"):
                m = _FREQ.search(s)
                if m:
                    d.freqs.extend(_floats(m.group(1)))
                    if "freq" not in d.stages:
                        d.stages.append("freq")
                continue

            # ---- 低频标记 ----
            if s.startswith("Normal termination"):
                d.normal_termination = True
                d.n_jobs += 1
                in_error = False
                continue

            if s.startswith("Error termination"):
                d.error_terminations.append(s[:120])
                d.normal_termination = False
                continue

            if s.startswith("Convergence failure"):
                d.scf_converged = False
                if s[:120] not in d.error_terminations:
                    d.error_terminations.append(s[:120])
                continue

            if s.startswith("Requested convergence"):
                if d.scf_maxcycle is None:
                    m = _MAXCYCLE.search(s)
                    if m:
                        v = _to_int(m.group(1))
                        if v is None:
                            d.note_unparsed("scf_maxcycle", s,
                                            f"cycles 无法转换: {m.group(1)!r}")
                        else:
                            d.scf_maxcycle = v
                if d.scf_conv_target is None:
                    v, bad = _grab_num(_CONV_TARGET, s)
                    if bad is not None:
                        d.note_unparsed("scf_conv_target", s,
                                        f"判据格式非法: {bad!r}")
                    elif v is not None:
                        d.scf_conv_target = v
                continue

            if s.startswith("Optimization completed"):
                d.opt_converged = True
                if "opt" not in d.stages:
                    d.stages.append("opt")
                continue

            if s.startswith(("Optimization stopped", "Number of steps exceeded")):
                d.opt_stopped = True
                d.opt_converged = False
                if "opt" not in d.stages:
                    d.stages.append("opt")
                continue

            if s.startswith("Charge ="):
                m = _CHARGE.search(s)
                if m:
                    d.charge = int(m.group(1))
                    d.multiplicity = int(m.group(2))
                continue

            if s.startswith("NAtoms="):
                if d.natoms is None:
                    m = _NATOMS.search(s)
                    if m:
                        d.natoms = int(m.group(1))
                continue

            if s.startswith("Standard basis:"):
                m = _STD_BASIS.search(s)
                if m:
                    d.basis = m.group(1).strip()
                continue

            if s.startswith("Standard orientation"):
                orient_state = 1
                dash_count = 0
                orient_zs = []
                # 必须和 orient_zs 一起清空：优化任务每步都打一个 Standard orientation，
                # 漏清会让末帧坐标变成「所有帧首尾相接」，20 原子读出 120 组坐标，
                # 跨帧重复原子把最小间距压成 0（B-05 会连正常优化一起误杀）。
                orient_xyz = []
                continue

            if s.startswith("S**2"):
                m = _S2_ANNIHIL.search(s)
                if m:
                    d.s2 = float(m.group(1))       # 湮灭前的原始值，才是污染程度
                else:
                    m = _S2_ARCHIVE.search(s)
                    if m:
                        d.s2 = float(m.group(1))
                continue

            if s.startswith("Alpha"):
                if "occ. eigenvalues" in s:
                    m = _OCC.search(s)
                    if m:
                        vals = _floats(m.group(1))
                        if vals:
                            d.homo_ev = vals[-1] * HARTREE_TO_EV
                elif "virt. eigenvalues" in s:
                    m = _VIRT.search(s)
                    if m:
                        vals = _floats(m.group(1))
                        if vals and d.lumo_ev is None:
                            d.lumo_ev = vals[0] * HARTREE_TO_EV
                continue

            if s.startswith("Sum of electronic"):
                if "zero-point Energies=" in s:
                    m = _ZPE.search(s)
                    if m:
                        d.zpe_energy = float(m.group(1))
                elif "thermal Free Energies=" in s:
                    m = _GIBBS.search(s)
                    if m:
                        d.gibbs_energy = float(m.group(1))
                continue

            if s.startswith("#"):
                m = _ROUTE.match(line.rstrip())
                if not m:
                    continue
                seg = m.group(1).strip()
                if d.route is None:
                    # 只有首段 route 是「用户写的那个」。后段（opt freq 的第二段、
                    # link1 串联出来的后续作业）是 Gaussian 自己生成的，
                    # 让它去写 functional / basis / route_basis，用户声明的值就被
                    # 静默顶掉，E-05（route 声明 vs 实际基组）会永远比不出东西。
                    d.route = seg
                    _parse_route(seg, d)
                # 每段都收集「泛函/基组」，给 E-08 判段与段之间方法变没变。
                # 相邻重复合并（opt freq 两段归一化后本就相同），保留顺序，
                # 这样 A→B→A 这种来回切也能在 evidence 里原样显示出来。
                func, basis = _route_method_basis(seg)
                if func is not None and basis is not None:
                    key = f"{func}/{basis}"
                    if not d.route_methods or d.route_methods[-1] != key:
                        d.route_methods.append(key)
                continue

    # ---- 收尾归一 ----
    if d.opt_steps is None and last_opt_table:
        d.opt_steps = 1
    d.opt_last_table = last_opt_table
    if last_opt_table and d.opt_converged is None:
        d.opt_converged = all(v == "YES" for v in last_opt_table.values())
    if d.opt_converged is False and d.opt_stopped is None and last_opt_table:
        d.opt_stopped = True

    if d.n_imag is None and d.freqs:
        d.n_imag = sum(1 for f in d.freqs if f < 0)

    if d.scf_converged is None and d.scf_energies:
        d.scf_converged = True          # SCF Done 只在收敛时打印

    if d.multiplicity is not None:
        s_ = (d.multiplicity - 1) / 2
        d.s2_expected = s_ * (s_ + 1)

    if d.normal_termination is None:
        d.normal_termination = False    # 没看到终止标志 = 没正常结束

    # 日志直接断掉（超时被杀/配额满）时没有任何 error 行，也要留一份末尾快照，
    # 否则「为什么停的」这条线索彻底丢失
    if not d.normal_termination and not d.error_blocks:
        d.error_blocks.append({"link": None, "lines": list(buf)})

    if d.has_dummy_atoms is None and d.atomic_numbers:
        d.has_dummy_atoms = False

    if not d.atomic_numbers and d.natoms:
        d.parse_warnings.append("未取到元素信息（无 Standard orientation 块）")

    # 「日志里明明有、是我没解析出来」必须让上层看得见。
    # 不提升到 parse_warnings 的话，它在下游只表现为「某个字段是空的」，
    # 规则层会判 UNKNOWN，用户看到的结论是「你的日志缺数据」——方向完全错了。
    if d.unparsed_hits:
        fields = sorted({h["field"] for h in d.unparsed_hits})
        n = len(d.unparsed_hits)
        capped = "（已截断）" if n >= MAX_UNPARSED_HITS else ""
        d.parse_warnings.append(
            f"{n} 处数据未能解析{capped}：{', '.join(fields)}。"
            f"这是解析器侧的缺口，不是日志缺失，明细见 unparsed_hits"
        )

    return d, n_lines


def _route_method_basis(route: str) -> tuple[str | None, str | None]:
    """从一段 route 里取 (泛函, 基组)，都做小写归一化。

    抽成独立函数是为了**逐段比对**：`opt freq` 在 Gaussian 内部必定拆成两段
    link1，第二段是它自动生成的

        #P Geom=AllCheck Guess=TCheck SCRF=Check GenChk RB3LYP/6-31G(d) Freq

    泛函多了个 R 前缀、基组和第一段一样，还夹着一堆流程控制词。所以拿两段
    route 原文直接比会永远"不同"——必须先归一化（剥掉 R/U 自旋前缀）再比。
    真正该警惕的是两段用了不同的方法或基组（例如 opt 用 STO-3G、freq 用
    6-31G(d)，那样频率与几何不自洽）。
    """
    low = route.lower()
    # 泛函/基组对：b3lyp/sto-3g、wb97xd/def2tzvp、ccsd(t)/aug-cc-pvdz
    # 左半边不允许裸括号，否则 "#p iop(6/7=3) ..." 会被读成泛函 "iop(6"、基组 "7"
    m = re.search(r"\b([a-z][a-z0-9\-]*(?:\([a-z]\))?)/([a-z0-9\-\+\(\)\*,]+)", low)
    if not m:
        return None, None
    return _strip_spin_prefix(m.group(1)), m.group(2)


def _parse_route(route: str, d: CalcData) -> None:
    """从 route 段抽方法信息。basis 另有权威来源（Standard basis）。"""
    low = route.lower()

    func, basis = _route_method_basis(route)
    if func is not None and basis is not None:
        d.functional = d.functional or func
        # 单独存一份 route 里的**声明值**，用于和 Standard basis 的实际值比对。
        # 不能直接拿 d.basis 比：d.basis 会被后面的 Standard basis 行覆盖，
        # 也就丢掉了"你写的是什么"这个信息 —— 而 geom=allcheck / guess=read
        # 恰恰会让 chk 里的旧基组静默顶掉 route 里的声明。
        d.route_basis = basis
        if basis in ("gen", "genecp"):
            # 基组写在输入文件里，输出里查不到。不要假装知道，交给规则层判 UNKNOWN
            d.parse_warnings.append(
                "基组为 Gen/GenECP，输出文件不含具体基组定义，无法核对基组质量"
            )
        else:
            d.basis = d.basis or basis

    # 阶段关键字不能靠 `"opt" in low.split()` 精确匹配——真实 route 里它们几乎
    # 总是带参数：opt=ts、opt=(calcfc,maxstep=5)、freq=hpmodes、td=(nstates=10)。
    # 精确匹配会让 A-05（优化未收敛）、E-01（opt 缺 freq）这类规则的 applies
    # 条件直接判 na，等于规则整条失效。所以按 "=" / "(" 切出关键字再比对。
    toks = {re.split(r"[=(]", t, maxsplit=1)[0] for t in low.split()}
    for kw in ("opt", "freq", "td", "scan"):
        if kw in toks and kw not in d.stages:
            d.stages.append(kw)

    for tag in ("gd3bj", "gd3", "d3bj", "d3"):
        if f"empiricaldispersion={tag}" in low.replace(" ", ""):
            d.dispersion = tag.upper()
            break

    m = re.search(r"scrf=\(?([a-z0-9]+)(?:,\s*solvent=([a-z0-9]+))?", low)
    if m:
        d.solvent = m.group(1).upper()
        if m.group(2):
            d.solvent += f"/{m.group(2)}"

    if not d.stages:
        d.stages.append("sp")
