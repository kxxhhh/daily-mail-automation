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

**GitHub Actions 巡检式调度器**（`dispatcher.yml` 每 5 分钟 tick + 脚本时段闸门），手动 `workflow_dispatch` 仅作备用/测试。

GitHub 原生 `schedule` 是 best-effort，实测会整段丢弃触发点（2026-11-14 北京 20:30 时段的三个触发点全部未创建 run）。因此不再把发送时间赌在某个 cron 点上，改为**高频巡检 + 到点判定**：

- `dispatcher.yml` 每 5 分钟 tick 一次；tick 内计算北京时间，自行判断是否该发：
  - tick 落在 **:20-:29** → 精确等到 :30 发送（准点）
  - tick 落在 **:30-:59** 且闸门未命中 → 立即补发（最多晚 29 分钟）
  - tick 落在 **:00-:19** → 跳过（后续 tick 处理）
- 每个发送时段有 **8 个冗余 tick**，只要任意一个被调度到就能发出；单个/多个 tick 丢失会自动自愈。
- **防重复**：脚本时段闸门（同一北京小时只发一封，状态写入 `state/` 并提交回仓库）+ `concurrency` 排队（后续 tick 用最新仓库状态判断）。
- 发送任务：作文时段 = 北京 05/08/11/14/17/20/23 时的 :30；高考倒计时 = 北京每天 05:30。
- 发送失败自动重试 3 次（指数退避）；失败时段的闸门不会写入，后续 tick 会继续重试到时段结束。

保活：GitHub 会在仓库连续 60 天无提交后自动禁用定时工作流，`keepalive.yml` 每周自动打一个空提交（`[skip ci]`，不触发其他工作流）。

手动触发（在仓库 Actions 页面点 Run workflow，或）：

```bash
gh workflow run dispatcher.yml --repo kxxhhh/daily-mail-automation
gh workflow run essay.yml     --repo kxxhhh/daily-mail-automation   # 手动直接发作文
gh workflow run gaokao.yml    --repo kxxhhh/daily-mail-automation   # 手动直接发倒计时
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
