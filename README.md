# daily-mail-automation

每天自动发送两封邮件：

1. **高中作文素材**：北京时间 05:30 / 08:30 / 11:30 / 14:30 / 17:30 / 20:30 / 23:30，每次 1 位人物。
2. **2027 高考倒计时**：北京时间每天 05:30，标题包含倒计时、日期、阶段提示、实时教育热点与生日提醒。

模型：`LongCat-2.5`（通过 LongCat OpenAI 兼容接口调用）。

## 目录

```text
scripts/essay_hourly.py        作文素材邮件
scripts/gaokao_countdown.py    高考倒计时邮件
state/essay_weekly_state.json  本周古代人物比例状态
state/essay_recent_people.json 近期人物去重历史
.github/workflows/              GitHub Actions 定时任务
```

## 定时方式

**准点发送由本地 Termux cron 负责**（见下方“本地运行”）。GitHub Actions 只保留手动 `workflow_dispatch` 作为备用。

原因：GitHub 原生 `schedule` 是 best-effort，高负载时可能延迟数小时甚至丢弃，无法保证“每天 05:30 准点”。

备用手动触发（在仓库 Actions 页面点 Run workflow，或）：

```bash
gh workflow run essay.yml        --repo kxxhhh/daily-mail-automation
gh workflow run gaokao.yml        --repo kxxhhh/daily-mail-automation
```

本地 crontab（北京时间 UTC+8）：

```cron
30 5-23/3 * * * python3 $HOME/scripts/essay_hourly.py >> $HOME/scripts/essay.log 2>&1
30 5 * * *     python3 $HOME/scripts/gaokao_countdown.py >> $HOME/scripts/gaokao.log 2>&1
```

> 依赖 Termux 进程存活；需保持 `termux-wake-lock` 与电池优化“不受限制”。

## 必需的 Repository Secrets

在 `Settings -> Secrets and variables -> Actions` 中添加：

| 名称 | 说明 |
|---|---|
| `SMTP_HOST` | SMTP 服务器，例如 `smtp.qq.com` |
| `SMTP_PORT` | SSL 端口，例如 `465` |
| `SENDER` | 发件邮箱 |
| `AUTH_CODE` | 邮箱授权码 |
| `RECEIVER` | 收件邮箱 |
| `LONGCAT_API_KEY` | LongCat API Key |

密钥只通过环境变量注入，不写入仓库文件，也不会出现在日志中。

## 本地运行

```bash
pip install -r requirements.txt

export SMTP_HOST=smtp.qq.com
export SMTP_PORT=465
export SENDER=you@example.com
export AUTH_CODE=your-auth-code
export RECEIVER=to@example.com
export LONGCAT_API_KEY=your-longcat-key

python scripts/essay_hourly.py --force
python scripts/gaokao_countdown.py
```

本地 Termux 也可以继续使用 `scripts/essay.env`（已被 `.gitignore` 忽略）。

## 状态持久化

作文任务每次发送成功后会把 `state/` 提交回仓库，用于：

- 每周古代人物占比控制；
- 最近 40 位人物去重。

倒计时任务无状态。

## 安全

- 不要提交 `essay.env`、API Key 或邮箱授权码。
- 如密钥曾出现在聊天、日志或提交历史中，请立即吊销并重新生成。
