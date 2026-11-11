# 基准与指标（Benchmark）

这里是对 **DoVideoAI 解析链路**的可复现测量：跑批脚本、原始数据、指标定义与结论。

所有数据来自现有 HTTP 接口 / 日志 / 数据库，**没有修改产品代码**。

## 一页结论

见 [summary.md](summary.md)。核心数字（`deepseek-flash` + 百炼 ASR/Embedding，本机 4C16G 无 GPU）：

| 素材时长 | 样本 | P50 | P95 | Critic 通过率 | 平均 token (in/out) | 平均成本 |
| :--- | ---: | ---: | ---: | ---: | :--- | ---: |
| 35 s | 20 | 157 s | 205 s | 12/20 | 13,528 / 7,929 | $0.0068 |
| 5 min | 5 | 178 s | 样本不足 | 2/5 | 20,685 / 7,412 | $0.0076 |
| 15 min | 3 | 223 s | 样本不足 | 2/3 | 30,802 / 6,956 | $0.0088 |

三条主要发现：

1. **耗时几乎与视频长度无关**：27 倍时长只让 P50 从 157 s 涨到 223 s，瓶颈是固定的 4–6 次大模型调用。
2. **成本由固定开销主导**：`成本 ≈ $0.0067 + $0.000133 × 分钟`，35 秒素材的固定开销占比约 99%。
3. **并发重复提交不会重复烧钱**：同一素材+目标并发提交，只触发 1 次分析，token 单份。

## 指标定义

见 [metrics.md](metrics.md)：5 张指标卡，每张写明**取数位置、分母、判定口径、标注人、已知局限**，
并说明为什么「上传成功率」「分片重传次数」在本项目**不可测**（缺分母，需 access log 或埋点）。

## 目录结构

```
docs/benchmark/
├── summary.md            # 结论文档（基准表 / 成本模型 / 质量实测 / 局限）
├── metrics.md            # 指标卡
├── run_bench.py          # 跑批脚本：20 短 + 5 中 + 3 长，结果写 results/runs.jsonl
├── collect.py            # 汇总：算出 P50/P95、成本，输出 results/summary.md
├── test-quality.py       # 断点续传 / 并发 complete / 并发提交 三项实测
├── reset-media.sh        # 清空某素材的检查点，保证每次都是冷路径
└── results/
    ├── runs.jsonl        # 28 条原始记录
    ├── quality-tests.json# 三项质量实测结果
    └── summary.md        # 自动生成的统计表
```

## 复现步骤

前置：按仓库 README 把服务跑起来（中间件 + 后端 + 前端），并在 `.env` 里配置好可用的模型 Key。

```bash
# 0. 准备三档素材（可选，脚本也接受 MEDIA_DIR 指向你自己的素材）
#    short: 约 35 s 的中文讲解视频
#    mid  : 约 5 min
#    long : 约 15 min
export MEDIA_DIR=/path/to/testmedia
#    脚本默认读取 $MEDIA_DIR/lesson_speech.mp4、lesson_5min.mp4、lesson_15min.mp4

# 1. 跑批（约 2 小时；可用 BENCH_SHORT / BENCH_MID / BENCH_LONG 调样本量）
python docs/benchmark/run_bench.py

# 2. 汇总
python docs/benchmark/collect.py

# 3. 质量实测
python docs/benchmark/test-quality.py
```

可调环境变量：

| 变量 | 默认 | 说明 |
| :--- | :--- | :--- |
| `API_BASE` | `http://localhost:9090` | 后端地址 |
| `MEDIA_DIR` | `<repo>/testmedia` | 素材目录 |
| `BENCH_SHORT/MID/LONG` | `20/5/3` | 各档样本数 |
| `BENCH_USER` / `BENCH_PASSWORD` | `demo_user` / `Demo@12345` | 基准账号（需自己注册或改默认值） |
| `BASH_BIN` | `bash` | Windows 上通常要指向 Git Bash |
| `REPO_ROOT` | 自动推断 | 仓库根目录，用于找 `.env` |

`reset-media.sh` 与 `test-quality.py` 会从仓库根目录的 `.env` 读取 `MYSQL_ROOT_PASSWORD`，
不会硬编码任何凭据。

## 局限（读数字前请先看这段）

1. **素材是合成循环视频**：画面只有 4 张幻灯片循环，感知哈希去重后 **OCR 调用恒为 1 次**。
   真实视频（口播 + 板书 + 场景切换）的 OCR 调用会多 1–2 个数量级，**长视频成本会被本表低估**。
2. **token 是服务端按字符估算**，不是 tokenizer 真值，与账单存在偏差（中文通常高估）。
3. **中长档样本量不足**（5 / 3 个），P95 只对 35 s 档有效；这些是内部基线，不是 SLO。
4. **只覆盖单机单实例**，跨实例并发依赖分布式锁，未在集群验证。
5. 成本按 DeepSeek 公开价（off-peak：输入 $0.15/1M、输出 $0.60/1M）折算，价格变动后需重算。
