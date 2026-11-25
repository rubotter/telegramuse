# Watchdog worker 指令模板（看门狗钩子唤醒 agent 时用）

把 `<KIT_DIR>` 替换为套件安装目录后，用作看门狗钩子的 worker 指令。
看门狗钩子本身用 `hook/telegram-bridge-watchdog.sh`，建议轮询间隔 5 秒。

---

Telegram 桥的监听服务 telegram-bridge-listener 当前不活跃（虚拟机/容器替换会抹掉它的 systemd 单元文件，桥会因此静默失效）。

请运行自愈脚本：`bash <KIT_DIR>/telegram_bridge/ensure_listener.sh`

- 输出 OK：什么都不用做，总结里写「监听服务正常」即可。
- 输出 REPAIRED：在总结里明确告诉主代理「监听服务曾掉线，已自动修复并恢复 active」，因为这意味着掉线期间 Telegram 消息可能延迟或丢失，需要主代理提醒机主。
- 输出 FAILED 或脚本报错：在总结里原样报告失败步骤，请主代理转告机主需要人工处理。

不要做脚本之外的其他操作，不要给 Telegram 发消息。
