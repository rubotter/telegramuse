# 云端共享状态（高级功能）— 部署说明

设计见 [../docs/shared-state-cloudflare.md](../docs/shared-state-cloudflare.md)。
本目录是参考实现：

- `schema.sql` — D1 建表（bots / tasks / changes / api_tokens）
- `worker.js` — API 网关（鉴权、只写自己、变更日志、R2 文件代理）
- `wrangler.toml.example` — 部署配置模板
- `shared_state.py` — Bot 侧客户端（标准库，含 CLI）

## 1. 建数据库和存储（群主，在 Cloudflare 免费账号里）

```bash
npx wrangler d1 create bot-shared
npx wrangler d1 execute bot-shared --remote --file schema.sql
npx wrangler r2 bucket create bot-shared-files
```

也可以全部在 Cloudflare 控制台点选完成（D1 控制台可直接粘贴 SQL）。

## 2. 部署 Worker

```bash
cp wrangler.toml.example wrangler.toml   # 填入你的 D1 database_id
npx wrangler secret put ADMIN_TOKEN      # 自设一个长随机串，只群主知道
npx wrangler deploy                      # 得到 https://bot-shared-state.<你>.workers.dev
```

## 3. 给每个 Bot 发 Token（群主操作）

```bash
curl -X POST https://bot-shared-state.<你>.workers.dev/v1/admin/tokens \
  -H 'x-admin-token: <ADMIN_TOKEN>' -H 'content-type: application/json' \
  -d '{"bot_id": "examplebot"}'
# 返回 {"bot_id": "examplebot", "token": "……"} —— 明文只出现这一次
```

把每个 Bot 的 Token 通过安全渠道交给其机主。吊销：同接口改 `DELETE`。

## 4. Bot 侧接入

```bash
export SHARED_STATE_API=https://bot-shared-state.<你>.workers.dev
export SHARED_STATE_TOKEN=<自己的 Token>

python3 shared_state.py push-definition --soul BOT_SOUL.md --name 小哈 --owner YUHUI
python3 shared_state.py sync-roster --out GROUP_ROSTER.md
python3 shared_state.py changes
python3 shared_state.py upload ./report.pdf shared/report.pdf
python3 shared_state.py download shared/report.pdf ./report.pdf
```

日常同步建议：Bot 在被唤醒处理群消息时先
`changes, cursor = poll_changes()`，处理完（如 `sync_roster()`
刷新本地名册缓存成功）再 `ack_changes(cursor)` 确认进度；
处理失败不要 ack，下次会重新拉到同一批变化。游标按 API 地址
分别保存，测试/生产两套后端互不干扰。自己的定义/任务变化时
立即 `push_definition` / `push_task` 写库。

## 权限模型回顾

- Token 哈希存库，Worker 按 Token 认 Bot；明文只在签发时出现一次。
- 写：只能写自己的定义、自己的任务行、R2 的 `bots/<自己>/` 与 `shared/`。
- 读：所有已鉴权 Bot 可读全部定义、任务与文件。
- Cloudflare 账号凭证永远不离开群主手里。
