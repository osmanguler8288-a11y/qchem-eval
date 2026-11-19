#!/bin/bash
# cu04 上的特殊用例执行器：这些用例要么依赖前一步的产物（chk），
# 要么需要外部信号（kill），没法塞进并行 runner 里。
#
# 注意 set +u：g16.profile 引用未定义变量，nounset 会当场打死脚本。
set +u
export g16root=/opt/soft
source "$g16root/g16/bsd/g16.profile" || exit 1
cd /tmp/qce || exit 1
mkdir -p out special

run() {  # run <name> <gjf>
    local name=$1
    export GAUSS_SCRDIR=$(mktemp -d /tmp/qce/scr/$name.XXXXXX)
    timeout 900 g16 < "$2" > "out/$name.log" 2>&1
    echo "$name rc=$?"
    rm -rf "$GAUSS_SCRDIR"
}

# ---------- A: geom=allcheck 吞掉 route 声明的基组（C-07 的真实触发场景） ----------
# 第一步用 6-31G(d) 优化并存 chk；第二步 route 写 STO-3G，但带 guess=read geom=allcheck，
# Gaussian 会从 chk 读回上一次的几何**和基组**，route 里的 STO-3G 被静默忽略。
cat > special/sp_allcheck_step1.gjf <<'EOF'
%nprocshared=1
%mem=1GB
%chk=/tmp/qce/special/allcheck.chk
#p B3LYP/6-31G(d) opt

step1: store checkpoint with 6-31G(d)

0 1
O   0.0000   0.0000   0.1173
H   0.0000   0.7572  -0.4692
H   0.0000  -0.7572  -0.4692

EOF
run sp_allcheck_step1 special/sp_allcheck_step1.gjf

# 第二步：只改 route 里的基组，坐标段完全省略（geom=allcheck 从 chk 取）。
cat > special/sp_allcheck_step2.gjf <<'EOF'
%nprocshared=1
%mem=1GB
%chk=/tmp/qce/special/allcheck.chk
#p B3LYP/STO-3G guess=read geom=allcheck

step2: route says STO-3G, chk holds 6-31G(d)

EOF
run sp_allcheck_step2 special/sp_allcheck_step2.gjf

# ---------- B: 真的被 SIGKILL 掉，日志停在半路（SIG-NOTERM 的真实样本） ----------
# 用 14 原子的 opt+freq，正常要跑十几秒，5 秒时强杀。
cat > special/sp_killed.gjf <<'EOF'
%nprocshared=1
%mem=2GB
#p B3LYP/6-311++G(2d,2p) opt freq

divinylbenzene, killed mid-flight

0 1
C   0.0000   0.0000   0.0000
C   1.3900   0.0000   0.0000
C   2.0950   1.2000   0.0000
C   3.4850   1.2000   0.0000
C   4.1800   0.0000   0.0000
C   3.4850  -1.2000   0.0000
C   2.0950  -1.2000   0.0000
C   4.8800   1.2100   0.0000
C   6.2800   1.2100   0.0000
C  -1.3900   0.0000   0.0000
H   0.0000   0.0000   1.0800
H  -2.0000   0.0000   1.0800
H   7.0000   0.0000   0.0000

EOF
export GAUSS_SCRDIR=$(mktemp -d /tmp/qce/scr/sp_killed.XXXXXX)
timeout -s KILL 5 g16 < special/sp_killed.gjf > out/sp_killed.log 2>&1
echo "sp_killed rc=$?  (137 = SIGKILL)"
rm -rf "$GAUSS_SCRDIR"

# ---------- C: 第一段正常终止，第二段崩（E-06「又崩又说正常」的场景） ----------
cat > special/sp_link1_partial.gjf <<'EOF'
%nprocshared=1
%mem=512MB
#p HF/STO-3G

link1 part 1 finishes fine

0 1
H 0.0 0.0 0.0
H 0.0 0.0 0.74

--Link1--
%nprocshared=1
%mem=512MB
#p HF/NOT-A-BASIS

link1 part 2 uses an illegal basis

0 1
H 0.0 0.0 0.0
H 0.0 0.0 0.90

EOF
run sp_link1_partial special/sp_link1_partial.gjf

# ---------- D: 优化过程中能量先降后升（D-01 能量突跳的真实来源） ----------
# 从一个明显不合理的构型出发，前几步能量会剧烈变化。
cat > special/sp_opt_bouncy.gjf <<'EOF'
%nprocshared=1
%mem=1GB
#p B3LYP/6-31G(d) opt=(maxstep=1)

bad starting geometry, slow crawl

0 1
O   0.0000   0.0000   0.0000
H   2.5000   0.0000   0.0000
H  -2.5000   0.1000   0.0000

EOF
run sp_opt_bouncy special/sp_opt_bouncy.gjf

echo "SPECIAL DONE"
