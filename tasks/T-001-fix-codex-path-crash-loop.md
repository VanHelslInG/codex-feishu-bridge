---
id: T-001
status: draft
title: 修掉 bridge 启动 codex app-server 的 WinError 2 崩溃循环，让 start.ps1 能稳定拉起
---

## 目标

`windows\start.ps1` 能把 bridge 稳定拉起来，`http://127.0.0.1:49660/` 返回 `ok: true`，且 `D:\Codex\CodexFeishuBridge\bridge.log` 不再新增 `FileNotFoundError [WinError 2]`。

当前状态：**每跑必崩**。崩溃点在 `core\appserver.py:59` 的 `subprocess.Popen(command)`，`command = [self.codex_path, "app-server"]`。

## 现象与已定位的根因（WorkBuddy 采集，请自行复核）

`D:\Codex\CodexFeishuBridge\bridge.log` 每 2~5 分钟一条同样的 traceback：

```
File "D:\Codex\飞书codex机器人\src\feishu_bridge\bridge.py", line 130, in run
    self.app.start()
File "D:\Codex\飞书codex机器人\src\feishu_bridge\core\appserver.py", line 59, in start
    self.proc = subprocess.Popen(
FileNotFoundError: [WinError 2] 系统找不到指定的文件。
```

推断链路（逐环已验证）：

1. `D:\Codex\CodexFeishuBridge\` 下**没有 `config.json`**（目录内只有 `bridge.log` / `bridge.pid` / `state.sqlite3` / `inbox`）；
2. → `core\config.py` 的 `CONFIG_DEFAULTS["codex_path"] = None` 生效；
3. → `platform\windows.py:115 resolve_codex_path(None)` 走 `_which("codex.exe") or _which("codex")`，即 `shutil.which` 按进程 PATH 查；
4. → 查不到（真实 exe 在 `C:\Users\gujin\AppData\Local\OpenAI\Codex\bin\9691020b546a15b2\codex.exe`，该目录不在用户 PATH 上）；
5. → 返回字面量 `"codex.exe"`，`Popen` 找不到文件 → WinError 2 → 进程秒退；
6. → 外部（Windows 计划任务 `CodexFeishuBridge`，每 5 分钟 keep-alive）反复重启 → 桌面上反复闪终端窗口。

旁证：`C:\Users\gujin\.codex\config.toml` 里有 `CODEX_CLI_PATH = 'C:\Users\gujin\AppData\Local\OpenAI\Codex\bin\9691020b546a15b2\codex.exe'`；同一 `bin\` 下还有另一个哈希目录 `441edec208d1681c\`（内含 `rg.exe`），说明 **Codex 桌面端更新会更换哈希目录名**。

## 验收标准

- [ ] `pwsh -File D:\Codex\飞书codex机器人\windows\start.ps1` → 末尾打印 `Health check: ok`，退出码 0
- [ ] `Invoke-RestMethod http://127.0.0.1:49660/` → `ok` 为 `true`
- [ ] **在删掉 `D:\Codex\CodexFeishuBridge\config.json`（保持不存在）的前提下重跑一次上面的 start.ps1，仍然能起来** —— 即修的是解析逻辑本身，不是靠人手写死一条绝对路径；重跑后 `bridge.log` 里没有新增 `WinError 2`
- [ ] 边界：`~\.codex\config.toml` 里 `CODEX_CLI_PATH` 指向的 exe 不存在时（可临时改名验证，验完改回），报错信息是**可读的**（说明去哪儿找过、期望什么路径），而不是裸的 `WinError 2`
- [ ] 已有的单测仍全绿：`D:\Codex\飞书codex机器人\.venv\Scripts\python.exe -m pytest`
- [ ] `windows\stop.ps1` 之后 health 端口不再应答：`Invoke-RestMethod http://127.0.0.1:49660/` 连接被拒绝（退出码非 0）

## 约束与不做项

- **不要 `Enable-ScheduledTask -TaskName CodexFeishuBridge`**。用户要求计划任务暂时保持禁用（已于 2026-10-09 01:26 由 WorkBuddy 执行 `Disable-ScheduledTask`），本次只修代码/配置，不要恢复自启。
- 不要改 `C:\Users\gujin\.codex\config.toml`（读它取 `CODEX_CLI_PATH` 可以，写不行）。
- 不要碰飞书凭证：`D:\Codex\CodexFeishuBridge\` 下没有 `secrets.dat`，凭证可能来自环境变量 `FEISHU_APP_ID` / `FEISHU_APP_SECRET`（见 `bridge.py:2052-2057`）。缺凭证时 `WsListener` 不启动是既有设计（`bridge.py:183-188`），不属于本卡范围。
- 不要新增第三方依赖（`requirements.txt` 目前只有 `lark-oapi`）。
- 不要回滚工作区里已有的未提交改动。开工前 `git status` 应看到这 10 个文件处于 modified 状态，它们是上一轮开发的成果：`README.md`、`docs/FEISHU-SETUP.md`、`src/feishu_bridge/core/config.py`、`src/feishu_bridge/platform/windows.py`、`windows/{ensure-running,install,pair-code,start,status,stop}.ps1`。
- 不要 `git commit`（本卡预期在 `--sandbox workspace-write` 下执行，`.git` 只读）。

## 相关文件 / 模块

- `src/feishu_bridge/platform/windows.py` — `resolve_codex_path()`，根因所在，最可能的改动点
- `src/feishu_bridge/core/config.py` — `CONFIG_DEFAULTS` / `load_config()` / `app_dir()`
- `src/feishu_bridge/core/appserver.py` — `AppServer.start()`，崩在 59 行
- `src/feishu_bridge/bridge.py` — `run()`（130 行 `self.app.start()`）
- `windows/start.ps1` — 拉起入口，含 `-c 'import feishu_bridge.bridge'` 预检
- `windows/ensure-running.ps1` — keep-alive 逻辑（本卡不改，只读）
- `tests/` — 现有测试目录

## 依赖与风险

- 修 `resolve_codex_path` 时请注意**不要把 `~/.codex/config.toml` 解析成硬依赖**：那是 Codex 桌面端的配置文件，格式/位置不保证稳定。建议优先级：`config.json` 显式值 → `shutil.which`（含 `.exe` / `.cmd` 两种 shim）→ `%LOCALAPPDATA%\OpenAI\Codex\bin\*\codex.exe` 里取最新 → `~/.codex/config.toml` 的 `CODEX_CLI_PATH`。以上只是建议，按你判断实现。
- 如果 Codex 桌面端正在运行，`bin\` 下的哈希目录可能变动，验收第 3 条必须在「配置缺失」场景下也自洽。
- 起 bridge 会拉起一个真实 `codex app-server` 子进程，验收完记得 `stop.ps1` 收尾。
- 若发现无法在不动 `config.toml` / 不装新依赖的前提下满足验收，**标记为阻塞并写明卡在哪一环**，不要绕过。

## 关联文档

- 方案：`C:\Users\gujin\.codex\plans\01a11b9a-0de1-7e23-91a7-909e2e2510f0\01a11b9a-103c-7522-965c-e21aafb870d3\PLAN.md`（飞书版 Codex Bridge）
- 仓库内说明：`README.md`、`docs/ARCHITECTURE.md`、`docs/FEISHU-SETUP.md`
- 崩溃日志：`D:\Codex\CodexFeishuBridge\bridge.log`
- 计划任务定义：`C:\Windows\System32\Tasks\CodexFeishuBridge`（UTF-16LE）

## 备注（WB 采集到的上下文）

- 计划任务动作：`pwsh -NoProfile -NonInteractive -WindowStyle Hidden -File D:\Codex\飞书codex机器人\windows\ensure-running.ps1`，触发器 = 登录后延迟 1 分钟 + 每 5 分钟重复；当前 **State=Disabled**。
- 用户原始诉求是「桌面上时不时弹终端窗口」，根因就是上面第 6 条；窗口来源已由 WorkBuddy 止住，本卡只管修桥本身。
- `D:\Codex\CodexFeishuBridge\bridge.pid` 里的 25320 是已崩溃进程的残留 PID，可清掉。
- 运行 bridge 用的解释器是项目内 `.venv\Scripts\python.exe`；建 venv 用的基础解释器是 `C:\Users\gujin\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe`（系统 `python` 是 Microsoft Store 存根，别用）。
- 本机环境限制：`schtasks.exe` / `reg.exe` 被安全策略拉黑；PowerShell 工具在本会话拿不到 stdout（改用 Python `winreg` / 直接解码 `C:\Windows\System32\Tasks\*` 兜底）。
