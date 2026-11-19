"""误差分析工具自身的回归测试。

为什么这个工具也要测：它输出的是**结论**（「需不需要模型」），而不是一个数字。
结论错的方式不是崩掉，而是安静地把工程债算成算法需求 —— 那种错没有报错，
只会让人多训练一个没用的模型。所以这里钉三件事：

  1. 标签源必须真的加载到了（EXPECT 是 scp 到服务器单文件跑的脚本，
     一旦它被改动，本地分析会静默失去一半标签）；
  2. 「机器可归因」和「需人工确认」的边界不能被越过；
  3. 当前语料上的判错必须与已知结论一致（多出一条就是新问题，少一条就是
     标签丢了）。
"""

from __future__ import annotations

import pytest

from tools.error_analysis import (
    ATTRIBUTION_NOTE,
    SILENT_BAD,
    Cause,
    build_report,
    detect_multifield_candidates,
    load_real_expect,
)


@pytest.fixture(scope="module")
def rep() -> dict:
    # 160 份样本 + 54 份真实日志各跑一遍，几秒钟；module 级只建一次
    return build_report(with_synth=True)


def test_real_labels_are_loaded():
    """真实日志的 EXPECT 必须从 verify_rules.py 里加载到，不能是空表。

    这张表是唯一一份「跑之前写好的」真实日志期望。它丢了的话，误差分析会
    认为 54 份真实日志全部「无分歧」—— 报告全绿，什么都没测。
    """
    expect = load_real_expect()
    # 28 份日志里有 26 份写了期望（sp_allcheck_step1 / sp_opt_bouncy 没写）
    assert len(expect) == 26, f"EXPECT 条目数变了：{len(expect)}"
    # 抽查两条语义不同的期望写法
    assert expect["fail_atom_overlap"]["verdict"] == "fail"
    assert expect["ok_link2_diff"]["hit"] == ["E-08"]


def test_labeled_and_unlabeled_real_logs_are_reported_separately(rep):
    """有标签和没标签必须分开报。把没标签的算成「判对了」是本工具最大的坑。"""
    s = rep["samples"]
    assert s["n_real"] == 54
    assert s["n_labeled_real"] + s["n_unlabeled_real"] == s["n_real"]
    assert s["n_unlabeled_real"] > 0, "如果全是 0，说明 EXPECT 被当成了全覆盖"


def test_only_known_disagreement_on_labeled_samples(rep):
    """当前语料上带标签样本只有 fail_coord_missing 一条分歧。

    多出来就是新问题（要看，不要改期望）；一条都没有则说明 EXPECT 没生效。
    """
    bad = [r["name"] for r in rep["rows"] if r["causes"]]
    assert bad == ["fail_coord_missing"], f"分歧清单变了：{bad}"


def test_coord_missing_is_coverage_not_multifield(rep):
    """它看着最像「需要模型」，实际是缺数据源（要拿输入文件的原子数来比）。

    这个归因如果被改成 multifield，模型的需求量会凭空多一条 —— 而它的
    解决方式是加一个 --input 交叉核对，跟 transformer 没有关系。
    """
    row = next(r for r in rep["rows"] if r["name"] == "fail_coord_missing")
    assert [c["cause"] for c in row["causes"]] == [Cause.COVERAGE.value]
    assert row["name"] in ATTRIBUTION_NOTE
    # 规则层确实全 miss、解析器没有承认缺口 —— 所以它是真·静默失败
    assert row["n_hit"] == 0 and row["n_unknown"] == 0 and row["silent"]


def test_multifield_candidates_come_only_from_annotations(rep):
    """候选必须来自人工标注表，机器不能自己把某个样本升格成「模型需求」。"""
    cands = detect_multifield_candidates(rep["rows"])
    assert all(c["name"] in SILENT_BAD for c in cands)


def test_no_multifield_errors_are_confirmed(rep):
    """已确认的「需多字段综合」错误为 0 —— 这是「现在不该训 transformer」的
    直接依据。它变成非 0 之前，模型没有定义好的问题可学。"""
    assert rep["causes"].get(Cause.MULTIFIELD.value, 0) == 0


def test_parser_gap_is_zero_on_real_corpus(rep):
    """全语料 unparsed_hits 为 0，所以没有任何一条错误归因到解析器。

    这条一旦变红，先去看引擎的 engine.parser.gap 日志，别急着改规则 ——
    解析器的漏会被误判成规则的问题，然后规则被改坏去迁就一个 bug。
    """
    assert rep["causes"].get(Cause.PARSER.value, 0) == 0


def test_unknown_noise_is_mostly_on_failed_logs(rep):
    """多数 unknown 出现在已判 FAIL 的日志上 —— 它们在报告里不携带信息。

    这个数字是「unknown 清单需要折叠」的依据，也是 v0.7 修 D-09 的同一个理由。
    """
    un = rep["unknown_noise"]
    assert un["n_unknown_total"] > 0
    assert un["share_on_fail"] > 0.5
