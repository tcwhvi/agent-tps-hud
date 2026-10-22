## 下载即用 / Download and run

Windows 10/11。下载 `agent-tps-hud.exe` 双击运行，不需要安装 Python。
如果 SmartScreen 提示"已保护你的电脑"，点 "More info → Run anyway" —— 个人项目没有代码签名证书，源码在仓库里，也欢迎自己 `pythonw tps_overlay.py` 核对。

Win10/11 only. Download `agent-tps-hud.exe` and double-click — no Python needed. The binary is unsigned (individual projects can't get a code-signing certificate), so SmartScreen warns once. The source is in this repo.

## v0.4.0：曲线不再被新会话清空

之前曲线只反映"当前最新那一个会话"里的轮次：新开一个会话，旧会话攒下的点会被整条覆盖，看起来就像"曲线只剩最后一个点"。现在：

- 曲线跨会话保留，滚动窗口仍是最近 24 轮；
- 新会话的第一个点前画一条细竖线作会话分隔，不同会话不会混成一段看不出来的折线；
- 每家的 `Reading` 带上真正的会话标识（ZCode `session_id`、MiMo `session_id`、Qoder / WorkBuddy 的 transcript 路径），不再拿轮次时间戳当会话号；
- 新增 `HistoryTrack` 及其单元测试。

限制没变：每家仍只显示当前最新活跃会话的实时数字——同时开国内版和国际版 Qoder 时，谁刚有动作谁占那一行（曲线会保留前一个会话的点）。

## 口径 / Metric

```text
TPS = Σ(输出 token，含思考) / Σ(模型请求耗时，含 TTFT)
```

不含本地工具执行时间；一轮内的多次模型请求全部计入；用户插话立即结束上一轮。

只读本机会话数据，没有任何网络请求，不上传 prompt 或回复内容。
