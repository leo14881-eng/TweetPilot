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

## 部署与配置

服务运行在云服务器 `/opt/tweetpilot`（Docker），代码每 15 分钟自动从 GitHub 更新。

**所有设置都在电脑上改**，不用登录服务器：

1. 第一次：双击 `install-sync.cmd`，装一个 Windows 定时任务 `TweetPilot-Sync`（每 5 分钟运行）。
2. 修改 `config\tweetpilot.env`（模板见 `config.example.env`）：监听名单、检查间隔、是否推回复、Telegram 机器人。
3. 保存后 5 分钟内自动上传到服务器并生效。

| 文件 | 用途 |
|---|---|
| `config\tweetpilot.env` | 全部设置 + Telegram 机器人 token |
| `config\x-auth-token.txt` | X 小号凭证（也可写在 tweetpilot.env 里） |
| `config\server-ip.txt` | 服务器 IP |
| `logs\sync.log` | 同步记录 |
| `app\status.request` | 新建这个空文件 → 5 分钟内把服务器状态写到 `logs\status.txt` |
| `app\sync.request` | 新建这个空文件 → 强制重新同步一次 |

`config\`、`app\`、`logs\` 不进 git。同步只上传 config 里的两个文件、运行固定的安装命令；SSH 密钥按路径引用电脑上 TrendPilot 的密钥，不复制、不修改 TrendPilot。

## 注意

用小号的登录凭证读取推文不符合 X 的服务条款，小号可能被限制或封禁，不要用主号。被封后换一个小号、更新凭证即可。
