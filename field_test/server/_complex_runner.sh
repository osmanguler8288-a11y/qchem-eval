#!/bin/bash
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
