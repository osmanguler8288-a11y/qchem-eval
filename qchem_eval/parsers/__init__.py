"""解析器注册表。

v1 只有 Gaussian。ORCA / Q-Chem 加进来时在 dispatch() 里加一条分支即可，
上层（engine / rules）完全不需要改——它们只认 CalcData。
"""

from __future__ import annotations

from pathlib import Path

from ..schema import CalcData
from .gaussian import parse_gaussian

# 扩展名 / 内容嗅探的关键词
_GAUSSIAN_MARKS = ("Gaussian, Inc.", "Entering Gaussian System", "SCF Done")


def dispatch(path: str | Path) -> str:
    """猜是哪家的输出。猜错不致命——解析出来字段大面积缺失会被规则判 UNKNOWN。

    只读前 64 KiB：早期版本读了 200 KB，在 200 KB 的小文件上等于把整个文件读了两遍，
    bench 时发现解析耗时有一半花在这个嗅探上。
    """
    p = Path(path)
    name = p.name.lower()
    if name.endswith((".log", ".out", ".gjf.out")):
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as fh:
                head = fh.read(65536)
        except OSError:
            return "unknown"
        if any(m in head for m in _GAUSSIAN_MARKS):
            return "gaussian"
        if "O   R   C   A" in head or "ORCA TERMINATED NORMALLY" in head:
            return "orca"
    return "unknown"


def parse(path: str | Path) -> tuple[CalcData, int]:
    """统一入口。返回 (CalcData, 行数)。"""
    software = dispatch(path)
    if software == "gaussian":
        return parse_gaussian(path)
    if software == "orca":
        raise NotImplementedError("ORCA 解析器尚未实现（v1 只做 Gaussian）")
    # 未知软件也返回空 CalcData，让规则层输出 UNKNOWN 而不是崩掉
    d = CalcData(source=str(path), software="unknown")
    d.parse_warnings.append("无法识别输出文件格式，未解析任何字段")
    return d, 0


__all__ = ["dispatch", "parse", "parse_gaussian"]
