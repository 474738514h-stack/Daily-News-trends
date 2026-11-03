#!/usr/bin/env python3
"""Same-origin static + SiliconFlow proxy for morning-feed.

POST /api/generate  — modes: parse | impact | deep | debate | debate_followup
GET  /api/schedule  — daily edition semantics (08:00 Asia/Shanghai, show 10)
GET  /api/cache     — cross-user AI cache stats
GET  /__mtime       — soft-refresh signal when morning-feed.html is rewritten
Env: repo-root `.env.siliconflow` (copy from `.env.example`; never logged / never committed)

Crawl/edition: HTML embeds the day's hottest pool. Clients filter by user
interests/tags + sources, rank latest/hottest, and hard-cap at SHOW_LIMIT=10.
Daily refresh target is 08:00 Asia/Shanghai; open tabs soft-reload via /__mtime
(and an 08:00 timer) without changing any public tunnel URL.

AI cache (cross-user): parse / impact / deep / debate (4-round seed) persist under
internal/ai-cache/. Key = news id|url + mode (+ industry for impact). Hits skip
SiliconFlow. debate_followup (路人甲) stays uncached / per-session.
Daily 07:50 Asia/Shanghai: wipe all previous-day files in ai-cache/ so the
08:00 edition starts with a cold cache.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent.parent
ROOT = (REPO_ROOT / "docs").resolve()
HTML = ROOT / "morning-feed.html"
ENV_PATH = REPO_ROOT / ".env.siliconflow"
CACHE_DIR = REPO_ROOT / "internal" / "ai-cache"
PORT = int(os.environ.get("MORNING_FEED_PORT", "8765"))

FEED_TZ_NAME = "Asia/Shanghai"
FEED_REFRESH_HOUR = 8
FEED_SHOW_LIMIT = 10
CACHE_WIPE_HOUR = 7
CACHE_WIPE_MINUTE = 50
SHANGHAI = ZoneInfo(FEED_TZ_NAME)
CACHEABLE_MODES = frozenset({"parse", "impact", "deep", "debate"})

os.chdir(ROOT)
CACHE_DIR.mkdir(parents=True, exist_ok=True)

_cache_io_lock = threading.Lock()
_inflight_guard = threading.Lock()
_inflight: dict[str, tuple[threading.Event, dict]] = {}
_last_cache_wipe: dict | None = None


def news_identity(payload: dict, title: str) -> str:
    """Stable cross-user id: prefer topicId, then url, else title hash."""
    tid = str(payload.get("topicId") or payload.get("id") or "").strip()
    url = str(payload.get("url") or "").strip()
    if tid:
        return "id:" + tid
    if url:
        return "url:" + url
    digest = hashlib.sha256(title.encode("utf-8")).hexdigest()[:24]
    return "title:" + digest


def cache_key_parts(mode: str, identity: str, industry: str = "") -> tuple[str, str]:
    raw = f"{mode}|{identity}"
    if mode == "impact":
        raw += "|" + str(industry or "").strip().lower()
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]
    return f"{mode}_{digest}.json", raw


def cache_path(mode: str, identity: str, industry: str = "") -> Path:
    fname, _ = cache_key_parts(mode, identity, industry)
    return CACHE_DIR / fname


def cache_get(mode: str, identity: str, industry: str = "") -> dict | None:
    if mode not in CACHEABLE_MODES:
        return None
    path = cache_path(mode, identity, industry)
    if not path.is_file():
        return None
    try:
        with _cache_io_lock:
            obj = json.loads(path.read_text(encoding="utf-8"))
        resp = obj.get("response") if isinstance(obj, dict) else None
        return resp if isinstance(resp, dict) else None
    except Exception:
        return None


def cache_put(mode: str, identity: str, industry: str, response: dict) -> None:
    if mode not in CACHEABLE_MODES or not isinstance(response, dict):
        return
    path = cache_path(mode, identity, industry)
    _, raw = cache_key_parts(mode, identity, industry)
    envelope = {
        "key": raw,
        "mode": mode,
        "identity": identity,
        "industry": industry if mode == "impact" else "",
        "cachedAt": datetime.now(SHANGHAI).isoformat(),
        "response": response,
    }
    tmp = path.with_name(path.name + ".tmp")
    try:
        blob = json.dumps(envelope, ensure_ascii=False, indent=2)
        with _cache_io_lock:
            tmp.write_text(blob, encoding="utf-8")
            os.replace(tmp, path)
    except Exception:
        try:
            if tmp.is_file():
                tmp.unlink()
        except Exception:
            pass


def next_cache_wipe_at(now: datetime | None = None) -> datetime:
    now = now or datetime.now(SHANGHAI)
    target = now.replace(
        hour=CACHE_WIPE_HOUR, minute=CACHE_WIPE_MINUTE, second=0, microsecond=0
    )
    if now >= target:
        target += timedelta(days=1)
    return target


def wipe_ai_cache(reason: str = "scheduled") -> dict:
    """Delete all previous-day AI cache files under CACHE_DIR (JSON + temps)."""
    global _last_cache_wipe
    removed = 0
    errors = 0
    with _cache_io_lock:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        for p in list(CACHE_DIR.iterdir()):
            if not p.is_file():
                continue
            name = p.name
            if not (name.endswith(".json") or name.endswith(".tmp") or name.endswith(".json.tmp")):
                continue
            try:
                p.unlink()
                removed += 1
            except Exception:
                errors += 1
    info = {
        "removed": removed,
        "errors": errors,
        "at": datetime.now(SHANGHAI).isoformat(),
        "reason": reason,
        "dir": str(CACHE_DIR),
    }
    _last_cache_wipe = info
    print(
        f"ai-cache wipe reason={reason} removed={removed} errors={errors} "
        f"at={info['at']}",
        flush=True,
    )
    return info


def cache_wipe_loop() -> None:
    """Daemon: every day at 07:50 Asia/Shanghai clear ai-cache before 08:00 edition."""
    while True:
        now = datetime.now(SHANGHAI)
        target = next_cache_wipe_at(now)
        wait = max(1.0, (target - now).total_seconds())
        print(
            f"ai-cache wipe armed next={target.isoformat()} waitSec={int(wait)}",
            flush=True,
        )
        end = time.time() + wait
        while True:
            left = end - time.time()
            if left <= 0:
                break
            time.sleep(min(left, 30.0))
        wipe_ai_cache("daily-0750-shanghai")


def start_cache_wipe_scheduler() -> None:
    t = threading.Thread(target=cache_wipe_loop, name="ai-cache-wipe", daemon=True)
    t.start()


def cache_stats() -> dict:
    files = list(CACHE_DIR.glob("*.json")) if CACHE_DIR.is_dir() else []
    by_mode: dict[str, int] = {}
    for p in files:
        m = p.name.split("_", 1)[0]
        by_mode[m] = by_mode.get(m, 0) + 1
    nxt = next_cache_wipe_at()
    return {
        "dir": str(CACHE_DIR),
        "entries": len(files),
        "byMode": by_mode,
        "cacheableModes": sorted(CACHEABLE_MODES),
        "uncachedModes": ["debate_followup"],
        "dailyWipe": f"{CACHE_WIPE_HOUR:02d}:{CACHE_WIPE_MINUTE:02d}",
        "tz": FEED_TZ_NAME,
        "nextWipeAt": nxt.isoformat(),
        "nextWipeUnix": int(nxt.timestamp()),
        "lastWipe": _last_cache_wipe,
        "alignsWithEdition": f"{FEED_REFRESH_HOUR:02d}:00",
    }


def inflight_run(key: str, producer):
    """Coalesce concurrent identical generates so only one hits SiliconFlow."""
    with _inflight_guard:
        existing = _inflight.get(key)
        if existing is not None:
            ev, holder = existing
            wait = True
        else:
            ev, holder = threading.Event(), {"resp": None, "err": None}
            _inflight[key] = (ev, holder)
            wait = False
    if wait:
        ev.wait(timeout=180)
        if holder.get("err") is not None:
            raise holder["err"]
        return holder.get("resp")
    try:
        resp = producer()
        holder["resp"] = resp
        return resp
    except Exception as e:
        holder["err"] = e
        raise
    finally:
        ev.set()
        with _inflight_guard:
            _inflight.pop(key, None)


def feed_schedule() -> dict:
    """Edition clock for clients / ops — no crawl side effects."""
    now = datetime.now(SHANGHAI)
    today_8 = now.replace(hour=FEED_REFRESH_HOUR, minute=0, second=0, microsecond=0)
    if now < today_8:
        nxt = today_8
        edition_day = (today_8 - timedelta(days=1)).date().isoformat()
    else:
        nxt = today_8 + timedelta(days=1)
        edition_day = today_8.date().isoformat()
    wipe_at = next_cache_wipe_at(now)
    return {
        "tz": FEED_TZ_NAME,
        "dailyRefresh": f"{FEED_REFRESH_HOUR:02d}:00",
        "dailyCacheWipe": f"{CACHE_WIPE_HOUR:02d}:{CACHE_WIPE_MINUTE:02d}",
        "showLimit": FEED_SHOW_LIMIT,
        "editionDay": edition_day,
        "nowShanghai": now.strftime("%Y-%m-%d %H:%M:%S"),
        "nextRefreshAt": nxt.isoformat(),
        "nextRefreshUnix": int(nxt.timestamp()),
        "nextCacheWipeAt": wipe_at.isoformat(),
        "nextCacheWipeUnix": int(wipe_at.timestamp()),
        "semantics": (
            "Daily 07:50 Asia/Shanghai: wipe internal/ai-cache/ (previous-day AI). "
            "Daily 08:00 Asia/Shanghai: serve latest hottest items from the "
            "embedded crawl pool, filtered by user follow tags + sources, "
            f"hard-capped at {FEED_SHOW_LIMIT}. Open tabs soft-reload on "
            "/__mtime change (HTML rewrite) or at the 08:00 timer."
        ),
    }


def load_env(path: Path) -> dict:
    out = {}
    if not path.is_file():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def cfg():
    e = load_env(ENV_PATH)
    key = e.get("SILICONFLOW_API_KEY") or os.environ.get("SILICONFLOW_API_KEY") or ""
    base = (e.get("SILICONFLOW_BASE_URL") or os.environ.get("SILICONFLOW_BASE_URL")
            or "https://api.siliconflow.cn/v1").rstrip("/")
    model = (e.get("SILICONFLOW_MODEL") or os.environ.get("SILICONFLOW_MODEL")
             or "deepseek-ai/DeepSeek-V4-Flash")
    return key, base, model


COMPLIANCE = (
    "合规：禁止违法、暴力恐怖、淫秽、政治敏感与仇恨内容；"
    "不文明用语用 * 打码（如傻*、他*的）；"
    "不构成投资建议；只依据给定标题与摘要，勿编造未给出的事实数字。"
)
NO_ELLIPSIS = (
    "严禁使用任何省略号或未写完语气（含 ...、…、……、。。。、⋯）；"
    "每句必须写完整并自然收束，勿用省略号中断或收尾。"
)
BOLD_SPARSE = (
    "加粗规则：只用 **关键词或数字** 局部加粗，禁止把整句或大半句包进双星号；"
    "每句最多 1–3 处，每处通常 2–8 字（主体名、百分比、金额等）；无可突出点则不加粗。"
)


def prompt_for(mode: str, title: str, summary: str, topic: str, user_text: str,
               industry: str = "") -> list:
    news = f"标题：{title}\n摘要：{summary}\n主题：{topic or '这条新闻'}"
    if mode == "parse":
        sys = (
            "你是中文财经/科技早报编辑。根据新闻写「AI 秒读」两行短评（不是新闻段落、勿复述标题/摘要）："
            "只输出一个 JSON 对象（无其它文字、无 markdown 代码块），字段固定为："
            '{"read":"结论句","subtext":"潜台词句"}。'
            "read：一句核心结论（约 18–42 字），结论先行；仅对关键数字/专名用 **局部加粗**（勿整句加粗）；"
            "subtext：一句锐利「潜台词」点评（约 16–36 字），点破行业/资本/用户视角下没写明的含义；勿空话、勿投资建议；"
            "潜台词一般不加粗，除非单个关键词确有必要；禁止写成新闻段落；禁止第三句或额外字段。"
            f"{BOLD_SPARSE}{NO_ELLIPSIS}{COMPLIANCE}"
        )
        user = (
            f"{news}\n"
            "请输出 JSON：read（核心结论；仅关键词/数字 **局部加粗**）+ subtext（潜台词一句）。禁止整句加粗；全文禁止省略号。"
        )
    elif mode == "impact":
        ind = industry or "该行业"
        sys = (
            "你是中文行业影响分析师。根据新闻给出对用户所在行业的三向影响（结论先行，禁止复述新闻）。"
            "只输出一个 JSON 对象（无其它文字、无 markdown 代码块），字段固定为："
            '{"positive":{"title":"主结论","detail":"谁受影响，怎么影响"},'
            '"negative":{"title":"主结论","detail":"谁受影响，怎么影响"},'
            '"observe":{"title":"主结论","detail":"谁受影响，怎么影响"},'
            '"confidence":72}。'
            "每项 title：一句主结论（约 10–22 字），界面会整句加粗展示，title 内不要使用 **加粗**；"
            "每项 detail：一句灰色推导（约 16–36 字），格式固定为「谁受影响，怎么影响」，删废话与空话；detail 默认不加粗；"
            "positive=正向机会、negative=负向风险、observe=需观察点；必须扣住「所在行业」；勿投资建议；"
            "弱相关时 title 标明关联有限，detail 给可核验观察点。"
            "confidence：0–100 整数；弱相关通常 18–45。"
            f"{BOLD_SPARSE}{NO_ELLIPSIS}{COMPLIANCE}"
        )
        user = (
            f"{news}\n用户所在行业：{ind}\n"
            "请输出 JSON：positive/negative/observe 各含 title+detail，外加 confidence。title 勿用 **；detail 默认不加粗；全文禁止省略号。"
        )
    elif mode == "deep":
        sys = (
            "你是中文深度解读作者。结构必须含三节，标题只用【】括号："
            "【发生了什么】【为什么重要】【趋势判断（有依据）】。"
            "严禁 Markdown：不要写 #、##、###、**、列表符；小节标题单独成行，紧接下文。"
            "节与节之间空一行即可；每段2–4句，总长约220–420字，贴合标题与摘要。"
            f"{NO_ELLIPSIS}{COMPLIANCE}"
        )
        user = (
            f"{news}\n请输出完整结构化深度解读（禁止省略号，句子写完整）。"
            "格式示例：\n【发生了什么】\n本节正文。\n\n【为什么重要】\n本节正文。\n\n【趋势判断（有依据）】\n本节正文。"
        )
    elif mode == "debate":
        sys = (
            "你是「AI观点碰撞」编剧。严格按 Role 卡生成乐观派(opt)与保守派(con)对辩，"
            "必须恰好 4 条、顺序固定不可打乱：\n"
            "1) opt 立论  2) con 立论  3) opt 反驳  4) con 反驳（到此结束，禁止第5条）\n\n"
            "【乐观派 Role】科技/财经多头；先「搜证」1–2个历史上涨或翻身成功案例作证据；"
            "语气进攻，可骂保守派短视；必用表情 🚀🔥🌟 中至少1个；每条≤150字。\n"
            "【保守派 Role】冷酷风险/财经空头；先「搜证」1–2个历史暴跌、泡沫破裂或踩坑案例；"
            "语气冷静刻薄；必用表情 🤔😰📉 中至少1个；每条≤150字。\n\n"
            "每条 text 必须含两段且用换行分隔：\n"
            "【搜证发言】（点名公开历史事件作「搜证」例证）\n"
            "【反驳与立论】（立论或反驳对方；扣住本条新闻）\n"
            "你无法真实联网搜索：只能引用广为人知的公开历史事件当「搜证」"
            "（如亚马逊早期连年亏损后崛起、特斯拉产能地狱后交付、2000年互联网泡沫、2008金融危机、"
            "瑞幸造假、WeWork估值崩塌、FTX暴雷、中概股从高点腰斩等），"
            "严禁编造假链接/假URL/不存在的报导出处；勿捏造精确假数字。\n"
            "口吻像微信群口语，勿官腔、勿投资建议口吻。\n"
            "只输出 JSON 数组（长度必须=4）："
            "[{{\"role\":\"opt\",\"text\":\"发言正文\"}},{{\"role\":\"con\",\"text\":\"发言正文\"}},"
            "{{\"role\":\"opt\",\"text\":\"发言正文\"}},{{\"role\":\"con\",\"text\":\"发言正文\"}}]，无其它文字。"
            f"{NO_ELLIPSIS}{COMPLIANCE}"
        )
        user = (
            f"{news}\n"
            "请生成恰好4轮观点碰撞：乐观派立论→保守派立论→乐观派反驳→保守派反驳。全文禁止省略号。"
        )
    elif mode == "debate_followup":
        sys = (
            "路人甲刚拱火插话。乐观派(opt)与保守派(con)继续对辩，恰好 4 条、交替发言，"
            "顺序仍为 opt→con→opt→con，结束后停止。\n"
            "继续沿用 Role：乐观派=科技/财经多头，搜证用历史成功翻身案例，表情🚀🔥🌟，可攻保守派短视；"
            "保守派=冷酷风险空头，搜证用历史崩盘/泡沫案例，表情🤔😰📉。\n"
            "每条≤150字，结构仍为【搜证发言】换行【反驳与立论】；"
            "必须回应路人观点并扣住新闻。无法联网时只用广为人知公开史实当「搜证」，禁止假URL。\n"
            "只输出 JSON 数组（长度=4）："
            "[{{\"role\":\"opt\",\"text\":\"发言正文\"}},{{\"role\":\"con\",\"text\":\"发言正文\"}},"
            "{{\"role\":\"opt\",\"text\":\"发言正文\"}},{{\"role\":\"con\",\"text\":\"发言正文\"}}]，无其它文字。"
            f"{NO_ELLIPSIS}{COMPLIANCE}"
        )
        user = f"{news}\n路人甲说：{user_text}\n请生成回击四轮（opt→con→opt→con）。全文禁止省略号。"
    else:
        raise ValueError(f"unknown mode: {mode}")
    return [
        {"role": "system", "content": sys},
        {"role": "user", "content": user},
    ]


def chat_complete(messages: list, temperature: float = 0.7, max_tokens: int = 900) -> str:
    key, base, model = cfg()
    if not key:
        raise RuntimeError("SILICONFLOW_API_KEY missing")
    body = json.dumps({
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", "ignore")[:400]
        raise RuntimeError(f"upstream {e.code}: {err_body}") from e
    try:
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        raise RuntimeError("bad upstream shape") from e


def ban_ellipsis(text: str, *, close_sentence: bool = False) -> str:
    """Strip ... / … / 。。。 and close incomplete trailing phrases."""
    t = str(text or "")
    if not t:
        return t
    t = re.sub(r"\.{3,}", "，", t)
    t = re.sub(r"…+", "，", t)
    t = re.sub(r"⋯+", "，", t)
    t = re.sub(r"．{3,}", "，", t)
    t = re.sub(r"。{2,}", "。", t)  # 。。。 → 。
    t = re.sub(r"，{2,}", "，", t)
    t = re.sub(r"，([。！？!?])", r"\1", t)
    t = re.sub(r"^，+", "", t)

    def fix_line(ln: str) -> str:
        s = ln.rstrip()
        if not s.strip():
            return s
        s = re.sub(r"[，、；;：:\s]+$", "", s)
        # hanging openers left by ellipsis removal / hard clip
        s = re.sub(r"(?:的|与|和|及|或|而|且|并|就|都|也|还|仍|再|更|很|较)$", "", s)
        s = re.sub(r"[，、；;：:\s]+$", "", s)
        if close_sentence and s.strip() and not re.search(r"[。！？!?）」』】\"”']$", s):
            s = s.rstrip() + "。"
        return s

    if "\n" in t:
        return "\n".join(fix_line(ln) for ln in t.split("\n"))
    return fix_line(t)



def unwrap_whole_bold(text: str) -> str:
    """If almost the whole string is one **...** wrap, unwrap it (keep sparse inner later)."""
    t = str(text or "").strip()
    if not t:
        return t
    # single outer wrap covering >=80% of visible chars
    m = re.fullmatch(r"\*\*([^*]{1,120})\*\*", t)
    if m:
        return m.group(1).strip()
    # multiple wraps that cover nearly all chars → too heavy; keep only short spans
    parts = re.findall(r"\*\*([^*]+)\*\*", t)
    if parts:
        plain = re.sub(r"\*\*[^*]+\*\*", "", t)
        plain_len = len(re.sub(r"\s+", "", plain))
        bold_len = sum(len(re.sub(r"\s+", "", p)) for p in parts)
        # if one bold span is nearly the whole sentence, unwrap that span
        for p in parts:
            if len(re.sub(r"\s+", "", p)) >= max(12, int(0.72 * (bold_len + plain_len))):
                t = t.replace(f"**{p}**", p, 1)
        # drop spans longer than 12 chars (likely clause/sentence)
        def _shorten(mm: re.Match) -> str:
            inner = mm.group(1)
            if len(inner) > 12:
                return inner
            return mm.group(0)
        t = re.sub(r"\*\*([^*]+)\*\*", _shorten, t)
    return t


def _hard_clip(t: str, max_len: int) -> str:
    """Clip without ellipsis; prefer sentence end, else close with 。"""
    if len(t) <= max_len:
        return t
    cut = t[:max_len]
    ends = list(re.finditer(r"[。！？!?]", cut))
    if ends and ends[-1].end() >= max(8, max_len // 2):
        return cut[: ends[-1].end()]
    commas = list(re.finditer(r"[，、；;]", cut))
    if commas and commas[-1].end() >= max(8, max_len // 2):
        return ban_ellipsis(cut[: commas[-1].start()], close_sentence=True)
    cut = re.sub(r"[，、；;：:\s]+$", "", cut)
    cut = re.sub(r"(?:的|与|和|及|或|而|且|并)$", "", cut)
    return ban_ellipsis(cut, close_sentence=True)


def _clip_parse_line(val: str, max_len: int = 56) -> str:
    t = re.sub(r"\s+", " ", str(val or "").strip())
    t = re.sub(r"^(✨\s*)?(AI\s*秒读|核心结论|结论)[:：\s]*", "", t, flags=re.I).strip()
    t = re.sub(r"^(💡\s*)?(潜台词|点评|言外之意)[:：\s]*", "", t).strip()
    t = unwrap_whole_bold(t)
    t = ban_ellipsis(t, close_sentence=True)
    return _hard_clip(t, max_len)


def extract_parse(raw: str) -> dict:
    """Parse AI 秒读 JSON → read + subtext (two lines only)."""
    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    m = re.search(r"\{[\s\S]*\}", s)
    if m:
        s = m.group(0)
    try:
        obj = json.loads(s)
    except Exception:
        # Fallback: line-based plain text
        lines = [ln.strip() for ln in re.split(r"[\n\r]+", raw) if ln.strip()]
        read = ""
        subtext = ""
        for ln in lines:
            if re.match(r"^(✨\s*)?(AI\s*秒读)", ln, re.I) or (not read and not subtext):
                read = re.sub(r"^(✨\s*)?(AI\s*秒读)[:：\s]*", "", ln, flags=re.I).strip() or read
            elif re.match(r"^(💡\s*)?(潜台词)", ln):
                subtext = re.sub(r"^(💡\s*)?(潜台词)[:：\s]*", "", ln).strip()
            elif read and not subtext:
                subtext = ln
        obj = {"read": read, "subtext": subtext}
    read = _clip_parse_line(
        obj.get("read") or obj.get("conclusion") or obj.get("text") or "",
        56,
    )
    subtext = _clip_parse_line(
        obj.get("subtext") or obj.get("aside") or obj.get("sub") or "",
        48,
    )
    # Plain-paragraph fallback: first sentence → read, second → subtext
    if not read and not subtext:
        blob = re.sub(r"\s+", " ", raw.strip())
        parts = [p for p in re.split(r"(?<=[。！？!?])\s*", blob) if p]
        read = _clip_parse_line(parts[0] if parts else blob, 56)
        subtext = _clip_parse_line(parts[1] if len(parts) > 1 else "信号在标题之外，细节仍需交叉核验。", 48)
    if not read:
        read = "信息有限，暂无法给出可靠结论。"
    if not subtext:
        subtext = "信号在标题之外，细节仍需交叉核验。"
    # Normalize bold markers: allow *word* → **word** if single-pair emphasis
    def _norm_bold(t: str) -> str:
        t = re.sub(r"(?<!\*)\*([^*\n]{1,24})\*(?!\*)", r"**\1**", t)
        # drop unmatched leftover single *
        return t

    read = ban_ellipsis(_norm_bold(read), close_sentence=True)
    subtext = ban_ellipsis(_norm_bold(subtext), close_sentence=True)
    return {"read": read, "subtext": subtext}


def _clip_impact_line(val: str, max_len: int = 48) -> str:
    t = re.sub(r"\s+", " ", str(val or "").strip())
    t = re.sub(r"^(正向|负向|观察|机会|风险)[:：\s]*", "", t).strip()
    t = re.sub(r"(?<!\*)\*([^*\n]{1,24})\*(?!\*)", r"**\1**", t)
    t = unwrap_whole_bold(t)
    t = ban_ellipsis(t, close_sentence=True)
    return _hard_clip(t, max_len)


def _impact_slot(val, fallback_title: str, fallback_detail: str) -> dict:
    """Normalize one impact slot → {title, detail} from object / 'title|detail' / plain string."""
    title, detail = "", ""
    if isinstance(val, dict):
        title = str(val.get("title") or val.get("main") or val.get("结论") or "").strip()
        detail = str(val.get("detail") or val.get("why") or val.get("推导") or val.get("desc") or "").strip()
        tag = str(val.get("tag") or "").strip()
        if tag and not title and val.get("text"):
            title = str(val.get("text") or "").strip()
    elif isinstance(val, str):
        s = val.strip()
        if "|" in s:
            left, right = s.split("|", 1)
            title, detail = left.strip(), right.strip()
        else:
            title = s
    title = _clip_impact_line(title, 28)
    detail = _clip_impact_line(detail, 48)
    if not title:
        title = fallback_title
    if not detail:
        detail = fallback_detail
    return {"title": title, "detail": detail}


def extract_impact(raw: str) -> dict:
    """Parse impact JSON → positive/negative/observe as {title, detail} + confidence."""
    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    m = re.search(r"\{[\s\S]*\}", s)
    if m:
        s = m.group(0)
    try:
        obj = json.loads(s)
    except Exception:
        obj = {}

    # Optional rows: [{tag, title, detail}, ...]
    if isinstance(obj.get("rows"), list):
        by_tag = {}
        for item in obj["rows"]:
            if not isinstance(item, dict):
                continue
            tag = str(item.get("tag") or item.get("type") or "").strip().lower()
            if tag in ("pos", "positive", "正向", "+"):
                by_tag["positive"] = item
            elif tag in ("neg", "negative", "负向", "-"):
                by_tag["negative"] = item
            elif tag in ("neu", "observe", "watch", "观察"):
                by_tag["observe"] = item
        for k, v in by_tag.items():
            if k not in obj or not obj.get(k):
                obj[k] = v

    try:
        conf = int(round(float(obj.get("confidence"))))
    except Exception:
        conf = 40
    conf = max(0, min(100, conf))

    legacy = str(obj.get("body") or obj.get("text") or "").strip()
    positive = _impact_slot(
        obj.get("positive") or obj.get("pos"),
        "暂无明显正向机会",
        "与该行业直接利好有限，建议结合原文再核。" if not legacy else _clip_impact_line(legacy, 48),
    )
    negative = _impact_slot(
        obj.get("negative") or obj.get("neg"),
        "暂无明显负向风险",
        "未见直接冲击路径，建议结合原文再核。",
    )
    observe = _impact_slot(
        obj.get("observe") or obj.get("watch") or ({"title": "关联有限", "detail": legacy} if legacy else None),
        "关联有限，继续观察",
        "与该行业绑定弱，交叉核验后再判断。",
    )
    return {
        "positive": positive,
        "negative": negative,
        "observe": observe,
        "confidence": conf,
    }


def extract_turns(raw: str) -> list:
    """Parse debate JSON; normalize to exactly 4 turns opt→con→opt→con."""
    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    m = re.search(r"\[[\s\S]*\]", s)
    if m:
        s = m.group(0)
    arr = json.loads(s)
    raw_turns = []
    for item in arr:
        role = str(item.get("role") or "").lower()
        if role not in ("opt", "con"):
            role = "opt" if len(raw_turns) % 2 == 0 else "con"
        text = str(item.get("text") or "").strip()
        # keep newlines for 【搜证发言】/【反驳与立论】 blocks; soft-cap ~180 CJK
        if text:
            text = ban_ellipsis(text[:220], close_sentence=False)
            if text:
                raw_turns.append({"role": role, "text": text})
    # Force canonical 4-round order regardless of model drift
    wanted = ("opt", "con", "opt", "con")
    out = []
    pool = list(raw_turns)
    for want in wanted:
        pick = None
        for i, t in enumerate(pool):
            if t["role"] == want:
                pick = pool.pop(i)
                break
        if pick is None and pool:
            pick = pool.pop(0)
            pick = {"role": want, "text": pick["text"]}
        if pick:
            out.append({"role": want, "text": pick["text"]})
    return out[:4]


class Handler(SimpleHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, code: int, obj: dict):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html", "/morning-feed.html"):
            self.path = "/morning-feed.html"
            return SimpleHTTPRequestHandler.do_GET(self)
        if path == "/api/health":
            key, base, model = cfg()
            sched = feed_schedule()
            stats = cache_stats()
            return self._json(200, {
                "ok": True,
                "hasKey": bool(key),
                "base": base,
                "model": model,
                "envPath": str(ENV_PATH),
                "showLimit": FEED_SHOW_LIMIT,
                "tz": FEED_TZ_NAME,
                "dailyRefresh": sched["dailyRefresh"],
                "dailyCacheWipe": sched["dailyCacheWipe"],
                "editionDay": sched["editionDay"],
                "nextRefreshAt": sched["nextRefreshAt"],
                "nextCacheWipeAt": sched["nextCacheWipeAt"],
                "aiCacheEntries": stats["entries"],
                "aiCacheDir": stats["dir"],
            })
        if path == "/api/schedule":
            return self._json(200, feed_schedule())
        if path == "/api/cache":
            return self._json(200, cache_stats())
        if path == "/__mtime":
            try:
                st = HTML.stat()
                sched = feed_schedule()
                return self._json(200, {
                    "mtime": st.st_mtime,
                    "mtimeMs": int(st.st_mtime * 1000),
                    "size": st.st_size,
                    "file": "morning-feed.html",
                    "showLimit": FEED_SHOW_LIMIT,
                    "tz": FEED_TZ_NAME,
                    "dailyRefresh": sched["dailyRefresh"],
                    "editionDay": sched["editionDay"],
                })
            except Exception as e:
                return self._json(500, {"error": str(e)})
        return SimpleHTTPRequestHandler.do_GET(self)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path != "/api/generate":
            self.send_error(404)
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            return self._json(400, {"error": "invalid json"})

        mode = str(payload.get("mode") or "").strip()
        title = str(payload.get("title") or "").strip()[:400]
        summary = str(payload.get("summary") or "").strip()[:1200]
        topic = str(payload.get("topicLabel") or payload.get("topic") or "").strip()[:120]
        user_text = str(payload.get("userText") or "").strip()[:200]
        industry = str(payload.get("industry") or "").strip()[:80]
        if mode not in ("parse", "impact", "deep", "debate", "debate_followup"):
            return self._json(400, {"error": "bad mode"})
        if not title:
            return self._json(400, {"error": "title required"})
        if mode == "impact" and not industry:
            return self._json(400, {"error": "industry required"})
        if mode == "debate_followup" and not user_text:
            return self._json(400, {"error": "userText required"})

        identity = news_identity(payload, title)
        industry_key = industry if mode == "impact" else ""
        ck_fname, ck_raw = cache_key_parts(mode, identity, industry_key) if mode in CACHEABLE_MODES else ("", "")

        # Cross-user disk cache (skip SiliconFlow). 路人甲 followups stay live.
        if mode in CACHEABLE_MODES:
            hit = cache_get(mode, identity, industry_key)
            if hit is not None:
                out = dict(hit)
                out["cached"] = True
                out["cacheKey"] = ck_raw
                return self._json(200, out)

        def produce():
            # Re-check cache inside coalesced critical section
            if mode in CACHEABLE_MODES:
                again = cache_get(mode, identity, industry_key)
                if again is not None:
                    out = dict(again)
                    out["cached"] = True
                    out["cacheKey"] = ck_raw
                    return out

            messages = prompt_for(mode, title, summary, topic, user_text, industry=industry)
            if mode == "parse":
                max_tok, temp = 320, 0.4
            elif mode == "impact":
                max_tok, temp = 450, 0.4
            elif mode == "deep":
                max_tok, temp = 900, 0.55
            else:
                max_tok, temp = 1400, 0.8
            text = chat_complete(messages, temperature=temp, max_tokens=max_tok)

            if mode == "parse":
                parsed = extract_parse(text)
                combined = f"✨ AI 秒读：{parsed['read']}\n💡 潜台词：{parsed['subtext']}"
                resp = {
                    "mode": mode,
                    "read": parsed["read"],
                    "subtext": parsed["subtext"],
                    "text": combined,
                }
            elif mode == "deep":
                text2 = re.sub(r"^(AI解析|AI\s*秒读|深度解读)[:：\s]*", "", text).strip()
                text2 = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", text2)
                text2 = re.sub(r"\n{3,}", "\n\n", text2).strip()
                text2 = ban_ellipsis(text2, close_sentence=False)
                resp = {"mode": mode, "text": text2}
            elif mode == "impact":
                impact = extract_impact(text)
                for k in ("positive", "negative", "observe"):
                    slot = impact.get(k) or {}
                    impact[k] = {
                        "title": ban_ellipsis(slot.get("title") or "", close_sentence=True),
                        "detail": ban_ellipsis(slot.get("detail") or "", close_sentence=True),
                    }
                resp = {"mode": mode, **impact}
            else:
                # debate | debate_followup
                turns = extract_turns(text)
                if not turns:
                    raise RuntimeError("empty turns:" + text[:120])
                for t in turns:
                    t["text"] = ban_ellipsis(t.get("text") or "", close_sentence=False)
                resp = {"mode": mode, "turns": turns}

            if mode in CACHEABLE_MODES:
                cache_put(mode, identity, industry_key, resp)
                resp = dict(resp)
                resp["cached"] = False
                resp["cacheKey"] = ck_raw
            return resp

        try:
            if mode in CACHEABLE_MODES:
                result = inflight_run(ck_fname or ck_raw, produce)
            else:
                result = produce()
            return self._json(200, result)
        except Exception as e:
            msg = str(e)
            if msg.startswith("empty turns:"):
                return self._json(502, {"error": "empty turns", "rawPreview": msg[len("empty turns:"):]})
            msg = re.sub(r"sk-[A-Za-z0-9]+", "sk-***", msg)
            return self._json(502, {"error": msg})

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt, *args):
        # avoid noisy mtime/schedule/cache + never log bodies/keys
        if args and isinstance(args[0], str):
            a0 = args[0]
            if (
                "/__mtime" in a0
                or "/api/health" in a0
                or "/api/schedule" in a0
                or "/api/cache" in a0
            ):
                return
        super().log_message(fmt, *args)


def main():
    key, base, model = cfg()
    sched = feed_schedule()
    stats = cache_stats()
    start_cache_wipe_scheduler()
    print(
        f"morning-feed proxy on 0.0.0.0:{PORT} model={model} hasKey={bool(key)} "
        f"env={ENV_PATH} showLimit={FEED_SHOW_LIMIT} refresh={sched['dailyRefresh']} "
        f"cacheWipe={sched['dailyCacheWipe']} {FEED_TZ_NAME} "
        f"edition={sched['editionDay']} aiCache={stats['entries']} "
        f"nextWipe={sched['nextCacheWipeAt']}",
        flush=True,
    )
    # do not print key or base auth
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
