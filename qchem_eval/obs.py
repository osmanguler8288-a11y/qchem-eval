"""可观测性：trace / span 与进程级日志。

为什么单独一个模块，而不是各处 `logging.getLogger(__name__)`：

1. **trace 必须能贯穿「一次运行 → 一份文件 → 一次解析」。** 批量跑 300 份时，
   日志行是按时间交错的，出了问题只能靠时间戳猜是哪份文件。有了 trace_id，
   `grep f3a9c21e` 就能捞出一次运行的全部上下文；报告 JSON 的 provenance 里
   也带同一个 id，日志和结果能对上。

2. **日志必须走 stderr。** stdout 是判定报告，Agent 会拿它当数据解析
   （`qchem-eval judge x.log --json | jq .verdict`）。日志混进 stdout 会让
   管道直接失败——这是可观测性最常见的自伤。

3. **默认必须安静。** 不传 --log-level 时只出 WARNING 及以上。一个质检工具的
   日志如果默认满屏，用户第一件事就是永久关掉它，那等于没有日志。

不引 loguru：本包依赖只有 pyyaml，装它的人机器上不一定有 loguru。
标准库 logging 的 Formatter / Handler 组合完全够用，而且不会跟调用方的
日志配置打架（我们只挂自己的 handler，不动 root）。
"""

from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Iterator

# ---------------------------------------------------------------- trace / span

_trace_id: ContextVar[str] = ContextVar("qce_trace", default="-")
_span_id: ContextVar[str] = ContextVar("qce_span", default="-")
_span_seq: ContextVar[int] = ContextVar("qce_span_seq", default=0)


def new_trace() -> str:
    """开一次运行，返回 8 位 trace_id。"""
    tid = uuid.uuid4().hex[:8]
    _trace_id.set(tid)
    _span_seq.set(0)
    _span_id.set("-")
    return tid


def current_trace() -> str:
    return _trace_id.get()


def current_span() -> str:
    return _span_id.get()


@contextmanager
def span() -> Iterator[str]:
    """给一个处理单元（通常是一份文件）编个号，异常退出时自动复位。

    编号用递增序号而不是 uuid：日志里 `[f3a9c21e/007]` 一眼能看出「第 7 份，
    共 N 份」，跳号还说明中间有文件没进到这里。
    """
    n = _span_seq.get() + 1
    _span_seq.set(n)
    sid = f"{n:03d}"
    token = _span_id.set(sid)
    try:
        yield sid
    finally:
        _span_id.reset(token)


# ---------------------------------------------------------------- 日志格式

class TextFormatter(logging.Formatter):
    """人读格式：时间 级别 [trace/span] event key=value。"""

    def format(self, record: logging.LogRecord) -> str:
        ts = time.strftime("%H:%M:%S", time.localtime(record.created))
        ms = int(record.msecs)
        head = (
            f"{ts}.{ms:03d} {record.levelname:<7} "
            f"[{_trace_id.get()}/{_span_id.get()}] {record.getMessage()}"
        )
        fields = getattr(record, "fields", None)
        if fields:
            head += "  " + " ".join(f"{k}={_fmt_val(v)}" for k, v in fields.items())
        if record.exc_info:
            head += "\n" + self.formatException(record.exc_info)
        return head


class JsonFormatter(logging.Formatter):
    """一行一条 JSON，给后端采集用（filebeat / loki / 直接 jq）。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
                  + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "trace_id": _trace_id.get(),
            "span_id": _span_id.get(),
            "event": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if fields:
            payload["fields"] = fields
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def _fmt_val(v: Any) -> str:
    if isinstance(v, str) and (" " in v or not v):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, (list, tuple)):
        return "[" + ",".join(str(x) for x in v) + "]"
    return str(v)


# ---------------------------------------------------------------- 配置

LOGGER_NAME = "qchem_eval"


def setup_logging(
    level: str | int = "WARNING",
    log_file: str | Path | None = None,
    json_logs: bool = False,
) -> logging.Logger:
    """配置进程级日志。可重复调用（后一次覆盖前一次），测试里要能改级别。

    只挂自己的 handler、不碰 root：调用方（FastAPI / Agent 后端）有自己的日志
    配置，从这里 `basicConfig()` 会把别人的输出也一起改掉，属于越界。
    """
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level if isinstance(level, int) else level.upper())
    logger.propagate = False           # 不往 root 冒泡，避免同一行被打印两遍

    for h in list(logger.handlers):
        logger.removeHandler(h)
        h.close()

    fmt: logging.Formatter = JsonFormatter() if json_logs else TextFormatter()

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(fmt)
    logger.addHandler(stream)

    if log_file:
        p = Path(log_file)
        if p.parent and not p.parent.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(p, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
        # 配置写错（目录不可写）必须当场炸，不能等到第一次 log 才失败 ——
        # 那时候异常会被 logging 内部吞掉并打到 stderr，看起来像程序本身出错。
        _verify_writable(p)

    return logger


def _verify_writable(p: Path) -> None:
    if not p.exists():
        raise OSError(f"日志文件未能创建：{p}")
    if not p.is_file():
        raise OSError(f"日志路径不是普通文件：{p}")


_null_attached = False


def _ensure_null_handler() -> None:
    """包级 logger 挂一个 NullHandler（PEP 282 的库约定），只挂一次。

    不挂的后果很具体：宿主没配置 logging 时，warning 级日志会漏给 Python 的
    lastResort handler，而它只打 record.getMessage() —— 于是 stderr 上出现一行
    光秃秃的 `engine.result`，**fields 全丢**，看不出是哪个文件、什么结论。
    实测症状：field_test/server/verify_rules.py 跑 28 份日志，stderr 冒出 12 行
    这样的裸事件名，而且只有判 FAIL 的那几份（WARNING 级）才有。

    这也是库的基本礼貌：import 一个库不该往宿主的 stderr 里写字。
    setup_logging() 会先摘掉全部 handler 再挂自己的，所以 CLI 行为不变。
    """
    global _null_attached
    if _null_attached:
        return
    logging.getLogger(LOGGER_NAME).addHandler(logging.NullHandler())
    _null_attached = True


def get_logger(name: str | None = None) -> logging.Logger:
    """取子 logger。名字挂到 qchem_eval.* 下，方便按模块开 DEBUG。"""
    _ensure_null_handler()
    return logging.getLogger(LOGGER_NAME if not name else f"{LOGGER_NAME}.{name}")


def log_event(logger: logging.Logger, event: str, level: int = logging.INFO,
              exc_info: object = None, **fields: Any) -> None:
    """打一条结构化事件。

    event 用点号命名（`engine.parse.done`）而不是写整句话：它同时是日志的
    检索键和（JSON 模式下的）字段名，写成句子就没法聚合了。
    细节全放 fields，这样文本模式和 JSON 模式能表达同样的信息量。

    exc_info 单独形参，不能混进 fields —— 否则 traceback 会被当成一个普通
    字段塞进 JSON，日志采集端再也认不出它是异常。
    """
    logger.log(level, event, extra={"fields": fields}, stacklevel=2,
               exc_info=exc_info)


__all__ = [
    "LOGGER_NAME",
    "JsonFormatter",
    "TextFormatter",
    "current_span",
    "current_trace",
    "get_logger",
    "log_event",
    "new_trace",
    "setup_logging",
    "span",
]
