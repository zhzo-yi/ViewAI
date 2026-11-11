# DoVideoAI 最小指标体系（5 张卡）

> 原则：**每个指标都必须能用一个命令复现**。取不到分母的指标不进体系，而不是估一个数字。
> 全部数据来自现有接口/日志/数据库，不改产品代码。

---

## M1 解析耗时 P50 / P95

| 项 | 内容 |
| :--- | :--- |
| **定义** | 从提交分析任务（HTTP 202）到任务进入终态（COMPLETED / FAILED）的墙钟时间 |
| **取数位置** | 基准脚本 `docs/benchmark/run_bench.py` 记录的 `wallMs`；分阶段耗时取 `GET /analysis/agent-trace` 的 `stageDurationMs` |
| **分母** | **成功任务**（终态 COMPLETED）。失败任务单列失败率，不混入分位数 |
| **判定口径** | 取最近 N 次成功任务排序后取 P50 / P95；**样本 < 20 时只报 P50 与 max，并标注「样本不足」** |
| **谁标注** | 自动采集（脚本），我复核异常样本 |
| **已知局限** | 合成素材（循环拼接）场景变化少，OCR 帧数偏低，长视频的真实耗时会高于本表 |

**复现**：`python docs/benchmark/run_bench.py && python docs/benchmark/collect.py`

---

## M2 Critic 通过率

| 项 | 内容 |
| :--- | :--- |
| **定义** | 完成分析的任务中，首轮或次轮 Critic 校验通过的比例 |
| **取数位置** | `agent-trace` 的 `counters.criticPassed` / `counters.criticRounds`；数据库 `agent_checkpoints.stage` 的 `ANALYSIS_COMPLETED` vs `ANALYSIS_COMPLETED_WITH_WARNINGS` |
| **分母** | **进入 Critic 的任务数**（排除预算超限、上下文构建失败等 Critic 之前就终止的任务） |
| **判定口径** | `criticPassed ≥ 1` 记为通过；终态为 `WITH_WARNINGS` 记为「交付但未通过」，单独统计 |
| **谁标注** | Critic 自动判定 + **人工抽检 10 条**，核对「结论里的时间戳能否在素材中回溯」，记录人机一致率 |
| **已知局限** | Critic 本身是 LLM，存在系统性偏好（偏爱结构完整的答案），人工抽检是唯一校准手段 |

**复现**：`python docs/benchmark/collect.py` 的「Critic 通过率」列

---

## M3 单任务 AI 调用成本

| 项 | 内容 |
| :--- | :--- |
| **定义** | 单次分析消耗的 token 与折算金额，同时给出**每 10 分钟素材**的归一化成本 |
| **取数位置** | `agent-trace` 的 `counters.inputTokensEstimated` / `outputTokensEstimated` / `modelCalls` / `asrCalls` / `ocrCalls`；单价取 DeepSeek 公开价 |
| **分母** | 每任务；归一化分母为素材时长（分钟） |
| **判定口径** | 成本 = 输入 token × $0.15/1M + 输出 token × $0.60/1M（off-peak、cache-miss）；**token 是服务端按字符估算，不是账单真值**，偏差需用月度账单校准 |
| **谁标注** | 自动采集 + 我按月对账 |
| **已知局限** | 估算 token 与实际 tokenizer 有偏差（中文通常高估）；`agent-trace` 只存 Redis，TTL 7 天，跑完必须当天取数 |

**复现**：`python docs/benchmark/collect.py` 的成本列

---

## M4 断点续传正确性（一次性实测，不做成持续指标）

| 项 | 内容 |
| :--- | :--- |
| **定义** | 上传中断后，已传分片是否被正确保留、只补传缺失部分、合并结果与原文件一致 |
| **取数位置** | `docs/benchmark/results/quality-tests.json` 的 `resume` 段：`statusAfterInterrupt` / `md5Expected` / `md5Stored` |
| **分母** | 单次实测（10 分片，60% 处中断） |
| **判定口径** | ① `upload-status` 返回已传分片数 = 中断前上传数 ② 续传后分片数 = 总数 ③ `content_hash` = 原文件 MD5 ④ 同名媒体记录 = 1 |
| **谁标注** | 自动断言 |
| **已知局限** | 只验证服务端契约，未覆盖弱网重试策略本身（那属于客户端行为） |

**复现**：`python docs/benchmark/test-quality.py`

---

## M5 并发幂等（一次性实测，不做成持续指标）

| 项 | 内容 |
| :--- | :--- |
| **定义** | 同一素材 + 同一目标被并发提交两次时，是否只真正执行一次，**AI 成本是否只花一份** |
| **取数位置** | `quality-tests.json` 的 `concurrentAnalysis`：`startEventsDelta`（后端日志中该任务的 START 事件增量）、`modelCalls`、`tokensIn/Out` |
| **分母** | 单次实测（两个并发提交） |
| **判定口径** | START 事件只 +1、token 与单次基线同量级 → 幂等生效；若 +2 且 token 翻倍 → 重复烧钱，需修 |
| **谁标注** | 自动断言 |
| **已知局限** | 只测同进程瞬时并发；跨实例并发依赖 Redisson 分布式锁，未在集群环境验证 |

**复现**：`python docs/benchmark/test-quality.py`

---

## 为什么砍掉「上传成功率」和「分片重传次数」

| 指标 | 砍掉的硬理由 |
| :--- | :--- |
| 上传成功率 | 分母是「上传尝试次数」，而 `backend.log` 里**没有任何上传相关日志**（实测 grep 结果为 0 行）。要拿到必须开 Undertow access log 或加埋点——为了一个数字动基础设施，不划算 |
| 分片重传次数 | 同上缺分母；而且即便能数，也分不清「客户端重试」与「断点续传重叠」，数字出来无法解释 |

> 面试表述：**「这两个指标我测不了，因为分母不可得；能测的那三个我已经有数字了。」**
> 主动指出指标不可测，比硬凑一个假数字更可信。
