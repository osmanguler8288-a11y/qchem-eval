"""失败指纹匹配。

与 catalog.yaml 的规则是两种不同机制，别混：
  rules/       基于**结构化事实**判定（阈值、一致性）——判「算完了但结果有毛病」
  signatures/  基于**日志原文**判定（官方错误目录照抄）——判「计算崩了，崩在哪」

签名匹配不需要模型，也不需要任何推断：官方的错误文本在日志里出现，就是出了问题。
这是整套系统里可信度最高的一层，因为输入和判定依据都来自外部。
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from ..schema import Action, CalcData, Finding, Severity

SIG_PATH = Path(__file__).with_name("signatures.yaml")


@dataclass
class Signature:
    id: str
    name: str
    category: str
    link: int | None
    match: list[str]
    severity: Severity
    action: Action
    official_fix: str
    ref: str
    derived: bool = False


@lru_cache(maxsize=1)
def load_signatures(path: str | Path = SIG_PATH) -> tuple[Signature, ...]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    out = []
    for s in raw.get("signatures", []):
        out.append(
            Signature(
                id=s["id"],
                name=s["name"],
                category=s.get("category", "L-异常终止"),
                link=s.get("link"),
                match=list(s.get("match") or []),
                severity=Severity(s.get("severity", "major")),
                action=Action(s.get("action", "human_review")),
                official_fix=(s.get("official_fix") or "").strip(),
                ref=s.get("ref", ""),
                derived=bool(s.get("derived")),
            )
        )
    return tuple(out)


NOTERM_FIX = (
    "官方条目 #1：如果任务不以 Normal termination 结尾且没有其他报错，"
    "通常是磁盘配额超限或内存超出申请值。先查 quota 和作业脚本的 --mem。"
)


def match(d: CalcData, sigs: tuple[Signature, ...] | None = None) -> list[Finding]:
    """把错误块逐条对签名。返回命中的 Finding（同一签名多次命中只报一条）。"""
    sigs = sigs or load_signatures()
    findings: dict[str, Finding] = {}
    hit_ids: set[str] = set()

    for block in d.error_blocks:
        text = "\n".join(block.get("lines") or [])
        if not text:
            continue
        link = block.get("link")

        for sig in sigs:
            if sig.derived or sig.id in hit_ids:
                continue
            matched_line = next((m for m in sig.match if m in text), None)
            if matched_line is None:
                continue

            hit_ids.add(sig.id)
            link_txt = f"l{link}" if link else "未标注 link"
            findings[sig.id] = Finding(
                rule_id=sig.id,
                category=sig.category,
                severity=sig.severity,
                title=sig.name,
                evidence=(
                    f"日志命中官方错误指纹（{link_txt}）。"
                    f"原文行：{matched_line!r}"
                ),
                fix_hint=f"{sig.official_fix}〔来源：{sig.ref}〕",
                action=sig.action,
            )

    # 日志直接断掉：没有任何错误行，官方条目 #1 把这归为配额/内存
    if not d.normal_termination and not hit_ids:
        tail = ""
        if d.error_blocks:
            lines = [x for x in d.error_blocks[-1].get("lines") or [] if x.strip()]
            tail = "；末行： " + " / ".join(lines[-3:]) if lines else ""
        findings["SIG-NOTERM"] = Finding(
            rule_id="SIG-NOTERM",
            category="L-进程被杀",
            severity=Severity.INFO,
            title="日志无任何终止标志",
            evidence=f"整份日志没有 Normal termination，也没有任何已知错误指纹。{tail}",
            fix_hint=NOTERM_FIX,
            action=Action.HUMAN,
        )

    return list(findings.values())


__all__ = ["Signature", "load_signatures", "match", "SIG_PATH"]
