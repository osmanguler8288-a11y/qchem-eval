#!/usr/bin/env python3
"""用真实 Gaussian 16 日志验收规则引擎。

和仓库里 tools/eval_rules.py 的区别：那套样本是我自己从 cclib 日志嫁接/变异来的，
本质上是「用我造的题考我自己」。这里的每一份日志都由 cu04 上真实的
Gaussian 16 Rev A.03 跑出来，包括真实的分段错误、真实的 SCF 失败、真实的 SIGKILL。

预期（EXPECT）是**跑之前**按构造意图写的，不是看完结果再回填的。
不符合预期的条目会在最后单列，当作缺陷而不是「调一下预期」。

用法：
    python verify_rules.py <logdir> [--json out.json]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from qchem_eval.engine import evaluate_file  # noqa: E402
from qchem_eval.schema import Severity, Verdict  # noqa: E402

# 期望的写法：
#   verdict   期望的最终结论（None 表示不预设）
#   hit       必须命中的规则/签名 ID
#   no_crit   不许出现 critical 及以上（即不能判「结果不可用」）
#   no_major  不许出现 major 及以上
#   why       为什么这么期望 —— 写给人看的
EXPECT: dict[str, dict] = {
    # ---- 正常终止、设定合理的计算：核心诉求是「别乱报」 ----
    "ok_h2_sp": dict(verdict=None, no_crit=True, no_major=True,
                     why="最小基线，HF/STO-3G 单点，正常终止"),
    "ok_water_opt": dict(verdict=None, no_crit=True, no_major=True,
                         why="水分子 B3LYP/6-31G(d) 优化收敛"),
    "ok_water_optfreq": dict(verdict=None, no_crit=True, no_major=True,
                             why="优化+频率，全正频率"),
    "ok_bigbasis": dict(verdict=None, no_crit=True, no_major=True,
                        why="aug-cc-pVTZ，基组充分"),
    "ok_scrf": dict(verdict=None, no_crit=True, no_major=True,
                    why="隐式溶剂模型，正常终止"),
    "ok_force": dict(verdict=None, no_crit=True,
                     why="Force 作业，验解析器不把力表当异常"),
    "ok_gen": dict(verdict=None, no_crit=True,
                   why="gen 自定义基组，C-07 应豁免 gen"),
    "ok_link1": dict(verdict=None, no_crit=True,
                     why="两段 Link1 均正常终止且方法/基组相同（HF/STO-3G），"
                         "E-08 应判 miss —— v0.6 之前这里会响一条 info 噪音"),
    "ok_link2_diff": dict(verdict=None, hit=["E-08"], no_crit=True,
                          why="两段 Link1：首段 HF/STO-3G，第二段换成 B3LYP/6-31G(d)。"
                              "这是真实工作里「省钱」的常见做法，但几何与单点不在同一个"
                              "势能面上，E-08 必须报。这条是 E-08 改判据后的真阳性对照："
                              "光「文件里有多段」不再算问题，段间换方法才算。"),

    # ---- 正常终止但化学上有问题：这才是规则层存在的理由 ----
    "ok_anion_oh": dict(verdict=None, hit=["C-03"],
                        why="OH- 用 6-31G(d) 没有弥散函数，阴离子体系必须报"),
    "ok_o2_triplet": dict(verdict=None,
                          why="开壳层三重态 O2；自旋污染是否显著要看实测 S**2"),
    "ok_fe_h2o": dict(verdict=None, no_crit=True,
                      why="LANL2DZ 自带 ECP，C-01 不该误报"),
    "ok_ts": dict(verdict="pass_with_warning", hit=["B-09"],
                  why="opt=(ts,calcfc,noeigentest) 优化 HCN，Gaussian 收敛到极小点、"
                      "频率全正（767/767/2213/3478）。用户以为拿到过渡态，实际不是。"
                      "注意：这一条是我跑完之后才发现要有规则的（v0.5 补的 B-09）。"),

    # ---- 真实失败：必须判 FAIL，且失败类型要认对 ----
    "fail_atom_overlap": dict(verdict="fail", hit=["A-01"],
                              why="两原子坐标重合，l202 真实崩溃"),
    "fail_bad_basis": dict(verdict="fail", hit=["A-01"],
                           why="非法基组名，l1 直接退出"),
    "fail_charge_parity": dict(verdict="fail", hit=["A-01"],
                               why="16 电子声明二重态，多重度奇偶矛盾"),
    "fail_chk_read": dict(verdict="fail", hit=["A-01"],
                          why="guess=read 但没有 chk 文件"),
    "fail_mem_low": dict(verdict="fail", hit=["A-01"],
                         why="%mem=1MB，l101 崩溃"),
    "fail_opt_maxcyc": dict(verdict="fail", hit=["A-05"],
                            why="opt=(maxcyc=1)，优化必然没收敛"),
    "fail_scf_maxcyc": dict(verdict="fail", hit=["A-02"],
                            why="scf=(maxcyc=1)，SCF 必然没收敛"),
    "fail_zmat_eof": dict(verdict="fail", hit=["A-01"],
                          why="输入缺末尾空行，Z-matrix 读到 EOF 后段错误"),
    "fail_nproc": dict(verdict="fail", hit=["A-01"],
                       why="%nprocshared=9999 后进程静默停住，日志里**没有**任何错误文本"),

    # ---- 这条是我最想看的：正常终止，但算的根本不是你以为的分子 ----
    "fail_coord_missing": dict(
        # 期望写 WARNING 而不是「不预设」，就是为了让它**失败**：
        # 只要引擎给它 PASS，这一行就会出现在「不符合预期」里，缺口不会被藏起来。
        verdict="pass_with_warning",
        why="声明 B3LYP/6-31G(d) 的水分子，但坐标只给了一行 O —— "
            "Gaussian 把它当单原子氧算完并正常终止（SCF Done -74.957）。"
            "这是最危险的静默错误：报告上看不出任何异常。"
            "单文件日志层面判不了（日志里没有「本该有几个原子」这个信息），"
            "必须靠输入文件与输出交叉核对。",
    ),

    # ---- 特殊场景 ----
    # 原以为这是 C-07 的真实触发场景（route 写 STO-3G，geom=allcheck 从 chk 读回
    # 6-31G(d)）。实测**否掉了**：G16 A.03 下 Standard basis 就是 STO-3G，
    # chk 不会覆盖 route 声明的基组。所以这里正确的期望是「不命中 C-07」。
    # 这条预期是跑完之后改的，原始预期（命中 C-07）错在我，不在引擎。
    "sp_allcheck_step2": dict(verdict=None, no_crit=True,
                              why="实测结论：G16 不以 chk 覆盖 route 基组，故不该命中 C-07"),
    "sp_killed": dict(verdict="fail", hit=["A-01"],
                      why="被 SIGKILL 强杀，日志停在半路，无终止行"),
    "sp_link1_partial": dict(verdict="fail", hit=["A-01"],
                             why="第一段正常终止、第二段非法基组崩溃"),
}


def main(argv: list[str]) -> int:
    logdir = Path(argv[1]) if len(argv) > 1 else Path("field_test/real")
    out_json = Path(argv[3]) if len(argv) > 3 and argv[2] == "--json" else None

    logs = sorted(logdir.glob("*.log"))
    if not logs:
        print(f"没有找到日志：{logdir}")
        return 1

    rows = []
    n_ok = n_bad = 0
    for p in logs:
        stem = p.stem
        try:
            r = evaluate_file(p)
        except Exception as exc:  # noqa: BLE001 —— 解析崩了本身就是要记录的结论
            rows.append(dict(log=stem, error=f"{type(exc).__name__}: {exc}"))
            n_bad += 1
            continue

        hits = [f.rule_id for f in r.findings]
        worst = min((f.severity for f in r.findings), key=lambda s: list(Severity).index(s),
                    default=None)

        exp = EXPECT.get(stem, {})
        problems = []
        if exp.get("verdict") and r.verdict.value != exp["verdict"]:
            problems.append(f"结论 {r.verdict.value} != 期望 {exp['verdict']}")
        for want in exp.get("hit", []):
            if want not in hits:
                problems.append(f"漏报 {want}")
        if exp.get("no_crit") and worst in (Severity.CRITICAL,):
            problems.append(f"出现 critical：{hits}")
        if exp.get("no_major") and worst in (Severity.CRITICAL, Severity.MAJOR):
            problems.append(f"出现 major+：{hits}")

        rows.append(dict(
            log=stem, verdict=r.verdict.value, score=r.score,
            action=r.action.value, hits=hits, worst=worst.value if worst else None,
            n_findings=len(r.findings),
            unknown=r.metrics.get("unknown_detail", []),
            coverage=r.metrics.get("coverage"),
            problems=problems,
            expected=bool(exp),
        ))
        if problems:
            n_bad += 1
        else:
            n_ok += 1

    # ---- 输出 ----
    print("=" * 100)
    print(f"真实 Gaussian 日志验收    日志 {len(logs)} 份    目录 {logdir}")
    print("=" * 100)
    print(f"{'日志':26s} {'结论':10s} {'分':>4s} {'最坏':9s} {'命中':28s} 期望")
    print("-" * 100)
    for r in rows:
        if "error" in r:
            print(f"{r['log']:26s} 解析失败  {r['error']}")
            continue
        mark = "OK" if not r["problems"] else "!!"
        exp_mark = "有" if r["expected"] else "-"
        hitstr = ",".join(r["hits"]) if r["hits"] else "—"
        print(f"{mark} {r['log']:24s} {r['verdict']:10s} {r['score']:>4d} "
              f"{str(r['worst'] or '—'):9s} {hitstr[:28]:28s} {exp_mark}")

    print("-" * 100)
    print(f"符合预期 {n_ok} / {len(rows)}")

    bad = [r for r in rows if r.get("problems")]
    if bad:
        print("\n不符合预期的条目：")
        for r in bad:
            print(f"  {r['log']}")
            for pr in r["problems"]:
                print(f"      - {pr}")
            if r.get("expected"):
                print(f"      为什么期望如此：{EXPECT[r['log']].get('why', '')}")

    if out_json:
        out_json.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n明细已写入 {out_json}")

    return 0 if n_bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
