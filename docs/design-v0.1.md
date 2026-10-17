# Kanban 审计面板 · 设计文档

- 版本:v0.1(待用户审)
- 日期:2026-10-17
- 状态:设计,未开发
- 产出目录:`<repo>\`

---

## 0. 一句话

给 Hermes desktop 做一个**独立全页插件**,把看板里每张卡和每个专家会话的**质量 / 速度 / token 效率**算成可比较的指标,**按"哪里有问题"排序**呈现,能实时看、能看趋势,替代每天早上读一份静态报告。

---

## 1. 目标与非目标

### 目标
1. 回答三个问题:**谁干得对**(分配)、**干得成不成**(完成)、**干得值不值**(效率)
2. **问题优先**:第一屏是"要处理的异常",不是一堆中性数字
3. **实时**:打开面板即最新状态,会话在跑时能看到它正在烧的 token
4. **趋势**:指标落历史,能看周对周的变化,支撑"持续优化"
5. 同一个引擎同时供**面板**和**CLI/cron**,两者永不失同步

### 非目标(v0.1 不做)
- 不做自动修复(只报问题 + 给建议动作,改派/拆卡仍由人或 orchestrator 执行)
- 不审计 cron 定时任务(本版范围:kanban 任务 + 主会话)
- 不跨机器(只看本机三处库)

---

## 2. 已确认的技术事实

### 2.1 数据底座(全部实测通过)

| 库 | 表 | 关键列 | 提供 |
|---|---|---|---|
| `~/.hermes/kanban.db` | `tasks` | `id, assignee, status, workspace_path, created_at, started_at, completed_at, session_id, model_override, max_runtime_seconds, block_kind, consecutive_failures` | 卡元数据 |
| | `task_runs` | `id, task_id, profile, status, started_at, ended_at, outcome, metadata, worker_session_id(*)` | 每次执行的画像 |
| | `task_events` | `task_id, run_id, kind, payload, created_at` | 全轨迹(blocked/decomposed/timed_out/promoted…) |
| | `task_comments` | `task_id, author, body, created_at` | 交接叙述 |
| | `task_links` | `parent_id, child_id` | 依赖图 |
| `~/.hermes/state.db` | `sessions` | `id, profile_name, model, cwd, started_at, ended_at, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, reasoning_tokens, api_call_count, tool_call_count, estimated_cost_usd, actual_cost_usd, message_count, title` | 主会话 token/成本 |
| `~/.hermes/state.db` | `session_model_usage` | `session_id, model, task, api_call_count, *_tokens, estimated_cost_usd` | 按模型/按用途拆分的用量 |
| `~/.hermes/profiles/<专家>/state.db` | `sessions` | 同上 | **各专家**的 token/成本 |

(*) `task_runs.metadata.worker_session_id` 是 JSON 字段;真正的专家会话 id 在这里,**不是** `tasks.session_id`。

### 2.2 连接键(踩过的坑)

```
tasks.session_id             → dispatcher 自己的会话(所有卡同一个 id,**不可用**)
task_runs.metadata.worker_session_id → 该次执行的真实专家会话
                                        → profiles/<profile>/state.db : sessions.id
                                        → token / cost / tool_call
```

实测:41 条 run 中 36 条带 `worker_session_id`,**27/27 全部在对应专家库中解析成功,零缺失**。

### 2.3 插件机制(Hermes desktop SDK,读本机源码文档确认)

- **形态**:统一包(一个文件夹装齐),app 会把 `desktop/` 半自动同步到 `$HERMES_HOME/desktop-plugins/<id>/`
- **后端**:`dashboard/plugin_api.py`,导出 `router = APIRouter()`(FastAPI),挂载在 `/api/plugins/<id>/`;**跑在 gateway 进程内**,可直接 import hermes-agent 代码、直接读 SQLite
- **前端全页**:`ROUTES_AREA`(`data:{path}`)+ `SIDEBAR_NAV_AREA`(侧栏入口)
- **取数**:`ctx.rest('/path')`(带 `useQuery({refetchInterval})` 轮询)
- **实时推送**:后端 `broadcast_plugin_event('kanban-audit', '<事件>', payload)` → 前端 `host.onEvent('plugin.kanban-audit.<事件>')`
- **两个开关都要开**:`config.yaml` 的 `plugins.enabled`(放行 Python 后端)+ Capabilities → Plugins 的 desktop half 开关(默认 off)
- **硬约束**:
  - 前端 import 白名单**只有** `@hermes/plugin-sdk`、`react`、`react/jsx-runtime` → **图表必须手写 SVG/canvas**,不能引 recharts 等
  - 文件**不编译** → 用 `jsx()` 调用,不能写 JSX 语法
  - `ctx.socket` 在 OAuth remote 上是 no-op → **必须有轮询兜底**
  - 前端**不能硬编码颜色**,用 `var(--ui-*)` 主题变量(随主题自动换肤)

---

## 3. 数据模型与连接

```
                        ┌──────────────────┐
                        │  tasks (卡)       │
                        │  assignee/status │
                        └────────┬─────────┘
                                 │ 1:N
                        ┌────────▼─────────┐        metadata.worker_session_id
                        │  task_runs (每次执行)├───────────────┐
                        │  profile/outcome  │               │
                        └────────┬─────────┘               │
                                 │                         ▼
                        ┌────────▼─────────┐      ┌────────────────────┐
                        │ task_events      │      │ <profile>/state.db │
                        │ 阻塞/重跑/超时     │      │ sessions(tokens)   │
                        └──────────────────┘      └────────────────────┘

主会话侧:state.db : sessions(source='desktop') ── 无 task 关联,按会话聚合
```

**聚合单元**
- 卡级(card):一个 `task` + 其所有 runs
- 执行级(run):一次 `task_run` + 对应 session 的 token
- 专家级(agent):按 `task_runs.profile` 聚合
- 会话级(session):主会话,按 `sessions.id` 聚合

---

## 4. 指标算法

> 原则:**能精算的不估,该分层的绝不合成假分数。** 不同难度的卡之间不做综合分横比。

### 4.1 Token 效率(可直接精算)

记一次执行(或一个会话)的:`i`=input_tokens、`c`=cache_read_tokens、`w`=cache_write_tokens、`o`=output_tokens、`r`=reasoning_tokens、`a`=api_call_count、`t`=tool_call_count。

| 指标 | 公式 | 含义 | 判据 |
|---|---|---|---|
| 上下文复用度 | `c / (c + i)` | 缓存命中,越高越好 | **< min(该专家历史中位数, 92%) 告警**(用户定 92%,**按专家出基线**;见 §4.1.1 与 §4.4 的裁定) |
| 有效产出率 | `o / (i + c)` | 每读 1 token 写回多少 | 越低越浪费 |
| 每轮产出 | `o / a` | 单次 API 调用的净产出 | 显著低于同专家基线 = 空转 |
| 工具密度 | `t / a` | 每轮工具调用 | 过低 = 只思考不动手 |
| 思考占比 | `r / (o + r)` | 思考开销 | **不设全局固定值**,见 §4.1.2 |
| 单卡成本 | `Σ estimated_cost_usd` | 全 runs 求和 | > 近 30 天均值 2× 告警 |
| 墙钟 | `ended_at − started_at` | 实时延迟 | 报 p50/p95/p99,不看均值 |

**依据**:Glean(cache 命中阈值、cost-per-task);Algolia(p50/p95/p99、cost per task、error rate)。

#### 4.1.1 阈值 —— 上下文复用度(定 92%,按专家出基线)

用户定 **92%** 为**封顶告警线**。**必须知道的后果**:本机实测 36 条 run 的中位数正好 **92.3%**,若用全局 92% 会 **16/36 触发** —— 这不是判错,是"对缓存要求很高"。(该 92.3% 是 **run 口径**的历史测量,只用于说明 92% 的来历;**基线已不取 run 口径**,见下与 §11。)

但一个全局数字不合理(各专家常态天差地别),故**按专家出基线**。

**权威口径(唯一,2026-10-18 engine.py 定死)**:

- `baselines[profile]` = 该专家 `sessions` 表**全部有 token 的会话**(`i+c>0`)的 `c/(c+i)` 序列取 **中位数**;
- 告警线 = `min(baselines[profile], 92%)`(**92% 封顶**);
- `cache_reuse = c/(c+i) < 告警线` → 报 `token 异常`。

**各专家基线(2026-10-17 快照,与 §11 同一组数)**:

| 专家 | 历史中位数 | 告警线 min(中位数,92%) | 92% 封顶生效? |
|---|---|---|---|
| reverse | 97.93% | 92.00% | 是 |
| coder | 95.25% | 92.00% | 是 |
| voice-artist | 92.95% | 92.00% | 是 |
| videos-designer | 92.73% | 92.00% | 是 |
| document-handler | 91.88% | 91.88% | 否 |
| orchestrator | 91.15% | 91.15% | 否 |
| reviewer | 90.37% | 90.37% | 否 |
| image-designer | 89.67% | 89.67% | 否 |
| researcher | 88.03% | 88.03% | 否 |
| audio-engineer | 84.39% | 84.39% | 否 |
| translator | 82.38% | 82.38% | 否 |

> **表取数说明**:本表为文档快照;engine.py 会随新会话累积**实时复算**,数值会微动(如 coder 实测 95.41%、orchestrator 因 root 库/会话库口径差异实测 92.07%)。**下游实现一律读 engine 的 `agents[].cache_baseline_min`,不要照抄本表。**

> **作废(2026-10-18,orchestrator 裁定)**:本节原先那张「按专家的实测**均复用度**」小表(coder 95.7 / translator 85.7 / image-designer 95.1 / audio-engineer 90.3 …)**是早期草稿,已失真,不再被任何一节引用**。独立复算(读各专家 `profiles/<p>/state.db` 的 `sessions` 全部 `i+c>0` 会话)结果:
> - **均值**口径与那张表**全部不吻合**(translator 71.71 对 85.7、coder 87.78 对 95.7、image-designer 74.84 对 95.1 …);
> - **中位数**口径 **9/11 与 §11 公布值精确吻合**。
>
> 故基线一律取 **中位数**,均值口径废弃。以本节 + §11 为准。

> **裁定(2026-10-17,orchestrator,压过 §4.4 的旧写法)**:本订单**唯一权威口径** = `cache_reuse = c/(c+i) < min(该专家历史中位数, 92%)`。
> §4.4 异常表里"cache 命中 < 85%"是早期草稿,已作废。实测差异是**实质性的**:同一批数据下 §4.1.1 口径命中 **11 张卡**,旧 85% 口径只命中 **4 张**。以本节为准。

> **样本范围(2026-10-18)**:基线取 `sessions` **全量**会话(全部 `i+c>0`)而非 run 口径 —— 样本更多、更稳,代表该专家的常态水平;代价是混入非任务会话(自评/探索)时会被拉偏,后续应排除空会话与异常会话。

#### 4.1.2 阈值 —— 思考占比(研究结论:不能设固定值)

**结论:思考占比没有"正确数字"。最优思考量取决于任务难度,不是一个比例。**

依据:Meta FAIR《OptimalThinkingBench》(ICLR 2026,评 33 个模型)——"没有任何模型能做到最优思考";thinking 模型"在最简单的问题上也要多想几百 token 而不提升"。它的判据不是思考占比,而是 **OAA(某思考预算下的准确率)及其曲线下面积**,即**思考量要匹配题目难度**。TALE(token-budget-aware,Redis 2026)补充:给简单题套硬预算,准确率反升(GSM8K 81.35%→84.46%),输出 token 降 67%。

本机做法分三层,**都不使用"比例阈值"**:

**① 粗筛带**(只看极端,几乎不误报):实测 36 条思考占比全部落在 **20%–39%**(均值 28.8%、p90 36%),每轮思考 token 中位 **331**。故:< **15%** 可能思考不足,> **45%** 值得看一眼。

**② 主判据 = 2×2**(成功与否 × 思考多少):

| | 思考少 | 思考多 |
|---|---|---|
| **卡成功** | 高效(好) | 难任务,值(正常) |
| **卡失败** | 思考不足 or 需求不清 | **白想 → 报** |

→ v0.1 只在**"失败 且 思考多"**时报异常——这才是真问题(烧了算力没产出)。

**③ 基线**:每专家自己的历史中位数,偏离 > 25% 才报。

**④ 口径**:思考 token 按 output 计费(贵的那档,Redis/OpenAI 文档均确认),"白想"是真金白银的浪费。

### 4.2 质量(分四层,不与速度混算)

| 层 | 指标 | 公式 / 来源 | 说明 |
|---|---|---|---|
| 结果 | 一次通过率 | `单 run 即 completed 的卡 / 总卡` | 核心 |
| | 重跑率 | `runs ≥ 2 的卡 / 总卡` | 返工 |
| | 阻塞率 / 超时率 | `blocked / timed_out 的 run / 总 run` | 卡点 |
| 过程 | milestone 完成率 | metadata 里验收项(V1..Vn)通过比例 | 部分给分 |
| | 工具选择/参数准确率 | 需基准(见 4.3);v0.1 先用"末端验收项"代理 | 待深化 |
| 独立验证 | 过审率 | 有 reviewer approved 的卡 / 送审卡 | 质量闸 |
| | 打回次数 | `changes_requested` 事件数 | 返工 |
| 归因 | 失败定位 | 见 4.5 | "哪一步坏的" |

**依据**:Samira Ghodratnama《Evaluating LLM-based Agents》(milestone KPI、tool-selection/parameter accuracy、action-advancement);arXiv 2505.00212《Which Agent Causes Task Failures and When?》(自动失败归因)。

**为什么不合成一个"质量分"**:卡之间难度不可比(改 3 行代码 vs 逆向协议)。综合分会把"难卡多花时间"误判成"专家差"。正确做法:分层报 + **只在同类同难度卡内横向比**;类别由 4.3 的基准表提供。

### 4.3 分配合理性(需要一张基准表)

一张人工维护的 **领域 → 对口专家** 映射(存 skill 或插件配置),例如:

| 卡的关键词/标签 | 对口专家 |
|---|---|
| 逆向 / 二进制 / 协议 | `reverse` |
| 出图 / 封面 / 贴图 | `image-designer` |
| 代码落地 / 脚本 / 测试 | `coder` |
| 调研 / 比价 / 选型 | `researcher` |

基于它算:

| 指标 | 公式 |
|---|---|
| 对口率 | `派给对口专家的卡 / 总卡` |
| 硬错配数 | `assignee ≠ 实际执行 profile`(如 t_b08e72f3) |
| 跨区率 | `assignee` 与 `workspace_path` 归属专家不一致 |
| 越级率 | 该由 orchestrator 拆的卡被直接派给单个专家 |

**修正(2026-10-17 实测)**:用关键词自动判"对口"**不 work** —— 拿基准表跑现有 50 张卡,**43 张被判"复合"**(一张卡几乎总能命中多个领域词,如"翻译测试:译这句并存入文件"同时命中 translator 和 coder)。所以"对口率"**不靠关键词自动判**,改用**多信号投票**:

| 信号 | 可靠度 |
|---|---|
| 卡被改派(同卡不同 run 的 profile 不同) | 高 |
| 跨区(workspace 归属 ≠ assignee) | 高 |
| 专家自评说"本职拿不到" | 高 |
| orchestrator 拆卡时标注的对口 | 中 |
| 关键词匹配(上面那张基准表) | **仅辅助**,不单独定案 |

基准表保留,但只作**给人看的提示**,不自动下结论。

### 4.4 异常检测与排序(面板的核心)

每条异常带 `severity`(0–100)与 `类型`,面板按其降序排 `severity`:

| 类型 | 触发条件 | 严重度权重 |
|---|---|---|
| 分配错配 | assignee ≠ 实跑 / 跨区 | 高 |
| 反复失败 | 同卡 runs ≥ 3 | 高 |
| 流程阻塞 | 卡 blocked 且未解决 | 高(带 block_kind) |
| token 异常 | cache 复用度 `c/(c+i)` < `min(该专家历史中位数, 92%)`(§4.1.1,权威)或 空转 | 中 |
| 耗时异常 | 墙钟 > 该专家 p95 | 中 |
| 成本异常 | 单卡成本 > 30 天均值 2× | 中 |
| 历史退化 | 某专家某项指标较上期恶化 > 20% | 低(趋势信号) |

严重度不是随便加权:它 = **可解释的规则命中**,点开每条都能看到"因为什么触发"(遵守"不拍脑袋"原则)。

### 4.5 失败归因

对失败/超时/重跑的卡,自动拼出一条**时间线**:

```
建卡 → 被谁 claim → 首轮跑了多久 → 卡在哪(最后一条 event) →
有无 blocked(什么 kind)→ 重跑几次 → 最终 outcome
```

并给一句机器判定:归到 `分配问题 / 需求不清(需求书缺项)/ 上下文膨胀 / 模型能力 / 外部依赖`。规则初版:

| 判定 | 依据 |
|---|---|
| 分配问题 | assignee 与实跑不一致,或该卡领域与专家不符 |
| 需求不清 | 卡内出现"需求书缺 X"类 comment,或 blocked kind=needs_input |
| 上下文膨胀 | cache_read 随轮次单调暴涨且 output 不增 |
| 外部依赖 | blocked kind=dependency/capability,或 timed_out |
| 模型能力 | 以上都不命中且反复重跑 |

### 4.6 编制审计(现有专家够不够)

**回答:有没有任务,现有 12 个专家都不对口——意味着缺一个新专家?**

| 信号 | 来源 | 含义 |
|---|---|---|
| 卡判「未知」 | 基准表零命中 | 现有领域表没覆盖 |
| 卡被跨专家改派 ≥ 2 次 | `task_runs.profile` 变化 | 谁都不对口 |
| blocked kind=capability 且原因="无对口专家" | `task_events` | 明确缺人 |
| 自评出现"本职能拿不到" | 自评 summary | 专家自认能力外 |

**输出**:缺专家候选——把「未知」卡按领域词聚类,**出现 ≥3 次且横跨多天**的领域 = 候选新专家,附证据卡列表。

### 4.7 能力审计(工具 / CLI / MCP / 技能 够不够)

**回答:专家干活时发现工具不合适、缺一个 CLI 或 MCP——要能被审计出来。**

三层数据源:

**① 专家自评**(已有机制,实测 11 张卡全部产出了结构化缺口清单——见 §11)

**② 假能力检测(最有价值,别处看不到)** —— "配了但实际不能用":

| 类型 | 实测例子 |
|---|---|
| MCP 连不上/被拒 | `videos-designer` 的 davinci-resolve:Resolve 免费版 → `SCRIPTING_UNAVAILABLE`,41 个工具全是摆设 |
| 技能挂了但环境缺 | `document-handler`:pdf/docx/xlsx/pptx/ovis-ocr 五个技能全部因"本机无 Python 文档环境"跑不起来 |
| 工具声明是空的 | `image-designer`:config 列了 image_gen 但无实体工具 |
| 有工具但缺配套 | `voice-artist`:无 STT/无音频分析 → 铁律"合成完自己听"执行不了 |

→ 检测法:**对该专家的每个声明能力做一次最小调用探针**,失败或返回"不可用"即标记。这是"审计才能发现"的隐性问题,也是本插件最大的增量价值。

**③ 运行时冒泡**:comment/metadata 出现"需要安装 / 缺 X / 没有 X";block kind=capability;同一工具反复失败。

**输出**:缺口清单,每条带——缺什么 / 谁受影响 / 影响多少卡 / 建议动作(`uv tool install X` · 装 MCP · 补技能)/ 优先级。

---

## 5. 面板设计(独立全页)

侧栏入口 **Audit**;打开是全页,**三个视图**用 Tab 切(问题 / 专家画像 / 缺口);**趋势视图留 v0.2**。

```
┌ Audit ─────────────────────────────────────────────────────┐
│ [ 问题 ]  [ 专家画像 ]  [ 趋势 ]            ⏱ 实时  ⟳ 5s   │
├─────────────────────────────────────────────────────────────┤
│ ⚠ 分配错配 (2)                                              │
│    t_b08e72f3  应为 coder,实为 default        [展开]        │
│ ⚠ 反复失败 (2)                                              │
│    t_61f3ea7a  重跑 3 次                      [展开]        │
│ ⚠ token 异常 (11)                                           │
│    t_d0f4c3e3  cache 命中 74% / api 4 次      [展开]        │
│ ─────────────────────────────────────────────────────────── │
│ 运行 3 · 待办 4 · 完成 36 · 阻塞 0                          │
└─────────────────────────────────────────────────────────────┘
```
> 上框为**示意草图**,数字随真实数据变。原稿写的 `t_937f105b cache 命中 41%` 是**示例数字**,与本机任何口径都对不上
> (真实 `c/(c+i)` = 98.06%);真实命中"cache 偏低"的样例改为 `t_d0f4c3e3`(translator,74.24% < 其基线 82.38%)。
> 裁定见 §4.1.1。

**问题视图**(默认):上面的异常清单,点开进"卡详情"——该卡时间线 + token/耗时逐轮曲线(SVG)+ 建议动作(改派 / 拆卡 / 收上下文)。
**专家画像视图**:每专家一行:卡数、完成率、均耗时、cache 命中、每轮产出、成本;可排序;点开看该专家的趋势。
**趋势视图**:折线/柱状(SVG 手绘):完成率、重跑率、token 效率、成本 的周对周。

图表全部用**内联 SVG**(受 import 白名单限制),颜色取 `var(--ui-*)`。

---

## 6. 架构与数据流

### 6.1 文件树(统一包)

```
~/.hermes/plugins/kanban-audit/
├── plugin.yaml                 # agent half(最小;可挂一条 skill 说明面板存在)
├── dashboard/
│   ├── manifest.json           # {"name":"kanban-audit","api":"plugin_api.py"}
│   └── plugin_api.py           # 指标引擎 + FastAPI 路由
└── desktop/
    └── plugin.js               # 全页 UI(jsx 调用,无 JSX 语法)
```

另出一份**同引擎 CLI**:`E:\hermes\scripts\kanban_audit.py`,复用 `plugin_api.py` 里的纯计算函数(把引擎抽成 importable 模块 `engine.py`,后端和 CLI 都 import 它)。

### 6.2 数据流

```
        ┌─────────────┐  read   ┌────────────────────────────┐
        │ kanban.db   │◄────────┤                            │
        └─────────────┘         │  plugin_api.py / engine.py │
        ┌─────────────┐  read   │  (跑在 gateway 进程内)       │
        │ */state.db  │◄────────┤                            │
        └─────────────┘         └───────┬──────────┬─────────┘
                                        │ REST     │ broadcast_plugin_event
                                        ▼          ▼
                                 ctx.rest('/…')  host.onEvent('plugin.kanban-audit.*')
                                        └────┬─────┘
                                             ▼
                                     desktop/plugin.js 全页
```

### 6.3 实时策略

| 级别 | 手段 | 用于 |
|---|---|---|
| 兜底 | `useQuery({refetchInterval: 5000})` | 始终开着,任何环境都能用 |
| 加速 | 后端在**任务状态变化**时 `broadcast_plugin_event` | 有 desktop 客户端连着的本机 |
| 会话级 | `host.state.focusedUsage` | 当前会话实时 token(无需后端) |

> 不追求毫秒级。SQLite 读很便宜,5 秒轮询足够"实时";事件推送只是锦上添花,因为 `ctx.socket` 在 remote 上是 no-op。

### 6.4 指标落历史

后端每次算完,把**聚合后的指标快照**写一行进插件自己的库(`plugin_db()`,如 `metrics_history` 表:日期 + 专家 + 指标)。趋势视图读它。不存原始明细(那两份源库已有)。

---

## 7. 交付物清单

| # | 交付物 | 说明 |
|---|---|---|
| 1 | `engine.py` | 纯计算:读两库、算 §4 全部指标、输出结构化 JSON |
| 2 | `plugin_api.py` | FastAPI 路由 + 事件推送,import engine |
| 3 | `plugin.js` | 全页 UI(三视图 + SVG 图) |
| 4 | `manifest.json` / `plugin.yaml` | 清单 |
| 5 | `context_map.yaml` | 领域→专家 基准表(§4.3,先给草稿给她审) |
| 6 | `kanban_audit.py` | CLI,复用 engine(无 UI 场景 / cron) |
| 7 | `README.md` | 安装 + 两个开关怎么开 + 排障 |

---

## 8. 分期计划

| 期 | 内容 | 验收 |
|---|---|---|
| **v0.1** | engine + CLI + 全页三视图(问题/专家画像/缺口,纯列表,SVG 先只做迷你条形) | CLI 跑出正确指标;面板打开显示异常清单;两库零缺失 |
| **v0.2** | 趋势视图 + 历史落库 + 事件推送加速 | 周对比图有数据;推送生效 |
| **v0.3** | 对口率(接基准表)+ 失败归因规则完善 + 上下文膨胀曲线 | 归因准确率抽样人工核对 |
| **v1.0** | 阈值自校准(用历史分布定基线,不写死)+ 报告导出 | — |

---

## 9. 风险 / 未知 / 待拍板

### 风险
1. **`worker_session_id` 缺失**:历史数据里有 5/41 条没这个字段(老版本建的)。→ 缺的回退到"只有耗时,无 token",面板标注"数据不全"而非报 0。
2. **后端跑在 gateway 进程**:它读其他 profile 的 `state.db` 靠文件路径,需要 gateway 进程有读权限(本机同用户,OK)。远程 backend 场景不支持读远端专家库。
3. **图表手写 SVG**:工作量比引库大,但这是 SDK 硬约束,只能接受。
4. **`reasoning_tokens` 口径**:不同 provider 上报方式不同(有的把思考算进 output)。→ 面板注明口径,跨模型比较时打星号。

### 待拍板
1. `context_map.yaml`(§4.3 基准表)——我起草,你审。
2. 阈值:上下文复用度 **< min(该专家历史中位数, 92%) 告警**(已定,口径见 §4.1.1 裁定,后果见 §4.1.1);思考占比**不设固定值**,走 2×2 判据(见 §4.1.2);重跑 ≥ 3 次。
3. 面板**只读**已定(v0.1 不做一键改派)。
4. 历史快照保留 **60 天** 已定。

---

## 10. 依据来源(全部读过正文)

1. Hermes 官方《Desktop Plugin SDK》(本机源码 `website/docs/developer-guide/desktop-plugin-sdk.md`,2013 行,通读关键段:SDK 契约、pane/page、backend、事件推送、安全模型)
2. Hermes 官方《Build a Hermes Plugin》(插件总览与接口地图)
3. Glean《Key metrics for evaluating token efficiency in AI systems》(cache 命中阈值、cost-per-task、trajectory efficiency)
4. Samira Ghodratnama《Evaluating LLM-based Agents: Metrics, Benchmarks, and Best Practices》(milestone KPI、工具准确率、多智能体协调效率)
5. Algolia《AI agent evaluation: frameworks, metrics & testing strategies》(p50/p95/p99、cost per task、error rate、五大降本杠杆)
6. arXiv 2505.00212《Which Agent Causes Task Failures and When?》(自动失败归因)
7. Snowflake《AI Agent Evaluation: Metrics and Methods》(运营指标:latency/cost/token/tool-call/retry/timeout)
8. Meta FAIR《OptimalThinkingBench: Evaluating Over and Underthinking in LLMs》(ICLR 2026)—— OAA / AUCOAA 指标、33 模型的过思考实测
9. 《When More Thinking Hurts: Overthinking in LLM Test-Time Compute Scaling》(arXiv 2604.10739)—— 边际收益曲线,最优预算随难度从 1K 到 7.5K
10. Redis《Token-budget-aware LLM reasoning》(2026-07)—— TALE 硬预算实测、reasoning token 按 output 计费

---

## 11. 变更记录

### 2026-10-17 · cache 口径裁定(orchestrator,源自 t_dd4e00f0)

**背景**:卡 `t_42297dbd`(engine)的验收样例把 `t_937f105b` 当"cache 偏低",实测该 run `c/(c+i)` = **98.06%**,高于 coder 告警线 92%,任何 v0.1 规则下都不产生 issue;而 §4.4 又写"cache 命中 < 85%",与 §4.1.1 的"按专家基线"冲突。同一错误前提写在 `t_56e1a3fc`(CLI)、`t_ba48bda1`(后端)的验收里。

**裁定**:
1. **口径唯一化** = §4.1.1:`cache_reuse`(`c/(c+i)`) < `min(该专家历史中位数, 92%)`。§4.4 表中"cache 命中 < 85%"作废。
2. **样例卡替换**:`t_937f105b` → **`t_d0f4c3e3`**(translator,实测 74.24%,基线 82.38%)。
3. **视图数唯一化**:v0.1 = 三视图(问题 / 专家画像 / 缺口);趋势视图 v0.2。(原 §5 写"四个视图"、§8 v0.1 写"两视图",§5 草图 3 个 tab —— 以本条为准。)

**只读实测证据**(sqlite `mode=ro`,2026-10-17):

| 卡 | 专家 | input | cache_read | `c/(c+i)` | 该专家基线 | 命中? |
|---|---|---|---|---|---|---|
| t_937f105b | coder | 121756 | 6157184 | 98.06% | 92.00% | 否 |
| t_d0f4c3e3 | translator | 21766 | 62720 | **74.24%** | 82.38% | 是 |
| t_1d54dded | researcher | 25925 | 100352 | 79.47% | 88.03% | 是 |
| t_606167be | videos-designer | 125605 | 607232 | 82.86% | 92.00% | 是 |

按 §4.1.1 口径共 **11 张卡**命中 token 异常;按旧 85% 口径只有 4 张 —— 差异实质,故必须定死。
各专家历史中位数(实测):coder 95.25%、reverse 97.93%、videos-designer 92.73%、voice-artist 92.95%、document-handler 91.88%、orchestrator 91.15%(注:root 库与会话库口径差异)、reviewer 90.37%、image-designer 89.67%、researcher 88.03%、audio-engineer 84.39%、translator 82.38%。

### 2026-10-18 · §4.1.1「均复用度」小表作废 · 基线口径唯一化(orchestrator,源自 t_e59b90ec)

**背景**:`t_42297dbd`(engine)验收时发现,设计文档里「专家基线」有**两处口径不自洽**:§4.1.1 有一张「按专家的实测**均复用度**」小表(coder 95.7 / translator 85.7 / image-designer 95.1 / audio-engineer 90.3 …),而 §4.1.1 裁定框与 §11 给的基线是**中位数**。engine 已按中位数口径落地(`cache_baseline_min == min(cache_reuse, 0.92)`),但那张均值小表仍会误导下游(CLI `t_56e1a3fc` / 后端 `t_ba48bda1` / 前端 `t_7b7218ca`)。

**独立复算(sqlite `mode=ro`,读各专家 `profiles/<p>/state.db` 的 `sessions` 全部 `i+c>0` 会话)**:
- **中位数**口径:9/11 与 §11 公布值**精确吻合**;
- **均值**口径:与 §4.1.1 那张小表**全部不吻合**(translator 71.71 对 85.7、coder 87.78 对 95.7、image-designer 74.84 对 95.1 …)。

**裁定**:
1. §4.1.1 的「均复用度」小表**作废**(已在原位标注),不再被任何一节引用;基线口径唯一化到「**该专家全部有 token 会话的 `c/(c+i)` 中位数,再用 92% 封顶**(`min(中位数, 92%)`)」。
2. §4.1.1 补出权威的**按专家基线的中位数表**(11 专家,含 92% 封顶是否生效)。
3. `token 异常` 判据不变 = `c/(c+i) < min(该专家历史中位数, 92%)`;命中 **11 张卡**。
4. 下游三卡统一读 engine 输出的 `agents[].cache_baseline_min`,**不得自行按均值口径复算或展示另一套数字**。
5. 明确:**基线取中位数,均值口径废弃**。

