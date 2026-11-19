"""规则引擎回归测试。

两层：
  1. 真实日志回归 —— tests/data 里 7 份真 Gaussian 16 输出，跑通不出异常、结论稳定
  2. 变异测试 —— 真实日志改一处，验证对应规则能命中

为什么必须做第 2 层：公开数据集里几乎不存在失败的计算（VQM24 统计了 262542 次
失败，但发布的 835947 条全是成功的）。所以 FAIL 分支没法靠"找数据"来验证，
只能靠对真实日志做定点变异——这和 linter 测自己是同一个思路。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from qchem_eval import evaluate_file, evaluate_pair
from qchem_eval.parsers import parse
from qchem_eval.schema import Action, Verdict

DATA = Path(__file__).parent / "data"
DEMO = Path(__file__).parent.parent / "demo"

REAL_LOGS = sorted(p.name for p in DATA.glob("*") if p.is_file())


# --------------------------------------------------------------- 1. 真实日志

@pytest.fixture(scope="module")
def real_cache():
    return {name: parse(DATA / name) for name in REAL_LOGS}


def test_all_real_logs_parse(real_cache):
    """每份真实日志都得解析出基本盘：原子数、电荷、能量。"""
    for name, (d, n_lines) in real_cache.items():
        assert n_lines > 0, f"{name} 没读到任何行"
        assert d.normal_termination is True, f"{name} 应正常终止"
        assert d.natoms, f"{name} 未取到 NAtoms"
        assert d.atomic_numbers, f"{name} 未取到元素列表"
        assert len(d.atomic_numbers) == d.natoms, f"{name} 元素数与 NAtoms 不一致"
        assert d.scf_energies, f"{name} 未取到 SCF 能量"


def test_known_values(real_cache):
    """钉死几处关键数值，防止后续改解析器改坏而没人发现。"""
    d, _ = real_cache["dvb_gopt.out"]
    assert d.natoms == 20
    assert d.charge == 0 and d.multiplicity == 1
    assert d.functional.lower() == "b3lyp"
    assert d.basis.upper().replace(" ", "") == "STO-3G"
    assert d.opt_converged is True
    assert d.opt_steps and d.opt_steps > 1
    assert set(d.opt_last_table) == {
        "Maximum Force", "RMS Force", "Maximum Displacement", "RMS Displacement"
    }
    assert all(v == "YES" for v in d.opt_last_table.values())
    assert d.scf_maxcycle == 128
    assert abs(d.last_scf_energy + 382.308266602) < 1e-9

    d2, _ = real_cache["dvb_ir.out"]
    assert len(d2.freqs) > 60
    assert d2.n_imag == 0
    assert d2.gibbs_energy is not None

    d3, _ = real_cache["dvb_un_sp_b.log"]
    assert d3.multiplicity == 2 and d3.charge == 1
    assert d3.unrestricted is True
    # 必须是湮灭前的原始值，不是湮灭后的
    assert abs(d3.s2 - 0.7778) < 1e-9

    d4, _ = real_cache["Mo4OCl4-sp.log"]
    assert d4.has_transition_metal is True          # Mo Z=42
    assert d4.basis is None                         # Gen 基组：诚实地说不知道
    assert any("Gen" in w for w in d4.parse_warnings)


def test_real_logs_never_fail(real_cache):
    """这 7 份都是能算完的正常计算，不该被判 FAIL。"""
    for name in REAL_LOGS:
        r = evaluate_file(DATA / name)
        assert r.verdict is not Verdict.FAIL, f"{name} 被误判为 FAIL: {r.reasons()}"


def test_gen_basis_yields_unknown_not_pass():
    """Gen 基组查不到基组质量 -> 相关规则必须 UNKNOWN，不能默认通过。"""
    r = evaluate_file(DATA / "Mo4OCl4-sp.log")
    unknown_ids = {u["rule_id"] for u in r.metrics["unknown_detail"]}
    assert "C-01" in unknown_ids
    assert "C-03" in unknown_ids
    assert r.metrics["coverage"] < 1.0


# --------------------------------------------------------------- 2. 变异测试

def _mutate(tmp_path: Path, src: str, old: str, new: str, *, count: int = -1) -> Path:
    text = (DATA / src).read_text(encoding="utf-8", errors="replace")
    assert old in text, f"变异锚点不存在: {old!r}"
    text = text.replace(old, new, count)
    out = tmp_path / f"mut_{src}"
    out.write_text(text, encoding="utf-8")
    return out


def test_abnormal_termination_is_fail(tmp_path):
    """把正常终止改成 Error termination -> A-01 必须命中且判 FAIL。"""
    p = _mutate(
        tmp_path, "dvb_sp.out",
        "Normal termination of Gaussian 16",
        "Error termination via Lnk1e in /g16/l502.exe",
    )
    r = evaluate_file(p)
    assert r.verdict is Verdict.FAIL
    assert r.action is Action.RERUN
    assert any(f.rule_id == "A-01" for f in r.findings)


def test_truncated_log_is_not_pass(tmp_path):
    """被截断的日志（模拟任务被杀）：既不能判通过，也不能崩。"""
    text = (DATA / "dvb_gopt.out").read_text(encoding="utf-8", errors="replace")
    p = tmp_path / "truncated.out"
    p.write_text(text[: int(len(text) * 0.45)], encoding="utf-8")

    r = evaluate_file(p)
    assert r.verdict is Verdict.FAIL          # 没有 Normal termination
    assert any(f.rule_id == "A-01" for f in r.findings)
    assert any(f.rule_id == "A-05" for f in r.findings)   # 优化也没走完


def test_garbage_file_is_unknown_not_pass(tmp_path):
    """完全无法解析的输入必须判 UNKNOWN。这是与 QMCert 的核心分歧点。"""
    p = tmp_path / "garbage.log"
    p.write_text("这不是量子化学输出\n随便写点东西\n" * 50, encoding="utf-8")
    r = evaluate_file(p)
    assert r.verdict is Verdict.UNKNOWN
    assert r.abstain is True
    assert r.score == 0


def test_scf_not_converged(tmp_path):
    """把 SCF Done 前面插一条 Convergence failure -> A-02。"""
    p = _mutate(
        tmp_path, "dvb_sp.out",
        " SCF Done:  E(RB3LYP) =  -382.308266602     A.U. after    1 cycles",
        " Convergence failure -- run terminated.\n"
        " SCF Done:  E(RB3LYP) =  -382.308266602     A.U. after    1 cycles",
    )
    r = evaluate_file(p)
    assert any(f.rule_id == "A-02" for f in r.findings)
    assert r.verdict is Verdict.FAIL


def test_imaginary_frequency_detected(tmp_path):
    """把第一个振动频率改成负值 -> B-02（单个虚频）。"""
    p = _mutate(
        tmp_path, "dvb_ir.out",
        "Frequencies ---    53.1981",
        "Frequencies ---   -53.1981",
    )
    d, _ = parse(p)
    assert d.n_imag == 1
    r = evaluate_file(p)
    ids = {f.rule_id for f in r.findings}
    assert "B-02" in ids
    assert r.verdict is Verdict.WARNING


def test_anion_without_diffuse_basis(tmp_path):
    """阴离子 + STO-3G（无弥散函数）-> C-03 必中。

    这条规则曾经因为 eval 作用域问题静默失效（推导式看不到 locals 里的 d），
    所以单独钉一个用例。
    """
    p = _mutate(
        tmp_path, "dvb_sp.out",
        "Charge =  0 Multiplicity = 1",
        "Charge = -1 Multiplicity = 2",
    )
    d, _ = parse(p)
    assert d.charge == -1
    r = evaluate_file(p)
    assert any(f.rule_id == "C-03" for f in r.findings)


def test_electron_parity_mismatch(tmp_path):
    """20 个原子的闭壳层体系却写多重度 2 -> E-04。

    这类输入 Gaussian 有时候会真的算完，但结果没有物理意义。
    """
    p = _mutate(
        tmp_path, "dvb_sp.out",
        "Charge =  0 Multiplicity = 1",
        "Charge =  0 Multiplicity = 2",
    )
    r = evaluate_file(p)
    assert any(f.rule_id == "E-04" for f in r.findings)
    assert r.verdict is Verdict.FAIL


# --------------------------------------------------------------- 3. 跨文件

def test_compare_same_method():
    """opt 与 sp 同级别：不应报 D-06。"""
    r = evaluate_pair(DATA / "dvb_gopt.out", DATA / "dvb_sp.out")
    assert r.metrics["pair_checks"]["method"] == "same"
    assert not any(f.rule_id == "D-06" for f in r.findings)


def test_compare_method_mismatch(tmp_path):
    """把 sp 的基组改掉 -> D-06 跨阶段方法不一致。"""
    p = _mutate(
        tmp_path, "dvb_sp.out",
        "Standard basis: STO-3G",
        "Standard basis: 6-31G(d)",
    )
    r = evaluate_pair(DATA / "dvb_gopt.out", p)
    assert r.metrics["pair_checks"]["method"] == "mismatch"
    assert any(f.rule_id == "D-06" for f in r.findings)
    assert r.verdict is Verdict.FAIL


# --------------------------------------------------------------- 4. 契约

def test_result_is_json_serialisable():
    """EvalResult 是 Agent 的唯一出口，必须能直接 JSON 化。"""
    import json

    r = evaluate_file(DATA / "dvb_gopt.out")
    blob = json.dumps(r.to_dict(), ensure_ascii=False)
    back = json.loads(blob)
    assert back["verdict"] in {v.value for v in Verdict}
    assert back["metrics"]["rules_total"] == back["provenance"]["n_rules"]
    assert isinstance(back["reasons"], list)


def test_parser_beats_cclib_on_speed():
    """性能护栏：解析耗时不能退化。

    用「解析耗时 ÷ 纯读文件耗时」这个**比值**当判据，不用绝对行速率。
    原因：这台机器（以及任何 CI）的绝对速度会被后台进程、杀毒扫描、CPU 降频
    左右，实测同一份改动在几小时内就从 0.5 漂到 0.26 M 行/秒 —— 绝对阈值必然
    偶发失败，而偶发失败的护栏最后都会被人注释掉。比值则稳定：机器慢下来时
    读文件和解析一起慢，商基本不变。

    取 6 次里的最小值：干扰只会让单次耗时变长，不会变短。
    当前基线约 4.2x（纯读 1.9 ms / 解析 8.1 ms），阈值 8.0 留了约两倍余量 ——
    解析器若退化成两倍慢（比如不小心引入 O(n^2)），比值会直接撞线。

    为什么还要跨轮取最小比值：分母只有 1.9 ms，一次 GC 或线程切换就能把它
    抬高 30%，而分子分母**不是同时**被抬高的（两段测量串行执行），于是比值
    偶发冲到 8 以上。实测同一份代码连跑 10 次，有 1 次红。取三轮里的最小比值
    不需要放松阈值就消掉了这个噪声——真退化是三轮全高，噪声只污染其中一轮。
    """
    import time

    p = DATA / "dvb_un_sp_b.log"

    def best_ms(fn, reps: int = 6) -> float:
        out = float("inf")
        for _ in range(reps):
            t0 = time.perf_counter()
            fn()
            out = min(out, (time.perf_counter() - t0) * 1000)
        return out

    def read_only() -> int:
        return p.read_text(encoding="utf-8", errors="replace").count("\n")

    parse(p)                      # 预热：首次要建表、编译正则
    read_only()
    n_lines = read_only()

    ratio = float("inf")
    t_parse = t_base = 0.0
    for _ in range(3):
        tp = best_ms(lambda: parse(p))
        tb = best_ms(read_only)
        r = tp / tb
        if r < ratio:
            ratio, t_parse, t_base = r, tp, tb

    rate = n_lines / t_parse * 1000 / 1e6       # 仅供参考，不做断言
    assert ratio < 8.0, (
        f"解析耗时是纯读文件的 {ratio:.1f} 倍（基线约 4.2 倍，"
        f"{t_parse:.1f} ms vs {t_base:.1f} ms，绝对速率 {rate:.2f} M 行/秒）")


# ------------------------------------------------- 5. 解析器回归（发现的真 bug）

def test_coords_column_shift(real_cache):
    """坐标块是 6 列：Center | Z | AtomicType | X | Y | Z。

    早期实现取 parts[2:5]，把 AtomicType（恒为 0）当成了 X，Z 坐标整个丢掉，
    结果所有原子被压到 x=0 平面上。这个 bug 直到加 B-05（原子重叠）才暴露。
    """
    d, _ = real_cache["dvb_sp.out"]
    assert len(d.coords_last) == 20
    # dvb_sp 是 C10H8，最短键必然是 C–H ≈ 1.08–1.10 Å
    assert 1.05 < d.min_atom_distance < 1.15
    # 钉死首原子坐标。列序若再错一位，这里立刻炸：错位时读成
    # (0, 0.269445, 1.410118)——AtomicType 当了 X。
    assert d.coords_last[0] == (0.269445, 1.410118, 0.0)
    # 该分子是平面的，z 全 0 才对；要有变化的是 y
    assert len({round(y, 3) for _, y, _ in d.coords_last}) > 5


def test_coords_reset_per_frame(real_cache):
    """多帧优化日志只能保留最后一帧，不能把各帧首尾相接。"""
    d, _ = real_cache["dvb_gopt.out"]
    assert d.opt_steps and d.opt_steps > 1          # 确实跑了多步
    assert len(d.coords_last) == d.natoms, "末帧坐标混进了历史帧"
    assert d.min_atom_distance > 0.9


def test_route_keyword_with_argument():
    """opt=ts / freq=hpmodes 这类带参数的写法必须仍能识别出阶段。

    早期用 `"freq" in route.split()` 精确匹配，freq=hpmodes 一条都认不出来，
    结果 A-05、E-01、E-07 的 applies 全判 na —— 规则等于不存在。
    """
    from qchem_eval.parsers.gaussian import _parse_route
    from qchem_eval.schema import CalcData

    d = CalcData()
    _parse_route("opt=(ts,calcfc,maxstep=5) freq=hpmodes b3lyp/6-31g(d)", d)
    assert "opt" in d.stages and "freq" in d.stages

    d2 = CalcData()
    _parse_route("td=(nstates=10) b3lyp/6-31g(d)", d2)
    assert "td" in d2.stages


def test_error_block_captures_cause_after_termination():
    """病因行在终止行之后时也必须抓到（官方 #6 就是这种结构）。"""
    import sys

    sys.path.insert(0, str(Path(__file__).parent.parent))
    from tools.synth_failures import build_samples

    s = next(x for x in build_samples() if x.name == "sig+SIG-FIL-1")
    import tempfile

    p = Path(tempfile.mkdtemp()) / "fil.log"
    p.write_text(s.text, encoding="utf-8")
    d, _ = parse(p)
    blob = "\n".join("\n".join(b["lines"]) for b in d.error_blocks)
    assert "Operation on file out of range" in blob
    assert "NtrErr Called from FileIO" in blob


# ------------------------------------------------- 6. 样本库端到端

def test_labeled_samples_all_detected():
    """跑完整的带标签样本集：签名层必须全中且零误报，规则层必须全中。

    这是把 tools/eval_rules.py 的结论钉进 CI。它同时守住三件事：
    签名召回、签名误报、规则召回。
    """
    import sys

    sys.path.insert(0, str(Path(__file__).parent.parent))
    from tools.eval_rules import build_report

    rep = build_report()

    missing_sig = [r for r in rep["signatures"]
                   if r["positives"] and r["tp"] < r["positives"]]
    assert not missing_sig, [(r["id"], r["fn_cases"]) for r in missing_sig]

    fp_sig = [r["id"] for r in rep["signatures"] if r["fp"]]
    assert not fp_sig, f"签名误报: {fp_sig}"

    missing_rule = [r for r in rep["rules"]
                    if r["positives"] and r["tp"] < r["positives"]]
    assert not missing_rule, [(r["id"], r["fn_cases"]) for r in missing_rule]

    # 结果可用的真实日志上不许出现 CRITICAL/MAJOR。
    # 注意「真实日志」不等于「成功日志」：ts_no_imag.log 正常终止但结果是错的，
    # 它不在 REAL_OK 里，在它身上报 MAJOR 是正确行为。
    high_fp = [(r["id"], r["high_fp_on_real"]) for r in rep["rules"] if r["high_fp_on_real"]]
    assert not high_fp, f"真实日志上的高严重性误报: {high_fp}"


# ------------------------------------------------- 7. 可观测性：不许静默失效
#
# 这一组守的不是"算得对不对"，而是"系统自己出了问题会不会吭声"。
# 两类失效都实测复现过，且在修复前的表现都是"报告照常输出、看不出任何异常"：
#   A. 解析器把一个错误的值当成正确的值（部分匹配），比解析失败更危险
#   B. 规则 YAML 里字段名拼错，规则永久 UNKNOWN / 永久 na，覆盖率指标也看不出来

_MINIMAL_LOG = """ Entering Gaussian System
 #p b3lyp/6-31g(d) opt freq

 Charge =  0 Multiplicity = 1
 NAtoms=      2
                          Standard orientation:
 ---------------------------------------------------------------------
 Center     Atomic      Atomic             Coordinates (Angstroms)
 Number     Number       Type             X           Y           Z
 ---------------------------------------------------------------------
      1          6           0        0.000000    0.000000    0.000000
      2          1           0        0.000000    0.000000    1.090000
 ---------------------------------------------------------------------
 Requested convergence on RMS density matrix=1.00D-08 within 128 cycles.
"""


def _parse_scf_line(line: str):
    """把一行 SCF 续行塞进最小日志里解析，返回 CalcData。"""
    import tempfile

    p = Path(tempfile.mkdtemp()) / "scf.log"
    p.write_text(
        _MINIMAL_LOG + line + "\n Normal termination of Gaussian 16\n",
        encoding="utf-8",
    )
    d, _ = parse(p)
    return d


@pytest.mark.parametrize(
    "line, want_conv, want_vt",
    [
        # G16 实机：Conv 无 g
        (" NFock= 12  Conv=0.13D-08     -V/T= 2.0160", 1.3e-09, 2.016),
        # 老版本 / 官方目录 #10：Convg 有 g，且 = 前后有空格
        (" Convg  =    0.8831D-03      -V/T = 2.0048", 8.831e-04, 2.0048),
        # E 记法（某些后处理文件把 D 换成 E）
        (" NFock= 12  Conv=1.9E-09      -V/T= 2.0001", 1.9e-09, 2.0001),
    ],
)
def test_scf_line_variants_parsed(line, want_conv, want_vt):
    """三种合法写法都必须解析出来，且不得产生任何诊断噪音。"""
    d = _parse_scf_line(line)
    assert d.scf_conv == [pytest.approx(want_conv)], f"scf_conv={d.scf_conv}"
    assert d.virial_ratio == pytest.approx(want_vt)
    assert d.unparsed_hits == [], f"合法行不该有诊断: {d.unparsed_hits}"


def test_garbage_number_is_not_partially_matched():
    """`Conv=0.1x9D-08` 绝不能被截成 0.1。

    这是修 `except ValueError: pass` 时才暴露出来的**次级缺陷**：
    `_NUM` 允许 "1." 这种省略小数部分的写法，于是宽松正则会"成功"匹配出 0.1，
    既不留痕也不报错。下游 A-06 拿 0.1 和 1e-8 比会得出"超出 1e7 倍"的 CRITICAL，
    即把一个乱码日志判成严重质量问题 —— 比原来的静默跳过危险得多。
    """
    d = _parse_scf_line(" NFock= 12  Conv=0.1x9D-08     -V/T= 2.0160")
    assert d.scf_conv == [], f"垃圾数字不得入库: {d.scf_conv}"
    bad = [h for h in d.unparsed_hits if h["field"] == "scf_conv"]
    assert bad, "垃圾数字必须进 unparsed_hits"
    assert "0.1x9D-08" in bad[0]["reason"], bad[0]["reason"]


def test_garbage_vt_is_flagged():
    """-V/T 里的乱码同理，且不能连累同行的 Conv。"""
    d = _parse_scf_line(" NFock= 12  Conv=0.13D-08     -V/T= 2.0o160")
    assert d.virial_ratio is None
    assert d.scf_conv == [pytest.approx(1.3e-09)]
    assert any(h["field"] == "virial_ratio" for h in d.unparsed_hits)


def test_unparsed_hits_surface_as_parse_warning():
    """解析诊断必须升级成用户可见的 parse_warnings —— 否则等于没记。"""
    d = _parse_scf_line(" NFock= 12  Conv=0.1x9D-08     -V/T= 2.0160")
    blob = " ".join(d.parse_warnings)
    assert "未能解析" in blob and "scf_conv" in blob, d.parse_warnings


# ------------------------------------------------- 8. 规则清单加载期校验

def _load_mutated_catalog(mutate, tmp_path):
    """改一条规则再重新加载，返回异常（None 表示加载通过）。"""
    import yaml

    from qchem_eval.rules import evaluator as ev

    base = yaml.safe_load((Path(ev.CATALOG_PATH)).read_text(encoding="utf-8"))
    doc = yaml.safe_load(yaml.safe_dump(base, allow_unicode=True))
    mutate(doc["rules"][0])
    p = tmp_path / "catalog_bad.yaml"
    p.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
    ev.load_ruleset.cache_clear()
    try:
        ev.load_ruleset(p)
        return None
    except ev.RuleCatalogError as exc:
        return exc
    finally:
        ev.load_ruleset.cache_clear()


@pytest.mark.parametrize(
    "mutate, needle",
    [
        # requires 拼错字段名 -> 永久 UNKNOWN，且看不出异常
        (lambda r: r.update(requires=list(r.get("requires", [])) + ["scf_convergedd"]),
         "scf_convergedd"),
        # when 引用不存在的名字 -> 运行期 NameError -> 被当成 unknown
        (lambda r: r.update(when="conv_lastt is not None"), "conv_lastt"),
        # applies 写错 -> 运行期被按 na 吞掉，而 na 不计入覆盖率分母，指标完全看不出来
        (lambda r: r.update(applies="opt and scf_convv"), "scf_convv"),
        # action 写错 -> 静默退化成 HUMAN，人工升级通道消失
        (lambda r: r.update(action="ESCALTE"), "ESCALTE"),
        (lambda r: r.update(severity="CRITICALL"), "CRITICALL"),
    ],
)
def test_catalog_typos_fail_fast(mutate, needle, tmp_path):
    exc = _load_mutated_catalog(mutate, tmp_path)
    assert exc is not None, "拼写错误必须在加载期就报错，不能留到运行期静默失效"
    assert needle in str(exc), str(exc)


@pytest.mark.parametrize(
    "mutate",
    [
        # @property 也是合法的 requires 对象：missing_fields() 走 getattr()
        lambda r: r.update(requires=list(r.get("requires", [])) + ["n_heavy"]),
        # 推导式里自己绑定的变量名，不该被当成"外部名字"误报
        lambda r: r.update(when='any(k in d.basis for k in ("a", "b"))'),
        # 属性访问的属性名在 co_names 里，但不是变量，不该误报
        lambda r: r.update(when="d.stages and conv_last is not None"),
    ],
)
def test_catalog_valid_edge_cases_load(mutate, tmp_path):
    """这三条都是"看着像拼错、其实是合法写法"，校验不许误伤。"""
    assert _load_mutated_catalog(mutate, tmp_path) is None


def test_derived_ctx_keys_cover_build_context(real_cache):
    """DERIVED_CTX_KEYS 不许漏 —— 漏了会让合法规则在加载期被误判为"未知名字"。

    拿真实日志跑出来的 ctx 反查这个常量：ctx 里除 CalcData 字段之外的键，
    必须全部登记在 DERIVED_CTX_KEYS 里。
    """
    from qchem_eval.rules.evaluator import CALC_FIELDS, DERIVED_CTX_KEYS, build_context

    seen: set[str] = set()
    for _name, (d, _n) in real_cache.items():
        seen |= set(build_context(d))
    undeclared = seen - CALC_FIELDS - DERIVED_CTX_KEYS
    assert not undeclared, f"build_context 里有未登记的派生键: {sorted(undeclared)}"


def test_broken_catalog_exit_code_is_not_fail(monkeypatch, capsys):
    """规则清单坏了必须用独立退出码（3），不能与「有 FAIL 所以返回 1」混淆。

    否则 CI 会把「配置写错了」读成「这批计算质量不合格」——一个是维护者的问题，
    一个是被测体系的问题，混在一起就再也分不清该找谁。
    """
    from qchem_eval import cli
    from qchem_eval.rules import RuleCatalogError

    def boom(*_a, **_k):
        raise RuleCatalogError("规则 A-01 的 requires 里有 CalcData 不存在的字段：'x'")

    monkeypatch.setattr(cli, "load_ruleset", boom)
    rc = cli.main(["rules"])
    assert rc == 3, f"坏清单的退出码应为 3，实际 {rc}"
    assert "规则清单加载失败" in capsys.readouterr().err




# ------------------------------------------------- 9. 基组解析与 C 类规则
#
# 这一组来自一次对账发现的问题：报告把 6-31G(d) 印成 6-31G，
# 而 demo 里那份「错基组」的日志反而被判了 100 分通过。

@pytest.mark.parametrize(
    "line, want",
    [
        # 结尾的 (5D, 7F) 是球谐标记，要剥；6-31G(d) 里的 (d) 是极化函数，要留
        (" Standard basis: 6-31G(d) (5D, 7F)", "6-31G(d)"),
        (" Standard basis: Aug-CC-pVQZ (5D, 7F)", "Aug-CC-pVQZ"),
        (" Standard basis: STO-3G (5D, 7F)", "STO-3G"),
        (" Standard basis: 6-311++G(2d,2p) (6D, 10F)", "6-311++G(2d,2p)"),
        # 没有球谐标记时不能把整个名字吃掉
        (" Standard basis: def2-SVP", "def2-SVP"),
        (" Standard basis: cc-pVTZ (5D, 7F)", "cc-pVTZ"),
    ],
)
def test_std_basis_keeps_polarization_marker(line, want):
    """旧写法 [^(]+ 在第一个 "(" 处切断，把 6-31G(d) 读成 6-31G。

    后果不是显示问题：basis 是 C 类规则的输入字段，极化标记丢了就
    无法判断「这个基组到底有没有极化函数」。
    """
    from qchem_eval.parsers.gaussian import _STD_BASIS

    m = _STD_BASIS.search(line)
    assert m is not None, line
    assert m.group(1).strip() == want, f"{line!r} -> {m.group(1)!r}，期望 {want!r}"


def test_route_basis_recorded():
    """route 里的声明值必须单独留一份，否则会被 Standard basis 覆盖掉。"""
    d, _ = parse(DATA / "dvb_sp.out")
    assert d.route_basis == "sto-3g", d.route_basis
    assert d.basis == "STO-3G", d.basis


def test_route_basis_mismatch_is_caught():
    """demo/wrong_basis.out：route 写 sto-3g，实际 Standard basis 是 6-31G(d)。

    这是 geom=allcheck / guess=read 从 chk 读旧基组的经典后果，全程无警告。
    修复前这份日志被判 100 分「直接采用」。
    """
    r = evaluate_file(DEMO / "wrong_basis.out")
    ids = [f.rule_id for f in r.findings]
    assert "C-07" in ids, f"应命中 C-07，实际 {ids}"
    assert r.verdict is not Verdict.PASS, "声明与实际不符不能判通过"


def test_route_basis_case_only_diff_not_flagged():
    """route 写 aug-cc-pvqz、Standard basis 印 Aug-CC-pVQZ —— 只是大小写，不许报。"""
    r = evaluate_file(DATA / "C_bigbasis.out")
    assert "C-07" not in [f.rule_id for f in r.findings]


# ------------------------------------------------- 9b. 多段 link1（E-08）

def _two_link_log(tmp_path: Path, second_route: str, second_basis: str | None = None) -> Path:
    """拿 dvb_sp.out 当首段，在末尾接一段「第二段作业」。

    接在 Normal termination 之后是对的：真实的串接日志就是这个形状（一段接一段）。
    """
    base = (DATA / "dvb_sp.out").read_text(encoding="utf-8", errors="replace")
    extra = f" {second_route}\n"
    if second_basis:
        extra += f" Standard basis: {second_basis} (6D, 7F)\n"
    extra += " Normal termination of Gaussian 16 at Thu Apr  5 09:07:00 2018\n"
    p = tmp_path / "two_link.out"
    p.write_text(base + extra, encoding="utf-8")
    return p


def test_link1_same_method_not_flagged(tmp_path):
    """`opt freq` 的第二段是 Gaussian 自动生成的 `RB3LYP/...`。

    归一化后与首段相同，**不该**被当成"换了方法" —— 这条规则改判据之前，
    它在每份正常的 opt freq 日志上都会响一次，纯噪音。
    """
    p = _two_link_log(
        tmp_path, "#P Geom=AllCheck Guess=TCheck SCRF=Check GenChk RB3LYP/STO-3G Freq"
    )
    ids = [f.rule_id for f in evaluate_file(p).findings]
    assert "E-08" not in ids, ids


def test_link1_method_changed_is_flagged(tmp_path):
    """段与段之间真换了方法/基组 —— 几何与单点不在同一势能面上，必须报。

    同时断言 C-07 不跟着响：那是同一件事的两种说法，双罚会把同一处缺陷扣两次分
    （实测 cu04 上的 ok_link2_diff.log：修之前 E-08+C-07 各扣 15，85 变 70）。
    """
    p = _two_link_log(tmp_path, "#p B3LYP/6-31G(d)", "6-31G(d)")
    ids = [f.rule_id for f in evaluate_file(p).findings]
    assert "E-08" in ids, ids
    assert "C-07" not in ids, f"多段换基组不该再让 C-07 报同一件事：{ids}"


def test_route_methods_collected_per_segment(tmp_path):
    d, _ = parse(_two_link_log(tmp_path, "#p B3LYP/6-31G(d)"))
    assert d.n_jobs == 2, d.n_jobs
    assert d.route_methods == ["b3lyp/sto-3g", "b3lyp/6-31g(d)"], d.route_methods
    # 首段 route 仍是「用户写的那个」：后段的值不许覆盖它
    assert d.route_basis == "sto-3g", d.route_basis


@pytest.mark.parametrize(
    "raw,want",
    [
        ("RB3LYP", "B3LYP"),       # 大写的自旋前缀要剥
        ("rb3lyp", "b3lyp"),       # route 是先小写再进来，同样要剥
        ("UB3LYP", "B3LYP"),
        ("ROHF", "ROHF"),          # RO 是 restricted open-shell，不是自旋前缀
        ("revPBE", "revPBE"),      # r 是 "revised"，剥掉就变成另一个泛函
        ("r2SCAN", "r2SCAN"),
        ("RPBE", "RPBE"),
        ("HF", "HF"),              # 太短（会剥成 F），不动
    ],
)
def test_strip_spin_prefix(raw: str, want: str):
    from qchem_eval.parsers.gaussian import _strip_spin_prefix

    assert _strip_spin_prefix(raw) == want


def test_no_polarization_flagged_but_polarized_basis_not():
    """STO-3G 该提示，aug-cc-pVQZ 自带极化不许误伤。"""
    assert "C-08" in [f.rule_id for f in evaluate_file(DATA / "dvb_sp.out").findings]
    assert "C-08" not in [
        f.rule_id for f in evaluate_file(DATA / "C_bigbasis.out").findings
    ]


# ------------------------------------------------- 10. risk-coverage（Phase 0 出口标准）

def test_perturbed_negatives_keep_normal_termination():
    """扰动负样本必须保留 Normal termination。

    历史 bug：perturb_matches 复用了 graft_signature 的结果，而 graft 是把
    终止行**整行替换**成错误块。于是"负样本"本身变成了一份异常终止日志，
    A-01 正确地判它 FAIL —— 那却被记成误报，把正确行为算成错误。
    负样本的语义只能是「正常计算 + 一段看起来像错误的文本」。
    """
    from tools.synth_failures import build_samples

    bad = [
        s.name
        for s in build_samples()
        if s.kind == "negative"
        and not s.name.startswith("real:")
        and "Normal termination of Gaussian 16" not in s.text
    ]
    assert not bad, f"这些扰动负样本丢了正常终止行: {bad}"


def test_risk_coverage_beats_random():
    """规则引擎的排序必须显著优于随机选样。

    不可用（critical）样本共 ~31%，理想情况全被排到后面。这里只钉一个
    保守门槛：50% 覆盖率处的漏放率必须低于随机基线的一半。
    """
    from tools.risk_coverage import at_coverage, collect, curve

    rows, _dups, _amb = collect()
    pts = curve(rows)
    base = sum(1 for r in rows if r["unusable"]) / len(rows)
    r50 = at_coverage(pts, 0.5)
    assert r50 is not None
    assert r50 < base / 2, f"50% 覆盖处漏放 {r50:.1%}，随机基线 {base:.1%}，排序效果不足"


# ------------------------------------- 6. 阈值校准暴露的问题（v0.7，真实日志驱动）

def _freq_status(freqs: list[float], n_imag: int | None = None) -> dict[str, str]:
    """直接构造 CalcData 驱动规则，不经过日志解析。

    测的是「给定这组频率，规则怎么判」，跟日志格式无关，所以不该被解析器的
    行为牵着走。真实日志驱动的部分在 field_test 里，这里只钉判定的语义。
    """
    from qchem_eval.rules.evaluator import load_ruleset, run_rules
    from qchem_eval.schema import CalcData

    d = CalcData(source="freq.log", software="gaussian")
    d.stages = ["freq"]
    d.freqs = list(freqs)
    d.n_imag = sum(1 for f in freqs if f < 0) if n_imag is None else n_imag
    d.normal_termination = True
    return {o.rule.id: o.status for o in run_rules(d, load_ruleset())}


def test_imaginary_freq_is_not_reported_as_lowfreq():
    """虚频不能被 B-03 当成「低频模」——这是真实日志暴露的第一个判定错误。

    来源：水二聚体 B3LYP/6-31G(d) opt freq，实测频率
    [-250.6, -44.2, 152.3, ...]（cu04，2026-11-19）。

    旧写法 `min(d.freqs) < 100` 让 -250.6 满足条件，于是：
      · B-01 已经判了 critical「有两个虚频，不是极小点」
      · B-03 又补一条 minor「低频模需准谐振校正」
    两者的处方完全相反——低频要做 qs 校正，虚频要重新优化——所以这不只是
    重复计分，是给错了处置建议。
    """
    st = _freq_status([-250.6, -44.2, 152.3, 162.2, 246.7])
    assert st["B-01"] == "hit"          # 两个虚频：critical
    assert st["B-03"] == "miss"         # 但没有任何实频模 < 100


def test_real_low_freq_still_flagged():
    """纯实频的低频模仍要报 —— 修虚频误报不能把这条规则的本来用途一起修掉。"""
    st = _freq_status([60.0, 150.0, 320.0])
    assert st["B-03"] == "hit"
    assert st["B-01"] == "miss"


def test_lowfreq_and_small_imaginary_coexist():
    """一个体系可以同时有「小的虚频」和「实的低频模」，三条规则各管各的。

    这是 `any(0 < f < 100)` 而不是 `0 < min(freqs) < 100` 的理由：用 min 的话
    -10 会盖住 50，B-03 就漏了。
    """
    st = _freq_status([-10.0, 50.0, 300.0])
    assert st["B-02"] == "hit"          # 一个虚频：major
    assert st["B-03"] == "hit"          # 实频 50 < 100
    assert st["B-04"] == "hit"          # 虚频 -10 > -50，疑似数值噪声


def test_d09_not_applicable_without_freq():
    """没有频率的作业里 G / ZPE 根本不存在，是 na 而不是 unknown。

    实测 28 份真实日志里有 26 份被判 unknown，把「未能判定」清单塞满，
    真正需要人看的 unknown 反而被埋。na 不计入覆盖率分母，这才是对的语义。
    """
    from qchem_eval.rules.evaluator import load_ruleset, run_rules
    from qchem_eval.schema import CalcData

    d = CalcData(source="sp.log", software="gaussian")
    d.stages = ["sp"]
    d.normal_termination = True
    st = {o.rule.id: o.status for o in run_rules(d, load_ruleset())}
    assert st["D-09"] == "na"


def test_single_atom_has_no_atom_distance_check():
    """单原子体系没有「原子间距」和「片段划分」，B-05 / B-10 该判 na 不是 unknown。

    来源：field_test/real/fail_coord_missing.log（单原子氧，2026-11-19）。
    两个派生量在 len(coords_last) < 2 时返回 None，而它们在 requires 里，
    于是走的是 unknown —— 报告上显示「缺数据判不了」，但这份日志什么都不缺，
    是这类任务根本没有这个量。和 D-09 同一个病，这是第三、四次犯。
    """
    from qchem_eval.rules.evaluator import load_ruleset, run_rules
    from qchem_eval.schema import CalcData

    d = CalcData(source="O_atom.log", software="gaussian")
    d.stages = ["sp"]
    d.normal_termination = True
    d.atomic_numbers = [8]
    d.natoms = 1
    d.coords_last = [(0.0, 0.0, 0.0)]
    st = {o.rule.id: o.status for o in run_rules(d, load_ruleset())}
    assert st["B-05"] == "na"
    assert st["B-10"] == "na"


def test_empty_geometry_still_unknown_not_na():
    """坐标为空 ≠ 单原子。崩溃的日志没打印几何，此时「有几个原子」是**不知道**，
    必须留在 unknown（缺数据）而不是 na（不适用）—— 否则一份崩在 l1 的日志
    会因为「没有几何」而看起来像是「这类检查不适用」，缺口被藏起来。

    这正是 applies 写 `len(d.coords_last) != 1` 而不是 `>= 2` 的理由。
    """
    from qchem_eval.rules.evaluator import load_ruleset, run_rules
    from qchem_eval.schema import CalcData

    d = CalcData(source="crash.log", software="gaussian")
    d.stages = ["sp"]
    d.normal_termination = False
    d.atomic_numbers = []
    d.coords_last = []
    st = {o.rule.id: o.status for o in run_rules(d, load_ruleset())}
    assert st["B-05"] == "unknown"
    assert st["B-10"] == "unknown"
