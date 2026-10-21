## 下载即用 / Download and run

Windows 10/11。下载 `agent-tps-hud.exe` 双击运行，不需要安装 Python。
如果 SmartScreen 提示"已保护你的电脑"，点 "More info → Run anyway" —— 个人项目没有代码签名证书，源码在仓库里，也欢迎自己 `pythonw tps_overlay.py` 核对。

Win10/11 only. Download `agent-tps-hud.exe` and double-click — no Python needed. The binary is unsigned (individual projects can't get a code-signing certificate), so SmartScreen warns once. The source is in this repo.

## 新增 / What's new

- 悬浮窗标题与弹窗头部显示版本号 / version shown in the HUD title
- 左键短按或右键打开统计弹窗：今天 / 7 天 / 30 天，按时间列出每个会话的 token 与 TPS
- OUT / IN / CACHE 三类 token 分列统计，不再把输入算进输出
- 生成期间不再显示"生成中"，改为上一轮 TPS 缓慢闪烁
- 启动预读 + 结果缓存，重复打开秒显；会话文件按 mtime / size 增量解析
- Qoder IDE 会话改用本地 `chat_message.token_info` 真值；CLI / Desktop transcript 无 usage，仍是估算并标 `≈`
- 修正 token 重复计数：provider 返回的 output 已含 reasoning，不再额外叠加估算
- 自绘细滚动条，滚到顶 / 底不再空滚

## 口径 / Metric

```text
TPS = Σ(输出 token，含思考) / Σ(模型请求耗时，含 TTFT)
```

不含本地工具执行时间；一轮内的多次模型请求全部计入；用户插话立即结束上一轮。

只读本机会话数据，没有任何网络请求，不上传 prompt 或回复内容。
