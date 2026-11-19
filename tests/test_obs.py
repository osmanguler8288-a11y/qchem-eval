"""可观测性回归测试：trace / 日志 / 汇总 / 退出码。

这四件事都属于「坏了不会报错，只会让人以错误的方式使用工具」，所以必须
用测试钉住。每条断言都对着一个真实的踩坑场景：

  1. trace 不贯穿 → 300 份批量跑挂了一份，只能靠时间戳猜是哪份
  2. 日志混进 stdout → `judge x --json | jq` 直接失败（最常见的自伤）
  3. 默认满屏 → 用户第一件事就是永久关掉日志，等于没有
  4. 汇总漏算错误文件 → 「12 个文件只跑成 11 个」这件事静默消失
  5. 退出码混用 → CI 把「配置写错了」当成「这批计算不合格」

不需要真实大日志：用 tests/data 里最小的一份，或纯构造 EvalResult。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from qchem_eval.cli import main
from qchem_eval.metrics import render_summary, summarize
from qchem_eval.obs import (
    LOGGER_NAME,
    current_span,
    current_trace,
    get_logger,
    log_event,
    new_trace,
    setup_logging,
    span,
)
from qchem_eval.schema import Action, EvalResult, Severity, Verdict

DATA = Path(__file__).parent / "data"
ROOT = Path(__file__).parent.parent
SP = DATA / "dvb_sp.out"
FAIL_LOG = ROOT / "demo" / "synth" / "A-01_abnormal_term.log"


@pytest.fixture(autouse=True)
def _reset_logging():
    """每个用例后把 logger 收回默认状态。

    setup_logging 挂的 StreamHandler 绑定的是**调用时**的 sys.stderr，而
    capsys 每个用例换一次对象。不清理的话上一个用例的 handler 会带着已经
    关闭的流活到下一个用例里，报 "I/O operation on closed file" ——
    这种错误跟被测代码毫无关系，最浪费时间。
    """
    yield
    import logging

    lg = logging.getLogger(LOGGER_NAME)
    for h in list(lg.handlers):
        lg.removeHandler(h)
        h.close()
    lg.setLevel(logging.WARNING)


# --------------------------------------------------------------- 1. trace / span

def test_new_trace_is_short_hex():
    """8 位十六进制：够短到能看（`grep f3a9c21e`），够长到不撞（4e9 种）。"""
    t = new_trace()
    assert len(t) == 8
    assert all(c in "0123456789abcdef" for c in t)
    assert current_trace() == t


def test_new_trace_resets_span_seq():
    """新一次运行必须从 001 重新数，否则日志里全是 /147、/148 这种续编号。"""
    new_trace()
    with span():
        pass
    new_trace()
    assert current_span() == "-"
    with span() as sid:
        assert sid == "001"


def test_span_increments_and_restores():
    new_trace()
    assert current_span() == "-"
    with span() as a:
        assert a == "001" and current_span() == "001"
        with span() as b:                  # 嵌套：内层有自己的编号
            assert b == "002" and current_span() == "002"
        assert current_span() == "001"     # 退出内层复位到外层
    assert current_span() == "-"           # 退出外层复位到空


def test_span_restores_on_exception():
    """异常退出也必须复位：否则一次解析崩溃会让后面所有文件都挂着旧 span。"""
    new_trace()
    with pytest.raises(RuntimeError):
        with span():
            raise RuntimeError("boom")
    assert current_span() == "-"
    with span() as sid:
        assert sid == "002"                # 序号照样递增（说明第 1 份确实处理过）


def test_trace_reaches_report_provenance():
    """trace 必须同时出现在日志和报告里，两边才对得上。

    只打日志不给报告的话，线上看到一条 ERROR 也没法定位到是哪个 verdict。
    """
    from qchem_eval.engine import evaluate_file

    tid = new_trace()
    with span():
        r = evaluate_file(SP)
    assert r.provenance["trace_id"] == tid
    assert r.provenance["span_id"] == "001"


# --------------------------------------------------------------- 2. 日志不污染 stdout

def test_default_level_is_quiet(capsys):
    """不传 --log-level 时 INFO 事件不出现。

    一个质检工具默认满屏，用户第一件事就是永久关掉它 —— 那等于没有日志。
    """
    setup_logging()
    log_event(get_logger("t"), "run.start", n_files=3)
    log_event(get_logger("t"), "run.done", level=30)      # WARNING
    err = capsys.readouterr().err
    assert "run.start" not in err
    assert "run.done" in err


def test_info_level_enables_events(capsys):
    setup_logging("INFO")
    log_event(get_logger("t"), "engine.parse.done", n_lines=1742, ms=3.9)
    err = capsys.readouterr().err
    assert "engine.parse.done" in err
    assert "n_lines=1742" in err            # 细节进 fields，不进 event 名
    assert "ms=3.9" in err


def test_logs_go_to_stderr_not_stdout(capsys):
    """stdout 是判定报告，Agent 会 `| jq`。日志混进去管道就断。"""
    setup_logging("DEBUG")
    log_event(get_logger("t"), "engine.result", verdict="fail", score=0)
    out, err = capsys.readouterr()
    assert out == ""
    assert "engine.result" in err


def test_text_format_carries_trace_and_span(capsys):
    setup_logging("INFO")
    tid = new_trace()
    with span():
        log_event(get_logger("t"), "engine.parse.done", n_lines=10)
    err = capsys.readouterr().err
    assert f"[{tid}/001]" in err


def test_json_logs_are_valid_jsonl(capsys):
    """一行一条 JSON，字段名固定 —— 后端采集端就靠这几个键建索引。"""
    setup_logging("INFO", json_logs=True)
    tid = new_trace()
    with span():
        log_event(get_logger("t"), "engine.parse.done",
                  path="a.log", n_lines=1742, software="gaussian")

    lines = [ln for ln in capsys.readouterr().err.splitlines() if ln.strip()]
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["event"] == "engine.parse.done"
    assert rec["trace_id"] == tid
    assert rec["span_id"] == "001"
    assert rec["level"] == "INFO"
    assert rec["fields"]["n_lines"] == 1742
    assert rec["fields"]["software"] == "gaussian"
    assert rec["ts"].endswith("Z")          # UTC，不带时区后缀就没法换算


def test_exception_goes_to_exc_not_fields(capsys):
    """traceback 必须是独立的 exc 字段。

    混进 fields 之后采集端只看到一个长字符串，再也认不出「这是异常」，
    告警规则（level=ERROR 或 exc 存在）就全失效了。
    """
    setup_logging("INFO", json_logs=True)
    try:
        raise ValueError("坏文件")
    except ValueError:
        log_event(get_logger("t"), "file.error", level=40,
                  path="bad.log", exc_info=True)

    rec = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert "ValueError" in rec["exc"]
    assert "exc" not in rec["fields"]
    assert rec["fields"]["path"] == "bad.log"


def test_log_file_gets_same_content(capsys, tmp_path):
    lf = tmp_path / "sub" / "run.log"       # 父目录不存在也要能建
    setup_logging("INFO", log_file=lf)
    log_event(get_logger("t"), "metrics.written", path="m.json")
    assert lf.exists()
    assert "metrics.written" in lf.read_text(encoding="utf-8")


def test_unwritable_log_path_raises(capsys, tmp_path):
    """日志路径不可用必须当场抛 OSError，由 main() 转成退出码 3。

    不能降级成「日志丢了但照常跑」：那样运维看到的日志其实是截断的。
    """
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x", encoding="utf-8")
    with pytest.raises(OSError):
        setup_logging("INFO", log_file=blocker / "run.log")


# --------------------------------------------------------------- 3. 批量汇总

def _mk(verdict: Verdict, score: int, *, unknown=(), sig=(), warns=(),
        coverage=1.0, parse_ms=5.0, n_lines=1000) -> EvalResult:
    return EvalResult(
        source=f"{verdict.value}_{score}.log",
        verdict=verdict,
        score=score,
        family="single",
        action=Action.ACCEPT if verdict is Verdict.PASS else Action.RERUN,
        findings=[],
        metrics={
            "coverage": coverage,
            "n_lines": n_lines,
            "unknown_detail": [{"rule_id": r, "title": r, "missing": ["x"]}
                               for r in unknown],
            "signature_hits": list(sig),
            "parse_warnings": list(warns),
        },
        timing={"parse": parse_ms, "rules": 0.4},
    )


def test_summary_counts_errors_separately():
    """错误文件不进 results，但不能从 n_files 里消失。

    混进 results 会变成一次 PASS（最坏的错），静默丢掉会让「12 个只跑成 11 个」
    这件事查不出来。
    """
    s = summarize([_mk(Verdict.PASS, 100)], [{"source": "bad.log", "error": "解码失败"}])
    assert s["n_files"] == 2
    assert s["n_ok"] == 1
    assert s["n_error"] == 1
    assert s["errors"][0]["source"] == "bad.log"


def test_summary_verdicts_and_actions():
    s = summarize([
        _mk(Verdict.PASS, 100),
        _mk(Verdict.WARNING, 85),
        _mk(Verdict.FAIL, 0),
        _mk(Verdict.UNKNOWN, 0),
    ])
    assert s["verdicts"] == {"pass": 1, "pass_with_warning": 1, "fail": 1, "unknown": 1}
    assert s["actions"]["accept"] == 1
    assert s["actions"]["rerun"] == 3


def test_summary_score_stats_and_histogram():
    s = summarize([_mk(Verdict.PASS, 100), _mk(Verdict.WARNING, 85),
                   _mk(Verdict.FAIL, 40)])
    assert s["score"]["mean"] == 75.0
    assert s["score"]["min"] == 40
    assert s["score"]["median"] == 85
    assert s["score"]["histogram"]["0-59"] == 1
    assert s["score"]["histogram"]["80-89"] == 1
    assert s["score"]["histogram"]["100"] == 1


def test_summary_p95_beats_mean_on_one_outlier():
    """p95 而非平均值：一份 300 MB 的日志能把均值彻底带偏。

    19 份 5 ms + 1 份 900 ms：均值 49.8 ms（看着像整体慢了一倍），
    p95 仍然是 5 ms —— 真实体验是「绝大多数很快」。
    """
    results = [_mk(Verdict.PASS, 100, parse_ms=5.0) for _ in range(19)]
    results.append(_mk(Verdict.PASS, 100, parse_ms=900.0))
    s = summarize(results)
    assert s["timing_ms"]["parse_mean"] == 49.8
    assert s["timing_ms"]["parse_p95"] == 5.0
    assert s["timing_ms"]["parse_max"] == 900.0


def test_p95_degrades_to_max_on_tiny_batches():
    """样本 < 20 时 p95 就等于最大值（nearest-rank 的必然结果）。

    把这个行为钉下来，是因为它会误导人：跑 10 份时看到 p95 == 最慢那份，
    很容易以为统计写错了。它不是 bug，但也别拿小批量的 p95 当分位数解读。
    """
    results = [_mk(Verdict.PASS, 100, parse_ms=5.0) for _ in range(9)]
    results.append(_mk(Verdict.PASS, 100, parse_ms=900.0))
    s = summarize(results)
    assert s["timing_ms"]["parse_p95"] == 900.0
    assert s["timing_ms"]["parse_max"] == 900.0


def test_summary_unknown_by_rule():
    """UNKNOWN 按规则聚合是解析器缺口的唯一信号源。"""
    s = summarize([
        _mk(Verdict.WARNING, 90, unknown=["C-05", "D-03"]),
        _mk(Verdict.WARNING, 90, unknown=["C-05"]),
    ])
    assert s["unknown"]["total"] == 3
    assert s["unknown"]["by_rule"]["C-05"] == 2


def test_summary_empty_batch_does_not_divide_by_zero():
    s = summarize([], [{"source": "a.log", "error": "x"}])
    assert s["n_ok"] == 0 and s["score"]["mean"] == 0.0
    assert s["timing_ms"]["parse_mean"] == 0.0
    assert s["throughput"]["mlines_per_s"] == 0.0


def test_render_summary_mentions_errors():
    s = summarize([_mk(Verdict.PASS, 100)], [{"source": "bad.log", "error": "解码失败"}])
    text = render_summary(s)
    assert "bad.log" in text and "解码失败" in text
    assert "处理失败" in text


# --------------------------------------------------------------- 4. CLI 端到端

def test_cli_json_stdout_is_pure_json():
    """子进程跑真 CLI：stdout 必须能被 json.loads 直接吃掉。

    这是唯一能证明「日志真的没混进 stdout」的测法 —— 进程内用 capsys，
    StreamHandler 抓的是同一个替换过的对象，行为可能和真实终端不一致。
    """
    cp = subprocess.run(
        [sys.executable, "-m", "qchem_eval", "judge", str(SP),
         "--json", "--log-level", "DEBUG"],
        cwd=str(ROOT), capture_output=True,
        encoding="utf-8", errors="replace",
    )
    payload = json.loads(cp.stdout)         # 混进一行日志就会抛
    assert payload["verdict"] == "pass_with_warning"
    assert payload["provenance"]["trace_id"] in cp.stderr   # 日志在 stderr 里
    assert cp.returncode == 0


def test_cli_batch_prints_summary(capsys):
    rc = main(["judge", str(SP), str(FAIL_LOG), "--quiet"])
    out = capsys.readouterr().out
    assert rc == 1                          # 有 FAIL
    assert "批量汇总" in out
    assert "2 个文件" in out


def test_cli_metrics_out_written(capsys, tmp_path):
    mf = tmp_path / "metrics.json"
    main(["judge", str(SP), str(FAIL_LOG), "--quiet", "--metrics-out", str(mf)])
    s = json.loads(mf.read_text(encoding="utf-8"))
    assert s["n_files"] == 2 and s["n_ok"] == 2
    assert s["verdicts"]["fail"] == 1
    assert len(s["trace_id"]) == 8


def test_cli_missing_path_exit_2(capsys):
    """路径不存在是**调用方**的错，不能算「这批计算不合格」（那是 1）。"""
    rc = main(["judge", str(DATA / "no_such_file.log")])
    assert rc == 2
    assert "文件不存在" in capsys.readouterr().err


def test_cli_directory_exit_4(capsys, tmp_path):
    """传目录：报「不是普通文件」，而不是让解析器降级成「格式无法识别」。

    后者是错的诊断 —— 真实原因是调用方把目录当文件传进来了。
    """
    rc = main(["judge", str(tmp_path)])
    assert rc == 4
    assert "不是普通文件" in capsys.readouterr().err


def test_cli_bad_log_file_exit_3(capsys, tmp_path):
    """日志初始化失败是**配置**错误，用 3 表示，与结论、故障都分开。"""
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    rc = main(["judge", str(SP), "--log-file", str(blocker / "run.log")])
    assert rc == 3
    assert "日志初始化失败" in capsys.readouterr().err


def test_cli_batch_continues_after_bad_file(capsys, tmp_path):
    """一份「读不懂」的文件不能让整批失败。

    实测行为（钉在这里，免得以后误以为是 bug）：解析器用
    `errors="replace"` 读文件，所以任何字节序列都能读成文本、永远不会抛，
    最终走「格式无法识别 → UNKNOWN」这条路，退出码仍是 0。

    也就是说退出码 4 实际上只由「非普通文件」触发；「有份日志完全读不懂」
    这件事体现在 summary 里（n_files 含它、verdicts.unknown 计数），而不是
    退出码。CI 要拦这种情况得看 summary 的 unknown，不能只看 returncode。
    """
    bad = tmp_path / "bad.log"
    bad.write_bytes(b"\xff\xfe\x00\x00 \x81garbage")     # 非 UTF-8
    rc = main(["judge", str(SP), str(bad), "--quiet", "--metrics-out",
               str(tmp_path / "m.json")])
    assert rc == 0                          # 好文件没 FAIL
    s = json.loads((tmp_path / "m.json").read_text(encoding="utf-8"))
    assert s["n_files"] == 2 and s["n_ok"] == 2 and s["n_error"] == 0
    assert s["verdicts"]["unknown"] == 1    # 坏文件在这里现身，没被静默丢掉


def test_library_adds_null_handler(capsys):
    """宿主不配置 logging 时，库不能往 stderr 漏裸事件名。

    漏的方式很隐蔽：warning 级日志交给 Python 的 lastResort handler，它只打
    record.getMessage()，于是 stderr 上出现一行 `engine.result`，**fields 全丢**。
    实测症状是 field_test/server/verify_rules.py 跑 28 份日志时冒出 12 行这样的
    裸事件名 —— 只看得出「有东西判了 FAIL」，看不出是哪份文件、什么结论。
    """
    import logging

    from qchem_eval import obs

    pkg = logging.getLogger(obs.LOGGER_NAME)
    saved_handlers, saved_prop = pkg.handlers[:], pkg.propagate
    saved_flag = obs._null_attached
    try:
        pkg.handlers.clear()
        pkg.propagate = True
        obs._null_attached = False
        obs._ensure_null_handler()
        # WARNING 级 —— 未挂 NullHandler 时这一行一定会漏到 stderr
        obs.log_event(obs.get_logger("engine"), "engine.result", verdict="fail")
    finally:
        pkg.handlers[:] = saved_handlers
        pkg.propagate = saved_prop
        obs._null_attached = saved_flag

    assert capsys.readouterr().err == ""
