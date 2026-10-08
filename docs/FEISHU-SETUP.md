# 飞书开通清单

按顺序做完这七步，Bridge 就能收到消息。括号里是每一步在开放平台里的位置。

## 0. 前置条件

- 一个可以创建自建应用的飞书企业/团队（免费版即可）。
- 如果当前账号是飞书个人版或未认证企业，先在「开发者后台 → 账号设置」完成**个人实名认证**，
  否则无法创建企业自建应用。

## 1. 创建企业自建应用

1. 打开 <https://open.feishu.cn/app> 。
2. 「创建企业自建应用」，填名称（例如 `Codex Bridge`）和图标。
3. 进入应用后，在「凭证与基础信息」里记下 **App ID** 和 **App Secret**。

App Secret 只在创建时完整显示一次；丢了就重置。**不要**把它贴进聊天、日志或仓库。

## 2. 开启机器人能力

「应用能力 → 添加应用能力 → 机器人」，开启后可以设置机器人名称和头像。

## 3. 事件订阅选「长连接」

「事件与回调 → 事件配置」：

1. 订阅方式选择 **使用长连接接收事件**（不需要公网 IP、不需要域名、不需要内网穿透）。
2. 添加事件：

   | 事件 | 用途 |
   | --- | --- |
   | `接收消息` (`im.message.receive_v1`) | 收用户发来的消息 |

3. 在「回调订阅」里添加 **卡片回传交互**（`card.action.trigger`），用于审批卡片和按钮点击。

> 长连接模式下，Bridge 必须在运行才能建立连接。Bridge 没启动时飞书不会再重推已经错过的事件。

## 4. 申请权限

「权限管理」里按下表搜索并开通。权限分为普通和**敏感**两类，敏感权限需要企业管理员审批。

| 权限名称（控制台里显示的名字） | 用途 | 类型 |
| --- | --- | --- |
| 获取与发送单聊、群组消息 | 收发消息的基础权限 | 普通 |
| 以应用的身份发消息 | 机器人身份发送 | 普通 |
| 获取群组信息 | 判断会话类型 | 普通 |
| 获取与上传图片或文件资源 | 图片输入与产物回传 | 普通 |
| 获取群组中所有消息 | **话题里直接发消息不用 @ 机器人** | 敏感 |

关于最后一条：飞书机器人默认只能收到 @它 的消息。要获得接近 Telegram「关闭 privacy mode」的体验，
必须拿到 `获取群组中所有消息`（`im:message.group_msg`）这条敏感权限，由企业管理员在
「管理后台 → 应用管理 → 待审核」里通过。

**拿不到也可以正常用**：把 `config.json` 里的
`feishu.require_mention_in_group` 设为 `true`，之后在话题里每条消息都 @ 一下机器人即可。

## 5. 发布版本

「版本管理与发布 → 创建版本」，填版本号和可用范围：

- 开发阶段：可用范围先只勾自己，用**测试企业与测试版本**可以免管理员审核直接生效。
- 正式使用：提交发布后需要管理员审核，通过后才对企业内其他成员可见。

## 6. 建话题群并拉机器人进群

1. 新建一个群，在「群设置 → 群类型/消息形式」里选 **话题形式**（群信息里会显示为话题群）。
   注意：普通群无法改成话题群，必须是新建时就是话题形式。
2. 把刚创建的机器人添加进群。
3. 如果想要「群里直接发消息不用 @」，确认第 4 步的敏感权限已通过审核。

## 7. 回填凭据并配对

这一步是把 App ID / App Secret 交给 Bridge，启动它，然后在飞书里完成配对。
一共三条命令加一条飞书消息。

**准备**：打开一个 PowerShell 窗口（Win+X → 终端），然后

```powershell
cd D:\Codex\飞书codex机器人
```

**macOS**：下面 7.1–7.3 的命令在 mac 上换成 `./macos/` 下的同名脚本，注意
**这套 macOS 脚本还没有在任何真机上跑过**（开发机上没有 Mac），第一次上机请先看
[HANDOFF-2026-10-09-macos.md](HANDOFF-2026-10-09-macos.md) 顶部的「未实机验证」清单：

```bash
cd ~/codex-feishu-bridge
./macos/install.sh --prompt    # ↔ install.ps1 -Prompt
./macos/start.sh               # ↔ start.ps1
./macos/pair-code.sh           # ↔ pair-code.ps1
```

### 7.1 把 App ID / App Secret 交给 Bridge

```powershell
.\windows\install.ps1 -Prompt
```

它会依次问两件事：

```text
Feishu App ID:      ← 粘贴 cli_ 开头的那串，回车
Feishu App Secret:  ← 粘贴密钥，回车
```

两个值都在开放平台「你的应用 → 凭证与基础信息」里。
**输入 App Secret 时屏幕上不会显示任何字符**，这是正常的，粘贴完直接回车即可。
密钥会被 DPAPI 加密后存到应用目录（默认 `D:\Codex\CodexFeishuBridge\secrets.dat`）下的 `secrets.dat`，不会写进配置文件。

看到 `credentials stored` 和 `Installed.` 就算完成。

### 7.2 启动 Bridge

```powershell
.\windows\start.ps1
```

正常会依次输出：

```text
Preflight: importing the bridge
Bridge started (pid 12345)
Health check: ok
```

看到 `Health check: ok` 就说明 Bridge 已经连上飞书的长连接，在群里 @它就会有反应。

### 7.3 取配对码

```powershell
.\windows\pair-code.ps1
```

它会直接打印 8 位配对码，以及要发的那行消息。

### 7.4 在飞书里配对

在**第 6 步建的那个话题群里**发送（如果敏感权限还没批下来，群里必须 @机器人）：

```text
@机器人 /bind 你刚拿到的8位码
```

机器人回「配对成功」即可。配对码用一次就立刻失效，之后不需要再配。

想私下用也可以改成和机器人**私聊**发 `/bind 你刚拿到的8位码`（私聊不需要 @）。

### 7.5 发第一条任务

```text
@机器人 codex 看看当前目录里都有什么项目
```

机器人会自动建话题、建任务并开始执行。之后就在这个话题里直接发消息，按先后顺序排队执行。

### 第 7 步常见问题

| 现象 | 原因与处理 |
| --- | --- |
| `Feishu App Secret:` 那里没反应 | 正常：安全输入不回显，粘贴后回车即可 |
| 提示 `Virtualenv missing` | 先跑 `.\windows\install.ps1`（不带 `-Prompt` 也行） |
| `Health check` 一直不就绪 | 看应用目录下 `bridge.log` 的尾部报错（默认 `D:\Codex\CodexFeishuBridge`） |
| 群里发 `/bind` 机器人没反应 | 敏感权限未批时必须 `@机器人 /bind …` |
| 机器人回「配对码不正确」 | 码已经被用过一次，重新 `.\windows\pair-code.ps1` 取新的 |

## 排查

| 现象 | 先查什么 |
| --- | --- |
| 机器人完全没反应 | `.\windows\status.ps1` 是否 `ok: true`；日志里有没有「Feishu credentials are missing」 |
| 话题里发消息没反应 | 敏感权限是否拿到；没拿到就 @机器人，或把 `require_mention_in_group` 设为 `true` |
| 机器人回了「还没有配对」 | 先 `/bind <配对码>` |
| 卡片按钮点了没反应 | 回调订阅里是否加了 `card.action.trigger` |
| 任务创建失败 | 日志里搜 `thread/start`；确认 `projects` 里的目录真实存在 |

日志位置：应用目录下的 `bridge.log`，默认 `D:\Codex\CodexFeishuBridge\bridge.log`
（`CODEX_FEISHU_BRIDGE_HOME` 可覆盖；已经装在系统盘的用 `windows\move-appdir.ps1` 搬走）。
启动脚本会先做一次前台导入预检，所以依赖缺失这类错误会在终端直接报出来，
不会因为后台窗口隐藏而丢失；需要完整堆栈时按 `start.ps1` 末尾提示在前台跑一次即可。
