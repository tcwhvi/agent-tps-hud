# Agent TPS HUD

[![Release](https://img.shields.io/github/v/release/tcwhvi/agent-tps-hud)](https://github.com/tcwhvi/agent-tps-hud/releases)

> A tiny always-on-top Windows HUD that shows output tokens/sec for ZCode, Qoder, WorkBuddy and MiMo Desktop — locally, without proxies or API keys.

[中文](README.md)

![Agent TPS HUD](docs/hero.png)

## Download and run

Grab `agent-tps-hud.exe` from [Releases](https://github.com/tcwhvi/agent-tps-hud/releases/latest) and double-click it. No Python, no configuration.

It only reads local session data and makes no network requests; closing the window quits. If SmartScreen says "Windows protected your PC" on first launch, that is because the binary has no code-signing certificate (individual projects can't get one) — choose "More info → Run anyway". The source is right here, so `pythonw tps_overlay.py` works too.

## What it does

It reads local session data (SQLite / JSONL), shows the latest completed turn's TPS for each agent, and keeps a small sparkline of recent turns. It does not modify model settings, intercept traffic, or require API keys.

The HUD itself is 190 × 128:

![Native size](docs/screenshot.png)

## Metric

**One turn** = one user prompt + all model requests and local tool calls until the agent stops.

```text
TPS = sum(output + thinking tokens) / sum(model request duration)
```

- Model request duration includes TTFT.
- Local tool execution time is excluded.
- All model requests inside a turn are included (not just the last one).
- A new user prompt immediately closes the previous turn and adds it to the sparkline; while the new turn is generating, the HUD keeps showing the interrupted turn's TPS instead of staying on “generating”.
- Big number = latest completed turn.
- Sparkline = last 24 turns; the rightmost point is the latest turn.

## Supported agents

| Tag | Agent | Local source | Token source |
|---|---|---|---|
| Z | ZCode | `~/.zcode/cli/db/db.sqlite` (`turn_usage` / `model_usage` / `message.tokens`) | provider output (includes reasoning) |
| Q | Qoder | `~/.qoder-cn/projects/**/*.jsonl` + `%APPDATA%/QoderCN/SharedClientCache/cache/db/local.db` | CLI text estimate (`≈`); IDE sessions use exact `chat_message.token_info` |
| W | WorkBuddy | `~/.workbuddy/projects/**/*.jsonl` | provider output (includes reasoning) |
| M | MiMo Desktop / MiMo Code | `~/.local/share/mimocode/mimocode.db` (`message` / `part`) | exact output + reasoning |

Colors: Z blue, Q purple, W yellow, M green.

## Run from source

Windows 10/11, Python 3.10+ (Tkinter included), no third-party dependencies. Use this if you would rather not run the release binary:

```bat
start_tps_overlay.bat
```

or:

```bat
pythonw tps_overlay.py
```

One-shot debug output:

```bat
python tps_overlay.py --once
```

## Controls

- Drag anywhere to move.
- Short left-click or right-click opens the stats popup; press and drag still moves the HUD.
- While generating, the number keeps the previous turn's TPS and blinks slowly instead of showing “generating”.
- Drop within 14px of a screen edge to hide, leaving a 5px accent strip.
- Hover the strip to expand; move away for 0.6s to hide again.
- Drag away from the edge to disable auto-hide.
- Click `×` to quit.

## Stats popup

Short left-click or right-click the HUD to inspect historical sessions:

- Switch between today / 7 days / 30 days.
- The big number is output tokens (the TPS basis); IN / CACHE show input and cached tokens separately, only for clients that expose provider usage.
- See overall TPS and session count for the period.
- List each session's output tokens and TPS by time.
- Scroll with the wheel or the thin bar on the right; scrolling stops at the top and bottom.
- Today's data is preloaded at startup, so the first open is usually instant.
- While open, today's data refreshes silently about every 60 seconds; 7-day / 30-day data refreshes on switching, reopening or clicking `↻`.
- Data is cached after reading, so reopening is instant.
- Session files are cached in memory by mtime/size, so unchanged files are not parsed again; the first scan after a restart still rebuilds the cache.

![stats](docs/stats.png)

## Accuracy and privacy

- MiMo is the most accurate (exact step tokens).
- ZCode / WorkBuddy use the provider's output token count directly; it already includes thinking tokens, so reasoning is not added twice.
- Qoder CLI transcripts have no usage tokens, so their value is an estimate prefixed with `≈`. Qoder IDE sessions can use exact `chat_message.token_info` completion tokens and `chat_session` titles. CLI session names use the first 40 characters of the first user message and stay local.
- Tokenizers and billing rules differ across vendors. Use this for trends and same-model comparisons, not as an official benchmark.
- Read-only local metadata only. No prompts/responses are stored or uploaded. No network requests. Qoder reads text lengths for its estimate, truncates the first user message into a local-only session name, or reads the IDE's own title; none is written to disk. SQLite reads fall back to a temporary snapshot when the source database is write-locked.

## Limitations

- Windows only; edge hiding uses the primary monitor work area.
- Local formats may change between client versions.
- Only the latest session per agent is read.
- Not affiliated with ZCode, Qoder, WorkBuddy, MiMo or their vendors.

## License

MIT. If this is useful, a star is appreciated.
