#!/usr/bin/env python3
"""两跳 SSH 助手：本机 -> yingjin@118.195.235.38:21015 -> mayj@159.226.49.8。

为什不直接用系统 ssh：这一链两跳都只有密码认证，没有可用的密钥，
而 Bash 工具是非交互的，`ssh -tt` 会在密码提示处永久挂起。
paramiko 可以主动喂密码，还能在第一条 Transport 上开 direct-tcpip
通道直达第二跳，不需要本地起端口转发。

再往下到 cu07 / cu04 就复用 mu01 上现成的 `ssh <host>`（那边免密），
避免在这里维护三层嵌套。

用法：
    python rsh.py run "hostname; whoami"
    python rsh.py put local.txt /home/mayj/x.txt
    python rsh.py get /home/mayj/x.txt local.txt
    python rsh.py node cu04 "ls /opt/soft/g16"
"""

from __future__ import annotations

import os
import sys

import paramiko

USER = os.environ.get("QCE_SSH_USER", "mayj")
PASSWORD = os.environ.get("QCE_SSH_PASSWORD", "")
JUMP_HOST = "118.195.235.38"
JUMP_PORT = 21015
JUMP_USER = "yingjin"
TARGET_HOST = "159.226.49.8"
TARGET_PORT = 10001


def connect() -> paramiko.SSHClient:
    """建到 mu01 的连接（经第一跳）。"""
    if not PASSWORD:
        raise SystemExit("未设置 QCE_SSH_PASSWORD")

    jump = paramiko.SSHClient()
    jump.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    jump.connect(
        JUMP_HOST, port=JUMP_PORT, username=JUMP_USER, password=PASSWORD,
        look_for_keys=False, allow_agent=False, timeout=30,
    )

    transport = jump.get_transport()
    chan = transport.open_channel(
        "direct-tcpip", (TARGET_HOST, TARGET_PORT), ("127.0.0.1", 0), timeout=30,
    )

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        TARGET_HOST, port=TARGET_PORT, username=USER, password=PASSWORD,
        sock=chan, look_for_keys=False, allow_agent=False, timeout=30,
    )
    # 把第一跳挂在 client 上，保证它不被 GC 掉（否则第二跳会跟着断）
    client._jump = jump  # type: ignore[attr-defined]
    return client


def run(client: paramiko.SSHClient, cmd: str, timeout: float = 600.0) -> tuple[int, str]:
    _, out, err = client.exec_command(cmd, timeout=timeout)
    body = out.read().decode("utf-8", "replace")
    errb = err.read().decode("utf-8", "replace")
    rc = out.channel.recv_exit_status()
    return rc, body + (("\n[stderr]\n" + errb) if errb.strip() else "")


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    verb = argv[1]
    client = connect()
    try:
        if verb == "run":
            rc, txt = run(client, argv[2])
            print(txt)
            return rc
        if verb == "node":
            rc, txt = run(client, f"ssh -o BatchMode=yes {argv[2]} {shell_quote(argv[3])}")
            print(txt)
            return rc
        if verb == "put":
            sftp = client.open_sftp()
            sftp.put(argv[2], argv[3])
            print(f"uploaded {argv[2]} -> {argv[3]}")
            sftp.close()
            return 0
        if verb == "get":
            sftp = client.open_sftp()
            sftp.get(argv[2], argv[3])
            print(f"downloaded {argv[2]} -> {argv[3]}")
            sftp.close()
            return 0
    finally:
        client.close()
        j = getattr(client, "_jump", None)
        if j is not None:
            j.close()
    print(f"未知动词 {verb}")
    return 2


def shell_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


if __name__ == "__main__":
    sys.exit(main(sys.argv))
