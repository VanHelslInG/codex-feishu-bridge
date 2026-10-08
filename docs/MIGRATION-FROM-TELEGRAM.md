# 与 Telegram 版的差异对照

源项目：`qianfengchen/codex-telegram-bridge`（macOS + Telegram，单文件 `bridge.py` 5863 行）。

## 直接继承的设计

- SQLite WAL 作为唯一真相源：入站事件、排队 turn、worker 租约、outbox、审批、产物。
- 出站 at-least-once + 入站幂等；用消息身份做去重边界。
- 启动恢复语义：在跑的 turn 标记中断并回队列。
- app-server 由 Bridge 自己拉起、自己监督回收，不依赖桌面客户端。
- 指令集与措辞基本逐条对齐（`/new /resume /tasks /use /status /progress /where /models
  /model /compact /renew /fork /clear /rename /stop /upload_revoke`）。
- 审批卡片三选项语义：允许一次 / 本任务始终允许 / 拒绝，点击后原卡片显示最终状态。
- 超出 Telegram 限制的长文本分段、表格转移动端可读布局这类展示细节，
  在飞书侧对应「表格转列表 + 卡片分段」。

## 换成飞书的部分

| 维度 | Telegram 版 | 飞书版 |
| --- | --- | --- |
| 收事件 | Bot API long polling | 长连接 WebSocket（事件订阅） |
| 话题模型 | Forum Topic（可程序创建/改名） | 话题形式群：`thread_id`(omt_) + `root_message_id`(om_)，不能改名 |
| 会话标识 | 整数 `chat_id` / `message_id` | 字符串 `oc_` / `om_` / `ou_` |
| 按钮回调 | Inline Keyboard `callback_data` | 交互卡片按钮 + `card.action.trigger` |
| 更新卡片 | `editMessageText` | `im.message.patch` |
| 富文本 | Markdown → Telegram HTML | Markdown → 卡片 markdown 组件（表格转列表） |
| 消息状态 | Reaction 👀/✅/❌ | 飞书表情反应（`OnIt`/`DONE`/`ERROR`），失败不影响投递 |
| 收群内全部消息 | 关闭 privacy mode 即可 | 需要敏感权限 `im:message.group_msg`，否则每条都要 @机器人 |

## 换成平台适配层的部分

| 维度 | Telegram 版（macOS 专属） | 飞书版 |
| --- | --- | --- |
| 密钥 | Keychain（`security`） | Windows DPAPI 保险库 / macOS Keychain |
| 自启 | launchd plist | Windows 先用手动脚本（`start/stop/status.ps1`），验证后再加计划任务 |
| 进程树终止 | `os.killpg` | Windows `taskkill /T /F`；macOS `os.killpg` |
| Codex 路径 | `ChatGPT.app` 内置 CLI | `Get-Command codex` 解析，`config.json` 可覆盖 |
| FD 计数 | `lsof` | macOS 同左；Windows 无对应实现，只保留年龄判据 |

## 本版没搬的功能

- 语音输入与语音回复（STT/TTS、词表、`/stt_test`、`/tts`）。
- macOS 通知横幅传感器（`NotificationSensor.swift`）。
- Computer Use MCP 注入与 Ego 浏览器通道。
- 多实例隔离（账号 B、通知专用实例）。
- `/desktop` 与桌面端单任务交接。

这些功能依赖 macOS 或需要额外平台能力，属于后续版本的候选。
