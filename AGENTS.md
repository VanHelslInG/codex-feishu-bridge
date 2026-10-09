# AGENTS.md — Codex Feishu Bridge

给 AI 助手（Codex、Claude Code、Cursor、其他 agent）看的项目说明。
人类使用教学见 [README.md](README.md) 与 [docs/USAGE.md](docs/USAGE.md)。

## 这是什么

在**飞书话题形式群**里远程驱动本机 Codex。一个飞书话题 = 一个 Codex 任务：
同一话题内的消息按 FIFO 排队，不同话题并行执行。桥自己拉起
`codex app-server` 子进程走 stdio JSON-RPC，**不碰** Codex 桌面端自己的服务。

## 架构：三层，别混改

| 层 | 位置 | 放什么 |
| --- | --- | --- |
| 内核 | `src/feishu_bridge/core/` | SQLite WAL 状态机、FIFO 调度与租约、app-server JSON-RPC 客户端、outbox 指数重试、指令解析、配置 |
| IM | `src/feishu_bridge/im/feishu/` | 长连接事件入口、卡片构造、Markdown→卡片渲染、飞书 API 封装 |
| 平台 | `src/feishu_bridge/platform/` | `base.py` 是接口；`windows.py` 是凭据存储（DPAPI）、进程组回收、路径与 Codex CLI 定位 |
| 组装 | `src/feishu_bridge/bridge.py` | 接线 + 主循环 |

规则：改 IM 只动 `im/`，改平台只动 `platform/`。**不要**为了某一个平台往
`core/` 里塞平台分支——那正是这层抽象存在的理由。

## 怎么装、怎么跑

依赖只有 `lark-oapi`，其余一律标准库。解释器要求 ≥ 3.9。

```powershell
.\windows\install.ps1 -Prompt   # 建虚拟环境、装依赖、写 config、凭据进 DPAPI
.\windows\start.ps1             # 拉起 + 等健康检查
.\windows\status.ps1
```

应用目录、日志与状态布局见 README。同一套核心也可以用
`PYTHONPATH=src .venv\Scripts\python.exe -m feishu_bridge.bridge run` 前台跑。

> **不要**用系统 `python`（Microsoft Store 占位符，退出码 9009），
> 用项目内 `.venv\Scripts\python.exe`。

## 怎么跑测试

```powershell
$env:PYTHONPATH = "src"; .\.venv\Scripts\python.exe -m pytest -q
```

当前基线：**68 项全部通过**。（本仓库只发布 Windows 适配器，不含 macOS 适配层与
其启动器资产的 31 项测试。）改动内核或 IM 层后，这个数字只许增不许减。

## 硬约束（改代码前先读）

- **不加依赖**。要加新库先改 `requirements.txt` 并在说明里讲清为什么 stdlib 做不到。
- **事件回调 3 秒内必须 ACK**。入站长连接回调只做「规范化 → 落 `feishu_events` → 返回」，
  任何业务逻辑都放到 worker 线程；否则飞书会重推。
- **幂等优先**。入站以 `event_id` 做主键去重，消息另以 `message_id` 作为 turn 级幂等键。
  出站是 at-least-once（飞书消息 API 没有幂等键）。
- **SQLite 是唯一真相源**。不要引入 Redis、队列服务之类的中间件。
- **密钥不进仓库**。App Secret 走 Windows DPAPI（`secrets.dat`）；
  `config.json`、`secrets.dat`、`state.sqlite3`、`bridge.log` 都在 `.gitignore` 里，
  别把它们提交上来。
- **产物回传是白名单制**。只允许 `artifact_roots`、当前项目目录、桥自己的 `artifacts/`
  下的文件，别放宽。
- **平台自启语义**：只在 **Codex 桌面端打开时**启动桥，不做开机自启
  （计划任务每 2 分钟跑一次 `windows\ensure-running.ps1`，先判断桌面端在不在）；
  **桌面端关掉之后，同一轮扫描要把桥连同它的 `codex app-server` 子进程一起收掉**，
  不留进程残留。

## 最容易踩的坑

- 真实 `codex app-server` 的 `fileChange` item 把路径放在 **`changes` 字典的 key** 上，
  没有顶层 `path`。产物采集要读 `changes`，别只看 `path`。
- `codex.exe` 的安装目录带哈希且随桌面端更新而变（`...\OpenAI\Codex\bin\<hash>\codex.exe`）。
  定位要按「配置显式值 → PATH → `bin\*\codex.exe` 取最新 → `config.toml` 的 `CODEX_CLI_PATH`
  → app bundle」的顺序找，别写死。
- 飞书 `im.message.patch` 更新卡片有场景限制；受限时改用 CardKit 卡片实体接口。

## 文档地图

- `docs/USAGE.md` — 使用教学：从建群到跑完一个任务
- `docs/FEISHU-SETUP.md` — 飞书开放平台开通清单（逐条附后台路径）
- `docs/ARCHITECTURE.md` — 数据流、话题与任务的映射、投递语义
- `docs/MIGRATION-FROM-TELEGRAM.md` — 与 Telegram 原版的差异对照

## 版本状态

- **Windows 11**：已实现并在真机跑通（安装、起停、配对、话题建任务、图片输入、
  恢复、watchdog、自启）。
- **macOS**：不在本仓库。这个版只发布 Windows 适配器；`platform/base.py` 的
  `current_platform()` 在非 Windows 上会直接抛出说明性错误，而不是静默失败。
