# kanban-audit v0.1 — 安装与使用

一句话:**只读**统计 Hermes kanban 的卡流/专家产能/能力缺口,给出问题清单与处置建议,以「桌面侧边栏 Audit 面板(三视图)」+「CLI」两种形态交付。

本 README 里每条命令都已由 coder 在本机实跑过(证据见文末「附:实测证据」);唯一没跑的是「用户点 GUI」那一步(按约定留给用户)。

---

## 1. 这套东西是什么 / 文件树

审计对象是 `$HERMES_HOME\kanban.db`(卡片、run、评论)+ 各 profile 的会话库(`profiles\<专家>\state.db`,token 用量)。全部以 SQLite `mode=ro` 打开,**只读不写**。

```
<repo>\
├─ engine.py                 纯计算引擎:只读开库 → 算指标 → 输出结构化 JSON(唯一判定逻辑所在地)
├─ kanban_audit.py           CLI:纯包装 engine,输出 JSON / 人读表格(不重算、不连库)
├─ plugin.yaml               插件清单:name / version / description
├─ context_map.yaml          领域 → 对口专家基准表(判「分配合理性」用;草稿 v0.1,待用户审核)
├─ dashboard\
│   ├─ manifest.json         桌面后端清单:{"name": "kanban-audit", "api": "plugin_api.py"}
│   └─ plugin_api.py         FastAPI 只读路由(6 个端点),import engine 后逐字段透传
├─ desktop\
│   └─ plugin.js             桌面插件:侧边栏 /audit 页面 + 三个 Tab + 5s 轮询兜底
├─ 设计文档-v0.1.md          设计与口径(§4.1.1 复用度基线、§11 基准公布值)
└─ 需求书-*.md / 验收-*.md   各卡的交接与验收记录
```

安装后的落位(`engine.py` 必须落在插件根,`plugin_api.py` 靠 `../engine.py` 找到它):

```
$HERMES_HOME\plugins\kanban-audit\
├─ engine.py
├─ plugin.yaml
├─ dashboard\{manifest.json, plugin_api.py}
└─ desktop\plugin.js
```

后端 6 个端点(全部挂在 `/api/plugins/kanban-audit/` 下):

| 端点 | 返回 |
|---|---|
| `GET /health` | 会话解析率概览:`runs_total / runs_with_session / session_resolution_rate / missing[]` |
| `GET /summary` | 卡状态计数 + 各问题类型的条数 |
| `GET /issues` | 问题清单(按 severity 降序),支持 `?fresh=1` |
| `GET /agents` | 12 个专家画像:卡数/完成率/耗时 p50·p95/复用度/每轮产出/成本 |
| `GET /gaps` | 能力缺口清单(谁受影响、影响卡数、建议动作、优先级) |
| `GET /card/{task_id}` | 单卡:task + timeline + runs + attribution + issues(未知卡 → 404) |

skill 面:桌面端 6 个端点共享 30s 内存缓存,`?fresh=1` 绕过;engine 抛异常时统一 `503 + {"ok": false, "error": "<Type>: <msg>"}`,不漏 traceback。

---

## 2. CLI 用法

```bash
cd <repo>

py kanban_audit.py                      # 默认:人读摘要
py kanban_audit.py --json               # 结构化 JSON(与 engine.compute_all() 逐字段相同)
py kanban_audit.py --view issues        # 问题清单表
py kanban_audit.py --view agents        # 专家画像表
py kanban_audit.py --view gaps          # 缺口清单表
py kanban_audit.py --selftest           # 自检:会话解析率 / 已知样本命中 / 纯函数样例
```

- 六种调用**退出码都是 0**;参数写错(如 `--view xyz`)退出码 2。
- `--selftest` 末行会打 `SELFTEST PASS`。
- 跑之前/之后 `kanban.db`、`profiles\*\state.db` 的 mtime 不变(只读)。
- 需要 Windows 的 `py` 启动器(本机无系统 python;`kanban_audit.py` 首行**不能**有 shebang,否则 `py` 启动器 103 退出)。

### 怎么挂 cron

无 agent 直跑(推荐,零 token 消耗):

```bash
# 1) 把两个文件一起复制到 scripts 目录(CLI 需要与 engine.py 同目录)
copy <repo>\kanban_audit.py %LOCALAPPDATA%\hermes\scripts\
copy <repo>\engine.py        %LOCALAPPDATA%\hermes\scripts\

# 2) 建 job:每天 09:00 直跑 script,输出直接投递(local = 只落 run 记录,不打扰)
hermes cron create "0 9 * * *" --no-agent --name kanban-audit-daily \
    --script kanban_audit.py --deliver local

hermes cron list          # 确认在列
hermes cron status        # 确认 scheduler 在跑(需要 gateway 服务该 profile)
hermes cron run kanban-audit-daily   # 立刻试跑一次
```

- `--no-agent` = 不走 LLM,script 的 stdout 即产出;**stdout 为空 = 静默**。
- 想让告警进 Telegram/飞书,把 `--deliver local` 换成 `--deliver telegram`。
- 更省事的替代:不复制文件,用 agent 模式 —— `hermes cron create "0 9 * * *" "跑 py <repo>\kanban_audit.py --view issues,把新出现的问题报给我"`(会消耗 token)。
- 注意:`hermes cron create` 与 `--script` 限定脚本放 `~/.hermes/scripts/` 下;越权外部路径不会被采用。

---

## 3. 两个开关(重点:**只开一个不够,两个都要**)

| # | 开关 | 在哪开 | 不开的后果 |
|---|---|---|---|
| 1 | **后端开关**(agent half) | `$HERMES_HOME\config.yaml` 的 `plugins.enabled` 里加 `kanban-audit` | 后端**根本不 import** `dashboard/plugin_api.py`;接口 404 `{"detail":"Plugin not found"}` |
| 2 | **desktop half 开关** | 桌面 app → **Capabilities → Plugins**,找到 `kanban-audit`,把 **desktop half** 打开(默认 **off**) | 后端有数据,但侧边栏不出 Audit 页;或页面 JS 不加载 |

第 1 个开关长这样(改前先备份 `config.yaml`):

```yaml
plugins:
  enabled:
    - kanban-audit        # ← 新增这一行
    - disk-cleanup
    - hermes-lcm-x
    - hindsight
    - security-guidance
    - time-gap
  disabled: []
```

第 2 个开关怎么点:
1. 打开桌面 app → 左侧 **Capabilities**(能力);
2. 切到 **Plugins** 标签;
3. 列表里找 `kanban-audit`,你会看到同一行有两个半区:agent half(后端)/ desktop half(桌面);
4. 把 **desktop half** 打开(agent half 会在你改了 `config.yaml` 后显示为 enabled);
5. 按 `Ctrl/Cmd+K` → **Reload desktop plugins**(或重开 app),侧边栏出现 **Audit**;
6. 点 Audit(路径 **`/audit`**),应看到三个 Tab:**问题 / 专家画像 / 缺口**。

> 为什么是两个开关:后端开关决定「Python 路由是否被 import」,desktop half 决定「Electron 侧是否加载 `desktop/plugin.js`」——两套进程、两套加载器,互不代偿。

---

## 4. 安装步骤(逐条照做即可)

```bash
# 0) 备份配置(唯一允许的写操作就是下一步改这个文件)
copy $HERMES_HOME\config.yaml $HERMES_HOME\config.yaml.bak

# 1) 复制插件文件到插件目录(engine.py 落插件根)
mkdir $HERMES_HOME\plugins\kanban-audit\dashboard
mkdir $HERMES_HOME\plugins\kanban-audit\desktop
copy <repo>\engine.py                  $HERMES_HOME\plugins\kanban-audit\
copy <repo>\plugin.yaml                $HERMES_HOME\plugins\kanban-audit\
copy <repo>\dashboard\plugin_api.py    $HERMES_HOME\plugins\kanban-audit\dashboard\
copy <repo>\dashboard\manifest.json    $HERMES_HOME\plugins\kanban-audit\dashboard\
copy <repo>\desktop\plugin.js          $HERMES_HOME\plugins\kanban-audit\desktop\

# 2) 校验(必须 Validation passed,退出码 0)
hermes plugins validate $HERMES_HOME\plugins\kanban-audit

# 3) 开第 1 个开关:编辑 config.yaml 的 plugins.enabled,加 kanban-audit(见 §3)

# 4) 无需重启后端:下一次请求会自动挂载路由(见 §5 排障「后端 404」的说明)

# 5) 开第 2 个开关(GUI,用户点):Capabilities → Plugins → kanban-audit 的 desktop half
# 6) Ctrl/Cmd+K → Reload desktop plugins → 侧边栏 Audit → /audit 三视图
```

自检一句:

```bash
hermes plugins validate $HERMES_HOME\plugins\kanban-audit
```

---

## 5. 排障

| 症状 | 先查什么 | 怎么办 |
|---|---|---|
| `hermes plugins validate` 失败 | 输出里哪一行没 `✓` | 多半是文件没复制全(缺 `desktop\plugin.js` 会挂 `loadable`;缺 `dashboard\plugin_api.py` 会挂 `api`)——回到 §4 第 1 步重拷 |
| 侧边栏**没有** Audit | ①第 2 个开关是不是还 off ②`desktop\plugin.js` 是否真在那个目录 | 打开 desktop half → `Ctrl/Cmd+K` → Reload desktop plugins;再看 `hermes logs gui -f` |
| 点 Audit 页面**空白/报错** | `hermes logs gui -f`;浏览器控制台 | 多为 desktop half 被打开但插件文件缺失/被改;重拷 `desktop\plugin.js` 后 reload |
| 接口 **404** `{"detail":"Plugin not found"}` | 第 1 个开关(`config.yaml` 的 `plugins.enabled`)里有没有 `kanban-audit` | 加上即生效。**注意:后端路由是「首次请求热挂载」,实测无需重启后端**(404 → 改配置 → 同一进程再请求即 200);若你改的是别的 profile 的 config,则要改对 profile(桌面 app 跑的是 `--profile default`,读的是全局 `$HERMES_HOME\config.yaml`;`hermes config get plugins.enabled` 打的是**当前 profile** 的值,在 coder 等 profile 下只显示 `- hindsight`,别被它误导) |
| 接口 **401** `{"detail":"Unauthorized"}` | 你是拿 curl 裸打后端吧 | 后端全部 `/api/*` 都要 session token(`X-Hermes-Session-Token` 头,或 `Authorization: Bearer <token>`)。桌面 app 自己带票;手工 curl 时 token = 后端进程的 `HERMES_DASHBOARD_SESSION_TOKEN`(桌面 app 是用随机端口 `--port 0` 拉起的) |
| 面板**无数据**/一直转圈 | ① `curl .../api/plugins/kanban-audit/health` ②库路径权限 | `/health` 返回 `ok:true` 说明后端正常;若 503 看 `error` 字段(engine 不可用);确认 `$HERMES_HOME\kanban.db` 存在且当前用户可读 |
| 面板显示「**数据不全**」 | `curl .../health` 的 `session_resolution_rate` 与 `missing[]` | 这是**预期**现象:老 run 没写 `worker_session_id`,无法归属到任一专家,会被记进 `missing` 并让页面走「数据不全」态(见 §6) |
| 后端 503 `engine unavailable: ...` | 503 的 `error` 文本 | `engine.py` 缺失或 import 报错;按 §4 第 1 步确认 `engine.py` 在插件**根**目录 |
| 数据明显是旧的 | 快照有 **30s 内存缓存** | 加 `?fresh=1`(`/issues`、`/agents`、`/gaps` 支持) |

诊断命令:

```bash
hermes plugins validate $HERMES_HOME\plugins\kanban-audit   # 插件包是否合规
hermes plugins list --plain --no-bundled                                          # 已注册插件(注:本插件不走 install,不会出现在这里,见下)
hermes logs gui -f                                                                # 桌面侧报错
hermes logs gateway -f                                                            # 后端侧报错
```

> **为什么 `hermes plugins list` 里看不到 kanban-audit**:该插件是「手工放进 `~/.hermes/plugins/`」的 user plugin,没走 `hermes plugins install/adopt` 的 provenance 登记,所以列表里没有它;但后端的插件发现 + 加载完全正常(实测接口 200)。验收请用 `hermes plugins validate <路径>`。

---

## 6. 只读声明 + 已知限制

**只读(设计红线,已独立复核)**

- `engine.py` 里所有 SQLite 连接一律 `file:<path>?mode=ro`;文件中无 `INSERT/UPDATE/DELETE`,无建表;不改任何 profile 配置。
- `dashboard/plugin_api.py` 零 SQL、零连接,只调用 `engine.compute_all()`。
- `kanban_audit.py` 同样零连接,只 import engine。
- 实测:跑完 6 种 CLI 调用后,`kanban.db` 与 `state.db` 的 mtime 不变。
- 桌面端只 GET,不 POST。

**已知限制(v0.1 有意不做)**

| 项 | 说明 |
|---|---|
| 老数据无法归属 | 早期 run 没写 `worker_session_id`,`tasks.session_id` **故意不用**(会串号),于是这些 run 记入 `/health` 的 `missing[]`,并让面板置「数据不全」态。全库解析率实测 0.7424(66 run / 49 有 session),属数据侧欠账,不是 bug |
| 假能力主动探针 | 缺口只从卡/评论/自评文本里**提取**,不会主动去探测某能力是否真的缺失 |
| 趋势视图 | 只有当前快照(问题/专家画像/缺口三视图);时间趋势留 v0.2 |
| 成本 | 各 profile 的 cost 依赖会话库里的计价字段,当前实测多为 0.0000(本机未计价),不是统计错误 |
| `context_map.yaml` | 对口专家基准表仍是**草稿 v0.1**(§4.3),v0.1 **不参与任何判定**,仅作给人看的提示;对口率 v0.3 才接它 |

---

## 附:实测证据(2026-10-18,由 coder 在本机执行)

> 本节所有运行数 / 解析率 / 问题条数均为 **2026-10-18 定稿时点的快照**;`kanban.db` 持续增长,这些数字会随库漂移,**不作为判据**。表内 sha256 是安装一致性判据,每次交付前重算。

**安装一致性**(安装目录 vs 订单目录,`sha256sum` 全等):

| 文件 | sha256 |
|---|---|
| engine.py | `cf33d821…f26e85` |
| plugin.yaml | `0129df4f…2335` |
| dashboard/plugin_api.py | `8dcef377…fc17` |
| dashboard/manifest.json | `5aae06a6…2057` |
| desktop/plugin.js | `01bafe30…701d5` |

**`hermes plugins validate $HERMES_HOME\plugins\kanban-audit`** → 退出码 0:

```
✓ manifest — plugin.yaml parses
✓ manifest fields — name, version, description present
✓ loadable — entry: desktop/plugin.js
✓ security scan — safe
✓ no core override — no runtime rebinds of Hermes core
✓ desktop surface — stays inside the plugin SDK surface
⚠ no __init__.py — capability probe skipped (manifest-only plugin)
◆ Isolation — portable plugin (no Python): runs anywhere
Validation passed.
```

**后端开关前后对比**(同一个 `hermes serve` 进程,127.0.0.1:8799,带 `X-Hermes-Session-Token`):

| 时刻 | 请求 | 结果 |
|---|---|---|
| 开之前 | `GET /api/plugins/kanban-audit/issues` | `404 {"detail":"Plugin not found"}` |
| 写入 `plugins.enabled` 后(**未重启**) | 同上 | `200`,真实数据 ✓ |
| 同上 | `/summary` | `200` `{"counts":{"running":2,"todo":1,"blocked":0,"done":49,"total":62},"issue_counts":{"分配错配":2,"反复失败":3,"token异常":11,"耗时异常":3}}` |
| 同上 | `/health` | `200` `{"ok":true,"runs_total":66,"runs_with_session":49,"session_resolution_rate":0.7424,...}` |
| 同上 | `/agents`、`/gaps` | `200` |
| 同上 | `/card/t_zzz_not_exist` | `404 {"ok":false,"error":"card not found: t_zzz_not_exist"}` |

**CLI** 六种调用全部退出码 0:`默认摘要`、`--json`、`--view issues`(19 条)、`--view agents`(12 专家)、`--view gaps`、`--selftest`(末行 `SELFTEST PASS`)。

**cron**:`hermes cron create "0 9 * * *" --no-agent --script kanban_audit.py --deliver local` 的语法在一个**沙箱 HERMES_HOME**里实跑通过(`Created job: 9f11a005362f` → `hermes cron list` 在列 → `hermes cron run` 报 `succeeded`,前提是 `engine.py` 与 CLI 同目录)。没有写进真实 `~/.hermes`(该步只允许改 `config.yaml` 的 `plugins.enabled`)。

**未做(留给用户)**:Capabilities → Plugins 里打开 desktop half;`/audit` 三视图目视验收。
