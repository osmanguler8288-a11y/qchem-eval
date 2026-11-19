#!/usr/bin/env python3
"""生成在 cu04 上跑的 Gaussian 16 输入文件。

规则：Gaussian 要求坐标块之后**必须有一个空行**表示输入结束，
否则 l101 读 Z-matrix 时会撞到 EOF，报 `End of file in ZSymb` 然后段错误。
这一点在 heredoc 里非常容易漏（第一版冒烟测试就是这么崩的），所以这里
统一由一个函数拼装并强制收尾空行，不手写。

用例分三类：
  ok_*    预期正常终止，用来验「不误报」和「能解析多任务类型」
  fail_*  预期真实失败，用来验「能不能认出来、认对类型」
  特殊处理见 allcheck（需要两段作业共享 chk）
"""

from __future__ import annotations

from pathlib import Path

OUT = Path(__file__).parent / "inputs"

WATER = """O   0.0000   0.0000   0.1173
H   0.0000   0.7572  -0.4692
H   0.0000  -0.7572  -0.4692"""

O2 = """O   0.0000   0.0000   0.6000
O   0.0000   0.0000  -0.6000"""

HCN_TS = """H   0.0000   1.1000   0.0000
C   0.0000   0.0000   0.0000
N   0.0000  -1.1500   0.0000"""

FE_WATER = """Fe  0.0000   0.0000   0.0000
O   0.0000   0.0000   2.0000
H   0.7572   0.0000   2.4692
H  -0.7572   0.0000   2.4692"""

DVB = """C   0.0000   0.0000   0.0000
C   1.3900   0.0000   0.0000
C   2.0950   1.2000   0.0000
C   3.4850   1.2000   0.0000
C   4.1800   0.0000   0.0000
C   3.4850  -1.2000   0.0000
C   2.0950  -1.2000   0.0000
C   4.8800   1.2100   0.0000
C   6.2800   1.2100   0.0000
C  -1.3900   0.0000   0.0000
H   0.0000   0.0000   1.0800
H  -2.0000   0.0000   1.0800
H   7.0000   0.0000   0.0000"""


def gjf(name: str, link0: str, route: str, title: str, charge_mult: str, geom: str,
        tail: str = "") -> None:
    body = f"{link0}\n{route}\n\n{title}\n\n{charge_mult}\n{geom}\n"
    # 坐标块后的空行是硬要求：不加 tail 时它充当「输入结束」标记；
    # 加了 tail（gen 基组）时它是坐标块与基组块的分隔符 —— 少了，Gaussian 会把
    # 基组第一行 `H 0` 当坐标行读，报 `Found an end-of-line for input` 再段错误。
    # tail 本身不带结尾换行，所以这里要补两个 \n（一个是 tail 的行尾，一个是空行），
    # 否则基组读完会报 `End of file reading basis center`。
    body += "\n" + tail + "\n\n" if tail else "\n"
    (OUT / f"{name}.gjf").write_text(body, encoding="utf-8", newline="\n")


def gen() -> None:
    OUT.mkdir(parents=True, exist_ok=True)

    # ---------- 预期成功 ----------
    gjf("ok_h2_sp", "%nprocshared=1\n%mem=512MB", "#p HF/STO-3G",
        "H2 single point baseline", "0 1",
        "H   0.0000   0.0000   0.0000\nH   0.0000   0.0000   0.7400")

    gjf("ok_water_opt", "%nprocshared=1\n%mem=512MB", "#p B3LYP/6-31G(d) opt",
        "water geometry optimisation", "0 1", WATER)

    gjf("ok_water_optfreq", "%nprocshared=1\n%mem=1GB",
        "#p B3LYP/6-31G(d) opt freq", "water opt then frequencies", "0 1", WATER)

    # 阴离子 + 小基组：故意不加弥散函数，用来验 C-03
    gjf("ok_anion_oh", "%nprocshared=1\n%mem=512MB", "#p UB3LYP/6-31G(d)",
        "hydroxide anion without diffuse functions", "-1 1",
        "O   0.0000   0.0000   0.0000\nH   0.0000   0.0000   0.9700")

    # 开壳层三重态 O2：验 C-02 自旋污染
    gjf("ok_o2_triplet", "%nprocshared=1\n%mem=1GB", "#p UB3LYP/6-31G(d)",
        "triplet oxygen, open shell", "0 3", O2)

    # 过渡金属：LANL2DZ 自带 ECP，验 C-01/C-06 的豁免分支
    gjf("ok_fe_h2o", "%nprocshared=1\n%mem=1GB", "#p UB3LYP/LANL2DZ",
        "iron water complex with ECP basis", "0 5", FE_WATER)

    gjf("ok_bigbasis", "%nprocshared=1\n%mem=2GB", "#p B3LYP/aug-cc-pVTZ",
        "large correlation consistent basis", "0 1", WATER)

    # 过渡态：验 B-02「恰好一个虚频」
    gjf("ok_ts", "%nprocshared=1\n%mem=1GB",
        "#p B3LYP/6-31G(d) opt=(ts,calcfc,noeigentest) freq",
        "HCN to HNC transition state", "0 1", HCN_TS)

    # Force：输出力表，验解析器对 force 作业的处理
    gjf("ok_force", "%nprocshared=1\n%mem=1GB", "#p B3LYP/6-31G(d) force",
        "analytic forces on water", "0 1", WATER)

    gjf("ok_scrf", "%nprocshared=1\n%mem=1GB",
        "#p B3LYP/6-31G(d) SCRF=(solvent=water)", "implicit solvation", "0 1", WATER)

    # 两段 link1：验 E-08
    link1 = (
        "%nprocshared=1\n%mem=512MB\n#p HF/STO-3G\n\nfirst link\n\n0 1\n"
        "H 0.0 0.0 0.0\nH 0.0 0.0 0.74\n\n--Link1--\n"
        "%nprocshared=1\n%mem=512MB\n#p HF/STO-3G\n\nsecond link\n\n0 1\n"
        "H 0.0 0.0 0.0\nH 0.0 0.0 0.90\n"
    )
    (OUT / "ok_link1.gjf").write_text(link1 + "\n", encoding="utf-8", newline="\n")

    # 两段 link1、但**第二段换了基组**：这是 E-08 的真阳性。
    # v0.6 起 E-08 判的是「段与段之间方法/基组变了没」，光「文件里有多段」
    # 不再算问题（opt freq 天然两段，那样每份正常日志都会响）。
    # 构造意图对应真实工作流里最省事的错误：几何用 STO-3G 收、能量用 6-31G(d) 算，
    # 两个数不在同一个势能面上。
    link2 = (
        "%nprocshared=1\n%mem=512MB\n#p HF/STO-3G\n\nfirst link, cheap geometry\n\n0 1\n"
        "H 0.0 0.0 0.0\nH 0.0 0.0 0.74\n\n--Link1--\n"
        "%nprocshared=1\n%mem=512MB\n#p B3LYP/6-31G(d)\n\nsecond link, better basis\n\n0 1\n"
        "H 0.0 0.0 0.0\nH 0.0 0.0 0.90\n"
    )
    (OUT / "ok_link2_diff.gjf").write_text(link2 + "\n", encoding="utf-8", newline="\n")

    # gen 自定义基组：验 C-07 对 gen 的豁免。
    # 语法要点（踩了三次才对）：
    #   ① 坐标块与基组块之间要一个空行；
    #   ② 基组**段与段之间不能有空行** —— `****` 就是元素块的分隔符，
    #      多插空行会让读取提前结束。第二次就栽在这里：日志打 `Centers: 2 3`，
    #      只认到两个 H，O 段整个被跳过，最后报 `5 occupied orbitals but only 2 basis functions`。
    #   ③ 同一元素的多个 shell 必须在同一个元素块里。
    gen_basis = "\n".join([
        "H 0", "S   3   1.00", "      3.42525091   0.15432897",
        "      0.62391373   0.53532814", "      0.16885540   0.44463454", "****",
        "O 0", "S   3   1.00", "    130.7093200    0.15432897",
        "     23.8088610    0.53532814", "      6.4436083    0.44463454",
        "SP   3   1.00", "      5.0331513   -0.09996723   0.15591627",
        "      1.1695961    0.39951283   0.60768372",
        "      0.3803890    0.70011547   0.39195739", "****",
    ])
    gjf("ok_gen", "%nprocshared=1\n%mem=512MB", "#p HF/gen",
        "user supplied basis set", "0 1", WATER, tail=gen_basis)

    # ---------- 预期失败 ----------
    # SCF 只给 1 次迭代：必然打 SCF Done 但没到判据 -> A-02 / A-06 的天然样本
    gjf("fail_scf_maxcyc", "%nprocshared=1\n%mem=512MB",
        "#p B3LYP/6-31G(d) scf=(maxcyc=1)", "forced SCF truncation", "0 1", WATER)

    # 优化只给 1 步：必然停在半路 -> A-05
    gjf("fail_opt_maxcyc", "%nprocshared=1\n%mem=1GB",
        "#p B3LYP/6-31G(d) opt=(maxcyc=1)", "forced optimisation truncation", "0 1", WATER)

    # 内存低到不可能：真实崩溃
    gjf("fail_mem_low", "%nprocshared=1\n%mem=1MB", "#p B3LYP/6-31G(d)",
        "insufficient memory", "0 1", WATER)

    # 非法基组名
    gjf("fail_bad_basis", "%nprocshared=1\n%mem=512MB", "#p B3LYP/NOT-A-BASIS",
        "unknown basis set name", "0 1", WATER)

    # 电子数奇偶与多重度矛盾：O 原子(8e)×2=16e 偶数，声明二重态不可能
    gjf("fail_charge_parity", "%nprocshared=1\n%mem=512MB", "#p UB3LYP/6-31G(d)",
        "multiplicity parity conflict", "0 2", O2)

    # 两个原子坐标完全重合：真实崩溃
    gjf("fail_atom_overlap", "%nprocshared=1\n%mem=512MB", "#p HF/STO-3G",
        "coincident atoms", "0 1",
        "H   0.0000   0.0000   0.0000\nH   0.0000   0.0000   0.0000")

    # 缺末尾空行（第一版冒烟测试的真实产物，留着当回归样本）
    (OUT / "fail_zmat_eof.gjf").write_text(
        "%nprocshared=1\n%mem=512MB\n#p HF/STO-3G\n\nmissing trailing blank line\n\n"
        "0 1\nH 0.0 0.0 0.0\nH 0.0 0.0 0.74", encoding="utf-8", newline="\n")

    # guess=read 但没有 chk：真实崩溃
    gjf("fail_chk_read", "%nprocshared=1\n%mem=512MB\n%chk=/tmp/qce/nonexistent.chk",
        "#p HF/STO-3G guess=read", "read guess from missing checkpoint", "0 1",
        "H 0.0 0.0 0.0\nH 0.0 0.0 0.74")

    # nprocshared 远超物理核数
    gjf("fail_nproc", "%nprocshared=9999\n%mem=512MB", "#p HF/STO-3G",
        "impossible core count", "0 1", "H 0.0 0.0 0.0\nH 0.0 0.0 0.74")

    # 坐标行数与原子不符：声明的分子和给的结构对不上
    gjf("fail_coord_missing", "%nprocshared=1\n%mem=512MB", "#p B3LYP/6-31G(d)",
        "geometry block shorter than molecule", "0 1", "O 0.0 0.0 0.0")

    print(f"generated {len(list(OUT.glob('*.gjf')))} inputs in {OUT}")


if __name__ == "__main__":
    gen()
