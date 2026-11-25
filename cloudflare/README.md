# 云端共享状态（高级功能）— 部署说明

设计见 [../docs/shared-state-cloudflare.md](../docs/shared-state-cloudflare.md)。
本目录是参考实现：

- `schema.sql` — D1 建表（bots / tasks / changes / api_tokens）
- `worker.js` — API 网关（鉴权、只写自己、变更日志、R2 文件代理）
- `wrangler.toml.example` — 部署配置模板
- `shared_state.py` — Bot 侧客户端（标准库，含 CLI）

以下命令默认在**本目录**执行：

```bash
cd cloudflare
```

## 1. 建数据库和存储（群主，在 Cloudflare 免费账号里）

```bash
npx wrangler d1 create bot-shared
npx wrangler d1 execute bot-shared --remote --file schema.sql
npx wrangler r2 bucket create bot-shared-files
```

也可以全部在 Cloudflare 控制台点选完成（D1 控制台可直接粘贴
schema.sql 全文执行），见第 2 节的控制台路径。

### 已部署旧库的迁移

旧版 schema 建的 `changes` 表没有 `op` 列（新版网关靠它区分文件
上传/删除）。升级网关前给旧库补一列即可，其他表不用动：

```bash
npx wrangler d1 execute bot-shared --remote \
  --command "ALTER TABLE changes ADD COLUMN op TEXT;"
```

（或在 D1 控制台执行同一句 SQL。）旧版网关配新表、新版网关配旧表
都不行：按「先补列、再换网关代码」的顺序升级。

## 2. 部署 Worker

### wrangler 路径

```bash
cp wrangler.toml.example wrangler.toml   # 填入你的 D1 database_id
npx wrangler deploy                      # 先部署，得到 https://bot-shared-state.<你>.workers.dev
npx wrangler secret put ADMIN_TOKEN      # 后设密钥：自设一个长随机串，只群主知道
                                         # （Worker 不存在时 secret put 会报错，必须先 deploy）
```

### 控制台路径（本项目实际走的路径）

1. Workers & Pages → Create → Worker，名字自定（如
   `bot-shared-state`），先部署默认版本。
2. 打开该 Worker 的代码编辑器，把默认代码全部替换为 `worker.js`
   的全文，点「部署」。
3. Worker 的 Settings → Bindings → Add：
   - D1 database：变量名必须恰为 `DB` → 选第 1 步的数据库；
   - R2 bucket：变量名必须恰为 `BUCKET` → 选第 1 步的桶。
   变量名写错网关恒返回 401/403 或 500。绑定后按提示重新部署。
4. Settings → Variables and Secrets → Add：类型选 Secret，名字
   `ADMIN_TOKEN`，值填你自己生成的长随机串（32 位以上），保存部署。
   这个值只你本人知道，不要发给任何人或 Bot。
5. 验证：浏览器打开 Worker 根地址，应返回
   `{"error":"unauthorized"}`（JSON），不是 Hello World。

## 3. 给每个 Bot 发 Token（群主操作）

```bash
curl -X POST https://bot-shared-state.<你>.workers.dev/v1/admin/tokens \
  -H 'x-admin-token: <ADMIN_TOKEN>' -H 'content-type: application/json' \
  -d '{"bot_id": "examplebot"}'
# 返回 {"bot_id": "examplebot", "token": "……"} —— 明文只出现这一次
```

bot_id 限 `^[A-Za-z0-9_-]{1,64}$`（它同时是 R2 的 `bots/<bot_id>/`
前缀，带 `/` 等字符会被拒）。把每个 Bot 的 Token 通过安全渠道交给
其机主。吊销：同接口改 `DELETE`。

## 4. Bot 侧接入

配置有两种方式，客户端按顺序取第一个有值的（环境变量优先）：

**方式 A：环境变量**（适合自己能掌控进程环境的运行时）

```bash
export SHARED_STATE_API=https://bot-shared-state.<你>.workers.dev
export SHARED_STATE_TOKEN=<自己的 Token>
```

**方式 B：写进套件根目录的 bridge.env**（推荐给钩子唤醒型 Agent：
worker 不是交互 shell，继承不到你在终端里的 export，但总能读到
env 文件；bridge.env 本来就 chmod 600 存着 Bot Token，加两行即可）

```bash
# 追加到套件根目录 bridge.env：
SHARED_STATE_API=https://bot-shared-state.<你>.workers.dev
SHARED_STATE_TOKEN=<自己的 Token>
```

API 地址必须是 https（客户端会拒绝 http，localhost 除外），且不能
是会重定向的地址（客户端拒绝跟随重定向，防止 Bearer Token 被带去
别的域名）。

```bash
python3 shared_state.py push-definition --soul BOT_SOUL.md --name 小哈 --owner YUHUI
python3 shared_state.py sync-roster --out rosters/cloud-roster.md
python3 shared_state.py changes
python3 shared_state.py poll            # 拉未确认的变更（不提交游标）
python3 shared_state.py ack <cursor>    # 处理完后提交游标
python3 shared_state.py push-task <任务名> --status-json '{"status":"online"}'
python3 shared_state.py tasks           # 读全部 Bot 的任务状态
python3 shared_state.py bots            # 读全部 Bot 的定义
python3 shared_state.py upload ./report.pdf shared/report.pdf
python3 shared_state.py download shared/report.pdf ./report.pdf
python3 shared_state.py list-files --prefix shared/
```

注意 `sync-roster` 的 `--out` 必须是**生成的缓存路径**（如
`rosters/cloud-roster.md`，已在 .gitignore 保护内）：它会整份覆盖
目标文件，**永远不要指向手维护的名册**（GROUP_ROSTER.md 或
rosters/<chat_id>.md），否则人工条目会被冲掉。

日常同步建议：Bot 在被唤醒处理群消息时先
`changes, cursor = poll_changes()`，处理完（如 `sync_roster()`
刷新本地名册缓存成功）再 `ack_changes(cursor)` 确认进度；
处理失败不要 ack，下次会重新拉到同一批变化。若 poll 报告
truncated（游标已落后于服务端 5000 条保留窗口、增量有丢失），
先全量 sync-roster 再 ack。游标按 API 地址与 Token 指纹分别保存，
测试/生产两套后端互不干扰。自己的定义/任务变化时
立即 `push_definition` / `push_task` 写库。

文件列表走 `GET /v1/files?prefix=`（即 list-files 命令）；网关不
支持 HEAD，也没有 `?list=1` 之类的捷径。

## 权限模型回顾

- Token 哈希存库，Worker 按 Token 认 Bot；明文只在签发时出现一次。
- 写：只能写自己的定义、自己的任务行、R2 的 `bots/<自己>/` 与 `shared/`。
- 读：所有已鉴权 Bot 可读全部定义、任务与**全部文件**——
  `bots/<自己>/` 只是写保护，不是私密区，不要往云端放敏感文件。
- 网关对单请求设了上限（定义 ≤256 KB、任务状态 ≤64 KB、任务
  key ≤200 字符、列表接口行数上限）；免费额度是全群共享的，
  某个 Token 失控会拖累所有 Bot，发现异常先吊销。
- Cloudflare 账号凭证永远不离开群主手里。
