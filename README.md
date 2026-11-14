# daily-mail-automation

每天自动发送两封邮件：

1. **高中作文素材**：北京时间 05:30 / 08:30 / 11:30 / 14:30 / 17:30 / 20:30 / 23:30，每次 1 位人物。
2. **2027 高考倒计时**：北京时间每天 05:30，标题包含倒计时、日期、阶段提示、实时教育热点与生日提醒。

模型：智谱 `glm-4.7-flash`（通过智谱开放平台 OpenAI 兼容接口调用）。

## 目录

```text
scripts/essay_hourly.py        作文素材邮件
scripts/gaokao_countdown.py    高考倒计时邮件
state/essay_weekly_state.json  本周古代人物比例状态
state/essay_recent_people.json 近期人物去重历史
.github/workflows/              GitHub Actions 定时任务（含 keepalive 保活）
```

## 定时方式

**GitHub Actions 自愈式定时**（`schedule` 多触发 + 脚本时段闸门，另保留 `workflow_dispatch` 手动备用）。

GitHub 原生 `schedule` 是 best-effort，可能延迟数分钟、甚至偶发丢失。为做到“到点必达、绝不重复”，两个工作流采用同一套自愈模式：

| 触发时刻（UTC） | 作用 |
|---|---|
| HH:20 主触发 | 等待到北京时间 HH:30 精确发送（触发延迟只要 < 10 分钟，邮件仍准点） |
| HH:35 备份 1 | 主触发丢失/失败时补发，最多延迟 5 分钟 |
| HH:50 备份 2 | 前两次都丢时再补发，最多延迟 20 分钟 |

防重复：脚本内有**时段闸门**（同一北京时间小时只发一封，状态写入 `state/` 并提交回仓库），备份触发命中闸门会直接跳过；`concurrency` 排队保证备份不会与主发送并行。`--force` / 手动输入 `force=true` 可绕过闸门（测试用）。

保活：GitHub 会在仓库连续 60 天无提交后自动禁用定时工作流，`keepalive.yml` 每周自动打一个空提交（`[skip ci]`，不触发其他工作流）。

手动触发（在仓库 Actions 页面点 Run workflow，或）：

```bash
gh workflow run essay.yml     --repo kxxhhh/daily-mail-automation
gh workflow run gaokao.yml    --repo kxxhhh/daily-mail-automation
gh workflow run keepalive.yml --repo kxxhhh/daily-mail-automation
# 测试时可在 Run workflow 页面勾选 force（绕过时段闸门）
```

可选：本地 Termux cron 仍可继续作为备份（北京时间 UTC+8）：

```cron
30 5-23/3 * * * python3 $HOME/scripts/essay_hourly.py >> $HOME/scripts/essay.log 2>&1
30 5 * * *     python3 $HOME/scripts/gaokao_countdown.py >> $HOME/scripts/gaokao.log 2>&1
```

> 本地备份依赖 Termux 进程存活；需保持 `termux-wake-lock` 与电池优化“不受限制”。

## 必需的 Repository Secrets

在 `Settings -> Secrets and variables -> Actions` 中添加：

| 名称 | 说明 |
|---|---|
| `SMTP_HOST` | SMTP 服务器，例如 `smtp.qq.com` |
| `SMTP_PORT` | SSL 端口，例如 `465` |
| `SENDER` | 发件邮箱 |
| `AUTH_CODE` | 邮箱授权码 |
| `RECEIVER` | 收件邮箱 |
| `ZHIPU_API_KEY` | 智谱开放平台 API Key |

密钥只通过环境变量注入，不写入仓库文件，也不会出现在日志中。

## 本地运行

```bash
pip install -r requirements.txt

export SMTP_HOST=smtp.qq.com
export SMTP_PORT=465
export SENDER=you@example.com
export AUTH_CODE=your-auth-code
export RECEIVER=to@example.com
export ZHIPU_API_KEY=your-zhipu-key

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
