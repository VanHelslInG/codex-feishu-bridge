# macOS 首次上机验证清单

macOS 侧的代码（`macos/*.sh`、`com.chen.codex-feishu-bridge.plist.template`、
`src/feishu_bridge/platform/macos.py`）是在 Windows 上写的，只做过静态审查与单元测试，
**一次都没有在真实 Mac 上运行过**。第一次上机请按下面顺序逐项确认，任何一条不通过都
说明该处需要修。

`macos/*.sh` 与 plist 模板的注释里都带 `NOT YET VERIFIED ON A REAL MAC` 标记，
它们对应的就是这份清单。

## 清单

1. **语法**：`bash -n macos/*.sh` 全部无语法错误。
   （开发机上没有 bash，这一步没做过。）
2. **安装**：`macos/install.sh --prompt` 建出 `.venv`、装上依赖、写出 `config.json`，
   凭据进钥匙串。验证：

   ```bash
   security find-generic-password -s codex-feishu-bridge -a feishu-app-id -w
   ```

3. **启动**：`macos/start.sh` 结尾出现 `Health check: ok`；`macos/status.sh`
   里 `app_server_pid` 非空。
4. **停止干净**：`macos/stop.sh` 之后 `pgrep -f 'codex app-server'` 应为空
   （不留孤儿进程）。
5. **Codex CLI 自动定位**：预期命中 app bundle 内的
   `codex-cli/CodexCLI.app/Contents/MacOS/codex`；命不中则退到
   `~/.codex/config.toml` 的 `CODEX_CLI_PATH`。两条都不行时用 `config.json`
   的 `codex_path` 显式指定。
6. **LaunchAgent 的启停语义**：装上 `macos/install-agent.sh` 后，
   **关掉 Codex 桌面端，两分钟内桥必须自己停住、且不再被拉起**；
   重新打开 Codex 桌面端后，桥应在一分钟内恢复。
   这是硬要求——不做开机自启。
7. **进程匹配**：`pgrep -fl 'Contents/MacOS/'` 确认 `bridge-agent.sh` 的锚定模式
   命中桌面端、且**不**命中桥自己拉起的 `codex app-server`；必要时用
   `BRIDGE_APP_PATTERN` 覆盖默认模式。

## 已知的待确认点

- `macos/lib/appdir.sh` 只从 `~/Library/Application Support/CodexFeishuBridge`
  解析应用目录，`CODEX_FEISHU_BRIDGE_HOME` 覆盖是否在每条脚本里都生效需要上机确认。
- `platform/macos.py` 的 `fd_count` 依赖 `/usr/sbin/lsof`，其输出格式在不同
  macOS 版本上可能有差异；watchdog 的 FD 回收阈值只在拿到真实计数后才有意义。
- Keychain 首次写入可能弹授权对话框；无人值守场景下要先手动跑一次
  `install.sh` 让钥匙串记住该条目。

## 反馈

哪一条没过，就在 issue 里贴 `macos/install.sh`、`macos/start.sh` 的完整输出与
`~/Library/Application Support/CodexFeishuBridge/bridge.log` 的尾部。
