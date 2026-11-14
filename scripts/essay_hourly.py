#!/data/data/com.termux/files/usr/bin/python3
"""高中作文素材定时邮件脚本（北京时间 UTC+8，不依赖系统时区）。"""

import html
import json
import logging
import os
import re
import smtplib
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from datetime import time as dtime
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from pathlib import Path

BJT = timezone(timedelta(hours=8))
SCRIPT_DIR = Path(__file__).resolve().parent
HOME = SCRIPT_DIR.parent  # 脚本固定位于仓库/scripts 或 $HOME/scripts，不依赖 cron 的 HOME
ENV_FILE = SCRIPT_DIR / "essay.env"
LOG_FILE = SCRIPT_DIR / "essay.log"
STATE_DIR = Path(os.environ.get("STATE_DIR", str(HOME / ".cache")))
STATE_FILE = STATE_DIR / "essay_weekly_state.json"
RECENT_FILE = STATE_DIR / "essay_recent_people.json"
GATE_FILE = STATE_DIR / "essay_slot_gate.json"
RECENT_LIMIT = 40

WINDOW_START = dtime(5, 30)
WINDOW_END = dtime(23, 30)
TITLE_MAX = 110
MODEL_TIMEOUT = 480
SUPPLEMENT_TIMEOUT = 6
SUPPLEMENT_MAX_ITEMS = 12

DEFAULT_MODEL = "glm-4.7-flash,glm-4.5-flash,glm-4-flash-250414"
DEFAULT_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
ENV_KEYS = (
    "SMTP_HOST",
    "SMTP_PORT",
    "SENDER",
    "AUTH_CODE",
    "RECEIVER",
    "ZHIPU_MODEL",
    "ZHIPU_BASE_URL",
)
REQUIRED_ENV = ("SMTP_HOST", "SMTP_PORT", "SENDER", "AUTH_CODE", "RECEIVER")


class BJTFormatter(logging.Formatter):
    """日志时间统一使用北京时间。"""

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
    return logger


def beijing_now():
    return datetime.now(timezone.utc).astimezone(BJT)


def monday_of(dt):
    return dt - timedelta(days=dt.weekday())


def load_env():
    values = {}
    if ENV_FILE.is_file():
        for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")

    # GitHub Actions 等环境通过环境变量注入；环境变量优先于本地文件
    for key in ENV_KEYS:
        env_value = os.environ.get(key)
        if env_value:
            values[key] = env_value.strip()

    missing = [k for k in REQUIRED_ENV if not values.get(k)]
    if missing:
        raise RuntimeError(f"配置缺少字段: {', '.join(missing)}")
    values.setdefault("ZHIPU_MODEL", DEFAULT_MODEL)
    values.setdefault("ZHIPU_BASE_URL", DEFAULT_BASE_URL)
    return values


def load_api_key():
    key = (os.environ.get("ZHIPU_API_KEY") or "").strip()
    if key:
        return key
    for path in (
        HOME / ".config" / "opencode" / "zhipu-api-key",
        Path.home() / ".config" / "opencode" / "zhipu-api-key",
    ):
        try:
            if path.is_file():
                value = path.read_text(encoding="utf-8").strip()
                if value:
                    return value
        except Exception:
            pass
    raise RuntimeError("缺少 ZHIPU_API_KEY（环境变量或 ~/.config/opencode/zhipu-api-key）")


def load_state(today_monday):
    state = {
        "week_start": today_monday.isoformat(),
        "ancient_count": 0,
        "total_count": 0,
    }
    try:
        with STATE_FILE.open(encoding="utf-8") as fh:
            saved = json.load(fh)
    except FileNotFoundError:
        logging.info("状态文件不存在，按本周初始状态处理")
        return state
    except Exception as exc:  # 损坏时按本周初始状态处理，不阻塞发送
        logging.warning("状态文件读取失败（%s），按本周初始状态处理", exc)
        return state

    if saved.get("week_start") == today_monday.isoformat():
        try:
            state["ancient_count"] = int(saved.get("ancient_count", 0))
            state["total_count"] = int(saved.get("total_count", 0))
        except (TypeError, ValueError):
            logging.warning("状态文件字段非法，按本周初始状态处理")
            return state
    else:
        logging.info(
            "跨周重置状态：week_start %s -> %s",
            saved.get("week_start"),
            today_monday.isoformat(),
        )
    return state


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, STATE_FILE)


def load_recent_people():
    try:
        data = json.loads(RECENT_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [str(x) for x in data if str(x).strip()][-RECENT_LIMIT:]
    except FileNotFoundError:
        pass
    except Exception as exc:
        logging.warning("近期人物历史读取失败（%s），忽略", exc)
    return []


def remember_person(name):
    name = (name or "").strip()
    if not name:
        return
    people = [p for p in load_recent_people() if p != name]
    people.append(name)
    people = people[-RECENT_LIMIT:]
    RECENT_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = RECENT_FILE.with_name(RECENT_FILE.name + ".tmp")
    tmp.write_text(json.dumps(people, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, RECENT_FILE)


def extract_person_name(title):
    match = re.match(r"^\s*([\u4e00-\u9fa5]{2,4})[·・]", title)
    return match.group(1) if match else None


def _strip_html(text):
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Linux; Android 15)"})
    with urllib.request.urlopen(req, timeout=SUPPLEMENT_TIMEOUT) as resp:
        return resp.read()


def _parse_rss(payload, default_source):
    items = []
    root = ET.fromstring(payload)
    channel = root.find("channel")
    if channel is None:
        return items
    for node in channel.findall("item")[:20]:
        title = (node.findtext("title") or "").strip()
        link = (node.findtext("link") or "").strip()
        if not title or not link:
            continue
        desc = _strip_html(node.findtext("description") or "")[:180]
        date_text = ""
        try:
            published = parsedate_to_datetime(node.findtext("pubDate") or "").astimezone(BJT)
            if (beijing_now() - published).days > 60:
                continue
            date_text = published.strftime("%Y-%m-%d")
        except Exception:
            pass
        source = _strip_html(node.findtext("source") or "") or default_source
        items.append(
            {"title": title, "link": link, "desc": desc, "date": date_text, "source": source}
        )
    return items


def fetch_supplement():
    """可选的联网补充检索：任何失败都只记日志，不影响主流程。"""
    feeds = [("https://www.chinanews.com.cn/rss/culture.xml", "中国新闻网")]
    for query in ("非遗 传承", "考古 新发现", "传统文化 当代生活"):
        feeds.append(
            (
                "https://cn.bing.com/search?q=" + urllib.parse.quote(query) + "&format=rss",
                "必应搜索",
            )
        )

    candidates = []
    for url, source in feeds:
        try:
            candidates.extend(_parse_rss(_http_get(url), source))
        except Exception as exc:
            logging.info("联网补充源不可用（%s）：%s", source, exc)

    seen = set()
    picked = []
    for item in candidates:
        if item["title"] in seen:
            continue
        seen.add(item["title"])
        picked.append(item)
        if len(picked) >= SUPPLEMENT_MAX_ITEMS:
            break

    lines = []
    for idx, item in enumerate(picked, 1):
        meta = " | ".join(part for part in (item["source"], item["date"]) if part)
        lines.append(f"{idx}. {item['title']}（{meta}）\n   {item['link']}\n   {item['desc']}")
    return "\n".join(lines)


def build_prompt(today, ancient_count, total_count, need_ancient, supplement="", recent_people=None):
    recent_text = "、".join(recent_people) if recent_people else "暂无"
    prompt = f"""你是高中作文素材日报编辑。只输出严格 JSON，不要 markdown，不要解释。字段：
{{"title":"...","body":"...","ancient_count":0或1,"total_people":1}}

今天日期：{today.isoformat()}。
本周已发古代人物 {ancient_count} 个，总人物 {total_count} 个；本次至少需要 {need_ancient} 个古代人物（0 表示不强制，但仍鼓励优先古代）。本次共 1 个人物素材，需满足古代人物数量要求。
近期已推送人物（禁止重复；同一领域或朝代也尽量换人）：{recent_text}

人物选择范围要宽：不要只照着“优先冷门”的类别示例找同类人物，也不要被提示词中提到的任何人物、类别或举例框住；可从政治、军事、文学、艺术、医学、工程、商贸、教育、女性、少数民族、民间匠人等不同领域与朝代中自由选择（禁用名单仍然必须避开）。

内容结构：只写 1 个人物素材，不要事件素材。

人物硬性规则：
- 古代指 1840 年前。
- 禁用古代名单：屈原、司马迁、陶渊明、李白、杜甫、苏轼、岳飞、文天祥、李时珍、郑板桥、蒲松龄、毕昇。
- 禁用近现代名单：袁隆平、屠呦呦、钟南山、张桂梅、樊锦诗、叶嘉莹、史铁生、爱因斯坦、霍金、乔布斯。
- 优先冷门：淡出仕途的实学者、落第后成事者、冷门学科开创者、历史夹缝小人物、反差型人物。
- 古代人物须有正史或权威出版物记载；不要虚构原话和出处。
- 每个人物包含：姓名 + 一句话定位；2-3 条亮点事迹，突出具体动作/数字/原话；1-2 句原话语录并注明出处；2-3 个适用主题；一句运用示范。
- 不要写任何事件素材、时事新闻或独立事件段落，全篇只围绕这一个人物展开。

标题规则（核心）：
- 格式：人物姓名·钩子词＋身份/时代＋2-3 个具体事迹或数字＋一句原话/评价（不要写日期，标题必须以人物姓名开头，只写这一个人物，不要加入事件关键词）
- 全标题必须为 90-110 个字符：少于 90 字视为不合格，不得用“等等”“诸多”或省略号凑长度。
- 标题要写成信息密集的完整长句，不要压缩成短标签；最关键信息放最前。
- 钩子词 = 该人物最具反差感的一个细节。
- 标题必须独立承载当日核心信息，让手环不打开邮件就知道今天推了什么、不值得点开细看。
- 正文可以详细，标题要在此额度内承载足够信息，不要只写短标签。
- 标题不得含换行、不要包含日期；超过 110 字符截断并写日志警告。

输出格式硬性要求：
- 只输出一个 JSON 对象，第一个字符必须是 {{，最后一个字符必须是 }}。
- 必须包含且仅包含这四个字段：title、body、ancient_count、total_people。
- JSON 字符串内部禁止出现未转义的英文双引号 "；需要引用时一律使用中文引号“”。
- 输出必须能被 json.loads 直接解析。
"""
    if supplement:
        prompt += (
            "\n\n补充参考（脚本自动联网检索，仅用于人物背景印证，不强制采用，也不要据此另写事件素材；"
            "若采用请核对来源，不要编造链接。若以下内容与人物无关，请忽略）：\n"
            + supplement
        )
    return prompt


def call_model(cfg, prompt):
    """调用智谱 GLM OpenAI 兼容接口（open.bigmodel.cn）。
    ZHIPU_MODEL 支持逗号分隔的降级链：前面的模型 429 过载/超时时自动切换下一个。"""
    base = cfg["ZHIPU_BASE_URL"].rstrip("/")
    models = [m.strip() for m in cfg["ZHIPU_MODEL"].split(",") if m.strip()] or [DEFAULT_MODEL]
    last_error = None
    for idx, model in enumerate(models):
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 8192,
            "temperature": 0.6,
            # 关闭深度思考：默认开启时思考会耗尽 max_tokens，导致 finish_reason=length、正文为空
            "thinking": {"type": "disabled"},
        }
        req = urllib.request.Request(
            base + "/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {load_api_key()}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        logging.info("调用智谱 GLM 生成素材（%d/%d：%s）", idx + 1, len(models), model)
        try:
            with urllib.request.urlopen(req, timeout=MODEL_TIMEOUT) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:800].decode("utf-8", "replace")
            last_error = RuntimeError(f"智谱 GLM HTTP {exc.code}（{model}）：{detail}")
            logging.warning("%s", last_error)
            if idx + 1 < len(models):
                time.sleep(3)
            continue
        except Exception as exc:
            last_error = RuntimeError(f"智谱 GLM 请求失败（{model}）：{exc}")
            logging.warning("%s", last_error)
            if idx + 1 < len(models):
                time.sleep(3)
            continue

        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            last_error = RuntimeError(f"智谱 GLM 返回非 JSON（{model}）：{raw[:800]!r}")
            logging.warning("%s", last_error)
            if idx + 1 < len(models):
                time.sleep(3)
            continue

        choices = body.get("choices") or []
        if not choices:
            last_error = RuntimeError(
                f"智谱 GLM 返回无 choices（{model}）：{json.dumps(body, ensure_ascii=False)[:800]}"
            )
            logging.warning("%s", last_error)
            if idx + 1 < len(models):
                time.sleep(3)
            continue

        message = choices[0].get("message") or {}
        content = (message.get("content") or "").strip()
        if content:
            return content
        reason = (message.get("reasoning_content") or "")[:300]
        last_error = RuntimeError(
            f"智谱 GLM 未返回正文（finish_reason={choices[0].get('finish_reason')}）：{reason}"
        )
        logging.warning("%s", last_error)
        if idx + 1 < len(models):
            time.sleep(3)
    raise last_error or RuntimeError("智谱 GLM 所有模型均失败")


def _unescape_json_text(value):
    def repl(match):
        char = match.group(1)
        return {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\", "/": "/"}.get(
            char, char
        )

    return re.sub(r"\\(.)", repl, value, flags=re.S)


def extract_json(stdout):
    start = stdout.find("{")
    end = stdout.rfind("}")
    if start == -1 or end < start:
        raise RuntimeError(f"模型输出中找不到 JSON；输出尾部：{stdout[-1500:]}")
    raw = stdout[start : end + 1]
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        # 模型常在字符串内直接使用未转义的英文双引号；按字段锚点做一次容错解析
        pattern = re.compile(
            r'"title"\s*:\s*"(?P<title>.*?)"\s*,\s*"body"\s*:\s*"(?P<body>.*)"'
            r'\s*,\s*"ancient_count"\s*:\s*(?P<ancient>\d+)'
            r'\s*,\s*"total_people"\s*:\s*(?P<total>\d+)',
            re.S,
        )
        match = pattern.search(raw)
        if not match:
            raise RuntimeError(f"JSON 解析失败：{exc}；内容片段：{raw[:800]}")
        logging.warning("模型 JSON 存在未转义引号，已使用容错解析")
        return {
            "title": _unescape_json_text(match.group("title")),
            "body": _unescape_json_text(match.group("body")),
            "ancient_count": int(match.group("ancient")),
            "total_people": int(match.group("total")),
        }


def validate(data, need_ancient):
    for key in ("title", "body", "ancient_count", "total_people"):
        if key not in data:
            raise RuntimeError(f"JSON 缺少字段：{key}")
    try:
        ancient = int(data["ancient_count"])
        total = int(data["total_people"])
    except (TypeError, ValueError):
        raise RuntimeError("ancient_count / total_people 不是整数")
    if not isinstance(data["title"], str) or not data["title"].strip():
        raise RuntimeError("title 为空或不是字符串")
    if not isinstance(data["body"], str) or not data["body"].strip():
        raise RuntimeError("body 为空或不是字符串")

    title_text = data["title"].strip()
    body_text = data["body"].strip()
    if not re.match(r"^[\u4e00-\u9fa5]{2,5}[·・]", title_text):
        raise RuntimeError(f"title 未以“人物姓名·”开头：{title_text[:40]}")
    if len(title_text) < 90:
        raise RuntimeError(f"title 过短（{len(title_text)} 字），要求 90-110 字")
    if len(body_text) < 200:
        raise RuntimeError(f"body 过短（{len(body_text)} 字），可能被截断")
    missing_sections = [name for name in ("适用主题", "运用示范") if name not in body_text]
    if missing_sections:
        raise RuntimeError(f"body 缺少字段：{'、'.join(missing_sections)}")
    if not body_text.endswith(("。", "！", "？", "…", "”", "」", "』", "）", "】")):
        raise RuntimeError(f"body 结尾不完整：{body_text[-30:]}")

    if ancient < 0 or total < 0:
        raise RuntimeError("ancient_count / total_people 不能为负数")
    if ancient < need_ancient:
        logging.warning("模型返回古代人物数 %s，少于本次要求 %s", ancient, need_ancient)
    return ancient, total


def clean_title(title):
    cleaned = title.replace("\r", " ").replace("\n", " ").strip()
    if cleaned != title.strip():
        logging.warning("标题包含换行，已替换为空格")
    if len(cleaned) > TITLE_MAX:
        logging.warning("标题超过 %s 个字符（实际 %s），已截断", TITLE_MAX, len(cleaned))
        cleaned = cleaned[:TITLE_MAX]
    return cleaned


def send_mail(cfg, title, body):
    msg = EmailMessage()
    msg["Subject"] = title
    msg["From"] = cfg["SENDER"]
    msg["To"] = cfg["RECEIVER"]
    msg.set_content(body, subtype="plain", charset="utf-8")

    context = ssl.create_default_context()
    port = int(cfg["SMTP_PORT"])
    with smtplib.SMTP_SSL(cfg["SMTP_HOST"], port, context=context, timeout=60) as server:
        server.login(cfg["SENDER"], cfg["AUTH_CODE"])
        server.send_message(msg)


def main():
    setup_logging()
    force = "--force" in sys.argv[1:]

    now = beijing_now()
    if not force and not (WINDOW_START <= now.time() <= WINDOW_END):
        logging.info("当前北京时间 %s 不在 05:30-23:30 窗口内，退出", now.strftime("%H:%M"))
        return 0

    # 时段闸门（幂等）：同一北京时间小时只允许发一封。
    # 主触发（HH:20 触发、等到 HH:30 发送）成功后闸门生效；
    # 备份触发（HH:35 / HH:50，主触发丢失或失败时补发）命中闸门则直接跳过，绝不重复发送。
    slot = now.strftime("%Y-%m-%d %H")
    if not force and GATE_FILE.is_file():
        try:
            gate = json.loads(GATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            gate = {}
        if gate.get("slot") == slot:
            logging.info("时段闸门：本时段（北京 %s 时）已发送，跳过", slot)
            return 0

    try:
        cfg = load_env()
        today = now.date()
        week_start = monday_of(today)
        state = load_state(week_start)

        ancient_count = state["ancient_count"]
        total_count = state["total_count"]
        need_ancient = 1 if ancient_count / (total_count + 1) < 0.7 else 0
        logging.info(
            "本周已发古代 %s / 总人物 %s；本次至少需 %s 个古代人物",
            ancient_count,
            total_count,
            need_ancient,
        )

        supplement = fetch_supplement()
        if supplement:
            logging.info("联网补充：已获取参考条目（%s 字符）", len(supplement))
        else:
            logging.info("联网补充：无可用条目，按模型自身素材库继续")

        prompt = build_prompt(
            today,
            ancient_count,
            total_count,
            need_ancient,
            supplement,
            load_recent_people(),
        )
        retry_note = (
            "\n\n上次输出不合格（常见原因：JSON 字符串内出现未转义的英文双引号、"
            "缺少 ancient_count/total_people 字段、标题不足 90 字，或正文缺少“适用主题/运用示范”）。"
            "请严格只输出一个合法 JSON 对象；标题必须以人物姓名·开头且为 90-110 字；"
            "正文必须完整并以中文句号结尾；字符串内引用一律使用中文引号“”，不要输出任何其他文字。"
            "\n\n标题长度示例（93 字，合格；你的标题必须同样长，不能短于 90 字）：\n"
            "李时珍·明代民间走方医出身·三次乡试落第后弃科举从医·跋涉万里遍尝百草·"
            "历时二十七载三易其稿著成一百九十万字本草纲目·收载药物一千八百余种·"
            "纠正历代本草记载谬误·刊印后远传朝鲜日本与欧洲"
        )
        data = None
        last_error = None
        for attempt in range(1, 3):
            stdout = call_model(cfg, prompt if attempt == 1 else prompt + retry_note)
            try:
                data = extract_json(stdout)
                try:
                    ancient, total = validate(data, need_ancient)
                except Exception:
                    logging.error(
                        "模型输出缺少必需字段，原始 JSON：%s",
                        json.dumps(data, ensure_ascii=False)[:1500],
                    )
                    raise
                break
            except Exception as exc:
                last_error = exc
                logging.warning("第 %s 次生成结果无效：%s", attempt, exc)
                data = None
        if data is None:
            raise RuntimeError(f"连续 2 次生成结果无效，最后一次错误：{last_error}")
        title = clean_title(data["title"])

        logging.info("生成完成：标题=%s；ancient_count=%s；total_people=%s", title, ancient, total)
        send_mail(cfg, title, data["body"])
        logging.info("邮件发送成功")

        # 发送成功即写闸门：后续备份触发会跳过，避免重复发送
        try:
            GATE_FILE.write_text(
                json.dumps({"slot": slot, "sent_at": now.isoformat()}, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as gate_exc:
            logging.warning("写入时段闸门失败：%s", gate_exc)

        state["ancient_count"] = ancient_count + ancient
        state["total_count"] = total_count + total
        state["week_start"] = week_start.isoformat()
        save_state(state)

        person = extract_person_name(title)
        if person:
            remember_person(person)
            logging.info("已记录人物到去重历史：%s", person)
        else:
            logging.warning("无法从标题提取人物姓名，未写入去重历史")
        logging.info(
            "状态已更新：ancient_count=%s；total_count=%s；week_start=%s",
            state["ancient_count"],
            state["total_count"],
            state["week_start"],
        )
        print(f"OK: 已发送《{title}》")
        return 0
    except Exception as exc:
        logging.error("执行失败：%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
