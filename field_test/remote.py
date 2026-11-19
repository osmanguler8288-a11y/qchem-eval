#!/usr/bin/env python3
"""远程验证驱动：本机 -> mu01 -> cu04(Gaussian) / cu07(规则引擎)。

拓扑要点（实测，不是照抄交接文档）：

  · mu01 上 `/nfs-cu07-1` 是 NFS4 挂载（10.0.0.17:/localdata），可读写。
    交接文档说「cu02/cu04 上访问不到」，这对 cu04 成立，但 mu01 自己能看到，
    所以无需 `scp -3` 绕道，直接以 mu01 为中转站即可。
  · mu01 -> cu07/cu04/cu02 全部免密（BatchMode=yes 可用）。
  · cu04 的 Python 是 3.6.8 且无 PyYAML，跑不了本仓库代码（需要 3.10+ 语法）。
    所以：Gaussian 留在 cu04，规则引擎跑在 mu01 的 /nfs-soft/anaconda3/bin/python3.12。
  · mu01 的 `/` 只剩 9G，`/home` 是 NFS 且 99% 满 —— 临时文件一律放 /tmp，
    产物写 /nfs-cu07-1（9T 可用）。

命令：
    python remote.py push          # 输入文件 -> cu04:/tmp/qce/in
    python remote.py gauss         # 在 cu04 批量跑 Gaussian
    python remote.py fetch         # cu04 输出 -> 本机 field_test/real
    python remote.py shell <cmd>   # mu01 上执行
    python remote.py cu04 <cmd>    # cu04 上执行
"""

from __future__ import annotations

import io
import os
import shlex
import sys
import tarfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from rsh import connect, run  # noqa: E402

HERE = Path(__file__).parent
INPUTS = HERE / "server" / "inputs"
REAL = HERE / "real"
REMOTE_STAGE = "/tmp/qce"          # mu01 上的中转目录
CU04_STAGE = "/tmp/qce"            # cu04 上的工作目录


def mu01(cmd: str, timeout: float = 900.0) -> tuple[int, str]:
    global _CLIENT
    return run(_CLIENT, cmd, timeout)


def cu04(cmd: str, timeout: float = 1800.0) -> tuple[int, str]:
    return mu01(f"ssh -o BatchMode=yes cu04 {shlex.quote(cmd)}", timeout)


def _tar_bytes(root: Path) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for p in sorted(root.rglob("*")):
            if p.is_file():
                tf.add(p, arcname=str(p.relative_to(root)))
    return buf.getvalue()


def push() -> int:
    """把 server/inputs/*.gjf 推给 cu04。先打 tar 再走 sftp，避免逐文件往返。"""
    mu01(f"mkdir -p {REMOTE_STAGE}")
    # cu04 的目录必须在 cu04 上建 —— mu01 的 mkdir 管不到别的机器。
    rc0, txt0 = cu04(f"mkdir -p {CU04_STAGE}/in {CU04_STAGE}/out {CU04_STAGE}/scr && echo dirs ok")
    print(txt0.strip())

    payload = HERE / "server" / ".inputs.tgz"
    payload.write_bytes(_tar_bytes(INPUTS))

    sftp = _CLIENT.open_sftp()
    sftp.put(str(payload), f"{REMOTE_STAGE}/inputs.tgz")
    sftp.close()

    rc, txt = mu01(f"scp -q -o BatchMode=yes {REMOTE_STAGE}/inputs.tgz cu04:{CU04_STAGE}/")
    print(txt or "scp ok")
    rc2, txt2 = cu04(f"cd {CU04_STAGE} && tar xzf inputs.tgz -C in && ls in | wc -l")
    print("files on cu04:", txt2.strip())
    return 0 if rc == 0 and rc2 == 0 and rc0 == 0 else 1


RUNNER = r"""#!/bin/bash
# cu04 上的批量执行器。每个作业独立 GAUSS_SCRDIR，互不污染。
# 注意：这里**不能**用 `set -u`。Gaussian 自带的 g16.profile 会引用未定义变量
# （第 32 行 PERLLIB），一旦开了 nounset，source 当场就把脚本打死。
#
# 输入/输出目录走参数，不要在 Python 侧用字符串替换 —— 早先试过
# `.replace("out", "bout")`，结果把 `timeout` 改成了 `timebout`，六个作业
# 全部 rc=127，日志里只有一行 `command not found`，看着像环境问题。
set +u
export g16root=/opt/soft
source "$g16root/g16/bsd/g16.profile" || exit 1
cd /tmp/qce || exit 1
IN_DIR=${1:-in}
OUT_DIR=${2:-out}
mkdir -p "$OUT_DIR" scr
for f in "$IN_DIR"/*.gjf; do
    name=$(basename "$f" .gjf)
    [ -s "$OUT_DIR/$name.log" ] && { echo "skip $name"; continue; }
    (
        export GAUSS_SCRDIR=$(mktemp -d /tmp/qce/scr/$name.XXXXXX) || exit 1
        timeout 900 g16 < "$f" > "$OUT_DIR/$name.log" 2>&1
        echo "$name rc=$?" >> "$OUT_DIR/_status.txt"
        rm -rf "$GAUSS_SCRDIR"
    ) &
done
wait
echo "ALL DONE"
"""


def gauss() -> int:
    """把执行器写到 cu04 并跑。"""
    script = HERE / "server" / "_runner.sh"
    # 必须显式指定 LF。Windows 默认写 CRLF，到了 Linux 上 `set -u` 会变成 `set -u\r`
    # 被解析成非法选项，`$g16root/g16/...` 也会带上 \r 变成不存在的路径 —— 一个
    # 换行符能把整个批量执行器打废，而且报错信息（invalid option）根本不指向真凶。
    script.write_text(RUNNER, encoding="utf-8", newline="\n")
    sftp = _CLIENT.open_sftp()
    sftp.put(str(script), f"{REMOTE_STAGE}/_runner.sh")
    sftp.close()
    mu01(f"scp -q -o BatchMode=yes {REMOTE_STAGE}/_runner.sh cu04:{CU04_STAGE}/")
    rc, txt = cu04(
        f"tr -d '\\r' < {CU04_STAGE}/_runner.sh > {CU04_STAGE}/r.sh && "
        f"chmod +x {CU04_STAGE}/r.sh && bash {CU04_STAGE}/r.sh",
        timeout=1700,
    )
    print(txt)
    return rc


def run_inputs(src: Path, label: str, timeout: float = 1700.0) -> int:
    """把 src/*.gjf 推到 cu04 跑一遍 Gaussian，再把日志拉回 field_test/<label>_out/。

    和 push/gauss 走同一个执行器（RUNNER），只是输入/输出目录换成参数传入，
    免得给每个专项都复制一遍同样的二十行。
    """
    if not src.is_dir() or not list(src.glob("*.gjf")):
        print(f"没有输入文件：{src}")
        return 1

    in_dir = label[:3]
    out_dir = f"{in_dir}out"
    tgz = f"{label}.tgz"

    # 先清掉上一轮产物。RUNNER 用 `-s` 判断跳过已完成作业，残留的空文件/半截
    # 日志会让它全跳过 —— 曾经被 53 字节的 `command not found` 卡住过一次。
    cu04(f"mkdir -p {CU04_STAGE}/{in_dir} {CU04_STAGE}/{out_dir} {CU04_STAGE}/scr && "
         f"rm -f {CU04_STAGE}/{out_dir}/* && echo ok")

    payload = HERE / "server" / f".{label}.tgz"
    payload.write_bytes(_tar_bytes(src))
    sftp = _CLIENT.open_sftp()
    sftp.put(str(payload), f"{REMOTE_STAGE}/{tgz}")
    sftp.close()
    mu01(f"scp -q -o BatchMode=yes {REMOTE_STAGE}/{tgz} cu04:{CU04_STAGE}/")
    cu04(f"cd {CU04_STAGE} && tar xzf {tgz} -C {in_dir} && ls {in_dir} | wc -l")

    runner = HERE / "server" / f"_{label}_runner.sh"
    # 目录走命令行参数，不做字符串替换（见 RUNNER 里的注释）
    runner.write_text(RUNNER, encoding="utf-8", newline="\n")
    sftp = _CLIENT.open_sftp()
    sftp.put(str(runner), f"{REMOTE_STAGE}/_{label}_runner.sh")
    sftp.close()
    mu01(f"scp -q -o BatchMode=yes {REMOTE_STAGE}/_{label}_runner.sh cu04:{CU04_STAGE}/")
    rc, txt = cu04(
        f"tr -d '\\r' < {CU04_STAGE}/_{label}_runner.sh > {CU04_STAGE}/{label}_r.sh && "
        f"bash {CU04_STAGE}/{label}_r.sh {in_dir} {out_dir}", timeout=timeout)
    print(txt)

    dst = HERE / f"{label}_out"
    dst.mkdir(exist_ok=True)
    cu04(f"cd {CU04_STAGE} && tar czf {CU04_STAGE}/{label}_out.tgz {out_dir}")
    mu01(f"scp -q -o BatchMode=yes cu04:{CU04_STAGE}/{label}_out.tgz {REMOTE_STAGE}/")
    sftp = _CLIENT.open_sftp()
    sftp.get(f"{REMOTE_STAGE}/{label}_out.tgz", str(HERE / "server" / f".{label}_out.tgz"))
    sftp.close()
    with tarfile.open(HERE / "server" / f".{label}_out.tgz") as tf:
        tf.extractall(dst, filter="data")
    got = {p.name for p in dst.rglob("*.log")}
    print(f"拉回 {len(got)} 份日志 -> {dst}")
    return 0 if rc == 0 else 1


def badgeom() -> int:
    """错误结构专项：6 种明显错的结构。"""
    return run_inputs(HERE / "server" / "badgeom", "badgeom")


def complex_() -> int:
    """B-10 对照组：真实的分子间复合物 + 一个故意的非相互作用极限。"""
    return run_inputs(HERE / "server" / "complex", "complex")


def calib() -> int:
    """阈值校准样本：opt freq 为主的真实难度阶梯。

    这批不是为了「认出失败」，是为了「看阈值该定在哪」——现有 28 份里只有
    2 份带频率，振动类规则根本没有分布可看。opt freq 比单点慢一个量级，
    所以超时给到 3000 秒（单核 6-8 原子的 B3LYP/6-31G(d) opt freq 约 3-8 分钟，
    6 份并行；拉开键长的那两份会走更多优化步）。
    """
    return run_inputs(HERE / "server" / "calib", "calib", timeout=3000.0)


DEPLOY_DIR = "/nfs-cu07-1/mqh/qchem-eval-verify"


def deploy() -> int:
    """把 qchem_eval 包 + 验收脚本 + 报告 + 真实日志部署到 NFS 工作目录。"""
    root = HERE.parent
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for p in sorted((root / "qchem_eval").rglob("*")):
            if p.is_file() and "__pycache__" not in str(p):
                tf.add(p, arcname=str(p.relative_to(root)))
        for p in sorted((root / "tools").glob("*.py")):
            tf.add(p, arcname=f"tools/{p.name}")
        for p in sorted((HERE / "server").glob("*.py")):
            tf.add(p, arcname=f"field_test/server/{p.name}")
        for p in sorted((HERE / "server" / "inputs").glob("*.gjf")):
            tf.add(p, arcname=f"field_test/server/inputs/{p.name}")
        for name in ("REPORT.md", "rsh.py", "remote.py"):
            f = HERE / name
            if f.is_file():
                tf.add(f, arcname=f"field_test/{name}")
        # 真实日志一起带过去，服务器侧验收才有东西可跑
        for p in sorted((HERE / "real").glob("*.log")):
            tf.add(p, arcname=f"field_test/real/{p.name}")
        # tools/eval_rules.py 和 risk_coverage.py 读的是 tests/data —— 不带过去，
        # 服务器上就只能跑 field_test/server/verify_rules.py，跑不了完整评估。
        # 约 2.2MB，为了「拿到这个目录就能自证」值得带。
        for p in sorted((root / "tests" / "data").glob("*")):
            if p.is_file():
                tf.add(p, arcname=f"tests/data/{p.name}")
    local = HERE / "server" / ".code.tgz"
    local.write_bytes(buf.getvalue())

    mu01(f"mkdir -p {DEPLOY_DIR}")
    sftp = _CLIENT.open_sftp()
    sftp.put(str(local), f"{REMOTE_STAGE}/code.tgz")
    sftp.close()
    rc, txt = mu01(
        f"cd {DEPLOY_DIR} && tar xzf {REMOTE_STAGE}/code.tgz && "
        f"mkdir -p logs field_test/real field_test/adversarial out && "
        f"ls -d qchem_eval tools && echo deploy-ok"
    )
    print(txt)
    return rc


def special() -> int:
    """跑依赖前序产物 / 外部信号的特殊用例（allcheck、kill、link1 半途失败）。"""
    script = HERE / "server" / "special.sh"
    sftp = _CLIENT.open_sftp()
    sftp.put(str(script), f"{REMOTE_STAGE}/special.sh")
    sftp.close()
    mu01(f"scp -q -o BatchMode=yes {REMOTE_STAGE}/special.sh cu04:{CU04_STAGE}/")
    rc, txt = cu04(
        f"tr -d '\\r' < {CU04_STAGE}/special.sh > {CU04_STAGE}/s.sh && "
        f"chmod +x {CU04_STAGE}/s.sh && bash {CU04_STAGE}/s.sh",
        timeout=1700,
    )
    print(txt)
    return rc


def pushlogs() -> int:
    """把本机 field_test/real 下的日志推到 NFS 工作目录，供服务器侧验收。"""
    payload = HERE / "server" / ".logs.tgz"
    payload.write_bytes(_tar_bytes(REAL))
    sftp = _CLIENT.open_sftp()
    sftp.put(str(payload), f"{REMOTE_STAGE}/logs.tgz")
    sftp.close()
    rc, txt = mu01(
        f"cd {DEPLOY_DIR} && tar xzf {REMOTE_STAGE}/logs.tgz -C field_test/real 2>/dev/null || "
        f"(mkdir -p field_test/real && tar xzf {REMOTE_STAGE}/logs.tgz -C field_test/real); "
        f"ls field_test/real/*.log | wc -l"
    )
    print("logs on nfs:", txt.strip())
    return rc


def fetch() -> int:
    """把 cu04 的输出拉回本机。"""
    REAL.mkdir(parents=True, exist_ok=True)
    # cu04 上没有 tar 到共享盘的能力（它看不到 /nfs-cu07-1），所以走 stdout 管道：
    # `ssh cu04 'tar czf - out' > /tmp/qce/out.tgz`，由 mu01 落盘后再 sftp 拉回。
    rc, txt = mu01(
        f"scp -q -o BatchMode=yes cu04:{CU04_STAGE}/out/_status.txt {REMOTE_STAGE}/_status.txt 2>/dev/null; "
        f"ssh -o BatchMode=yes cu04 'cd {CU04_STAGE} && tar czf - out' > {REMOTE_STAGE}/out.tgz; "
        f"ls -l {REMOTE_STAGE}/out.tgz"
    )
    print(txt)
    sftp = _CLIENT.open_sftp()
    sftp.get(f"{REMOTE_STAGE}/out.tgz", str(HERE / "server" / ".out.tgz"))
    try:
        sftp.get(f"{REMOTE_STAGE}/_status.txt", str(HERE / "server" / "_status.txt"))
    except OSError:
        pass
    sftp.close()

    with tarfile.open(HERE / "server" / ".out.tgz") as tf:
        tf.extractall(REAL.parent, filter="data")
    got = sorted((REAL.parent / "out").glob("*.log"))
    for p in got:
        p.replace(REAL / p.name)
    print(f"fetched {len(got)} logs -> {REAL}")
    return 0


_CLIENT = None


def main(argv: list[str]) -> int:
    global _CLIENT
    if len(argv) < 2:
        print(__doc__)
        return 2
    _CLIENT = connect()
    try:
        verb = argv[1]
        if verb == "push":
            return push()
        if verb == "gauss":
            return gauss()
        if verb == "badgeom":
            return badgeom()
        if verb == "complex":
            return complex_()
        if verb == "calib":
            return calib()
        if verb == "deploy":
            return deploy()
        if verb == "special":
            return special()
        if verb == "pushlogs":
            return pushlogs()
        if verb == "fetch":
            return fetch()
        if verb == "shell":
            rc, txt = mu01(argv[2])
            print(txt)
            return rc
        if verb == "cu04":
            rc, txt = cu04(argv[2])
            print(txt)
            return rc
    finally:
        _CLIENT.close()
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
