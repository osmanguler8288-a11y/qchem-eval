#!/usr/bin/env python3
"""生成用来校准阈值的真实计算输入。

## 为什么需要专门造这批

`field_test/real` 里的 28 份日志来自「能不能认出失败」，不是「阈值合不合理」。
两者的样本需求正好相反：

  · 认失败：需要**边界/崩溃**样本，越多越怪越好
  · 校阈值：需要**正常但接近边界**的样本 —— 因为阈值要放过它们

实测缺口很具体：28 份里只有 **2 份带频率**。于是 B-01/B-02/B-03/B-04/B-07
和 D-09 这 6 条规则，全部只有 2 个点可看，"余量"根本算不出来。
A-04（SCF > 50 步）也一样：现有最难的样本才 25 步（Fe-H2O），不知道 50 是
宽松还是误伤。

所以这批样本按「难度阶梯」设计，每一份都对着一条规则的边界：

| 输入 | 打的是哪条 | 预期落点 |
|---|---|---|
| `cal_dimer_hb` | B-03 低频 <100 | 氢键二聚体分子间摆动 100–200 cm⁻¹ |
| `cal_dimer_loose` | B-03 真阳性 | O-O 拉到 3.9 Å，最低频掉到几十 |
| `cal_h2_stretch` | A-04 SCF >50 步 | 从 2.0 Å 起步，优化沿途键在断与不断之间 |
| `cal_n2_stretch` | A-04 / C-05 | 三键拉伸，静态相关强，SCF 最慢 |
| `cal_hf_dimer` | B-03 另一类氢键 | F-H···F 线性，低频段不同 |
| `cal_cu_h2o` | C-02 自旋污染 / C-05 小 gap | d 电子，开壳层 |

## 一个刻意的选择：不从"完美几何"出发

拉伸样本的初始键长故意远离平衡值。理由是**真实工作流就是这么用的**：用户从
别的工具拿一个粗略结构直接算，不会先手工调到平衡。而且拉开键长会同时压出
SCF 难收敛（A-04）和优化步数多（D-08）两种真实压力，一份样本顶三份。

风险是可能跑飞（优化到断键产物）。对校准来说这不是失败——那份日志照样是
真实的「优化没收敛」样本，A-05 正缺这种。
"""

from __future__ import annotations

from pathlib import Path

OUT = Path(__file__).parent / "calib"


def gjf(name: str, route: str, title: str, charge_mult: str, geom: str,
        mem: str = "1GB", nproc: int = 1) -> None:
    """拼一个输入文件。

    坐标块之后必须有空行收尾：Gaussian 的 l101 读到 EOF 会报
    `End of file in ZSymb` 然后段错误，而报错信息完全不指向"少了一个空行"。
    这里统一由函数补，不手写。
    """
    body = (f"%nprocshared={nproc}\n%mem={mem}\n{route}\n\n{title}\n\n"
            f"{charge_mult}\n{geom}\n\n")
    (OUT / f"{name}.gjf").write_text(body, encoding="utf-8", newline="\n")


# ---------------------------------------------------------------- 几何

# 水二聚体：O-O 3.00 Å，供体水分子的一个 H 指向受体水的 O。
# 这是氢键体系里最低频的来源——两个分子绕氢键的摆动/扭转，实验值 100–200 cm⁻¹。
WATER_DIMER = """O   0.0000   0.0000  -1.5000
H   0.7570   0.5860  -1.2000
H  -0.7570   0.5860  -1.2000
O   0.0000   0.0000   1.5000
H   0.0000   0.7800   1.9200
H   0.0000  -0.7800   1.9200"""

# 同一个二聚体，但 O-O 拉到 3.90 Å。氢键基本断了，剩下的相互作用更弱，
# 最低频会掉到几十 cm⁻¹ —— 如果 B-03 的 100 阈值对它是真阳性，这条就该报；
# 如果认为"松散复合物的低频是物理的、不该报"，那 100 这个数就是错的。
WATER_DIMER_LOOSE = """O   0.0000   0.0000  -1.9500
H   0.7570   0.5860  -1.6500
H  -0.7570   0.5860  -1.6500
O   0.0000   0.0000   1.9500
H   0.0000   0.7800   2.3700
H   0.0000  -0.7800   2.3700"""

# HF 二聚体：线性 F-H···F-H。平衡 F-F 约 2.79 Å，这里给 2.50 Å 让它自己走。
HF_DIMER = """F   0.0000   0.0000   0.0000
H   0.0000   0.0000   0.9200
F   0.0000   0.0000   2.5000
H   0.0000   0.0000   3.4200"""

# H2 拉伸：平衡 0.74 Å，这里给 2.00 Å。沿途 SCF 要在"成键/不成键"之间找解，
# 是 A-04（步数）最干净的探针，且没有任何其它复杂度干扰。
H2_STRETCH = """H   0.0000   0.0000   0.0000
H   0.0000   0.0000   2.0000"""

# N2 拉伸：平衡 1.098 Å，给 1.30 Å。三键区域静态相关强，SCF 收敛振荡，
# 同时也是 C-05（HOMO-LUMO 小 gap）的自然候选。
N2_STRETCH = """N   0.0000   0.0000   0.0000
N   0.0000   0.0000   1.3000"""

# Cu-H2O：Cu 基态 ²S（3d¹⁰4s¹），配合物多重度 2。d 电子 + 开壳层，
# 同时压 C-02（<S²> 偏离）和 C-05（d 参与的前线轨道 gap 小）。
# 必须配 LANL2DZ：Cu 是重元素，不带 ECP 的基组在 Gaussian 里会直接报错。
CU_H2O = """Cu  0.0000   0.0000   0.0000
O   0.0000   0.0000   2.0000
H   0.7570   0.0000   2.3800
H  -0.7570   0.0000   2.3800"""


def gen() -> None:
    OUT.mkdir(parents=True, exist_ok=True)

    # opt freq 用同一个级别：频率只有在优化到极小点后才有意义，
    # 级别不一致的 opt+freq 会算出虚频来（那反而会污染 B-03 的分布）。
    LEVEL = "B3LYP/6-31G(d)"

    gjf("cal_dimer_hb", f"#p {LEVEL} opt freq",
        "water dimer, hydrogen bonded, O-O 3.0 A", "0 1", WATER_DIMER, mem="2GB")

    gjf("cal_dimer_loose", f"#p {LEVEL} opt freq",
        "water dimer, stretched to O-O 3.9 A", "0 1", WATER_DIMER_LOOSE, mem="2GB")

    gjf("cal_hf_dimer", f"#p {LEVEL} opt freq",
        "HF dimer, linear hydrogen bond", "0 1", HF_DIMER, mem="2GB")

    # 拉伸体系给 opt=loose + 更多步数：默认判据会在键断裂区来回震荡，
    # 那不是我们想看的「SCF 难度」，是优化器在打转。
    gjf("cal_h2_stretch", f"#p {LEVEL} opt=(maxstep=5) freq",
        "H2 stretched to 2.0 A then optimised", "0 1", H2_STRETCH, mem="1GB")

    gjf("cal_n2_stretch", f"#p {LEVEL} opt freq",
        "N2 stretched to 1.3 A", "0 1", N2_STRETCH, mem="1GB")

    # Cu 用 LANL2DZ：重元素必须带 ECP，且 DZ 对 d 电子够用。
    # 只做单点 + 频率太贵（Cu 频率要数值二阶导），所以这一份只出单点能量与轨道。
    gjf("cal_cu_h2o", "#p UB3LYP/LANL2DZ",
        "Cu-water complex, open shell doublet", "0 2", CU_H2O, mem="1GB")

    print(f"generated {len(list(OUT.glob('*.gjf')))} inputs in {OUT}")


if __name__ == "__main__":
    gen()
