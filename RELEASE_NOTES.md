## 下载即用 / Download and run

Windows 10/11。下载 `agent-tps-hud.exe` 双击运行，不需要安装 Python。
如果 SmartScreen 提示"已保护你的电脑"，点 "More info → Run anyway" —— 个人项目没有代码签名证书，源码在仓库里，也欢迎自己 `pythonw tps_overlay.py` 核对。

Win10/11 only. Download `agent-tps-hud.exe` and double-click — no Python needed. The binary is unsigned (individual projects can't get a code-signing certificate), so SmartScreen warns once. The source is in this repo.

## v0.4.1 修复：贴边隐藏后第一次悬停没反应

现象：HUD 贴边缩成 5px 提示条后，隔一段时间第一次把鼠标压上去，窗口纹丝不动；移开再压第二次才弹出来。

根因：`wm geometry` 移动请求会被静默丢弃（实测在窗口长时间没有位移之后第一次最容易发生，间隔越久越稳定复现），而代码在发出请求后立刻把内部状态改成"已展开"，于是程序以为展开了、窗口其实没动，也就再没有第二次请求。

修法：发出移动请求后记录目标位置，在 80ms 的心跳里核对窗口真实坐标是否到位，没到位就重发，到位后停止。真窗口验证：连续丢掉 3 次请求，仍能在 0.26 秒内落位；落位后不再产生多余的移动调用。

同时修掉：左键压在 5px 提示条上只展开、不弹统计窗（`ButtonRelease` 在 `dragging=False` 时直接 return 了）。现在隐藏状态下左键和右键一样，展开 + 弹统计。

另外把悬停心跳的 `after` 重排移进 `finally`：之前只要心跳函数里抛一次异常，悬停和自动隐藏会一起永久停摆。

## v0.4.0 曲线跨会话

曲线不再被新会话清空：会话切换时保留上一个会话的点，滚动窗口仍是最近 24 轮，新会话第一个点前画一条细竖线作分隔。

## 口径 / Metric

```text
TPS = Σ(输出 token，含思考) / Σ(模型请求耗时，含 TTFT)
```

不含本地工具执行时间；一轮内的多次模型请求全部计入；用户插话立即结束上一轮。

只读本机会话数据，没有任何网络请求，不上传 prompt 或回复内容。
