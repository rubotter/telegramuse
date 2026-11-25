# 高级功能：公共数据库 + 对象存储共享状态（Cloudflare 免费实现）

多 Bot 同群协作的进阶形态：不再靠群里互相广播同步，而是所有 Bot
对接同一套云端存储——**数据库是唯一事实来源**，对象存储传公共文件。
本地名册降级为缓存。此功能可选，单 Bot 或小群用本地文件版足够。

## 选型：Cloudflare 三件套（全免费额度内）

| 组件 | 用途 | 免费额度（2026-11 查官方定价页） |
|---|---|---|
| **D1**（SQLite 数据库） | 角色定义、任务状态、变更日志 | 读 500 万行/天、写 10 万行/天、存储 5 GB |
| **R2**（对象存储） | Bot 之间传公共文件 | 10 GB 存储、写 100 万次/月、读 1000 万次/月、**出站流量免费** |
| **Workers**（API 网关） | 唯一入口，鉴权与权限隔离 | 10 万请求/天 |

按几个 Bot、每分钟轮询一次估算：每天约几万次读、几百次写，
只用掉免费额度的零头。注意两者的超额行为不同：**D1 免费版**
超额后查询直接报错（停服不扣费）；**R2 是按量计费产品**，
超出免费额度按价收费（存储 $0.015/GB·月等），且启用 R2 需要
绑定支付方式——用量虽小，账单责任在账号主，部署后建议偶尔
看一眼用量页。

## 架构

```
        ┌────────────── Cloudflare（群主的一个账号）──────────────┐
        │  Worker（API 网关）── D1（定义/任务/变更日志）           │
        │        └──────────── R2（公共文件 bucket）               │
        └──────────────▲──────────────────▲───────────────────────┘
                  每 Bot 一个独立 API Token（Bearer）
              ┌────────┴───┐        ┌─────┴──────┐
              │  Bot A     │        │  Bot B     │   ……
              │ 本地缓存=名册副本 │ 本地缓存=名册副本 │
              └────────────┘        └────────────┘
```

**关键安全原则：任何 Bot 都不拿 Cloudflare 账号凭证。** D1 和 R2
只绑在 Worker 上；每个 Bot 只持自己的 API Token，Worker 按
Token → bot_id 映射强制权限：

- 只能写**自己**的定义行、自己的任务行、R2 里 `bots/<bot_id>/` 前缀
- 可以读全部 Bot 的定义/任务、R2 的 `shared/` 公共前缀
- Token 由群主（Cloudflare 账号主）签发和吊销；泄露只影响该 Bot 自己

## 数据模型（D1）

```sql
CREATE TABLE bots (
  bot_id      TEXT PRIMARY KEY,        -- 如 examplebot
  display_name TEXT NOT NULL,
  owner       TEXT,
  definition  TEXT NOT NULL,           -- SOUL 全文（JSON 或 Markdown）
  version     INTEGER NOT NULL DEFAULT 1,
  updated_at  TEXT NOT NULL            -- ISO 时间
);
CREATE TABLE tasks (
  bot_id TEXT NOT NULL, task_key TEXT NOT NULL,
  status TEXT NOT NULL,                -- JSON：进度/状态/备注
  updated_at TEXT NOT NULL,
  PRIMARY KEY (bot_id, task_key)
);
CREATE TABLE changes (                  -- 变更日志，供增量同步
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  bot_id TEXT NOT NULL, kind TEXT NOT NULL,  -- definition | task | file
  ref TEXT, changed_at TEXT NOT NULL
);
CREATE TABLE api_tokens (               -- Token 只存 SHA-256 哈希
  token_hash TEXT PRIMARY KEY,
  bot_id TEXT NOT NULL, created_at TEXT NOT NULL
);
-- 完整建表以 cloudflare/schema.sql 为准（含索引），部署时直接用它，
-- 不要照抄本页片段。
```

## API（Worker 提供，示例）

- `PUT /v1/me/definition` — 更新自己的角色定义（version+1，写变更日志）
- `PUT /v1/me/tasks/<key>` — 更新自己的任务状态
- `GET /v1/bots` — 拉全部 Bot 的定义（同步名册用）
- `GET /v1/changes?since=<seq>` — 增量拉变更，返回新 seq 游标
- `PUT /v1/files/<path>` / `GET /v1/files/<path>` — R2 文件读写
  （自己的前缀可写，`shared/` 全员可读、按约定可写）

## 同步流程

1. Bot 改了自己的定义/任务 → 调 API 写库（顺带写一条 changes）。
2. 其他 Bot 定时（或被群消息唤醒时）调 `/changes?since=` 增量拉取，
   有变化才更新本地名册缓存。
3. 可选混合模式：写库后在群里发一句「定义已更新 v3」当唤醒信号，
   数据库仍是事实来源——群消息丢了也不怕，下次轮询自动补齐。
4. 群主触发「状态对齐」时，不再逐个发群消息，直接各自查库汇报。

## 部署步骤（群主操作）

1. 注册/登录 Cloudflare（免费），创建 D1 数据库并执行上面的建表 SQL。
2. 创建 R2 bucket（如 `bot-shared`）。
3. 部署 Worker（绑定 D1、R2；代码见套件后续版本），设置每个 Bot 的
   Token（Worker 环境变量或 KV 存 Token 哈希 → bot_id 映射）。
4. 把 API 地址和各自 Token 分发给每个 Bot 的机主（Token 走安全渠道，
   不进群、不进仓库），各 Bot 在本地配置里填好即可。

## 与本地文件版的关系

- 本地版（SOUL + 名册文件 + 群内对齐）是基础形态，零依赖。
- 云端版是同一套语义的升级：定义的内容结构不变，只是存放与同步
  换成数据库；Bot 侧逻辑从「听群消息更新名册」变成「查库更新缓存」。
- 离线/云端故障时 Bot 仍可用本地缓存工作，恢复后按 version 对齐。
