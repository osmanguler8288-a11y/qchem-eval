#!/usr/bin/env python3
"""造几种「明显错误的结构」，看规则引擎能不能从输出日志里判出来。

这一组专门回答一个问题：用户手里有个错得很离谱的结构（键长荒谬、原子重叠、
原子写错），引擎能不能发现？

关键前提：引擎读的是**输出日志**，不是输入结构。所以它只能看「这个错误结构
算完之后留下了什么痕迹」。有些错误会留下痕迹（崩了、能量荒谬），有些不会
（Gaussian 老老实实把它算完，报告全绿）——后者才是要暴露的盲区。

用法：
    python field_test/server/gen_badgeom.py            # 生成 gjf
    python field_test/remote.py badgeom                # 推到 cu04 跑并拉回
"""

from __future__ import annotations

from pathlib import Path

OUT = Path(__file__).resolve().parent / "badgeom"

LINK0 = "%nprocshared=1\n%mem=512MB"
ROUTE = "#p HF/STO-3G"


def gjf(name: str, title: str, charge_mult: str, geom: str) -> None:
    body = f"{LINK0}\n{ROUTE}\n\n{title}\n\n{charge_mult}\n{geom}\n\n"
    (OUT / f"{name}.gjf").write_text(body, encoding="utf-8", newline="\n")


def gen() -> None:
    OUT.mkdir(parents=True, exist_ok=True)

    # 1) 两个氢原子坐标完全重合 —— 核间距离 0，库仑项发散
    gjf("bad_overlap", "H2 with coincident atoms", "0 1",
        "H   0.000000   0.000000   0.000000\n"
        "H   0.000000   0.000000   0.000000")

    # 2) H2 键长 5.0 Å —— 比正常值（0.74）大近 7 倍，等于两个互不相干的氢原子
    gjf("bad_bondlen", "H2 with 5 Angstrom bond", "0 1",
        "H   0.000000   0.000000   0.000000\n"
        "H   5.000000   0.000000   0.000000")

    # 3) 水分子三个原子排成一条直线 —— 键角 180°，物理上不可能稳定
    gjf("bad_collinear", "water forced collinear", "0 1",
        "O   0.000000   0.000000   0.000000\n"
        "H   0.960000   0.000000   0.000000\n"
        "H  -0.960000   0.000000   0.000000")

    # 4) 水分子配 +1 电荷、二重态 —— 9 个电子，奇数配二重态本身不矛盾，
    #    但 O 带 +1 且只有 1 个 H 时才是正常离子。这里 3 个原子却要 9 电子，
    #    实际是「把一个电子凭空拿掉」，得到的是 H2O+ 而不是水。
    gjf("bad_charge", "water declared as cation", "1 2",
        "O   0.000000   0.000000   0.117300\n"
        "H   0.000000   0.757200  -0.469200\n"
        "H   0.000000  -0.757200  -0.469200")

    # 5) 想做水，但把 O 写成了 C —— 输入语法完全合法，Gaussian 会认真算
    #    一个 CH2 单重态。引擎没有任何办法知道「用户本来想要氧」。
    gjf("bad_element", "meant water but wrote C", "0 1",
        "C   0.000000   0.000000   0.117300\n"
        "H   0.000000   0.757200  -0.469200\n"
        "H   0.000000  -0.757200  -0.469200")

    # 6) 甲烷里两个 H 落在同一点 —— 局部重叠，不是全局重合
    gjf("bad_dup_h", "methane with two H duplicated", "0 1",
        "C   0.000000   0.000000   0.000000\n"
        "H   0.629000   0.629000   0.629000\n"
        "H  -0.629000  -0.629000   0.629000\n"
        "H  -0.629000   0.629000  -0.629000\n"
        "H   0.629000   0.629000   0.629000\n"
        "H   0.629000  -0.629000  -0.629000")

    print(f"已生成 {len(list(OUT.glob('*.gjf')))} 个错误结构输入 -> {OUT}")


if __name__ == "__main__":
    gen()
