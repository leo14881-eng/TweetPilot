# TweetPilot

实时监听 X（推特）账号的新推文，翻译成中文，通过 Telegram 推送，附原帖链接。

- 监听账号：马斯克 @elonmusk、赵长鹏 @cz_binance、孙宇晨 @justinsuntron（`.env` 的 `ACCOUNTS` 可改）
- 每 60 秒检查一次；默认只推主帖，不推回复（`EXCLUDE_REPLIES`）
- 首次启动只记录已有推文，不推送；之后每条新推文只推一次
- 连续 30 分钟读不到推文会提醒（通常是 X 小号登录过期或被限制）
- 免费：不用 X 官方付费 API

## 组成

| 组件 | 作用 |
|---|---|
| RSSHub（Docker） | 用 X 小号的登录凭证读取推文，转成 RSS |
| monitor.py | 轮询 RSS、去重（SQLite）、翻译、推送 Telegram |
| scripts/install.sh | 服务器一键安装 / 更新 / 查看状态 |

## 部署

运行在跑 TrendPilot 的云服务器上，目录 `/opt/tweetpilot`。Telegram 机器人复用 TrendPilot 的配置。

X 小号凭证放在服务器 `/opt/tweetpilot/secret/x-auth-token.txt`（不进 git）。

```bash
bash scripts/install.sh           # 安装或更新并启动
bash scripts/install.sh --status  # 查看状态
```

## 注意

用小号的登录凭证读取推文不符合 X 的服务条款，小号可能被限制或封禁，不要用主号。被封后换一个小号、更新凭证即可。
