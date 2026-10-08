# Codex Feishu Bridge

在飞书**话题形式群**里远程使用本机 Codex。一个飞书话题绑定一个 Codex 任务：同一任务内的消息按 FIFO 排队，不同任务并行运行。

这是 `codex-telegram-bridge` 的飞书移植版：保留它已经跑通的持久化与调度内核，把 Telegram 层换成飞书、把 macOS 层换成跨平台适配层。

- 想直接用起来 → [docs/USAGE.md](docs/USAGE.md)（从建群、配对到跑完一个任务）
- 给 AI 助手读的入口 → [AGENTS.md](AGENTS.md)（结构、测试、硬约束）与 [llms.txt](llms.txt)
- 飞书开放平台怎么开 → [docs/FEISHU-SETUP.md](docs/FEISHU-SETUP.md)

```text
飞书话题群
    ↕ 长连接 WebSocket（事件订阅）
Codex Feishu Bridge  ──  SQLite WAL（真相源）
    ↕ JSON-RPC over stdio
codex app-server
    ↕
本机项目与终端
```

## 当前状态

| 平台 | 状态 |
| --- | --- |
| Windows 11 | 已实现并在真机跑通：安装、起停、配对、话题建任务、FIFO 排队、模型记忆、图片输入与图文混排、卡片按钮、线程丢失自愈、断线重连、崩溃自愈、仅在 Codex 桌面端打开时自启。单元与集成测试 **99 项通过** |
| macOS | 已实现（`install/start/stop/status/pair-code`、LaunchAgent、Keychain、Codex CLI 自动定位，适配层有单元测试覆盖）；**尚未在 Mac 真机上验证** |

> **macOS 未做实机验证**：代码在 Windows 上开发，macOS 侧只做过静态审查与单元测试。
> 第一次上机请按 [docs/MACOS-VERIFY.md](docs/MACOS-VERIFY.md) 的清单逐项确认；
> `macos/*.sh` 与 plist 模板的注释里都带 `NOT YET VERIFIED ON A REAL MAC` 标记。

首版范围是核心闭环：消息/图片输入、任务路由与队列、审批卡片、进度与中断、结果与图片回传、重启恢复。
语音（STT/TTS）、通知传感器、Computer Use、多实例隔离**不在本版**。

## 快速开始（Windows）

1. 按 [docs/FEISHU-SETUP.md](docs/FEISHU-SETUP.md) 在飞书开放平台把自建应用开出来，拿到 App ID 与 App Secret。
2. 安装与配置：

```powershell
cd D:\Codex\飞书codex机器人
.\windows\install.ps1 -Prompt
.\windows\start.ps1
.\windows\status.ps1
```

3. 取配对码并配对：

```powershell
.\windows\pair-code.ps1
```

然后在飞书**话题形式群**里发送 `@机器人 /bind <配对码>`（敏感权限未批时必须 @机器人；
和机器人私聊则直接发 `/bind <配对码>`）。

4. 在群里 `@机器人 <项目> <任务内容>`，机器人会自动建话题、建任务并开始执行。之后直接在该话题里发消息即可继续。

停止：`.\windows\stop.ps1`（SQLite 状态与产物都会保留）。

## 快速开始（macOS）

> 这套脚本从未在 Mac 上跑过，第一次上机请按
> [docs/MACOS-VERIFY.md](docs/MACOS-VERIFY.md) 的清单逐项确认。

```bash
cd ~/codex-feishu-bridge
./macos/install.sh --prompt      # 建虚拟环境、装依赖、写 config、把凭据存进钥匙串
./macos/start.sh                 # 前台预检 + 后台拉起 + 等健康检查
./macos/status.sh
./macos/pair-code.sh             # 取配对码，然后在飞书里 /bind
```

要装「只在 Codex 桌面端打开时启动」的 LaunchAgent：

```bash
./macos/install-agent.sh         # 装 launchd 配置（改动当前用户的 launchd 状态）
./macos/uninstall-agent.sh       # 卸载
```

停止：`./macos/stop.sh`（SQLite 状态与产物都会保留）。

与 Windows 的对应关系：`install.sh ↔ install.ps1`、`start/stop/status/pair-code`
一一对应、`install-agent.sh ↔ install-autostart.ps1`。两边的自启语义一致——按
`StartInterval` 轮询（120 秒），**只有当 Codex 桌面端在跑**才把桥拉起来，不开机自启、
不在 Codex 关闭后继续跑。

应用目录在 `~/Library/Application Support/CodexFeishuBridge`（`bridge.log`、
`state.sqlite3`、`inbox/`、`artifacts/`），可用 `CODEX_FEISHU_BRIDGE_HOME` 覆盖。
凭据存在登录钥匙串（service `codex-feishu-bridge`），不落盘、不进 Git。

## 项目别名

`config.json` 的 `projects` 决定 `/new` 和 `@机器人` 能选哪些目录，默认只有：

```json
{ "projects": { "codex": "D:\\Codex" } }
```

想加更多目录就改这里；`quick_start.project_keywords` 可以给项目配关键词，
让「@机器人 修一下 FitTrack 的登录」这类模糊说法也能命中正确项目。

## 指令

```
/bind <配对码>          把当前会话与 Bridge 配对
/projects              查看项目别名
/new [项目] [任务内容]   新建任务并绑定当前话题
/tasks [关键词]         搜索最近的任务
/use <任务 ID 前缀>      把当前话题切到某个任务
/resume [任务 ID 前缀]   绑定到已有任务；不带参数时列出最近任务
/status                当前话题的任务与队列
/progress              立即查看执行进度（不占用队列）
/where                 当前话题的路由与模型设置
/models                列出可用模型
/model <模型> [强度]     设置模型与推理强度
/compact               压缩当前任务上下文
/renew                 压缩后继承历史开新任务
/fork                  分叉当前任务
/clear                 在当前话题里开一个空白任务
/rename [新标题]         修改当前话题标题
/upload_revoke         撤销持续上传授权
/stop                  中断当前正在执行的处理
/cancel                取消进行中的选择
/help                  帮助
```

## 模型

默认不预设模型：第一个任务会弹模型选择卡片，选中后记住，之后可以直接干活。
`/model` 随时可以换。要在配置里写死默认值就设置 `default_model` / `default_effort`。

## 权限与安全

- 首版按无人值守运行：`approval_policy = "never"`、沙箱 `dangerFullAccess`。
  Codex 在正常执行时不会弹审批；只有外部工具自己要求授权时才会出现审批卡片。
- App Secret 存在 Windows DPAPI 保险库（`D:\Codex\CodexFeishuBridge\secrets.dat`），
  或 macOS 登录钥匙串；**不会写进 `config.json`，也不会进 Git**。
- 只有通过 `/bind` 配对的会话、或 `feishu.allowlist` 里列出的会话/用户能下发任务。
- 产物回传只允许 `artifact_roots`、当前项目目录和 Bridge 自己的 `artifacts/` 下的文件。

## 开发

```powershell
# 解释器固定用 Codex 自带的那支；系统 python 是 Microsoft Store 占位符，不可用
$py = "D:\Codex\飞书codex机器人\.venv\Scripts\python.exe"
$env:PYTHONPATH = "D:\Codex\飞书codex机器人\src"
& $py -m pytest tests -q
```

日志与状态都在应用目录：Windows 上默认 `D:\Codex\CodexFeishuBridge`（`bridge.log`、
`state.sqlite3`、`inbox/`、`artifacts/`），没有 D: 盘时回退 `%LOCALAPPDATA%\CodexFeishuBridge`。
用 `CODEX_FEISHU_BRIDGE_HOME` 可以覆盖；已经装在系统盘上的用 `windows\move-appdir.ps1` 搬过来。

设计细节见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)，与 Telegram 版的差异对照见
[docs/MIGRATION-FROM-TELEGRAM.md](docs/MIGRATION-FROM-TELEGRAM.md)。
