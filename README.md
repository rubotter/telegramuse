# Telegram Bridge Kit — 可复用的 Telegram Bot 双向桥

一套已经实战验证过的 Telegram 双向对话桥代码，纯 Python 标准库、零依赖。
任何 Bot 都能用：换上自己的 Bot Token 就能跑。给「AI Agent + Telegram」场景设计：
用户在 Telegram 发消息，桥秒级唤醒 Agent 处理，Agent 通过桥回文字/图片。

## 架构

```
Telegram 用户
   │  消息
   ▼
listener.py ──长轮询 getUpdates──► inbox.jsonl（本地队列，一行一条 JSON）
   │                                    │
   │ 机主消息入队即发                    │ 每 5 秒轮询 diff
   ▼                                    ▼
「思考中」动画占位              hook/telegram-bridge.sh（检查点 diff，有新消息才唤醒）
   ▲                                    │
   │ 正式回复前自动删除                  ▼
send_reply.py ◄─────────────── Agent worker（规则见 hook/worker-prompt.md）
```

四个部件：

| 部件 | 文件 | 作用 |
|---|---|---|
| 监听器 | `telegram_bridge/listener.py` | systemd 常驻，长轮询收消息入队；兼管「思考中」动画 |
| 发送器 | `telegram_bridge/send_reply.py` | 唯一对外发送通道：文字、图片、删占位、机器人对聊限流 |
| 公共层 | `telegram_bridge/common.py` | Bot API 调用（JSON / multipart）、路径与配置 |
| 唤醒钩子 | `hook/telegram-bridge.sh` + `hook/worker-prompt.md` | 5 秒轮询队列，有新消息才唤醒 Agent，并附处理规则 |
| 自愈看门狗 | `hook/telegram-bridge-watchdog.sh` + `hook/watchdog-worker-prompt.md` + `telegram_bridge/ensure_listener.sh` | 5 秒轮询监听服务状态，掉线才唤醒 Agent 跑自愈脚本重装单元并拉起服务 |

## 特性

- **秒级唤醒**：监听器长轮询 + 5 秒钩子轮询本地队列，不依赖任何平台 Webhook。
- **「思考中」动画**：机主消息一入队就发出占位——Telegram 原生「正在输入…」状态（每 4 秒刷新）+ 消息里月相轮换 🌑🌒🌓🌔🌕🌖🌗🌘（1 秒一帧，按截止时间定速）；正式回复发出前自动删除；不回复的轮次有 `--delete-thinking` 清理；10 分钟 TTL 兜底。**群聊自动降速**（4 秒一帧、typing 8 秒刷新）：群里 Bot 限流约 20 次动作/分钟，1 秒一帧会触发 HTTP 429 并连累正式回复（实测踩过）。
- **图片直达**：`send_reply.py --photo` 用 multipart 上传，图片直接发到 Telegram。
- **机器人防死循环（两道护栏）**：对机器人会话滚动 60 秒最多 60 条，超限脚本直接拒绝（退出码 3 + `RATE_LIMITED`）；另有连发硬停：连续 20 条 Bot 对话中间没有任何真人发言时直接拒绝（退出码 4 + `STREAK_LIMITED`，真人发言后自动解除）——前者只限速、后者才保证循环终止。两道都按**实际发出的消息条数**计（长文分片逐条计、部分失败按已发数计），且检查与记账在文件锁内完成，并发发送绕不过去。
- **发送目标护栏**：发送器只向监听器实际收到过消息的会话发送（`state/known-chats.json`），未知会话默认拒绝（退出码 5 + `UNKNOWN_CHAT`），防止入队消息里的注入内容诱导 worker 向任意会话发消息或贴本机文件；确需首发新会话用 `--force`。
- **认领账本（防重复回复）**：回复带 `--update-id <入站消息的 update_id>` 时，发送器先在文件锁内把该 id 记入 `state/reply-claims.json` 再发送；同一条消息的第二次回复直接拒绝（退出码 7 + `ALREADY_CLAIMED`）。钩子重投、积压重放、并发 worker 因此最多产生一条回复；发送在第一条消息发出前失败会释放认领，真正的重试仍可进行。账本条目 7 天过期自动清理。
- **配对白名单**：初始配对模式不回复任何人，机主确认后才锁定 owner ID；之后只处理机主消息，其他真人不回（与机主事务有关的转达机主），其他机器人仅任务相关才回。
- **重启不丢消息**：getUpdates offset 持久化；状态文件全部原子写（tmp + replace）。
- **掉线自愈**：虚拟机/容器替换会抹掉 systemd 单元文件、监听服务随之静默死亡（实测一晚多次）。看门狗钩子每 5 秒查一次服务状态，掉线才唤醒 Agent 运行 `ensure_listener.sh`：重装单元 → daemon-reload → enable --now → 验证 active，全程约 10 秒，并向机主汇报。
- **每 Bot 一份角色定义（SOUL）**：创建 Bot 时（配对后、入群前）在私聊里定好身份、角色、负责的账号与任务（数据口径），后续只能由机主在私聊修改。多 Bot 同群时配一份群名册（每群一份、本地保存、不进仓库），并有「状态对齐」协议：群主触发时各 Bot 上报自己的定义，任何 Bot 定义变化后主动发群同步。模板与细则见 `docs/bot-soul-template.md`、示例见 `BOT_SOUL.example.md`。
- **高级功能：云端共享状态**：所有 Bot 对接 Cloudflare D1（数据库）+ R2（对象存储）+ Workers（API 网关），定义/任务写库同步、文件走对象存储，每 Bot 一个 Token 且只能写自己的数据；免费额度远超所需。设计见 `docs/shared-state-cloudflare.md`，参考实现在 `cloudflare/`。

## 快速开始

前置要求：**Python ≥ 3.10**（代码用了 3.10+ 语法，install.sh 会先检查）；systemd 路径还需要 sudo（写单元文件、管理服务）。

```bash
# 1. 准备 Token（@BotFather 创建 Bot 后拿到）
cp bridge.env.example bridge.env
chmod 600 bridge.env        # 把 TELEGRAM_BOT_TOKEN 填进去（忘了改权限 install.sh 会自动收紧到 600）

# 2. 安装监听服务（会生成配对模式的 allowlist.json）
./install.sh

# 3. 接上 Agent 侧唤醒（二选一）
#    a) Muse 事件钩子：用 hook/telegram-bridge.sh 注册一个 5 秒钩子，
#       worker 指令用 hook/worker-prompt.md（替换 <KIT_DIR>）。
#    b) 其他运行时：任何「每 5 秒 diff inbox.jsonl 与检查点、有新行就唤醒
#       Agent 并把 worker-prompt.md 作为指令」的机制都可以。

# 4. 注册自愈看门狗（强烈建议）：用 hook/telegram-bridge-watchdog.sh
#    再注册一个 5 秒钩子，worker 指令用 hook/watchdog-worker-prompt.md。
#    监听服务被环境替换弄掉线时，它会自动修复并汇报。

# 5. 配对：用你的 Telegram 给 Bot 发一句话，Agent 会在执行总结里把
#    你的候选 ID（from_id）报给机主。机主在主聊天确认后，由主代理
#    （或机主本人）把 owner_id / owner_username 写入 allowlist.json
#    ——监听器每次入队都会重读该文件，改完立即生效、无需重启。
#    然后你再发一条消息，桥会用 send_reply 回一条配对确认，配对完成。
#    之后在私聊里完成角色定义（见 docs/bot-soul-template.md 时机一）。
```

不用 systemd 时也可以直接跑监听器：

```bash
set -a; source bridge.env; set +a
python3 telegram_bridge/listener.py
```

注意非 systemd 模式的边界：`ensure_listener.sh` 自愈只驱动 systemd，
无 systemd 时它会明确报 FAILED（no systemd），需人工拉起监听器；
看门狗钩子在无 systemd 时自动退化为只看轮询心跳判活，唤醒原因会是
heartbeat_stale，worker 收到后应人工检查/重启监听器进程，而不是
反复跑自愈脚本。

## 在 Muse 中部署（给另一个 Muse 用）

把仓库链接发给另一个 Muse，再加一句话即可：

> 照这个仓库给我部署一套 Telegram 桥，Bot 我新建，Token 走安全录入：https://github.com/rubotter/telegramuse

对方 Muse 会自己完成：clone 仓库 → 跑 `install.sh` 装监听服务 → 按 `hook/` 里的脚本和 worker 提示词模板新建 5 秒事件钩子 → 走配对流程锁定机主。部署时注意三点：

1. **每套部署必须用自己的新 Bot**：去 @BotFather 新建 Bot、拿新 Token。同一个 Bot 的 `getUpdates` 只能被一个监听器消费，两套桥共用一个 Bot 会互相抢消息，两边都只能收到半截对话。
2. **Token 走安全录入，不要贴在聊天里**：让 Muse 把 Token 写进 `bridge.env`（chmod 600）或它自己的保险库，不要让 Token 留在对话记录、日志或代码里。
3. **钩子要在目标 Muse 里新建，不是复制文件**：`hook/` 里是脚本和提示词模板，目标 Muse 需要用它自己的钩子机制注册，并把模板里的 `<KIT_DIR>` 换成它那边的实际安装目录。

## 配置

环境变量（见 `telegram_bridge/common.py`）：

- `TELEGRAM_BOT_TOKEN`：必填。只放 env / 密钥管理里，禁止入库、禁止打印。
- `TGBRIDGE_HOME`：可选，状态目录，默认套件根目录。其下有 `inbox.jsonl`、`allowlist.json`、`state/`。解析顺序全组件一致：真实环境变量 > 套件根目录 `bridge.env` 里的同名键 > 套件根目录——只写在 bridge.env 里也对所有组件生效，不会把桥劈成两个目录。

可调参数（都在 `listener.py` / `send_reply.py` 顶部）：

- `THINKING_FRAMES`：占位动画帧，想换风格（表情轮换、进度条、打字机）改这里。
- `THINKING_INTERVAL`：帧间隔秒数。注意一次编辑往返约 0.9 秒，循环已按截止时间定速，设更小也不会更快。
- `THINKING_TTL`：占位自动过期秒数。
- `BOT_LIMIT` / `WINDOW`：机器人对聊速率护栏（默认 60 条 / 60 秒，按消息条数计）。
- `STREAK_LIMIT`：机器人连发护栏（默认连续 20 条，真人发言重置）。

## 从 Muse 部署版移植时的差异

原版跑在 Muse 里，Token 放在 Muse 保险库、由运行时在出口处代换，
代码全程不接触明文；本套件改成最通用的 **env 变量**方式，其他环境直接能跑。
其余逻辑（队列格式、动画、限流、配对规则）与实战版逐行对应。

## 踩坑清单（实测）

1. 路径一律从配置/绝对路径推导：systemd 环境下 `$HOME` 可能不是用户家目录，相对路径会把队列写错地方且无报错。
2. VM/容器被替换会抹掉 `/etc/systemd/system/` 的单元文件；单元模板留在仓库里，恢复时重跑 `install.sh`。
3. Telegram 没有给 Bot 改头像的 API，机器人头像只能在 BotFather 用 `/setuserpic` 改。
4. Bot API 不向机器人投递其他机器人的消息，60 条/分钟限流是兜底不是主防线。
5. 收到 429 立即停手，按 `retry_after` 等待，不循环重试。

## 文件清单

```
telegram_bridge/   common.py / listener.py / send_reply.py
hook/              telegram-bridge.sh（消息钩子）/ telegram-bridge-watchdog.sh（看门狗）
                   worker-prompt.md / watchdog-worker-prompt.md
docs/              bot-soul-template.md（角色定义与名册模板）
                   shared-state-cloudflare.md（云端共享状态设计）
cloudflare/        schema.sql / worker.js / wrangler.toml.example
                   shared_state.py / README.md（云端版参考实现）
BOT_SOUL.example.md  角色定义示例（实例文件本地保存，不入库）
install.sh         systemd 一键安装
bridge.env.example / allowlist.example.json
telegram-bridge-listener.service.example
```
