"""qchem-eval：面向量子化学 Agent 的科学计算质量评测。

分层（与方案文档 2 节对应）：
  L0 采集  -> parsers/        把 .log 变成 CalcData
  L1 特征  -> features.py     CalcData -> 特征向量（L3 模型用，v1 留空）
  L2 规则  -> rules/          确定性裁定（本版重点）
  L4 决策  -> engine.py       findings -> verdict / score / action
  L5 闭环  -> （不在 demo 范围）

核心原则：**缺数据不等于通过**。
"""

__version__ = "0.1.0"

from .engine import evaluate_file, evaluate_pair
from .schema import Action, CalcData, EvalResult, Finding, Severity, Verdict

__all__ = [
    "Action",
    "CalcData",
    "EvalResult",
    "Finding",
    "Severity",
    "Verdict",
    "evaluate_file",
    "evaluate_pair",
    "__version__",
]
