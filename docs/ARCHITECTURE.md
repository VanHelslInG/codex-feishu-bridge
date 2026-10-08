# Architecture

单机控制面。SQLite（WAL）是唯一真相源，不需要 Redis 或任何外部中间件。

## 数据流

```text
飞书长连接事件
  → _intake：规范化 → feishu_events 落盘（3 秒内 ACK）
  → events 线程：按 event_id 取事件、处理、标记完成
  → queued_turns：每个 Codex 任务一条 FIFO 队列
  → scheduler：按 thread 取租约 → turn/start（不同 thread 并行）
  → app-server 事件：item/*、turn/* 记录进度、产物与最终回答
  → feishu_outbox：文本/卡片/图片/文件/表情，带租约与指数退避
  → outbox 线程投递到原话题
```

## 为什么先落盘再处理

飞书要求在 3 秒内响应事件回调，否则会重推。因此长连接回调只做一件事：
把规范化后的事件写进 `feishu_events` 并立即返回。重推靠 `event_id` 主键去重，
入站消息另以飞书 `message_id` 作为 turn 级幂等键（`UNIQUE(chat_id, source_message_id)`）。

投递语义：入站事件与排队完全幂等；出站是 at-least-once——
飞书消息 API 没有幂等键，进程在「飞书已接收」与「SQLite 记成功」之间崩溃会产生重复消息。

## 话题与任务的映射

- 一个飞书话题 = 一个 Codex 任务，绑定关系存在 `thread_routes`：
  `thread_id`(Codex) ↔ `feishu_thread_id`(omt_…) + `root_message_id`(om_…)。
- 所有下行消息都是对 `root_message_id` 的**话题内回复**，因此永远回到同一个话题。
- 如果用户是在话题群里直接 @机器人 开的新任务，就**沿用用户已经建立的那个话题**，
  不再额外建话题；只有机器人主动开任务时才自己发根消息建话题。
- 飞书话题不支持改名，`/rename` 通过改写根消息内容并记录内部标题来实现。

## 并发与恢复

- 同一任务一次只跑一个 turn，后续消息 FIFO。
- 不同任务（不同话题）并行，`worker_leases` 保证同一 thread 只有一个 worker 在派发。
- 进程重启：`recover_queue()` 把 `running` 的 turn 退回 `queued`、清空租约；
  `recover_outbox()` 把 `sending` 退回 `pending`；未处理完的 `feishu_events` 重新入队。
- `/stop` 之后新到的消息会按发送顺序合并成一个请求（`coalesce_queued_turns`）。

## app-server 生命周期

Bridge 自己拉起 `codex app-server` 子进程（stdio JSON-RPC），不碰桌面客户端自己的 daemon。
watchdog 每 `check_interval_seconds` 检查一次：进程死了就重启；文件描述符超过
`recycle_fd_threshold` 或存活超过 `max_age_seconds` 时，在**没有活跃 turn 与请求**的前提下回收，
排队工作留在 SQLite 里，下个进程继续跑。Windows 上没有 `lsof`，FD 计数返回 null，只保留年龄判据。

## 适配层

| 层 | 职责 |
| --- | --- |
| `core/` | 与平台、IM 无关：配置、SQLite 状态机、app-server 客户端、指令解析 |
| `im/feishu/` | 长连接接入、消息/卡片 API、Markdown→卡片渲染、卡片模板 |
| `platform/` | 密钥存储（DPAPI / Keychain）、进程树控制、路径解析、FD 计数 |

`im/base.py` 定义了适配接口，测试用内存假实现替换飞书，因此完整流程可以在不联网、
不依赖飞书的条件下跑通。

## 已知取舍

- `im.message.patch` 更新卡片在部分场景受限；若遇到更新失败，改走 CardKit 卡片实体接口。
- 飞书反应表情类型是固定枚举，本版只用 `OnIt` / `DONE` / `ERROR` 三种；失败只记 debug 日志，
  状态仍然由卡片承担。
- 敏感权限拿不到时降级为「每条消息都要 @机器人」，代码两条路径都实现。
