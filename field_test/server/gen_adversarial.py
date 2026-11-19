#!/usr/bin/env python3
"""从一份真实日志派生出「解析器未必见过」的输入形态。

目的不是造更多失败计算，而是问一个不同的问题：
**输入形态本身异常时，评测系统会崩、会瞎，还是会老实说「我判不了」？**

期望（对所有变体一致）：
  · 不许抛未捕获异常（崩溃 = 让调用方无从处理）
  · 不许因为解析不到东西就判 PASS（这正是「缺数据不等于通过」要防的）
  · 最好给 UNKNOWN + 说清缺什么

用法：
    python gen_adversarial.py <source.log> <outdir>
"""

from __future__ import annotations

import random
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 2
    src = Path(argv[1])
    out = Path(argv[2])
    out.mkdir(parents=True, exist_ok=True)

    raw = src.read_bytes()
    text = raw.decode("utf-8", "replace")
    lines = text.splitlines(keepends=True)

    def w(name: str, data: bytes) -> None:
        (out / name).write_bytes(data)

    # 1) CRLF 换行：Windows 上转存、邮件附件、FTP ASCII 模式都会造成这个
    w("adv_crlf.log", text.replace("\n", "\r\n").encode("utf-8"))

    # 2) 截断在不同位置：模拟磁盘满 / 传输中断
    for frac in (0.15, 0.5, 0.9):
        cut = int(len(raw) * frac)
        w(f"adv_trunc_{int(frac * 100)}.log", raw[:cut])

    # 3) 空文件 / 只有版权头：最常见的「传错文件」
    w("adv_empty.log", b"")
    w("adv_only_header.log", raw[:1500])

    # 4) 二进制垃圾混入：解压错文件、被其他程序写过一半
    rng = random.Random(20261118)
    noise = bytes(rng.randrange(256) for _ in range(4096))
    mid = len(raw) // 2
    w("adv_binary_noise.log", raw[:mid] + noise + raw[mid:])

    # 5) 超长单行：某些工具会把整个 JSON 塞成一行
    body = raw.decode("utf-8", "replace")
    w("adv_long_line.log", ("X" * 2_000_000 + "\n" + body).encode("utf-8"))

    # 6) 非法 UTF-8 字节：老版本程序用 latin-1 写日志
    w("adv_latin1.log", text.encode("latin-1", "replace"))

    # 7) 没有结尾换行：很多编辑器会去掉它
    w("adv_no_final_nl.log", raw.rstrip(b"\n"))

    # 8) 两份日志首尾相接：cat a.log b.log 的产物，会出现两个 SCF Done
    w("adv_concat.log", raw + raw)

    # 9) 冒充老版本：G09 的排版与 G16 有差异，看解析器是否死认版本号
    w("adv_g09_label.log", text.replace("Gaussian 16", "Gaussian 09").encode("utf-8"))

    # 10) 只留下错误块所在的那一段（信息极少的碎片）
    idx = text.find("Error termination")
    frag = text[max(0, idx - 800): idx + 800] if idx >= 0 else text[:1600]
    w("adv_fragment.log", frag.encode("utf-8"))

    made = sorted(p.name for p in out.glob("*.log"))
    print(f"生成 {len(made)} 个对抗样本 -> {out}")
    for m in made:
        n = (out / m).stat().st_size
        print(f"  {m:26s} {n:>9d} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
