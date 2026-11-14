#!/data/data/com.termux/files/usr/bin/python3
"""2027 高考倒计时每日邮件：所有内容写在标题里。

标题内容动态生成：
  - 日期与倒计时阶段（按月、按剩余天数、按星期变化）
  - 当日实时热点（教育新闻，抓取失败自动跳过）
  - 生日提醒（本人 4/28、父亲 2/13、母亲农历 07/04）
"""

import logging
import os
import re
import smtplib
import ssl
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from pathlib import Path

try:  # 农历换算；缺失时母亲生日提醒自动跳过，不影响发信
    from lunardate import LunarDate
except Exception:  # pragma: no cover
    LunarDate = None

BJT = timezone(timedelta(hours=8))
SCRIPT_DIR = Path(__file__).resolve().parent
ENV_FILE = SCRIPT_DIR / "essay.env"
LOG_FILE = SCRIPT_DIR / "gaokao.log"
EXAM_DATE = date(2027, 6, 7)

REQUIRED_ENV = ("SMTP_HOST", "SMTP_PORT", "SENDER", "AUTH_CODE", "RECEIVER")
ENV_KEYS = REQUIRED_ENV
HOT_TIMEOUT = 8
HOT_MAX_CHARS = 22
HOT_MAX_AGE_HOURS = 72
SUBJECT_WARN_LEN = 120

# 公历生日：(称呼, 月, 日)
SOLAR_BIRTHDAYS = (
    ("你", 4, 28),
    ("父亲", 2, 13),
)
MOTHER_LUNAR = (7, 4)  # 母亲农历七月初四
BIRTHDAY_LOOKAHEAD_DAYS = 30

MONTH_HINT = {
    1: "期末月：先保课内，再谈拓展",
    2: "寒假：弯道期，作息别乱",
    3: "一模季：按考试节奏做整套卷",
    4: "二模季：限时训练，练取舍",
    5: "三模季：回归真题与错题",
    6: "临考月：调状态，保睡眠",
    7: "暑假：补基础，别盲目赶进度",
    8: "暑假：提前进入一轮节奏",
    9: "开学月：先定目标与周计划",
    10: "十月：把弱科拉到及格线以上",
    11: "期中季：复盘试卷，查漏补缺",
    12: "年末：一轮收尾，别留知识死角",
}

WEEKDAY_HINT = (
    "周一：先立本周三个小目标",
    "周二：把最弱一科排进今天",
    "周三：限时做一套专项",
    "周四：整理本周错题",
    "周五：给这周做个小复盘",
    "周六：半天休息，半天补漏",
    "周日：早睡，准备新一周",
)

MILESTONES = {
    300: "倒计时300天，基础优先",
    200: "倒计时200天，进入一轮收尾",
    100: "百日冲刺从今天开始",
    50: "最后50天，主攻错题",
    30: "最后30天，稳住节奏",
    10: "最后10天，调作息保状态",
    1: "明天开考，早点睡",
}

HOT_KEYWORDS = (
    "高考",
    "中考",
    "招生",
    "考试",
    "教育部",
    "校园",
    "学生",
    "教师",
    "高校",
    "教育",
    "开学",
    "寒假",
    "志愿",
    "专业",
    "就业",
    "课",
)


class BJTFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        return datetime.fromtimestamp(record.created, BJT).strftime(datefmt or "%Y-%m-%d %H:%M:%S")


def setup_logging():
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    fmt = BJTFormatter("%(asctime)s %(levelname)s %(message)s")
    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    logger.addHandler(sh)


def load_env():
    values = {}
    if ENV_FILE.is_file():
        for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    for key in ENV_KEYS:
        env_value = os.environ.get(key)
        if env_value:
            values[key] = env_value.strip()
    missing = [k for k in REQUIRED_ENV if not values.get(k)]
    if missing:
        raise RuntimeError(f"配置缺少字段: {', '.join(missing)}")
    return values


def _fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Linux; Android 15)"})
    with urllib.request.urlopen(req, timeout=HOT_TIMEOUT) as resp:
        return resp.read()


def _rss_items(url):
    root = ET.fromstring(_fetch(url))
    channel = root.find("channel")
    if channel is None:
        return []
    items = []
    for node in channel.findall("item"):
        title = re.sub(r"\s+", " ", (node.findtext("title") or "")).strip()
        if not title:
            continue
        published = None
        try:
            published = parsedate_to_datetime(node.findtext("pubDate") or "")
            if published.tzinfo is None:
                published = published.replace(tzinfo=BJT)
            published = published.astimezone(BJT)
        except Exception:
            published = None
        items.append((published, title))
    return items


def _is_fresh(published):
    if published is None:
        return False
    return timedelta(0) <= (datetime.now(BJT) - published) <= timedelta(hours=HOT_MAX_AGE_HOURS)


def _condense(title):
    text = re.sub(r"\s+", " ", title).strip()
    text = re.sub(r"^【[^】]*】", "", text).strip()
    if "：" in text:
        left, right = text.split("：", 1)
        if len(left) <= 10 and any(k in left for k in ("观察", "关注", "聚焦", "专访", "调查", "评论", "一线")):
            text = right
        elif len(right) >= 8:
            text = right
    text = text.rstrip("？?。！!，,；;：:")
    for sep in ("如何", "何以", "为何", "是否"):
        if sep in text:
            text = text.split(sep, 1)[0]
    text = text.strip("，,；;：: ")
    if not text:
        return ""
    if len(text) > HOT_MAX_CHARS:
        text = text[:HOT_MAX_CHARS].rstrip("，,；;：: ") + "…"
    return text


def fetch_hot_topic():
    """抓取当日教育热点；任何失败都只记日志，不影响发信。"""
    feeds = (
        ("中国新闻网·教育", "https://www.chinanews.com.cn/rss/edu.xml"),
        ("中国新闻网·滚动", "https://www.chinanews.com.cn/rss/scroll-news.xml"),
    )
    for source, url in feeds:
        try:
            items = _rss_items(url)
        except Exception as exc:
            logging.info("热点源不可用（%s）：%s", source, exc)
            continue
        fresh = [(pub, title) for pub, title in items if _is_fresh(pub)]
        if not fresh:
            logging.info("热点源无近期条目（%s）", source)
            continue
        fresh.sort(key=lambda x: x[0], reverse=True)
        matched = [x for x in fresh if any(k in x[1] for k in HOT_KEYWORDS)]
        chosen = (matched or fresh)[0][1]
        condensed = _condense(chosen)
        if condensed:
            logging.info("热点已选用（%s）：%s", source, condensed)
            return condensed
    logging.info("热点：无可用条目，标题仅按日期生成")
    return ""


def _next_solar(today, month, day):
    try:
        candidate = date(today.year, month, day)
    except ValueError:
        return None
    if candidate < today:
        try:
            candidate = date(today.year + 1, month, day)
        except ValueError:
            return None
    return candidate


def _next_lunar(today, month, day):
    if LunarDate is None:
        return None
    for lunar_year in (today.year - 1, today.year, today.year + 1):
        try:
            solar = LunarDate(lunar_year, month, day).to_solar_date()
        except Exception:
            continue
        if solar >= today:
            return solar
    return None


def upcoming_birthdays(today):
    items = []
    for label, month, day in SOLAR_BIRTHDAYS:
        solar = _next_solar(today, month, day)
        if solar:
            items.append((label, solar, "solar"))
    mother = _next_lunar(today, *MOTHER_LUNAR)
    if mother:
        items.append(("母亲", mother, "lunar"))
    if LunarDate is None:
        logging.warning("未安装 lunardate，母亲农历生日提醒已跳过")
    return sorted(items, key=lambda item: item[1])


def birthday_note(today):
    notes = []
    for label, solar, kind in upcoming_birthdays(today):
        delta = (solar - today).days
        if delta == 0:
            if label == "你":
                notes.append("今天是你生日，生日快乐")
            elif kind == "lunar":
                notes.append(f"今天是{label}生日（农历七月初四），别忘了问候")
            else:
                notes.append(f"今天是{label}生日，记得说声生日快乐")
        elif delta <= BIRTHDAY_LOOKAHEAD_DAYS:
            if label == "你":
                notes.append(f"距你生日 {delta} 天")
            elif kind == "lunar":
                notes.append(f"距{label}生日 {delta} 天（农历七月初四）")
            else:
                notes.append(f"距{label}生日 {delta} 天")
    return "；".join(notes)


def date_hint(today, days):
    if days == 0:
        return "今天开考：先易后难，稳住能拿的分"
    if days < 0:
        return "高考已结束，接下来是新的阶段"
    if days in MILESTONES:
        base = MILESTONES[days]
    elif today.month in (1, 2, 3, 4, 5, 9):
        # 期末、寒假、模考、开学等学校节点优先，避免与阶段提示重复
        base = MONTH_HINT[today.month]
    elif days <= 10:
        base = "临考：调作息、看错题、别刷新题"
    elif days <= 30:
        base = "最后一个月：回归真题，稳住节奏"
    elif days <= 60:
        base = "冲刺期：限时训练，主攻错题"
    elif days <= 100:
        base = "百日冲刺：专题突破，补齐短板"
    elif days <= 150:
        base = "二轮复习：专题加套卷，练速度"
    elif days <= 220:
        base = "一轮收尾：框架化知识，建错题本"
    elif days <= 300:
        base = "一轮复习：课本为本，基础题不丢分"
    else:
        base = "打基础：跟住课堂，别急着刷难题"
    return f"{base}；{WEEKDAY_HINT[today.weekday()]}"


def build_subject(now, hot=""):
    today = now.date()
    days = (EXAM_DATE - today).days
    weekday = "一二三四五六日"[today.weekday()]

    if days > 0:
        head = f"2027高考倒计时 {days} 天"
    elif days == 0:
        head = "2027高考今天开考"
    else:
        head = f"2027年高考已结束 {-days} 天"

    parts = [head, f"{today.month}月{today.day}日 周{weekday}", date_hint(today, days)]
    if hot:
        parts.append(f"热点：{hot}")
    birthday = birthday_note(today)
    if birthday:
        parts.append(birthday)

    subject = "｜".join(parts)
    if len(subject) > SUBJECT_WARN_LEN:
        logging.warning("标题偏长（%s 字符），仍按完整信息发送", len(subject))
    return subject


def send_mail(cfg, subject):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg["SENDER"]
    msg["To"] = cfg["RECEIVER"]
    msg.set_content("内容见标题。", subtype="plain", charset="utf-8")

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(
        cfg["SMTP_HOST"], int(cfg["SMTP_PORT"]), context=context, timeout=60
    ) as server:
        server.login(cfg["SENDER"], cfg["AUTH_CODE"])
        server.send_message(msg)


def main():
    setup_logging()
    try:
        cfg = load_env()
        now = datetime.now(timezone.utc).astimezone(BJT)
        subject = build_subject(now, fetch_hot_topic())
        send_mail(cfg, subject)
        logging.info("倒计时邮件发送成功（%s 字符）：%s", len(subject), subject)
        print(f"OK: {subject}")
        return 0
    except Exception as exc:
        logging.error("执行失败：%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
