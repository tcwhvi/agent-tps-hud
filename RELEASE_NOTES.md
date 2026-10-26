## 下载即用 / Download and run

Windows 10/11。下载 `agent-tps-hud.exe` 双击运行，不需要安装 Python。
如果 SmartScreen 提示"已保护你的电脑"，点 "More info → Run anyway" —— 个人项目没有代码签名证书，源码在仓库里，也欢迎自己 `pythonw tps_overlay.py` 核对。

Win10/11 only. Download `agent-tps-hud.exe` and double-click — no Python needed. The binary is unsigned (individual projects can't get a code-signing certificate), so SmartScreen warns once. The source is in this repo.

## v0.4.3 修复：WorkBuddy 生成中不闪烁、ZCode 长工具链闪烁中断

- **WorkBuddy 正在输出却不闪烁**：两条闪烁信号此前同时失效。运行中的会话在 `sessions` 表里的状态是 `working`，代码只认 `active`，数据库信号永远为假；流式期间 jsonl 还会写入不带 usage 的中间 assistant 消息（工具调用前的输出），任何 assistant 行都会把轮次标记成已完成，文件侧的"进行中"信号也随之失效。现在按 `working` 判定（兼容 `active`），并且只有带 usage 的最终消息才闭合轮次；首条回复尚未产生 usage 时也会闪。
- **ZCode 长工具链闪烁中途熄灭**：生成中判定此前从"最近 5 条消息"里找未完成的 assistant 消息，而工具结果也是消息行——实测一轮里出现过 assistant 之后连续 7 条工具结果，第 5 次工具调用后打开中的 assistant 消息就被挤出窗口。现在先按角色筛出 assistant 再取最新。
- **没有 `part` 表的旧库不再整行报"读取失败"**：part 新鲜度查询失败时自动降级为只看消息时间。

新增 5 个用例：连续工具结果挤不掉窗口、无 part 表降级、中间 assistant 消息不闭合轮次、最终消息闭合轮次、`working` 状态在首条回复前就闪烁。

## v0.4.2 修复：生成中状态两头都不对

- **ZCode 正在输出却不闪烁**：轮次表 `turn_usage` 只在轮次结束时才写入，状态永远只有 `completed/cancelled/error`，所以旧判定 `status not in (…)` 恒为假 —— 这一行的闪烁其实从来没工作过。现在改看 `message` 表：最新的 assistant 消息没有 `time.completed` 就是在生成中。
- **MiMo 已退出却一直在闪**：旧判定是"最后一轮 `finish != stop`"，客户端中途退出就永远停在"生成中"。现在加新鲜度上限：消息和它的 `part` 都超过 120 秒没动就停止闪烁（120 秒沿用 WorkBuddy 已有的窗口）。
- 长回答不会被误停：assistant 消息行的 `time_updated` 在创建后就不动了，所以时钟取"消息与 part 的最新时间"，实测单次模型请求跑 117 秒也不会被误判成已停。

新增 9 个用例覆盖这套判定（含"只看消息时间就会漏掉长回答"这一条）。

## v0.4.1 修复：贴边隐藏后第一次悬停没反应

`wm geometry` 移动请求会被 Tk 静默丢弃（窗口长时间没位移后的第一次请求最容易发生），旧代码发完请求就把状态改成"已展开"，于是程序以为动了、窗口其实没动，也不会再发第二次。现在记下目标位置，在 80ms 心跳里核对窗口真实坐标，没到位就重发，到位后停止。同时修了隐藏态左键只展开不弹统计窗，并把悬停心跳的 `after` 重排移进 `finally`。

## v0.4.0 曲线跨会话

曲线不再被新会话清空：会话切换时保留上一个会话的点，滚动窗口仍是最近 24 轮，新会话第一个点前画一条细竖线作分隔。

## 口径 / Metric

```text
TPS = Σ(输出 token，含思考) / Σ(模型请求耗时，含 TTFT)
```

不含本地工具执行时间；一轮内的多次模型请求全部计入；用户插话立即结束上一轮。

只读本机会话数据，没有任何网络请求，不上传 prompt 或回复内容。
