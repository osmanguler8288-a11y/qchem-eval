#!/usr/bin/env python3
"""B-10（碎片检测）的对照组：真实的分子间复合物，以及一个故意的非相互作用极限。

为什么必须做这一组：B-10 的判据是「片段间距 > 4.5 Å」。这个阈值是照着
「正常分子间作用力的上界」推出来的（氢键 1.5–2.2、π 堆积 3.3–3.8、vdW 约 4.0），
但推理不等于证据。如果阈值定错了，凡是算复合物的人都会被误报，而这个系统
一旦在正常计算上瞎报警，就没人会再信它。

三个体系：

  1. water_dimer   水二聚体，O···O = 2.98 Å（文献值）
                   → 2 个片段，间距 2.98 < 4.5 → B-10 **不该**命中
  2. benzene_dimer 苯二聚体，平行堆积间距 3.4 Å
                   → 2 个片段，间距 3.4 < 4.5 → B-10 **不该**命中
  3. h2_pair_far   两个 H2 相距 10 Å（故意的非相互作用极限，BSSE 校正会这么算）
                   → 2 个片段，间距 9.26 > 4.5 → B-10 **该**命中（对照组，
                     证明规则不是死的、在前两个身上只是没触发而已）

几何来源：水用 O–H 0.957 Å / H–O–H 104.5°，苯用 C–C 1.39 / C–H 1.09 的
正六边形。这些是教科书值，够用来测「片段切分对不对」，不需要优化。

用法：
    python field_test/server/gen_complex.py
    python field_test/remote.py complex
"""

from __future__ import annotations

import math
from pathlib import Path

OUT = Path(__file__).resolve().parent / "complex"

LINK0 = "%nprocshared=1\n%mem=512MB"


def gjf(name: str, route: str, title: str, charge_mult: str, geom: str) -> None:
    (OUT / f"{name}.gjf").write_text(
        f"{LINK0}\n{route}\n\n{title}\n\n{charge_mult}\n{geom}\n\n",
        encoding="utf-8", newline="\n")


def water_dimer() -> None:
    """Cs 对称水二聚体。供体的两个 H 朝 +z，受体在 -z 侧、O···O = 2.98 Å。"""
    r_oh, ang = 0.957, math.radians(104.5 / 2)
    hy, hz = r_oh * math.sin(ang), r_oh * math.cos(ang)     # 0.757 / 0.586
    oo = 2.98
    lines = [
        f"O   {0.0:9.6f} {0.0:9.6f} {0.0:9.6f}",
        f"H   {0.0:9.6f} {hy:9.6f} {hz:9.6f}",          # 供体 H，指向受体
        f"H   {0.0:9.6f} {-hy:9.6f} {hz:9.6f}",
        f"O   {0.0:9.6f} {0.0:9.6f} {-oo:9.6f}",        # 受体 O
        f"H   {0.0:9.6f} {hy:9.6f} {-(oo + hz):9.6f}",
        f"H   {0.0:9.6f} {-hy:9.6f} {-(oo + hz):9.6f}",
    ]
    gjf("water_dimer", "#p HF/STO-3G", "water dimer, O...O = 2.98 A", "0 1",
        "\n".join(lines))


def benzene_ring(z: float, dy: float = 0.0) -> list[str]:
    """正六边形苯环，C–C 1.39 / C–H 1.09，放在 z 高度。"""
    rc, rh = 1.39, 2.48
    out = []
    for k in range(6):
        t = math.radians(60 * k)
        out.append(f"C   {rc*math.cos(t):9.6f} {rc*math.sin(t)+dy:9.6f} {z:9.6f}")
    for k in range(6):
        t = math.radians(60 * k)
        out.append(f"H   {rh*math.cos(t):9.6f} {rh*math.sin(t)+dy:9.6f} {z:9.6f}")
    return out


def benzene_dimer() -> None:
    """平行堆积，层间距 3.4 Å，上层沿 y 错开 1.5 Å（避免完全重叠的假象）。"""
    lines = benzene_ring(0.0) + benzene_ring(3.4, dy=1.5)
    gjf("benzene_dimer", "#p HF/STO-3G", "benzene dimer, pi stacking 3.4 A", "0 1",
        "\n".join(lines))


def h2_pair_far() -> None:
    """两个 H2 相距 10 Å —— 故意的非相互作用极限，B-10 应该报。"""
    lines = [
        f"H   {0.0:9.6f} {0.0:9.6f} {0.0:9.6f}",
        f"H   {0.74:9.6f} {0.0:9.6f} {0.0:9.6f}",
        f"H   {10.0:9.6f} {0.0:9.6f} {0.0:9.6f}",
        f"H   {10.74:9.6f} {0.0:9.6f} {0.0:9.6f}",
    ]
    gjf("h2_pair_far", "#p HF/STO-3G", "two H2 10 A apart, non-interacting limit",
        "0 1", "\n".join(lines))


def gen() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    water_dimer()
    benzene_dimer()
    h2_pair_far()
    print(f"已生成 {len(list(OUT.glob('*.gjf')))} 个复合物输入 -> {OUT}")


if __name__ == "__main__":
    gen()
