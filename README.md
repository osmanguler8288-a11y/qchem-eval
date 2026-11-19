# qchem-eval

量子化学计算结果的质量判定。**缺数据不等于通过。**

一句话定位：给量子化学 Agent 一个「这次算出来的数能不能用」的确定性出口。
不开模型，先做规则层（L2）—— 因为规则层是唯一能在没有标注数据时就把系统跑起来的部分。

## 跑起来

```bash
python -m qchem_eval judge  xxx.log              # 单份判定，中文诊断
python -m qchem_eval judge  a.out b.out --json   # 机器可读（Agent 消费）
python -m qchem_eval compare opt.log sp.log      # 跨阶段一致性
python -m qchem_eval rules                       # 打印规则清单
python -m qchem_eval bench  xxx.log              # 解析性能 vs cclib

# 可观测性（每个子命令都支持，放在子命令后面更顺手）
judge a.out b.out --log-level INFO --log-json --log-file run.log \
                  --metrics-out metrics.json
```

退出码：`0` 无 FAIL / `1` 有 FAIL / `2` 路径不存在 / `3` 配置错（规则清单或日志初始化）
/ `4` 有文件处理失败。**结论和故障不用同一个码** —— `1` 是「这批计算不合格」，
`2`/`3`/`4` 是「你调用错了 / 配置错了 / 我这边出了故障」，CI 和 Agent 必须能分开处理。

> 注意 `4` 实际只由「非普通文件」（目录、管道、设备）触发。日志内容完全读不懂
> 时解析器走 `errors="replace"` 降级成 UNKNOWN，**退出码仍是 0**。要让 CI 拦住
> 这种，得看 summary 里的 `verdicts.unknown`，不能只看 returncode。

## 两条独立的判定机制（别混）

| | `rules/catalog.yaml` | `rules/signatures.yaml` |
|---|---|---|
| 判据来源 | 阈值、一致性等**结构化事实** | 官方错误目录**日志原文** |
| 回答的问题 | 算完了，但结果有没有毛病 | 崩了没有，崩在哪一步 |
| 数量 | 39 条 + 2 条跨阶段 | 28 条（官方 27 + 派生 1） |
| 数据哪来的 | 自建 + QMCert/AiiDA/GoodVibes 阈值 | Supreeth Prasad《Master List of Gaussian Error Messages》逐字照抄 |

签名层的可信度基础是「照抄」二字，且这一点**可复核**：

```bash
python tools/verify_signatures.py    # 拉官方页面 → 逐条 match 串 + 逐行 cause_text 做包含检查
```

## 三个和现有开源工具不一样的地方

**1. 缺数据判 UNKNOWN，不判 PASS**

QMCert 的入口是 `assess_qm_quality(scf_converged: bool = True)` —— 解析失败时默认值让计算安静地通过。
这里是反过来的：每条规则声明 `requires`，任一字段缺失即 `UNKNOWN`，`EvalResult.abstain=True`。

规则状态是四态，不是三态：

| 状态 | 含义 | 计入覆盖率分母 |
|---|---|---|
| `hit` | 命中，产出 Finding | 是 |
| `miss` | 判定不触发 | 是 |
| `unknown` | 数据缺失，判不了 | 是（拉低覆盖率） |
| `na` | 这类任务没这个检查项 | 否 |

`na` 是必要的：opt-only 的任务去问「有没有虚频」不是缺数据，是不适用。

**2. 跨阶段一致性检查**

QMCert / atomate / AiiDA 全都只做单次计算自检。真实工作流是 `opt → sp → freq` 串起来的，
各阶段能量必须自洽 —— 这条没人做。见 `compare` 子命令与 D-05/D-06。

**3. 规则是数据，不是代码**

39 条规则全在 `rules/catalog.yaml`，带来源标记（`[Q]` QMCert / `[A]` AiiDA exit code /
`[K]` Kulik 论文 / `[G]` GoodVibes / `[C]` cclib / `[M]` 自建）。阈值要按体系调档，埋进 Python 就调不动了。

SCF 收敛判据也不写死：从日志自己的
`Requested convergence on RMS density matrix=1.00D-08` 里读，用户改了 `scf=conver=N` 也能正确比较。

## 结构

```
qchem_eval/
  schema.py              两个契约：CalcData（事实）/ EvalResult（唯一出口）
  parsers/gaussian.py    单遍流式解析，Gaussian 16
  parsers/__init__.py    格式嗅探与分发（ORCA 待接）
  rules/catalog.yaml     39 条结构化规则 + 2 条跨阶段规则（v0.8）
  rules/signatures.yaml  28 条官方错误指纹（verbatim）
  rules/evaluator.py     求值 + 加载期校验 + 最坏优先聚合
  rules/signatures.py    错误块 → 指纹匹配
  engine.py              L4 决策 + 跨阶段核对
  obs.py                 trace/span + 进程级日志（只挂自己的 handler，不动 root）
  metrics.py             批量汇总（纯函数：不读文件、不打日志，服务端也能复用）
  cli.py                 报告渲染 + 退出码
tools/
  verify_signatures.py   「照抄官方」的可复核证据
  synth_failures.py      带标签失败样本生成（嫁接 + 变异）
  eval_rules.py          规则级 / 签名级 P/R 报告
  risk_coverage.py       risk-coverage 曲线 + AURC（零绘图依赖，手写 SVG）
  calibrate.py           用真实日志校准阈值（分布 + 余量 + 未校准项）
  error_analysis.py      判错四分类归因 + 无标签日志的一致性检查
```

## 实测

| | |
|---|---|
| 解析速度 | 1–2 M 行/秒（`startswith(元组)` 单次 C 层分派） |
| vs cclib | **35–48x**，数值交叉校验一致 |
| 规则求值 | 0.16–0.5 ms / 份（39 条 + 28 条签名，表达式预编译） |
| 测试 | 102 个用例，含 11 份真实日志回归 + 变异测试 + 106 个带标签样本端到端 + 静默失效防护 |

## 规则库评估（可复跑）

```bash
python tools/eval_rules.py
```

当前结果：

| 层 | 召回 | 误报 |
|---|---|---|
| 签名层（27 条有正样本） | 27/27 = 100% | 0/38 负样本 |
| 规则层（39 条全部有正样本） | 41/41 = 100% | 9 份结果可用的日志上零 CRITICAL/MAJOR |
| 空规则（11 份真实日志全 UNKNOWN） | **0 条** | — |

## 误差分析：错在哪一类，该不该上模型（可复跑）

```bash
python tools/error_analysis.py
```

准确率回答不了「剩下那点错在哪」，而「要不要训 transformer」只能由后者回答。
工具把判错分成五类，**每一类对应完全不同的修法**：

| 类别 | 含义 | 修法 | 是模型的活吗 |
|---|---|---|---|
| `parser` | 日志里有，解析器没提取出来 | 补解析器 | ✗ |
| `decl` | `requires`/`applies` 用错信号 | 改 YAML 声明 | ✗ |
| `rule` | 字段都在，判据/阈值/定级错了 | 修判据 | ✗ |
| `coverage` | 没有规则盯着这种失败 | 加规则 | ✗ |
| `multifield` | 单字段全正常、合起来说不通 | 训练 | ✓ |

机器不允许自己把错误叫做「需要模型」——一旦允许，工程债就会被伪装成算法需求，
模型永远训不出来（它在学解析器和规则的漏）。所以 `multifield` 只能人工标注，
工具只输出**候选**。

当前结果（160 份样本 / 54 份真实日志）：

```
判错样本 1 / 160    → 规则没覆盖 1 条（fail_coord_missing，需输入文件交叉核对）
multifield 候选 1 条，复核结论：缺数据源，不是模型需求
```

标注集已经饱和，所以工具同时扫**没有标签**的 54 份真实日志做一致性检查，
三处问题都出在那里：动作通道 48% 全喊人工、11 条 `minor+human_review`
自相矛盾、92% 的 unknown 落在已判 FAIL 的日志上。详见
`field_test/REPORT.md` 第十一节。

## risk-coverage：什么时候可以不听它的

规则引擎的输出不是二元的，它还带一个 score。真实场景里 Agent 不会全信也不会全不信，
而是设一个接受阈值：分够高直接用，分低的转人工。阈值挪到哪里，代价是一条曲线。

横轴 = 被接受的计算占比（coverage），纵轴 = 被接受的那部分里**真正不可用**的占比（risk）。

```bash
python tools/risk_coverage.py     # 打印表格 + 写 docs/risk_coverage.svg / .csv
```

当前结果（98 个样本，可用 67 / 不可用 31）：

| 覆盖率 | 规则引擎 risk | 随机基线 |
|---|---|---|
| 30% | 3.4% | 31.6% |
| 50% | 2.0% | 31.6% |
| 70% | 10.3% | 31.6% |
| 80% | 14.1% | 31.6% |
| 90% | 23.9% | 31.6% |
| 100% | 31.6% | 31.6% |

AURC（曲线下面积，越低越好）**0.0781**，理想 AURC **0.0500**。
图 `docs/risk_coverage.svg`，数据 `docs/risk_coverage.csv`（零绘图依赖，SVG 手写）。

**当前唯一的漏放：`realpos:B-09`（score 85，判 WARNING）。** 这是那份真实跑出来的
`ts_no_imag.log`：route 声明 `opt=ts`，Gaussian 却收敛到极小点、频率全正。引擎按
「major 偏差」给了 85 分的 WARNING，而对用户来说这份计算毫无用处 —— 他要的是过渡态。
把 B-09 提升到 critical 会直接把 score 压到不通过区间，代价是更多「其实没错」的体系
被拦下复核。**这个定级等你们的判断，我先留在 major。**

**这条曲线能证明什么、不能证明什么。** 两点先说清楚，否则它会被当成"准确率 95%"那类数字来读：

1. **「不可用」不是我贴的标签，是规则作者声明的 `severity`。** 只有 critical 算不可用
   （崩溃、原子重叠、电子数矛盾）；major/minor 被接受是**正确**行为，不计入漏放。
   若按"构造上有没有缺陷"来标，曲线会先降后升、看起来毫无意义 —— 第一版就是这么错的。
   唯一例外是**已知不可用的真实日志**（目前只有 `ts_no_imag.log`）：它按内容哈希单独登记，
   不看 severity。因为它「没崩」这件事会骗过 severity，而它确实不能用。
2. 它测的是**决策层的排序能力**，不是检测能力（检测看上面那张召回表），
   也不能说明阈值对你们体系合理 —— 那是要拿真实失败 log 校的事。

一个副作用值得单说：跑这个脚本时发现 `SIG-DISK-2` 的扰动负样本被兄弟签名 `SIG-DISK-1`
接管、判了 FAIL —— 两个签名的文本区域有重叠。脚本没有静默剔除这类样本，而是单独列出来，
因为**签名库内部文本重叠本身就是个该知道的盲点**。

## 验证数据的来源与局限（重要，别跳过）

`tests/data/` 里 11 份是真日志（7 份来自 cclib 仓库、4 份自己在 cu04 上跑的：TS 落到极小点、H2 键长 5 Å、水二聚体、苯二聚体），覆盖闭壳层/开壳层/
过渡金属（Mo，Z=42）/大基组/频率计算。

**但它们全是正常结束的计算。** 公开数据集里几乎不存在失败的计算——
实测 cclib 仓库 `data/` 下 2046 份输出，文件名含 error/fail/trunc 的为 **0**；
VQM24 统计了 262,542 次失败、发布出去的 835,947 条全是成功的。

所以失败分支只能合成，方法有三条，各有各的边界：

| 来源 | 做法 | 能证明什么 | **不能**证明什么 |
|---|---|---|---|
| 签名正样本（27） | 把官方错误原文嫁接进真实日志，路径 `/disc30/g98/` → `/g16/` | 匹配器能在真实日志里找到官方文本，且不依赖官方路径 | 官方目录能覆盖所有真实崩溃 |
| 规则正样本（40） | 定点变异（改能量/坐标/原子序数/频率/route 基组…） | 阈值判据实现正确 | 阈值对你们的体系合理 |
| 负样本（38） | 11 份真实日志（其中 9 份结果可用）+ 27 份「官方原文扰动一个字符」 | 不会对正常计算误报，不会被相似文本骗 | 样本量太小，precision 置信区间很宽 |

一条仍然存在的硬缺口：**官方 27 条里约 20 条是「计算崩了」，只有 5 条是「算完但有问题」。**
崩溃 ≠ 静默错误。后者得靠 `catalog.yaml` 的数值规则和跨阶段检查兜，而那部分的阈值
只能用你们自己的失败 log 来校。

## 加规则时注意的坑（都是踩过的）

| 坑 | 症状 | 正确做法 |
|---|---|---|
| `eval` 的 `d` 放 locals | 生成器表达式里 `d` 不可见 → C-03 静默失效 | 变量全塞 `globals` |
| 坐标块 6 列取成 `parts[2:5]` | `AtomicType`（恒 0）当 X，Z 坐标丢失，所有原子压到 x=0 面 | 按列数分支：6 列取 `parts[3:6]`，5 列取 `parts[2:5]` |
| 多帧优化只重置 `orient_zs` | 末帧坐标 = 所有帧首尾相接，20 原子读出 120 组 → 最小间距 0 | `orient_xyz` 一起清空 |
| `"freq" in route.split()` | `freq=hpmodes` / `opt=ts` 认不出 → 相关规则 applies 全 na | 按 `=` `(` 切关键字再比 |
| 只收 `ERROR_MARK` 行 | 官方块里终止行之后的病因行（`Operation on file out of range`）丢失 | 终止行后再收 ≤8 行，遇正常输出前缀刹车 |
| `_CONVG = r"Convg\s*="` | G16 实机是 `Conv=`（无 g），7 份日志全抓不到 | `Conv(?:g)?` |
| `_OPT_ITEM` 用 `^\s+` | 传入的是 `strip()` 后的串，一条都匹配不上 | `^\s*` |
| 规则 `requires` 含空列表字段 | 空列表被当「缺数据」，正常日志判 UNKNOWN | 该字段从 requires 移出，交给 `when` 处理 |
| 宽松数字正则**部分匹配** | `Conv=0.1x9D-08` 被截成 `0.1` 入库 → A-06 拿 0.1 比 1e-8，判出假的「超 1e7 倍」CRITICAL | 先抓整个非空白 token 再 `fullmatch` 校验，不合文法一律算失败 |
| `requires` 拼错字段名 | `getattr` 永远得 `None` → 该规则**永久 UNKNOWN**，报告上看不出异常 | 加载期校验字段名，拼错直接抛 `RuleCatalogError` |
| `applies` 写错名字 | 运行期 `NameError` 被按 `na` 吞掉，而 `na` **不计入覆盖率分母** → 规则从所有指标里消失 | 同上：加载期静态校验表达式引用的名字 |
| `_STD_BASIS` 用 `[^(]+` | `Standard basis: 6-31G(d) (5D, 7F)` 被切成 `6-31G` —— 极化标记丢失。后果不是显示问题：报告在误报方法信息，C 类规则也失去判断依据 | 只剥结尾球谐标记：`(.+?)\s*(?:\(\s*\d+D\s*,\s*\d+F\s*\))?\s*$` |
| 负样本复用 `graft_signature` | 嫁接要把 `Normal termination` 整行替换成错误块，于是"负样本"自己变成异常终止日志，A-01 正确判 FAIL 却被记成误报（risk-coverage 起点 75%，比随机还差） | 负样本保留终止行，扰动文本插在它**之前** |
| 报告里硬编码条数 | 加了 C-07/C-08 后规则变 37 条，报告标题还印着 35，报告自己在误报规则库规模 | 条数一律从清单实测（`len(rules)`） |
| **派生量在某类任务上不存在却没写 `applies`** | `requires` 里放一个「只在部分任务里有定义」的派生量（G/ZPE、最小原子间距、片段数），于是缺这类量的算例全判 `unknown`（=缺数据）。**同一个模式犯了四次**：D-09 一次，B-05/B-10 并列两次，共 26+2 条假 unknown。单原子体系（单原子催化剂）因此被报「原子间距取不到」 | 加规则时逐条问一次：**这个量在哪些算例上根本不存在？** 存在就先想「该判 `na` 吗」——`na` 不进覆盖率分母，`unknown` 进。守卫生效范围还要想清边界：没有几何 ≠ 单原子，前者是「不知道有几个原子」，仍该 `unknown` |

## 出问题会不会吭声（可观测性）

这一层的设计原则是：**宁可报错，也不许"看起来正常"。** 已落地六处硬约束：

1. **解析侧不静默**。`CalcData.unparsed_hits` 记「日志里明明有、是我没取出来」，
   收尾时提升为 `parse_warnings`（面向用户）。和 `parse_warnings` 原本的语义
   （日志客观缺失）严格分开 —— 把解析器 bug 报成"数据缺失"是最坏的一种不可观测。
2. **规则清单加载期 fail fast**。`requires` 字段名、`when`/`applies` 引用的名字、
   `severity`/`action` 取值，任一不合法直接抛 `RuleCatalogError`，并把拼错的名字
   连"你是不是想写 `xxx`"一起打出来。CLI 用**独立退出码 3**，与「有 FAIL 返回 1」
   区分开 —— 配置错和被测体系不合格不是同一件事。
3. **`applies` 求值异常归 `unknown` 而不是 `na`**。`na` 不计入覆盖率分母，
   按 `na` 处理等于让这条规则从指标里蒸发。
4. **日志走 stderr，默认只报 WARNING**。stdout 是判定报告，Agent 会
   `judge x --json | jq .verdict`；日志混进 stdout 会让管道直接失败——这是
   可观测性最常见的自伤。默认安静是因为一个质检工具如果满屏，用户第一件事
   就是永久关掉日志，那等于没有日志。
5. **`trace_id` 同时出现在日志和报告里**。`provenance.trace_id` / `span_id` 和
   日志行 `[f3a9c21e/007]` 是同一个值，`grep f3a9c21e` 就能把一次运行的全部
   上下文捞出来；反过来看到一条 ERROR 也能定位到具体哪份报告的结论。
   span 用递增序号而不是 uuid：一眼看出「第 7 份」，跳号说明中间有文件没进到这里。
6. **批量汇总算的是「这一批到底怎么样」**，而不只是「每份怎么样」。跑 300 份时
   真正要看的是：多少份该转人工、哪条规则老在报 UNKNOWN（那几乎总是解析器的缺口）、
   哪条规则命中最多、耗时分布、以及**有几个文件直接没跑成**——后者不进 results，
   不单独统计就会静默消失。

```bash
$ judge a.out b.out --log-level INFO --log-json --log-file run.log --metrics-out m.json

 批量汇总   trace aa556a0c    2 个文件（成功 2，处理失败 0）
 结论   通过 0   有警告 1   不通过 1   判不了 0
 评分   均值 42.5   中位 85   最低 0
 耗时   wall 127.6 ms   解析合计 7.9 ms（均值 4.0 / p95 5.3 / 最慢 5.3）
 吞吐   2209 行   0.28 M 行/秒
 覆盖率 均值 92%   最低 84%
 命中   共 6 条（critical 3 / major 2 / minor 1 / info 0）   签名层 1 条
```

耗时给 **p95 不给平均值**：一份 300 MB 的日志能把均值彻底带偏。反过来，
样本少于 20 份时 nearest-rank 的 p95 就等于最大值——这不是 bug，但别拿小批量的
p95 当分位数解读（`tests/test_obs.py::test_p95_degrades_to_max_on_tiny_batches`
把这个行为钉住了）。

日志格式两种：`TextFormatter`（人读，`10:27:57.604 INFO [65ece2e6/001] engine.parse.done n_lines=1742 ms=3.9`）
和 `--log-json`（一行一条 JSONL，供 filebeat/loki 采集）。事件名用点号
（`engine.parse.done`）而不是整句 —— 它同时是检索键和 JSON 字段名，写成句子就没法聚合。

不引 `loguru` / `structlog`：本包依赖只有 `pyyaml`，装它的人机器上不一定有。
标准库 `logging` 的 Formatter/Handler 组合够用，而且**只挂自己的 handler、不动 root**
——调用方（FastAPI / Agent 后端）有自己的日志配置，从这里 `basicConfig()` 属于越界。

## 还没做

- ORCA / Q-Chem 解析器（分发入口已留）
- 阈值按 `family` 分档（现在全局一套；`family` 字段已产出，见 `engine.derive_family`）
- L3 模型层（`features.py` 空着）—— 前置条件是先跑通 GBC/TabPFN 基线。
  2026-11-19 的误差分析给出的结论是：**现在不该上**。三道闸门全未达标
  （独立真实样本 54 / 失败类 11 / 需多字段综合的错 0），且「模型要学什么」
  至今没有定义
- D-05 的容差目前硬编码 1 kcal/mol，未按体系档位区分
- 签名层只覆盖「有明确错误文本」的崩溃；纯数值崩坏（无错误行）只靠 SIG-NOTERM 兜底
- C 类（方法/基组化学合理性）目前只覆盖**基组**：极化函数、ECP、弥散函数、声明值 vs 实际值。
  泛函选择是否适配体系（比如开壳层过渡金属用纯 GGA）、色散校正该不该加，都还没有规则 ——
  这部分光看日志判不了，得结合体系性质，属于「需要人先定判据」的范围
- **阈值校准**（真正的瓶颈）：39 条规则里 13 条已在真实日志上校准过（v0.7），
  B-04（小虚频 -50 线）与 C-05（gap 0.5 eV）仍缺样本，见 `field_test/REPORT.md` 第十节
- **动作模型自相矛盾**：11 条 `minor` 规则配了 `action: human_review`，加上
  `ACTION_PRIORITY` 里 `rerun > escalate` 的顺序倒置，导致 54 份日志里 26 份
  （48%）被判「必须人工复核」。`action` 是流水线唯一的路由字段，一半文件都喊人
  就等于没有这个字段。改法会改变 26 份日志的动作，属于契约变更，尚未动
- **unknown 噪音**：92% 的 unknown 出现在已判 FAIL 的日志上（崩溃的日志里
  SCF 没跑、几何没打印，二三十条规则集体判不了）。报告上它们不携带信息，
  却把真正需要人看的埋了进去。修法是在聚合阶段认出「计算没跑完」并把下游标 `na`，
  需要给规则引擎加因果顺序
- **输入文件交叉核对**（`judge log --input in.gjf`）。现在只看输出日志，
  而有些错误在日志里**根本不留痕**：声明算水、坐标只给一行，Gaussian 就把 O 原子
  算完正常终止，判 PASS 100 分。要抓这类必须对比「输入声明了什么」和「实际算了什么」。
  单文件能力的天花板就在这里，细节见 `field_test/REPORT.md`

## 一个被我讲错过的样本（留着当反面教材）

`demo/wrong_basis.out` 里 route 写 `sto-3g`，而 `Standard basis` 打印 `6-31G(d)`。

我最初把这解释成「`geom=allcheck` + `guess=read` 让 Gaussian 从 chk 读回了旧基组」，
还把这个说法写进了 C-07 的修复建议里。**这是错的。**

2026-11-18 在 cu04 上用真实 Gaussian 16 Rev A.03 验过：同样带 `guess=read` 和
`geom=allcheck`，`Standard basis` 就是 route 里声明的 STO-3G，chk 并不会覆盖基组。

真正的情况更朴素：`demo/wrong_basis.out` 是 cclib 的 `dvb_sp.out` 的**副本**，
那一行 `Standard basis` 被人为改过（原版是 `STO-3G`）。也就是说它是个**构造样本**，
不是真实运行的产物 —— 我拿一份假日志当证据，给一条规则编了一个病因。

现在 C-07 保留，但含义变了：它检测的是「route 声明与实际执行不一致」这个**现象**，
常见成因是日志被拼接、被手工编辑、或混入了别的运行片段，而不是 allcheck。
两条教训：

1. 日志现象 ≠ 成因。没复现过的因果链，不该写成修复建议塞给用户。
2. 「真实日志」和「真的跑过的日志」是两回事。前者只保证格式真实。

同类但**是真事**的反例在 `field_test/` 里：`fail_coord_missing.log` 声明算水分子、
坐标却只剩一行，Gaussian 把 O 原子算完正常终止（`SCF Done -74.957`）。
这个到现在还判不出来 —— 单看日志判不了，见 `field_test/REPORT.md`。

