"""
微信聊天分析引擎 — 封装 wx-cli，输出结构化分析数据
含深度关系分析：阶段检测、亲密度指数、情绪信号、发展建议
"""

import json
import subprocess
import re
import shlex
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from collections import Counter, defaultdict
from typing import Any

from llm import generate_advice_ai, generate_phase_insight

# ── In-memory wx cache (TTL 30s, process lifetime) ──
_wx_cache = {}
_WX_CACHE_TTL = 30  # seconds

# ── wx-cli path detection ──
def _wx_binary() -> str:
    """Find wx-cli binary: bundled copy first, then system PATH."""
    import sys
    if getattr(sys, 'frozen', False):
        # PyInstaller bundle: check alongside the executable
        import os as _os
        bundled = _os.path.join(_os.path.dirname(sys.executable), 'wx')
        if _os.path.exists(bundled):
            return bundled
        # Also check in _MEIPASS
        meipass = _os.path.join(sys._MEIPASS, 'wx')
        if _os.path.exists(meipass):
            return meipass
    return 'wx'  # fallback: rely on system PATH


def _q(s: str) -> str:
    """Shell-quote a string for safe use in wx-cli commands."""
    return shlex.quote(s)


def _run_wx(cmd: str) -> Any:
    """Run a wx-cli command and return parsed JSON. Caches history calls for 30s."""
    # Cache wx history calls (most expensive, most repeated)
    if "history" in cmd:
        cache_key = cmd
        now = time.time()
        if cache_key in _wx_cache:
            entry = _wx_cache[cache_key]
            if now - entry["ts"] < _WX_CACHE_TTL:
                return entry["data"]
        result = _run_wx_raw(cmd)
        _wx_cache[cache_key] = {"ts": now, "data": result}
        # Prune old entries occasionally
        if len(_wx_cache) > 200:
            _wx_cache.clear()
        return result
    return _run_wx_raw(cmd)


def _run_wx_raw(cmd: str) -> Any:
    """Run a wx-cli command and return parsed JSON (no cache)."""
    # Resolve wx-cli path: bundled copy first, then system PATH
    wx_bin = _wx_binary()
    cmd = cmd.replace('wx ', wx_bin + ' ', 1)
    result = subprocess.run(
        cmd, shell=True, capture_output=True, text=True, timeout=90
    )
    output = result.stdout.strip()
    stderr_output = result.stderr.strip()

    # If command completely failed, raise with useful diagnostics
    if result.returncode != 0 and not output:
        err_msg = stderr_output or f"exit code {result.returncode}"
        # Check common causes
        if "Operation not permitted" in err_msg or "not permitted" in err_msg:
            err_msg += "\n→ 请授予完全磁盘访问权限：系统设置 → 隐私与安全性 → 完全磁盘访问"
        elif "cannot execute" in err_msg.lower() or "bad cpu" in err_msg.lower():
            err_msg += "\n→ 请运行: xattr -cr /Applications/WeChat\\ Analyzer.app"
        elif "No such file" in err_msg:
            err_msg += f"\n→ wx-cli 路径: {wx_bin}"
        raise RuntimeError(f"wx-cli 调用失败: {err_msg}\n命令: {cmd}")

    idx = output.find("[")
    if idx >= 0:
        output = output[idx:]
    if not output:
        return [] if cmd.strip().endswith("--json") else {}
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        lines = output.strip().split("\n")
        for line in lines:
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
        return {}

# Module-level cache
_sessions_cache = None
_sessions_cache_time = 0
_CONTACTS_CACHE = None
_CONTACTS_CACHE_TIME = 0
_SESSIONS_CACHE_TTL = 60  # seconds — sessions list changes rarely

def _get_sessions():
    """Get sessions list, cached for 60s."""
    global _sessions_cache, _sessions_cache_time
    now = time.time()
    if _sessions_cache is not None and (now - _sessions_cache_time) < _SESSIONS_CACHE_TTL:
        return _sessions_cache
    try:
        raw = _run_wx("wx sessions -n 9999 --json")
        if isinstance(raw, list) and len(raw) > 0:
            _sessions_cache = raw
            _sessions_cache_time = now
            return _sessions_cache
    except Exception:
        pass
    # Fallback: read from cached decrypted SQLite
    _sessions_cache = _get_sessions_from_cache()
    _sessions_cache_time = now
    return _sessions_cache


def _get_sessions_from_cache() -> list[dict]:
    """Read sessions from cached decrypted SQLite as fallback."""
    import sqlite3, os, glob
    cache_dir = os.path.expanduser("~/.wx-cli/cache")
    if not os.path.isdir(cache_dir):
        return []

    # Find session cache DB (contains SessionTable)
    session_db = None
    contact_db = None
    for f in glob.glob(os.path.join(cache_dir, "*.db")):
        try:
            conn = sqlite3.connect(f)
            cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='SessionTable'")
            if cur.fetchone():
                session_db = f
            conn.close()
        except Exception:
            continue

    for f in glob.glob(os.path.join(cache_dir, "*.db")):
        try:
            conn = sqlite3.connect(f)
            cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='contact'")
            if cur.fetchone():
                contact_db = f
            conn.close()
        except Exception:
            continue

    if not session_db:
        return []

    # Build display name map from contact cache
    name_map = {}
    if contact_db:
        try:
            conn = sqlite3.connect(contact_db)
            for row in conn.execute("SELECT username, nick_name, remark FROM contact"):
                uname = row[0] or ""
                nick = row[1] or ""
                remark = row[2] or ""
                name_map[uname] = remark or nick or uname
            conn.close()
        except Exception:
            pass

    results = []
    try:
        conn = sqlite3.connect(session_db)
        for row in conn.execute(
            "SELECT username, summary, sort_timestamp FROM SessionTable WHERE type=0 ORDER BY sort_timestamp DESC"
        ):
            username = row[0] or ""
            summary = row[1] or ""
            ts = row[2] or 0

            chat_type = "group" if username.endswith("@chatroom") else "private"
            display_name = name_map.get(username, username)

            results.append({
                "chat": display_name,
                "chat_type": chat_type,
                "username": username,
                "summary": summary[:80],
            })
        conn.close()
    except Exception:
        return []

    return results

def list_contacts(query: str = "") -> list[dict]:
    """Search contacts and groups.


    Private contacts: ONLY from sessions with chat_type=private (friends).
    Groups: from sessions + contacts (for @chatroom entries).
    This ensures group members who are not friends don't appear as private chat options.
    """

    def _raw_id(name: str) -> bool:
        """Check if a contact name is a raw/internal ID, not a real display name."""
        if not name or not name.strip():
            return True
        # Enterprise WeChat group/user raw IDs
        if '@qy_g' in name or '@qy_u' in name:
            return True
        # OpenIM platform users
        if '@openim' in name:
            return True
        # Hardcoded system entries
        if '@hardcode' in name:
            return True
        # Pure-numeric chatroom IDs without a proper name (e.g. "45287246329@chatroom")
        if re.match(r'^\d+@chatroom$', name):
            return True
        # Enterprise WeChat raw pattern: ww[digits]_[digits]...[suffix]
        if re.match(r'^ww\d', name):
            return True
        return False

    # Primary source: sessions (authoritative for friends, cached 60s)
    sessions = _get_sessions()

    results = []
    seen = set()

    system_usernames = {
        "notifymessage", "brandsessionholder", "brandservicesessionholder",
        "medianote", "weixin", "qqsafe", "filehelper", "mphelper",
        "@placeholder_foldgroup",
    }
    skip_names = {"服务通知", "微信团队", "QQ安全中心", "文件传输助手", "公众号安全助手"}

    for s in sessions:
        if not isinstance(s, dict):
            continue
        name = s.get("chat", "").strip()
        chat_type = s.get("chat_type", "")
        username = s.get("username", "")

        # Skip system/folded/official entries
        if username in system_usernames or chat_type in ("folded", "official_account"):
            continue
        if name in skip_names or not name:
            continue

        # Skip raw/internal IDs that aren't real display names
        if _raw_id(name):
            continue

        # Normalize chat_type
        if chat_type not in ("group", "private"):
            chat_type = "group" if username.endswith("@chatroom") else "private"

        # Apply query filter
        if query and query.lower() not in name.lower():
            continue

        if name not in seen:
            seen.add(name)
            seen.add(username)
            results.append({
                "name": name,
                "chat_type": chat_type,
                "username": username,
            })

    # Supplement groups from wx contacts (only @chatroom entries)
    if query:
        cmd = f'wx contacts --query {_q(query)} --json'
    else:
        cmd = "wx contacts -n 500 --json"
    contacts = _run_wx(cmd)
    if isinstance(contacts, list):
        for c in contacts:
            name = (c.get("display") or c.get("nickname") or c.get("remark") or c.get("name") or "").strip()
            username = c.get("username", "")
            if not name or username in seen or name in seen:
                continue
            if username in system_usernames or name in skip_names:
                continue
            if _raw_id(name):
                continue
            # ONLY add groups from contacts — not private contacts
            if username.endswith("@chatroom"):
                seen.add(username)
                seen.add(name)
                results.append({
                    "name": name,
                    "chat_type": "group",
                    "username": username,
                })

    # Supplement: probe contacts with private chat history that aren't in sessions
    # Handles friends who haven't chatted recently but are still valid contacts
    if not query and len(results) < 600:
        # Sample up to 30 contacts not in sessions, verify they have chat history
        probed = 0
        for c in contacts:
            name = (c.get("display") or c.get("nickname") or c.get("remark") or c.get("name") or "").strip()
            username = c.get("username", "")
            if not name or username in seen or name in seen:
                continue
            if username in system_usernames or name in skip_names:
                continue
            if _raw_id(name):
                continue
            if username.endswith("@chatroom"):
                continue
            # Quick probe: has private chat history?
            try:
                cmd = f'wx history {_q(name)} -n 1 --json'
                probe = _run_wx(cmd)
                if isinstance(probe, list) and probe:
                    probe = [p for p in probe if isinstance(p, dict)]
                    if probe:
                        seen.add(username)
                        seen.add(name)
                        results.append({
                            "name": name,
                            "chat_type": "private",
                            "username": username,
                        })
            except Exception:
                pass  # contact not found in wx history, skip gracefully
            probed += 1
            if probed >= 30:
                break

    return results


def _parse_timestamp(ts_str: str) -> datetime:
    try:
        return datetime.strptime(ts_str, "%Y-%m-%d %H:%M")
    except ValueError:
        return datetime.strptime(ts_str, "%Y-%m-%d")


def _safe_timestamp(m: dict) -> int:
    ts = m.get("timestamp", 0)
    if isinstance(ts, str):
        try:
            return int(ts)
        except (ValueError, TypeError):
            return 0
    return int(ts) if ts else 0


# ═══════════════════════════════════════════
#  Emotional Keyword Dictionaries
# ═══════════════════════════════════════════

AFFECTION_WORDS = [
    "想你", "爱你", "喜欢你", "爱你哟", "亲亲", "宝贝", "抱抱", "么么哒",
    "想你了", "爱", "心动", "甜蜜", "温暖", "在意", "在乎",
]

CARE_WORDS = [
    "吃饭", "睡觉", "注意", "辛苦", "累了", "休息", "小心", "多穿",
    "多喝", "别熬夜", "照顾好", "别太累", "注意安全", "路上小心",
    "好点没", "早点睡", "按时", "别饿", "身体",
]

CONFLICT_WORDS = [
    "烦", "生气", "算了", "无语", "受不了", "别说了", "不想",
    "别管", "随便", "够了", "忍", "呵呵",
]

FUTURE_WORDS = [
    "以后", "永远", "在一起", "将来", "下次", "到时候", "总有一天",
    "以后我们", "计划", "安排", "等",
]

TEASING_WORDS = [
    "猪", "笨蛋", "坏", "傻瓜", "傻子", "臭", "憨", "蠢",
]


def _count_keywords(text: str, keywords: list[str]) -> dict:
    """Count keyword hits in text, return {keyword: count, ...}."""
    hits = {}
    for kw in keywords:
        c = text.count(kw)
        if c > 0:
            hits[kw] = c
    return hits


def _safe_div(a, b, default=0):
    return round(a / b, 1) if b else default


# ═══════════════════════════════════════════
#  Analysis Functions
# ═══════════════════════════════════════════

def _parse_messages(messages: list[dict]) -> list[dict]:
    """Add parsed datetime fields to messages."""
    enriched = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        try:
            dt = datetime.strptime(m["time"], "%Y-%m-%d %H:%M")
        except (ValueError, KeyError):
            continue
        enriched.append({
            **m,
            "dt": dt,
            "date": dt.strftime("%Y-%m-%d"),
            "hour": dt.hour,
            "month": dt.strftime("%Y-%m"),
            "weekday": dt.weekday(),  # 0=Mon, 6=Sun
        })
    enriched.sort(key=lambda x: x.get("timestamp", 0))
    return enriched


def _detect_phases(enriched: list[dict], user_name: str, identity: str = "lover") -> dict:
    """Detect relationship phases from monthly volume trends. Identity-aware labels."""
    IDENTITY_PHASES = {
        "lover": {
            "initial": ("初识期 🌱", "数据不足，尚在建立联系阶段"),
            "escalation": ("爆发期 🔥", "近3月消息量是前期的 {n} 倍，关系快速升温"),
            "growth": ("上升期 📈", "消息量稳步增长 {n} 倍，互动日益密切"),
            "cooling": ("冷淡期 ❄️", "近期消息量下降至前期的 {n} 倍，需要关注"),
            "stable": ("稳定期 💚", "高频稳定互动，关系已进入舒适区"),
            "building": ("构建期 🌱", "逐步建立互动节奏，尚有增长空间"),
        },
        "friend": {
            "initial": ("点头之交 👋", "互动较少，关系尚浅"),
            "escalation": ("热络期 🔥", "近期互动激增 {n} 倍，关系快速升温"),
            "growth": ("升温期 📈", "互动稳步增长 {n} 倍，越来越熟"),
            "cooling": ("疏远期 🌥️", "近期互动减少至前期的 {n} 倍，可能各忙各的"),
            "stable": ("老铁 🤝", "稳定互动，默契十足"),
            "building": ("发展期 🌱", "逐步建立友谊，有发展空间"),
        },
        "colleague": {
            "initial": ("初识 🏢", "沟通较少，还在建立工作关系"),
            "escalation": ("紧密协作 ⚡", "近期沟通激增 {n} 倍，项目协作频繁"),
            "growth": ("协作增多 📈", "沟通稳步增长 {n} 倍，配合越来越顺畅"),
            "cooling": ("沟通减少 📉", "近期沟通下降至前期的 {n} 倍，可能项目收尾或调岗"),
            "stable": ("稳定协作 🤝", "沟通节奏稳定，配合默契"),
            "building": ("建立默契 🌱", "逐步磨合工作节奏，有提升空间"),
        },
        "family": {
            "initial": ("疏于联系 📞", "联系很少，需要多关心"),
            "escalation": ("亲密期 💕", "近期联系激增 {n} 倍，家庭互动频繁"),
            "growth": ("回暖期 📈", "联系稳步增长 {n} 倍，关系回暖"),
            "cooling": ("疏远期 🌥️", "近期联系减少至前期的 {n} 倍，需要主动问候"),
            "stable": ("稳定联系 🤝", "联系节奏稳定，亲情在线"),
            "building": ("重建联系 🌱", "逐步恢复联系，亲情可培养"),
        },
        "business": {
            "initial": ("初步接触 💼", "合作尚在试探阶段"),
            "escalation": ("深度合作 🔥", "近期沟通激增 {n} 倍，合作进入深水区"),
            "growth": ("合作推进 📈", "沟通稳步增长 {n} 倍，合作持续推进"),
            "cooling": ("合作放缓 📉", "近期沟通下降至前期的 {n} 倍，可能项目搁置"),
            "stable": ("稳定合作 🤝", "合作节奏稳定，关系成熟"),
            "building": ("建立关系 🌱", "逐步建立合作信任，有发展空间"),
        },
        "service": {
            "initial": ("新客户 🆕", "刚开始服务，建立信任中"),
            "escalation": ("高频互动 🔥", "近期咨询激增 {n} 倍，客户需求旺盛"),
            "growth": ("服务增多 📈", "互动稳步增长 {n} 倍，客户依赖加深"),
            "cooling": ("减少咨询 📉", "近期互动下降至前期的 {n} 倍，可能问题已解决或流失"),
            "stable": ("稳定服务 ✓", "互动节奏稳定，客户满意度高"),
            "building": ("建立信任 🌱", "逐步建立服务关系，有提升空间"),
        },
    }
    labels = IDENTITY_PHASES.get(identity, IDENTITY_PHASES["lover"])

    months = defaultdict(lambda: {"her": 0, "me": 0, "total": 0})
    for m in enriched:
        months[m["month"]]["total"] += 1
        if m.get("sender") == "":
            months[m["month"]]["her"] += 1
        else:
            months[m["month"]]["me"] += 1

    sorted_months = sorted(months.keys())
    if len(sorted_months) < 2:
        label, desc = labels["initial"]
        return {"phase": "initial", "label": label, "description": desc,
                "timeline": [{"month": k, **v} for k, v in months.items()]}

    volumes = [months[m]["total"] for m in sorted_months]
    max_vol = max(volumes) if volumes else 1
    growth_ratio = 1.0

    if len(sorted_months) >= 3:
        recent_3 = sum(volumes[-3:])
        earlier = sum(volumes[:-3]) if len(volumes) > 3 else sum(volumes[:len(volumes)//2])
        earlier_count = len(volumes) - 3 if len(volumes) > 3 else len(volumes) // 2
        recent_avg = recent_3 / min(3, len(volumes))
        earlier_avg = earlier / max(1, earlier_count)
        growth_ratio = _safe_div(recent_avg, earlier_avg)

        if growth_ratio > 3.0:
            phase = "escalation"
        elif growth_ratio > 1.5:
            phase = "growth"
        elif growth_ratio < 0.5:
            phase = "cooling"
        elif max_vol > 50 and growth_ratio > 0.7:
            phase = "stable"
        else:
            phase = "building"
    else:
        phase = "initial"

    label, desc_tpl = labels.get(phase, labels["initial"])
    desc = desc_tpl.format(n=f"{growth_ratio:.1f}")

    return {
        "phase": phase,
        "label": label,
        "description": desc,
        "timeline": [{"month": k, **v} for k, v in months.items()],
        "volume_trend": volumes,
        "month_labels": sorted_months,
    }


def _compute_intimacy(enriched: list[dict], user_name: str, reply_stats: dict) -> dict:
    """Compute intimacy index (0-100) with sub-scores."""
    scores = {}

    # 1. Response speed (30%)
    if reply_stats and reply_stats.get("total_exchanges"):
        fast_rate = reply_stats["under_1min"] / reply_stats["total_exchanges"]
        scores["response_speed"] = min(100, round(fast_rate * 100))
    else:
        scores["response_speed"] = 50

    # 2. Message balance (20%)
    her_count = sum(1 for m in enriched if m.get("sender") == "")
    me_count = sum(1 for m in enriched if m.get("sender") != "")
    total = her_count + me_count
    if total:
        balance = her_count / total
        # 50/50 = 100, deviation penalizes
        scores["balance"] = max(0, 100 - int(abs(balance - 0.5) * 200))
    else:
        scores["balance"] = 50

    # 3. Volume trend (15%)
    months_data = defaultdict(int)
    for m in enriched:
        months_data[m["month"]] += 1
    sorted_m = sorted(months_data.keys())
    if len(sorted_m) >= 2:
        recent_avg = sum(months_data[m] for m in sorted_m[-2:]) / min(2, len(sorted_m))
        earlier_avg = sum(months_data[m] for m in sorted_m[:-2]) / max(1, len(sorted_m) - 2)
        if earlier_avg > 0:
            ratio = recent_avg / earlier_avg
            scores["trend"] = min(100, max(0, round(50 + (ratio - 1) * 50)))
        else:
            scores["trend"] = 60
    else:
        scores["trend"] = 50

    # 4. Late-night share (15%)
    late_night = sum(1 for m in enriched if m["hour"] >= 22 or m["hour"] <= 3)
    late_ratio = _safe_div(late_night, len(enriched))
    scores["late_night"] = min(100, round(late_ratio * 400))  # 25% → 100

    # 5. Affection density (10%)
    all_text = " ".join(m.get("content", "") for m in enriched if m.get("type") == "文本")
    affection_hits = sum(_count_keywords(all_text, AFFECTION_WORDS).values())
    per_1000 = _safe_div(affection_hits * 1000, len(enriched))
    scores["affection"] = min(100, round(per_1000 * 5))  # 20/1000 → 100

    # 6. Voice + image share (5%)
    voice_img = sum(1 for m in enriched if m.get("type") in ("语音", "图片"))
    vi_ratio = _safe_div(voice_img, len(enriched))
    scores["multimodal"] = min(100, round(vi_ratio * 250))  # 40% → 100

    # 7. Active day ratio (5%)
    all_dates = sorted(set(m["date"] for m in enriched))
    if len(all_dates) >= 2:
        start = datetime.strptime(all_dates[0], "%Y-%m-%d")
        end = datetime.strptime(all_dates[-1], "%Y-%m-%d")
        calendar_days = max(1, (end - start).days + 1)
        scores["active_days"] = min(100, round(len(all_dates) / calendar_days * 100))
    else:
        scores["active_days"] = 50

    # Weighted total
    weights = {
        "response_speed": 0.30, "balance": 0.20, "trend": 0.15,
        "late_night": 0.15, "affection": 0.10, "multimodal": 0.05,
        "active_days": 0.05,
    }
    total_score = round(sum(scores[k] * weights[k] for k in scores))

    # Per-metric tips for low scores
    metric_tips = {
        "回复速度": (60, "你的回复偏慢，试着在她发消息后1-2分钟内回应。快速回复传递'你在意'的信号"),
        "消息平衡": (40, "她发言明显更多，试着每天主动分享1-2件日常——照片、趣事、想法"),
        "趋势增长": (40, "近期互动在减少，从她感兴趣的话题自然切入，别太刻意"),
        "深夜互动": (30, "深夜聊天较少，睡前一句晚安也能拉近距离"),
        "情感密度": (30, "亲昵词太少，试着在关心中夹带'想你'：'想你所以问问你吃了没'"),
        "多模态": (30, "几乎不发语音/图片，偶尔发张照片或用语音回复，更有温度"),
        "活跃天数": (50, "互动不够连续，试着每天保持至少一次联系，哪怕是表情"),
    }

    sub_scores_out = []
    for name, sc in [("回复速度", scores["response_speed"]), ("消息平衡", scores["balance"]),
                      ("趋势增长", scores["trend"]), ("深夜互动", scores["late_night"]),
                      ("情感密度", scores["affection"]), ("多模态", scores["multimodal"]),
                      ("活跃天数", scores["active_days"])]:
        entry = {"name": name, "score": sc, "weight": int(weights.get({
            "回复速度": "response_speed", "消息平衡": "balance", "趋势增长": "trend",
            "深夜互动": "late_night", "情感密度": "affection", "多模态": "multimodal",
            "活跃天数": "active_days",
        }[name], 0.05) * 100)}
        if name in metric_tips:
            threshold, tip = metric_tips[name]
            if sc < threshold:
                entry["alert"] = True
                entry["tip"] = tip
            else:
                entry["alert"] = False
        sub_scores_out.append(entry)

    # Label
    if total_score >= 80:
        label = "亲密 💕"
    elif total_score >= 65:
        label = "温暖 ☀️"
    elif total_score >= 50:
        label = "稳定 🤝"
    elif total_score >= 35:
        label = "疏离 🌥️"
    else:
        label = "冷淡 ❄️"

    return {
        "total": total_score,
        "label": label,
        "sub_scores": sub_scores_out,
    }


def _analyze_signals(enriched: list[dict], user_name: str) -> dict:
    """Analyze emotional signals in the conversation."""
    all_text = " ".join(m.get("content", "") for m in enriched if m.get("type") == "文本")
    total_msgs = len(enriched)

    affection = _count_keywords(all_text, AFFECTION_WORDS)
    care = _count_keywords(all_text, CARE_WORDS)
    conflict = _count_keywords(all_text, CONFLICT_WORDS)
    future = _count_keywords(all_text, FUTURE_WORDS)
    teasing = _count_keywords(all_text, TEASING_WORDS)

    per_1000 = lambda hits: round(sum(hits.values()) * 1000 / max(1, total_msgs), 1)

    # Determine who uses which signals more
    her_text = " ".join(m.get("content", "") for m in enriched if m.get("type") == "文本" and m.get("sender") == "")
    me_text = " ".join(m.get("content", "") for m in enriched if m.get("type") == "文本" and m.get("sender") != "")

    return {
        "signals": [
            {
                "name": "亲昵",
                "icon": "💕",
                "density": per_1000(affection),
                "benchmark": 5,
                "top_words": sorted(affection, key=affection.get, reverse=True)[:5],
                "color": "#db61a2",
            },
            {
                "name": "关心",
                "icon": "🤗",
                "density": per_1000(care),
                "benchmark": 10,
                "top_words": sorted(care, key=care.get, reverse=True)[:5],
                "color": "#3fb950",
            },
            {
                "name": "冲突",
                "icon": "⚡",
                "density": per_1000(conflict),
                "benchmark": 5,
                "top_words": sorted(conflict, key=conflict.get, reverse=True)[:5],
                "color": "#f85149",
            },
            {
                "name": "未来",
                "icon": "🔮",
                "density": per_1000(future),
                "benchmark": 10,
                "top_words": sorted(future, key=future.get, reverse=True)[:5],
                "color": "#a371f7",
            },
            {
                "name": "调侃",
                "icon": "😏",
                "density": per_1000(teasing),
                "benchmark": 3,
                "top_words": sorted(teasing, key=teasing.get, reverse=True)[:5],
                "color": "#d2991d",
            },
        ],
        "summary": _signal_summary(affection, care, conflict, future, teasing),
    }


def _signal_summary(affection, care, conflict, future, teasing) -> str:
    """Generate a human-readable signal summary."""
    total_affection = sum(affection.values())
    total_care = sum(care.values())
    total_conflict = sum(conflict.values())

    if total_conflict > total_affection:
        return "冲突信号超过亲昵信号，关系可能处于紧张期，需多表达温暖"
    if total_care > total_affection * 2:
        return "关心远多于亲昵，建议增加直接情感表达，不只是'照顾'模式"
    if total_affection > total_care:
        return "亲昵信号充足，情感表达直接，关系温度良好"
    return "情绪信号分布均衡，沟通模式健康"


# ═══════════════════════════════════════════
#  Identity-Specific Signal Keywords
# ═══════════════════════════════════════════

IDENTITY_SIGNAL_KEYWORDS = {
    "lover": {},  # uses default _analyze_signals
    "friend": {
        "轻松": {"words": ["哈哈", "笑死", "笑", "搞笑", "逗", "哈哈哈哈", "hhh", "hhhh"], "icon": "😄", "color": "#d2991d"},
        "分享": {"words": ["推荐", "安利", "这个", "你看", "分享", "好玩", "有趣"], "icon": "📤", "color": "#3fb950"},
        "吐槽": {"words": ["烦", "无语", "离谱", "受不了", "气死", "绝了", "有毒"], "icon": "😤", "color": "#f85149"},
        "关心": {"words": ["注意", "小心", "好好", "休息", "保重", "没事吧", "还好吗"], "icon": "🤗", "color": "#58a6ff"},
        "邀约": {"words": ["一起", "约", "聚", "出来", "见面", "吃饭", "喝酒", "玩"], "icon": "📅", "color": "#a371f7"},
    },
    "colleague": {
        "专业": {"words": ["方案", "需求", "进度", "确认", "完成", "上线", "测试", "文档", "数据"], "icon": "📋", "color": "#58a6ff"},
        "同步": {"words": ["同步", "对齐", "更新", "通知", "提醒", "关注", "看一下"], "icon": "🔄", "color": "#3fb950"},
        "协作": {"words": ["帮忙", "协助", "支持", "配合", "一起", "对接", "协调"], "icon": "🤝", "color": "#d2991d"},
        "效率": {"words": ["尽快", "马上", "立即", "抓紧", "加急", "deadline", "截止"], "icon": "⚡", "color": "#f85149"},
        "反馈": {"words": ["收到", "明白", "了解", "好的", "没问题", "不行", "需要改"], "icon": "💬", "color": "#a371f7"},
    },
    "family": {
        "关心": {"words": ["吃了吗", "身体", "注意", "冷", "热", "多穿", "早点睡", "别太累", "照顾好"], "icon": "🤗", "color": "#3fb950"},
        "生活": {"words": ["最近", "忙", "工作", "家里", "孩子", "上学", "放假", "周末"], "icon": "🏠", "color": "#58a6ff"},
        "经济": {"words": ["钱", "工资", "红包", "转账", "买", "花", "省", "贵", "便宜"], "icon": "💰", "color": "#d2991d"},
        "叮嘱": {"words": ["记得", "别忘了", "一定要", "千万", "必须", "不要", "别"], "icon": "📢", "color": "#f85149"},
        "团聚": {"words": ["回来", "回家", "过年", "吃饭", "聚", "一起", "接", "送"], "icon": "👨‍👩‍👧", "color": "#a371f7"},
    },
    "business": {
        "专业": {"words": ["方案", "报价", "合同", "交付", "验收", "结算", "发票", "预算"], "icon": "📋", "color": "#58a6ff"},
        "效率": {"words": ["尽快", "马上", "尽快处理", "抓紧", "加急", "尽快安排"], "icon": "⚡", "color": "#3fb950"},
        "信任": {"words": ["放心", "靠谱", "信任", "没问题", "保证", "一定", "承诺"], "icon": "🤝", "color": "#d2991d"},
        "推进": {"words": ["下一步", "继续", "推进", "落实", "确认", "安排", "启动"], "icon": "📈", "color": "#a371f7"},
        "风险": {"words": ["延期", "问题", "风险", "不行", "不能", "不确定", "再确认", "考虑"], "icon": "⚠️", "color": "#f85149"},
    },
    "service": {
        "效率": {"words": ["马上", "稍等", "尽快", "处理中", "已安排", "已完成", "解决了"], "icon": "⚡", "color": "#3fb950"},
        "礼貌": {"words": ["您好", "请", "谢谢", "不客气", "抱歉", "麻烦您", "久等"], "icon": "🙏", "color": "#58a6ff"},
        "解决": {"words": ["解决", "处理", "搞定", "好了", "可以了", "没问题", "核实", "查到"], "icon": "✅", "color": "#d2991d"},
        "投诉": {"words": ["不满", "投诉", "差评", "退款", "赔偿", "太慢", "不行", "错了"], "icon": "😟", "color": "#f85149"},
        "满意": {"words": ["满意", "好评", "不错", "挺好", "感谢", "辛苦了", "很专业"], "icon": "👍", "color": "#a371f7"},
    },
}


def _compute_identity_signals(enriched: list[dict], identity: str) -> dict:
    """Compute identity-specific signals. Returns same format as _analyze_signals."""
    if identity == "lover" or identity not in IDENTITY_SIGNAL_KEYWORDS:
        return None  # caller should use _analyze_signals

    all_text = " ".join(m.get("content", "") for m in enriched if m.get("type") == "文本")
    total_msgs = len(enriched)
    signals_config = IDENTITY_SIGNAL_KEYWORDS[identity]

    per_1000 = lambda hits: round(sum(hits.values()) * 1000 / max(1, total_msgs), 1)

    signal_list = []
    all_hits_total = 0
    for name, cfg in signals_config.items():
        hits = _count_keywords(all_text, cfg["words"])
        all_hits_total += sum(hits.values())
        signal_list.append({
            "name": name,
            "icon": cfg["icon"],
            "density": per_1000(hits),
            "benchmark": 5,
            "top_words": sorted(hits, key=hits.get, reverse=True)[:5],
            "color": cfg["color"],
        })

    # Generate summary
    if all_hits_total == 0:
        summary = "信号数据不足，互动可能较少"
    else:
        # Find strongest and weakest signals
        best = max(signal_list, key=lambda s: s["density"])
        worst = min(signal_list, key=lambda s: s["density"])
        summary = f"{best['icon']}「{best['name']}」信号最密集，{worst['icon']}「{worst['name']}」相对较少"

    return {"signals": signal_list, "summary": summary}


def _analyze_dynamics(enriched: list[dict], user_name: str) -> dict:
    """Analyze interaction dynamics: initiators, streaks, etc."""
    # Daily initiator
    by_date = defaultdict(list)
    for m in enriched:
        by_date[m["date"]].append(m)

    her_starts = 0
    me_starts = 0
    for date, msgs in sorted(by_date.items()):
        first = msgs[0]
        if first.get("sender") == "":
            her_starts += 1
        else:
            me_starts += 1

    total_days = her_starts + me_starts
    her_start_pct = round(her_starts / total_days * 100) if total_days else 50

    # Streak analysis (consecutive messages per turn)
    her_streaks = []
    me_streaks = []
    current_sender = None
    current_count = 0
    for m in enriched:
        sender = "her" if m.get("sender") == "" else "me"
        if sender == current_sender:
            current_count += 1
        else:
            if current_sender == "her" and current_count > 0:
                her_streaks.append(current_count)
            elif current_sender == "me" and current_count > 0:
                me_streaks.append(current_count)
            current_sender = sender
            current_count = 1
    # Last streak
    if current_sender == "her":
        her_streaks.append(current_count)
    elif current_sender == "me":
        me_streaks.append(current_count)

    her_avg_streak = round(sum(her_streaks) / len(her_streaks), 1) if her_streaks else 0
    me_avg_streak = round(sum(me_streaks) / len(me_streaks), 1) if me_streaks else 0

    # Late night ratio
    late_night = sum(1 for m in enriched if m["hour"] >= 22 or m["hour"] <= 3)
    late_pct = round(late_night / len(enriched) * 100) if enriched else 0

    # Voice preference
    her_voice = sum(1 for m in enriched if m.get("type") == "语音" and m.get("sender") == "")
    me_voice = sum(1 for m in enriched if m.get("type") == "语音" and m.get("sender") != "")
    her_voice_pct = round(her_voice / max(1, sum(1 for m in enriched if m.get("sender") == "")) * 100)

    # Weekend vs weekday
    weekday_msgs = sum(1 for m in enriched if m["weekday"] < 5)
    weekend_msgs = sum(1 for m in enriched if m["weekday"] >= 5)
    weekday_avg = round(weekday_msgs / max(1, sum(1 for d in set(m["date"] for m in enriched if m["weekday"] < 5))), 1)
    weekend_avg = round(weekend_msgs / max(1, sum(1 for d in set(m["date"] for m in enriched if m["weekday"] >= 5))), 1)

    return {
        "initiator": {
            "her": her_starts,
            "me": me_starts,
            "her_pct": her_start_pct,
            "me_pct": 100 - her_start_pct,
        },
        "streaks": {
            "her_avg": her_avg_streak,
            "me_avg": me_avg_streak,
        },
        "late_night_pct": late_pct,
        "voice": {
            "her_voice_pct": her_voice_pct,
            "her_voice_count": her_voice,
            "me_voice_count": me_voice,
        },
        "weekday_vs_weekend": {
            "weekday_avg": weekday_avg,
            "weekend_avg": weekend_avg,
        },
    }


def _generate_advice(intimacy: dict, signals: dict, dynamics: dict, phases: dict) -> list[dict]:
    """Generate personalized relationship advice based on analysis weaknesses."""
    advice = []

    # Check weak areas in intimacy
    sub_scores = {s["name"]: s["score"] for s in intimacy["sub_scores"]}
    weak_spots = [(k, v) for k, v in sub_scores.items() if v < 60]

    # Signal-based advice
    signal_list = signals.get("signals", [])
    signal_dict = {s["name"]: s["density"] for s in signal_list}

    # 1. Response speed
    if sub_scores.get("回复速度", 100) < 50:
        advice.append({
            "priority": 1,
            "title": "提升回复速度",
            "detail": "你的回复间隔偏长，试着在她发消息后 1-2 分钟内回应。快速回复传递'你在意'的信号，比任何话术都有力。",
            "icon": "⚡",
            "topics": ["她发消息时先回一个表情或'在'，不用想太多", "设置特别提醒音，不错过她的消息"],
        })

    # 2. Balance
    if sub_scores.get("消息平衡", 100) < 50:
        advice.append({
            "priority": 2,
            "title": "增加主动分享",
            "detail": "她发起的话题明显更多。试着每天主动分享 1-2 件你的日常——一张照片、一个趣事、一个想法。关系需要双向流动。",
            "icon": "📤",
            "topics": ["拍一张你正在吃的/正在看的发给她", "分享一个今天遇到的有趣小事", "问她一个开放性问题：'你觉得XX怎么样'"],
        })

    # 3. Affection vs care
    if signal_dict.get("关心", 0) > signal_dict.get("亲昵", 0) * 2:
        advice.append({
            "priority": 2,
            "title": "从'照顾'切换到'喜欢'",
            "detail": "你的关心词远多于亲昵词。她需要的不只是'吃饭了吗'，也需要'想你'。试着把关心包装成喜欢——'想你所以问问你吃了没'。",
            "icon": "💕",
            "topics": ["今晚睡前发一句'有点想你'", "把'吃了吗'改成'想你，所以问问你吃了没'", "偶尔发一句'刚才突然想到你'"],
        })

    # 4. Conflict signals
    if signal_dict.get("冲突", 0) > 15:
        advice.append({
            "priority": 1,
            "title": "减少争论，增加共情",
            "detail": "冲突信号偏高。她吐槽时不要分析/普法，站她这边一起骂就够了。'这也太离谱了'比'违反劳动法'有效 100 倍。",
            "icon": "🛡️",
            "topics": ["她吐槽工作时：'这也太离谱了吧' '他是想让你一个人扛吗'", "她抱怨时先接情绪，别急着给方案"],
        })

    # 5. Late night
    if dynamics.get("late_night_pct", 0) > 30:
        advice.append({
            "priority": 3,
            "title": "增加白天优质时间",
            "detail": "深夜聊天占比很高，这是亲密信号，但也意味着白天互动不足。试着在午休或下午茶时间发起轻松话题。",
            "icon": "☀️",
            "topics": ["午休时发一张你的午餐照片", "下午三点发个表情问问'在干嘛'", "分享一篇有趣的公众号文章"],
        })

    # 6. Voice
    if dynamics.get("voice", {}).get("her_voice_pct", 0) > 30:
        advice.append({
            "priority": 3,
            "title": "匹配她的语音偏好",
            "detail": "她喜欢发语音，这是信任和舒适的表现。偶尔也用语音回复她——你的声音比文字更有温度。",
            "icon": "🎤",
            "topics": ["下次回复时按住语音键说两句话", "睡前发一条语音晚安"],
        })

    # 7. Phase-based advice
    phase = phases.get("phase", "")
    if phase == "cooling":
        advice.append({
            "priority": 1,
            "title": "温和回暖",
            "detail": "近期消息量下降明显。不要突然加大力度（会显得刻意），从她感兴趣的话题自然切入。游泳装备、美食、抖音热点——用她最近提过的事开场。",
            "icon": "🔥",
            "topics": ["翻翻聊天记录，找她最近提过但你没接的话题", "分享一个她感兴趣的领域的新闻或视频"],
        })
    elif phase == "escalation":
        advice.append({
            "priority": 3,
            "title": "保持节奏，别踩油门",
            "detail": "关系正在快速升温，此时最忌用力过猛。维持当前互动频率，让升温自然发生。过度的关心反而会制造压力。",
            "icon": "🎯",
            "topics": ["维持现在的聊天频率，别突然加倍", "保持自然，她主动你回应，你主动她回应"],
        })

    # 8. Future words
    if signal_dict.get("未来", 0) < 5 and phases.get("phase") in ("stable", "escalation"):
        advice.append({
            "priority": 4,
            "title": "轻量级未来锚点",
            "detail": "很少提到未来计划。不需要宏大承诺，小锚点就够了：'下次休息一起去那家新开的店吧'。让未来成为自然话题，而非沉重话题。",
            "icon": "🔮",
            "topics": ["'下次休息一起去XX吧'（XX=她提过的店/地方）", "看到好玩的活动发给她：'这个看起来不错，要不要一起去'"],
        })

    # 9. Weekday imbalance
    wd = dynamics.get("weekday_vs_weekend", {})
    if wd.get("weekday_avg", 0) and wd.get("weekend_avg", 0):
        if wd["weekend_avg"] > wd["weekday_avg"] * 2:
            advice.append({
                "priority": 3,
                "title": "工作日也要存在感",
                "detail": "周末消息量是工作日的 2 倍以上。她工作日忙但不代表不想收到你的消息。午休时发个表情或短句，不需要长篇。",
                "icon": "📅",
                "topics": ["工作日午休发个表情包", "周一早上发一句'新的一周加油'"],
            })

    # 10. Low multimodal (voice/image)
    if sub_scores.get("多模态", 100) < 30:
        advice.append({
            "priority": 2,
            "title": "多维度表达",
            "detail": "几乎不发语音/图片。偶尔发张照片或用语音回复，互动会更有温度。视觉和声音的维度比纯文字更亲密。",
            "icon": "📸",
            "topics": ["拍一张你窗外的风景发给她", "看到可爱/有趣的东西随手拍一张分享", "说晚安时发一条语音"],
        })

    # 11. Low affection density
    if sub_scores.get("情感密度", 100) < 30:
        advice.append({
            "priority": 2,
            "title": "增加情感表达",
            "detail": "亲昵词太少（<3/千条）。试着在日常对话中自然融入情感表达，不必刻意，但要有。",
            "icon": "❤️",
            "topics": ["'今天天气好好，想到你了'", "'刚看到一个东西，觉得你会喜欢'", "用'想你了'代替'在干嘛'"],
        })

    # Ensure we have at least 4, at most 6
    advice.sort(key=lambda x: x["priority"])
    return advice[:6]


# ═══════════════════════════════════════════
#  Identity Detection & Multi-Index System
# ═══════════════════════════════════════════

IDENTITY_RULES = [
    # (identity_key, label, icon, keywords, weight_rules)
    ("lover", "恋人", "💕", {
        "affection_high": ["想你", "爱你", "喜欢你", "亲亲", "宝贝", "抱抱", "么么哒", "想你了"],
        "late_night": True,  # >30% messages at night
        "high_volume": True,  # >30 msg/day avg
        "multimodal": True,   # voice/images >10%
    }),
    ("family", "家人", "👨‍👩‍👧", {
        "family_words": ["妈", "爸", "妹妹", "哥", "嫂子", "舅舅", "舅妈", "侄子", "侄女", "家", "回家", "家里"],
        "care_high": True,    # care words > affection words
        "call_heavy": True,   # calls > 5%
    }),
    ("business", "合作伙伴", "💼", {
        "business_words": ["合作", "项目", "合同", "方案", "报价", "客户", "预算", "交付", "验收", "签约",
                          "投资", "融资", "股份", "分红", "代理", "渠道"],
        "formal_tone": True,  # longer messages, fewer emojis
        "link_heavy": True,   # links/files > 20%
    }),
    ("colleague", "同事", "🏢", {
        "work_words": ["会议", "汇报", "审批", "周报", "日报", "打卡", "请假", "加班", "调休", "排班",
                      "OKR", "KPI", "需求", "上线", "测试", "发版"],
        "weekday_focus": True,  # mostly weekday messages
        "office_hours": True,   # 9-18 active
    }),
    ("service", "客服/服务", "📞", {
        "service_words": ["咨询", "请问", "您好", "帮助", "问题", "处理", "反馈", "投诉", "售后",
                         "订单", "退款", "保修", "预约", "办理"],
        "qa_pattern": True,    # question-answer structure
        "one_way": True,        # other party initiates more
    }),
    ("friend", "朋友", "🤝", {
        "friend_signals": True,  # default fallback with relaxed criteria
    }),
]

IDENTITY_LABELS = {r[0]: r[1] for r in IDENTITY_RULES}
IDENTITY_ICONS = {r[0]: r[2] for r in IDENTITY_RULES}


def _detect_identity(enriched: list[dict], user_name: str, contact_name: str = "") -> dict:
    """Auto-detect relationship identity based on chat content."""
    if not enriched:
        return {"identity": "unknown", "label": "未识别", "icon": "👤", "confidence": 0, "features": []}

    all_text = " ".join(m.get("content", "") for m in enriched if m.get("type") == "文本")
    total = len(enriched)

    # ── Gender detection ──
    gender = _detect_gender(all_text, enriched)
    # Fall back to name-based if content-based is unknown
    if gender == "unknown":
        gender = _detect_gender_from_name(contact_name)
    gender_icon = {"male": "♂️", "female": "♀️", "unknown": ""}.get(gender, "")
    gender_label = {"male": "男", "female": "女", "unknown": "未知"}.get(gender, "")

    # Compute basic signals
    affection_count = sum(all_text.count(w) for w in AFFECTION_WORDS)
    care_count = sum(all_text.count(w) for w in CARE_WORDS)
    affection_density = round(affection_count / max(1, total) * 1000, 1)

    late_night = sum(1 for m in enriched if m.get("hour", 0) >= 22 or m.get("hour", 0) <= 3)
    late_pct = round(late_night / total * 100) if total else 0

    dates = sorted(set(m.get("date", "") for m in enriched))
    days = len(dates) if dates else 1
    daily_avg = round(total / days, 1)

    voice_img = sum(1 for m in enriched if m.get("type") in ("语音", "图片"))
    multimodal_pct = round(voice_img / max(1, total) * 100)

    call_count = sum(1 for m in enriched if m.get("type") == "通话")
    call_pct = round(call_count / max(1, total) * 100)

    link_count = sum(1 for m in enriched if m.get("type") == "链接/文件")
    link_pct = round(link_count / max(1, total) * 100)

    emoji_count = sum(1 for m in enriched if m.get("type") == "表情")
    emoji_pct = round(emoji_count / max(1, total) * 100)

    # Average message length
    texts = [m.get("content", "") for m in enriched if m.get("type") == "文本"]
    avg_len = round(sum(len(t) for t in texts) / max(1, len(texts)), 1)

    # Weekday focus
    weekday_count = sum(1 for m in enriched if m.get("weekday", 0) < 5)
    weekday_pct = round(weekday_count / max(1, total) * 100)

    office_hours = sum(1 for m in enriched if 9 <= m.get("hour", 0) <= 18)
    office_pct = round(office_hours / max(1, total) * 100)

    # Initiation ratio
    her_init = sum(1 for m in enriched if m.get("sender") == "")
    her_pct = round(her_init / max(1, total) * 100)

    # Score each identity
    scores = {}
    features_map = {}

    # Lover
    lover_score = 0
    lover_features = []
    if affection_density > 3:
        lover_score += 25
        lover_features.append(f"亲昵词 {affection_density}/千条")
    if late_pct > 20:
        lover_score += 20
        lover_features.append(f"深夜聊天 {late_pct}%")
    if daily_avg > 20:
        lover_score += 20
        lover_features.append(f"日均 {daily_avg} 条")
    if multimodal_pct > 8:
        lover_score += 15
        lover_features.append(f"语音/图片 {multimodal_pct}%")
    if affection_density > care_count / max(1, total) * 1000 * 0.5:
        lover_score += 10
    # Bonus: high volume alone is a strong lover signal
    if daily_avg > 50:
        lover_score += 10
        lover_features.append("高频互动")
    scores["lover"] = lover_score
    features_map["lover"] = lover_features

    # Family
    family_words_count = sum(all_text.count(w) for w in IDENTITY_RULES[1][3].get("family_words", []))
    family_score = 0
    family_features = []
    if family_words_count > 10:
        family_score += 40
        family_features.append(f"家庭词 {family_words_count} 次")
    if care_count > affection_count * 2:
        family_score += 30
        family_features.append("关怀 > 亲昵")
    if call_pct > 5:
        family_score += 20
        family_features.append(f"通话 {call_pct}%")
    if daily_avg < 20:
        family_score += 10
    scores["family"] = family_score
    features_map["family"] = family_features

    # Business partner
    biz_count = sum(all_text.count(w) for w in IDENTITY_RULES[2][3].get("business_words", []))
    biz_score = 0
    biz_features = []
    if biz_count > 5:
        biz_score += 40
        biz_features.append(f"商业词 {biz_count} 次")
    if link_pct > 20:
        biz_score += 25
        biz_features.append(f"链接/文件 {link_pct}%")
    if avg_len > 15 and emoji_pct < 5:
        biz_score += 20
        biz_features.append("正式语气")
    if daily_avg < 30:
        biz_score += 15
    scores["business"] = biz_score
    features_map["business"] = biz_features

    # Colleague
    work_count = sum(all_text.count(w) for w in IDENTITY_RULES[3][3].get("work_words", []))
    colleague_score = 0
    colleague_features = []
    if work_count > 5:
        colleague_score += 35
        colleague_features.append(f"工作词 {work_count} 次")
    if weekday_pct > 70:
        colleague_score += 25
        colleague_features.append(f"工作日 {weekday_pct}%")
    if office_pct > 50:
        colleague_score += 25
        colleague_features.append(f"办公时段 {office_pct}%")
    if avg_len > 10:
        colleague_score += 15
    scores["colleague"] = colleague_score
    features_map["colleague"] = colleague_features

    # Service
    svc_count = sum(all_text.count(w) for w in IDENTITY_RULES[4][3].get("service_words", []))
    service_score = 0
    service_features = []
    if svc_count > 3:
        service_score += 35
        service_features.append(f"服务词 {svc_count} 次")
    if her_pct > 60:
        service_score += 30
        service_features.append("对方发起为主")
    if daily_avg < 15:
        service_score += 20
    if avg_len > 15:
        service_score += 15
    scores["service"] = service_score
    features_map["service"] = service_features

    # Friend (default)
    friend_score = 40  # Base score
    friend_features = []
    if daily_avg > 5:
        friend_score += 20
        friend_features.append(f"日均 {daily_avg} 条")
    if emoji_pct > 5:
        friend_score += 15
        friend_features.append(f"表情 {emoji_pct}%")
    if avg_len < 12:
        friend_score += 15
        friend_features.append("轻松短句")
    features_map["friend"] = friend_features
    scores["friend"] = friend_score

    # Find best match with priority: lover > family > business > colleague > service > friend
    # Lover wins if score >= 40 AND no other identity is significantly higher
    priority_order = ["lover", "family", "business", "colleague", "service", "friend"]

    if scores.get("lover", 0) >= 40:
        best = "lover"
        best_score = scores["lover"]
        # Only override if another identity is MUCH stronger
        for ident in priority_order[1:]:
            if scores.get(ident, 0) > best_score + 25:
                best = ident
                best_score = scores[ident]
                break
    else:
        best = max(scores, key=scores.get)
        best_score = scores[best]

    if best_score >= 50:
        identity = best
        confidence = min(99, best_score)
    elif best_score >= 30:
        identity = best
        confidence = best_score
    else:
        identity = "unknown"
        confidence = 0

    return {
        "identity": identity,
        "label": IDENTITY_LABELS.get(identity, "未识别"),
        "icon": IDENTITY_ICONS.get(identity, "👤"),
        "confidence": confidence,
        "features": features_map.get(identity, []),
        "all_scores": {k: v for k, v in sorted(scores.items(), key=lambda x: -x[1]) if v > 10},
        "gender": gender,
        "gender_icon": gender_icon,
        "gender_label": gender_label,
    }


def _detect_gender(all_text: str, enriched: list[dict]) -> str:
    """Detect gender from chat content. Returns 'male', 'female', or 'unknown'."""
    other_msgs = [m for m in enriched if m.get("sender") != "" and m.get("type") == "文本"]

    she_count = 0
    he_count = 0

    for m in other_msgs:
        content = m.get("content", "")
        she_count += (content.count("她") + content.count("妳")) * 3  # "她" is strongly intentional
        he_count += content.count("他")

    # Female-coded words in the other party's own messages
    other_party_msgs = [m for m in enriched if m.get("sender") == "" and m.get("type") == "文本"]
    female_signals = ["姐妹", "闺蜜", "化妆", "美甲", "裙子", "包包", "姨妈", "大姨妈", "想做美甲", "指甲"]
    male_signals = ["兄弟", "哥们", "打球", "抽烟"]

    for m in other_party_msgs:
        content = m.get("content", "")
        for w in female_signals:
            if w in content:
                she_count += 5
        for w in male_signals:
            if w in content:
                he_count += 5

    # Self-reference
    all_other = " ".join(m.get("content", "") for m in other_party_msgs)
    if "我是女生" in all_other or "我是女的" in all_other:
        she_count += 20
    if "我是男生" in all_other or "我是男的" in all_other:
        he_count += 20

    # Name-based hint: common female name characters
    female_name_chars = ["婷", "芳", "丽", "敏", "静", "娟", "秀", "娜", "艳", "霞", "玲", "萍", "红", "芸", "颖", "琪", "萱", "怡"]
    # The contact name is not directly available here, gender detection runs inside _detect_identity
    # which has access to enriched messages only. Name hint is applied at call site.

    if she_count > he_count + 1:
        return "female"
    elif he_count > she_count + 1:
        return "male"
    return "unknown"


def _detect_gender_from_name(name: str) -> str:
    """Detect gender from Chinese name characters. Returns 'male', 'female', or 'unknown'."""
    female_chars = ["婷", "芳", "丽", "敏", "静", "娟", "秀", "娜", "艳", "霞", "玲", "萍", "红", "芸", "颖", "琪", "萱",
                    "怡", "雅", "韵", "婉", "媚", "妮", "嫦", "婵", "娇", "美", "媛", "紫", "蕾", "薇", "月", "雪", "慧", "淑"]
    male_chars = ["伟", "强", "军", "勇", "刚", "磊", "峰", "涛", "斌", "浩", "杰", "鹏", "辉", "明", "超", "龙",
                  "东", "国", "建", "平", "志", "文", "海", "林", "飞", "凯", "毅", "旭"]
    score = 0
    for ch in name:
        if ch in female_chars:
            score += 1
        if ch in male_chars:
            score -= 1
    if score > 0:
        return "female"
    elif score < 0:
        return "male"
    return "unknown"


def _compute_monthly_trend(enriched: list[dict]) -> list[dict]:
    """Compute monthly message count for charting."""
    months = defaultdict(lambda: {"her": 0, "me": 0})
    for m in enriched:
        if m.get("sender") == "":
            months[m["month"]]["her"] += 1
        else:
            months[m["month"]]["me"] += 1
    return [{"month": k, "her": v["her"], "me": v["me"], "total": v["her"] + v["me"]}
            for k, v in sorted(months.items())]


# ═══════════════════════════════════════════
#  Identity-Specific Index Functions
# ═══════════════════════════════════════════

def _compute_friend_index(enriched: list[dict], user_name: str) -> dict:
    """Friendship index: rapport, diversity, lightness."""
    total = len(enriched)
    all_text = " ".join(m.get("content", "") for m in enriched if m.get("type") == "文本")
    texts = [m.get("content", "") for m in enriched if m.get("type") == "文本"]
    avg_len = round(sum(len(t) for t in texts) / max(1, len(texts)), 1)
    emoji_pct = round(sum(1 for m in enriched if m.get("type") == "表情") / max(1, total) * 100)
    tease_count = sum(all_text.count(w) for w in TEASING_WORDS)

    dates = sorted(set(m.get("date", "") for m in enriched))
    active_days = len(dates)
    freq_score = min(100, round(active_days / max(1, 30) * 100))

    # Topic diversity
    topic_hits = sum(1 for topic, kw in TOPIC_KEYWORDS.items() if any(k in all_text for k in kw))
    diversity_score = min(100, topic_hits * 15)

    # Lightness
    lightness = min(100, round((tease_count / max(1, total) * 1000 + emoji_pct * 5)))

    # Response speed
    intervals = []
    for i in range(1, len(enriched)):
        prev, curr = enriched[i-1], enriched[i]
        if prev.get("sender") != curr.get("sender"):
            gap = _safe_timestamp(curr) - _safe_timestamp(prev)
            if 0 < gap < 86400:
                intervals.append(gap)
    fast_pct = round(sum(1 for x in intervals if x <= 300) / max(1, len(intervals)) * 100) if intervals else 50

    total_score = round(freq_score * 0.3 + diversity_score * 0.25 + lightness * 0.25 + fast_pct * 0.2)

    label = "铁杆 🤜🤛" if total_score >= 70 else "好朋友 👋" if total_score >= 50 else "普通朋友 👋" if total_score >= 30 else "点头之交"

    return {
        "name": "友谊指数", "total": total_score, "label": label,
        "sub_scores": [
            {"name": "互动频率", "score": freq_score, "weight": 30},
            {"name": "话题多样", "score": diversity_score, "weight": 25},
            {"name": "轻松度", "score": lightness, "weight": 25},
            {"name": "回应速度", "score": fast_pct, "weight": 20},
        ],
    }


def _compute_biz_index(enriched: list[dict], user_name: str) -> dict:
    """Business collaboration index."""
    total = len(enriched)
    link_pct = round(sum(1 for m in enriched if m.get("type") == "链接/文件") / max(1, total) * 100)
    texts = [m.get("content", "") for m in enriched if m.get("type") == "文本"]
    avg_len = round(sum(len(t) for t in texts) / max(1, len(texts)), 1)

    intervals = []
    for i in range(1, len(enriched)):
        prev, curr = enriched[i-1], enriched[i]
        if prev.get("sender") != curr.get("sender"):
            gap = _safe_timestamp(curr) - _safe_timestamp(prev)
            if 0 < gap < 86400:
                intervals.append(gap)
    fast_pct = round(sum(1 for x in intervals if x <= 600) / max(1, len(intervals)) * 100) if intervals else 50

    dates = sorted(set(m.get("date", "") for m in enriched))
    span_days = max(1, (datetime.strptime(dates[-1], "%Y-%m-%d") - datetime.strptime(dates[0], "%Y-%m-%d")).days) if len(dates) >= 2 else 1
    longevity_score = min(100, round(span_days / 365 * 100))

    pro_score = min(100, round(link_pct * 3 + min(avg_len, 30) * 2))

    total_score = round(fast_pct * 0.35 + pro_score * 0.35 + longevity_score * 0.3)
    label = "深度合作 🤝" if total_score >= 70 else "良好合作" if total_score >= 50 else "初步合作" if total_score >= 30 else "初次接触"

    return {
        "name": "协作指数", "total": total_score, "label": label,
        "sub_scores": [
            {"name": "响应速度", "score": fast_pct, "weight": 35},
            {"name": "专业度", "score": pro_score, "weight": 35},
            {"name": "合作时长", "score": longevity_score, "weight": 30},
        ],
    }


def _compute_family_index(enriched: list[dict], user_name: str) -> dict:
    """Family closeness index."""
    total = len(enriched)
    all_text = " ".join(m.get("content", "") for m in enriched if m.get("type") == "文本")
    care_count = sum(all_text.count(w) for w in CARE_WORDS)
    care_density = round(care_count / max(1, total) * 1000, 1)
    call_pct = round(sum(1 for m in enriched if m.get("type") == "通话") / max(1, total) * 100)

    dates = sorted(set(m.get("date", "") for m in enriched))
    active_days = len(dates)
    freq_score = min(100, round(active_days / max(1, 30) * 100))

    care_score = min(100, round(care_density * 2))
    call_score = min(100, round(call_pct * 10))
    total_score = round(care_score * 0.4 + freq_score * 0.3 + call_score * 0.3)
    label = "亲密 👨‍👩‍👧" if total_score >= 70 else "温暖" if total_score >= 50 else "疏远" if total_score >= 30 else "冷淡"

    return {
        "name": "亲情指数", "total": total_score, "label": label,
        "sub_scores": [
            {"name": "关怀密度", "score": care_score, "weight": 40},
            {"name": "联系频率", "score": freq_score, "weight": 30},
            {"name": "通话偏好", "score": call_score, "weight": 30},
        ],
    }


def _compute_colleague_index(enriched: list[dict], user_name: str) -> dict:
    """Colleague rapport index: work-hours sync, professionalism, weekday focus."""
    total = len(enriched)
    all_text = " ".join(m.get("content", "") for m in enriched if m.get("type") == "文本")
    texts = [m.get("content", "") for m in enriched if m.get("type") == "文本"]
    avg_len = round(sum(len(t) for t in texts) / max(1, len(texts)), 1)

    # Work-hours activity (9-18 on weekdays)
    work_hours = sum(1 for m in enriched if m.get("weekday", 0) < 5 and 9 <= m.get("hour", 0) <= 18)
    work_hours_score = min(100, round(work_hours / max(1, total) * 250))

    # Work vocabulary density
    work_words = ["会议", "汇报", "审批", "周报", "日报", "打卡", "请假", "加班", "调休", "排班",
                  "OKR", "KPI", "需求", "上线", "测试", "发版", "进度", "确认", "完成", "安排",
                  "对接", "跟进", "同步", "反馈", "协调", "流程", "文档", "方案"]
    work_count = sum(all_text.count(w) for w in work_words)
    work_density = round(work_count / max(1, total) * 1000, 1)
    work_score = min(100, round(work_density * 3))

    # Weekday focus
    weekday_count = sum(1 for m in enriched if m.get("weekday", 0) < 5)
    weekday_pct = round(weekday_count / max(1, total) * 100)
    weekday_score = min(100, round(weekday_pct * 1.2))

    # Response speed (within work hours)
    intervals = []
    for i in range(1, len(enriched)):
        prev, curr = enriched[i-1], enriched[i]
        if prev.get("sender") != curr.get("sender"):
            gap = _safe_timestamp(curr) - _safe_timestamp(prev)
            if 0 < gap < 86400:
                intervals.append(gap)
    fast_pct = round(sum(1 for x in intervals if x <= 600) / max(1, len(intervals)) * 100) if intervals else 50

    total_score = round(work_hours_score * 0.30 + work_score * 0.25 + weekday_score * 0.25 + fast_pct * 0.20)

    if total_score >= 70:
        label = "高效搭档 ⚡"
    elif total_score >= 50:
        label = "良好同事 🤝"
    elif total_score >= 30:
        label = "普通同事 👋"
    else:
        label = "点头之交"

    return {
        "name": "同事指数", "total": total_score, "label": label,
        "sub_scores": [
            {"name": "工作时段", "score": work_hours_score, "weight": 30},
            {"name": "工作密度", "score": work_score, "weight": 25},
            {"name": "工作日", "score": weekday_score, "weight": 25},
            {"name": "回应速度", "score": fast_pct, "weight": 20},
        ],
    }


def _compute_service_index(enriched: list[dict], user_name: str) -> dict:
    """Service quality index: responsiveness, resolution, politeness."""
    total = len(enriched)
    all_text = " ".join(m.get("content", "") for m in enriched if m.get("type") == "文本")

    # Response speed
    intervals = []
    for i in range(1, len(enriched)):
        prev, curr = enriched[i-1], enriched[i]
        if prev.get("sender") != curr.get("sender"):
            gap = _safe_timestamp(curr) - _safe_timestamp(prev)
            if 0 < gap < 86400:
                intervals.append(gap)
    fast_pct = round(sum(1 for x in intervals if x <= 300) / max(1, len(intervals)) * 100) if intervals else 50
    speed_score = min(100, round(fast_pct * 1.2))

    # Solution/resolution keywords
    solution_words = ["解决", "处理", "已完成", "好了", "可以了", "没问题", "搞定", "安排",
                      "收到", "明白", "了解", "马上", "稍等", "核实", "确认", "查到",
                      "已处理", "已反馈", "已安排", "已提交"]
    solution_count = sum(all_text.count(w) for w in solution_words)
    solution_density = round(solution_count / max(1, total) * 1000, 1)
    solution_score = min(100, round(solution_density * 4))

    # Politeness
    polite_words = ["您好", "请", "谢谢", "不客气", "抱歉", "麻烦", "感谢", "稍候",
                    "为您", "帮您", "不好意思", "久等", "见谅"]
    polite_count = sum(all_text.count(w) for w in polite_words)
    polite_density = round(polite_count / max(1, total) * 1000, 1)
    polite_score = min(100, round(polite_density * 3))

    # Proactive care (checking in without being asked)
    care_signals = ["最近", "还好吗", "怎么样", "使用", "体验", "需要", "帮", "建议",
                    "提醒", "通知", "优惠", "活动"]
    care_count = sum(all_text.count(w) for w in care_signals)
    care_density = round(care_count / max(1, total) * 1000, 1)
    care_score = min(100, round(care_density * 3))

    total_score = round(speed_score * 0.35 + solution_score * 0.25 + polite_score * 0.20 + care_score * 0.20)

    if total_score >= 80:
        label = "金牌服务 🥇"
    elif total_score >= 60:
        label = "优质服务 👍"
    elif total_score >= 40:
        label = "达标服务 ✓"
    else:
        label = "待改进 ⚠️"

    return {
        "name": "服务指数", "total": total_score, "label": label,
        "sub_scores": [
            {"name": "响应速度", "score": speed_score, "weight": 35},
            {"name": "解决效率", "score": solution_score, "weight": 25},
            {"name": "礼貌度", "score": polite_score, "weight": 20},
            {"name": "主动关怀", "score": care_score, "weight": 20},
        ],
    }


# ═══════════════════════════════════════════
#  Main Analysis Entry Points
# ═══════════════════════════════════════════

def analyze_private(contact_name: str, since: str, until: str) -> dict:
    """Analyze private chat history with full relationship insights."""
    try:
        cmd = f'wx history {_q(contact_name)} --since {since} --until {until} -n 99999 --json'
        raw = _run_wx(cmd)
    except Exception as e:
        return {"error": f"无法读取 {contact_name} 的聊天记录: {e}"}
    if not isinstance(raw, list) or not raw:
        return {"error": f"未找到 {contact_name} 的聊天记录"}

    messages = raw
    user_name = "我"
    other_name = contact_name

    for m in messages:
        if m.get("sender") and m["sender"] != "":
            user_name = m["sender"]
            break

    # Enrich with datetime fields
    enriched = _parse_messages(messages)
    if not enriched:
        return {"error": "消息解析失败"}

    yayun_msgs = [m for m in enriched if m.get("sender") == ""]
    user_msgs = [m for m in enriched if m.get("sender") != ""]

    total = len(enriched)
    her_count = len(yayun_msgs)
    me_count = len(user_msgs)

    # Date range
    dates = sorted(set(m["date"] for m in enriched))
    date_range = f"{dates[0]} — {dates[-1]}" if dates else "N/A"
    days = len(dates) if dates else 1
    daily_avg = round(total / days, 1) if days else 0

    # Message types
    type_counts = Counter(m.get("type", "文本") for m in enriched)

    # Hourly distribution
    hourly = [0] * 24
    for m in enriched:
        hourly[m["hour"]] += 1

    # Reply intervals
    intervals = []
    for i in range(1, len(enriched)):
        prev = enriched[i - 1]
        curr = enriched[i]
        if prev.get("sender") != curr.get("sender"):
            gap = _safe_timestamp(curr) - _safe_timestamp(prev)
            if 0 < gap < 86400:
                intervals.append(gap)

    reply_stats = {}
    if intervals:
        intervals.sort()
        reply_stats = {
            "fastest_sec": intervals[0],
            "median_sec": intervals[len(intervals) // 2],
            "slowest_sec": intervals[-1],
            "under_1min": sum(1 for x in intervals if x <= 60),
            "under_5min": sum(1 for x in intervals if x <= 300),
            "total_exchanges": len(intervals),
        }

    # Voice & calls
    voice_count = sum(1 for m in enriched if m.get("type") == "语音")
    call_count = sum(1 for m in enriched if m.get("type") == "通话")

    # Message length
    her_texts = [m["content"] for m in yayun_msgs if m.get("type") == "文本"]
    me_texts = [m["content"] for m in user_msgs if m.get("type") == "文本"]
    her_avg_len = round(sum(len(t) for t in her_texts) / len(her_texts), 1) if her_texts else 0
    me_avg_len = round(sum(len(t) for t in me_texts) / len(me_texts), 1) if me_texts else 0

    # Topics
    all_text = " ".join(m.get("content", "") for m in enriched if m.get("type") == "文本")
    topics = _extract_topics(all_text)

    # Recent messages (last 20)
    recent = []
    for m in messages[-20:]:
        who = other_name if m.get("sender") == "" else user_name
        recent.append({
            "time": m.get("time", "").split(" ")[-1] if " " in m.get("time", "") else m.get("time", ""),
            "who": who,
            "type": m.get("type", ""),
            "content": m.get("content", "")[:100],
        })

    # Daily breakdown
    daily_breakdown = defaultdict(lambda: {"her": 0, "me": 0})
    for m in enriched:
        if m.get("sender") == "":
            daily_breakdown[m["date"]]["her"] += 1
        else:
            daily_breakdown[m["date"]]["me"] += 1

    # ═══ NEW: Deep Analysis (uses FULL history from the beginning) ═══
    full_cmd = f'wx history {_q(contact_name)} -n 99999 --json'
    full_raw = _run_wx(full_cmd)
    full_enriched = []
    if isinstance(full_raw, list) and full_raw:
        full_enriched = _parse_messages([m for m in full_raw if isinstance(m, dict)])

    if full_enriched:
        # Recompute reply stats from full history for intimacy
        full_intervals = []
        for i in range(1, len(full_enriched)):
            prev = full_enriched[i - 1]
            curr = full_enriched[i]
            if prev.get("sender") != curr.get("sender"):
                gap = _safe_timestamp(curr) - _safe_timestamp(prev)
                if 0 < gap < 86400:
                    full_intervals.append(gap)
        full_reply = {}
        if full_intervals:
            full_intervals.sort()
            full_reply = {
                "fastest_sec": full_intervals[0],
                "median_sec": full_intervals[len(full_intervals) // 2],
                "slowest_sec": full_intervals[-1],
                "under_1min": sum(1 for x in full_intervals if x <= 60),
                "under_5min": sum(1 for x in full_intervals if x <= 300),
                "total_exchanges": len(full_intervals),
            }

        phases = _detect_phases(full_enriched, user_name, "lover")  # placeholder, overridden below
        intimacy = _compute_intimacy(full_enriched, user_name, full_reply)
        signals = _analyze_signals(full_enriched, user_name)
        dynamics = _analyze_dynamics(full_enriched, user_name)
        # AI advice first, fall back to rule-based
        advice = generate_advice_ai(full_raw, contact_name, intimacy, signals, dynamics, phases, user_name)
        if advice is None:
            advice = _generate_advice(intimacy, signals, dynamics, phases)
        monthly_trend = _compute_monthly_trend(full_enriched)
        identity = _detect_identity(full_enriched, user_name, contact_name)
        # Re-detect phases with correct identity label
        phases = _detect_phases(full_enriched, user_name, identity.get("identity", "lover"))
        # AI-generated phase insight
        ai_insight = generate_phase_insight(full_raw, contact_name, identity.get("identity", "lover"), phases, intimacy, user_name)
        if ai_insight:
            phases = {**phases, "ai_description": ai_insight}
    else:
        phases = _detect_phases(enriched, user_name, "lover")  # placeholder
        intimacy = _compute_intimacy(enriched, user_name, reply_stats)
        signals = _analyze_signals(enriched, user_name)
        dynamics = _analyze_dynamics(enriched, user_name)
        # AI advice first, fall back to rule-based
        advice = generate_advice_ai(messages, contact_name, intimacy, signals, dynamics, phases, user_name)
        if advice is None:
            advice = _generate_advice(intimacy, signals, dynamics, phases)
        monthly_trend = _compute_monthly_trend(enriched)
        identity = _detect_identity(enriched, user_name, contact_name)
        # Re-detect phases with correct identity label
        phases = _detect_phases(enriched, user_name, identity.get("identity", "lover"))
        # AI-generated phase insight
        ai_insight = generate_phase_insight(messages, contact_name, identity.get("identity", "lover"), phases, intimacy, user_name)
        if ai_insight:
            phases = {**phases, "ai_description": ai_insight}

    # Apply any custom tag override
    custom_tag = _load_custom_tag(contact_name)
    if custom_tag:
        ck = custom_tag.get("identity_key", "")
        cl = custom_tag.get("label", "")
        if ck:
            # Override identity key, label AND icon — full systematic identity change
            identity["identity"] = ck
            # Look up icon: check custom identities first, then built-in
            cid_icon = "🏷️"
            for ci in _load_custom_identities():
                if ci["key"] == ck:
                    cid_icon = ci.get("icon", "🏷️")
                    break
            identity["icon"] = IDENTITY_ICONS.get(ck, cid_icon)
            if cl:
                identity["custom_label"] = cl
            else:
                for rule_key, rule_label, *_ in IDENTITY_RULES:
                    if rule_key == ck:
                        identity["custom_label"] = rule_label
                        break
                if not identity.get("custom_label"):
                    identity["custom_label"] = cl or "自定义"
            identity["label"] = identity.get("custom_label", identity["label"])
        elif cl:
            # Legacy: only label override, keep auto-detected identity key
            identity["custom_label"] = cl
        identity["has_custom"] = True

    # Compute identity-specific index
    identity_index = None
    id_key = identity.get("identity", "")
    # Resolve custom identities to their base for analysis dimensions
    if id_key not in {"lover", "friend", "business", "family", "colleague", "service"}:
        for ci in _load_custom_identities():
            if ci["key"] == id_key:
                id_key = ci.get("base", "friend")
                break
    data_for_index = full_enriched if full_enriched else enriched
    if id_key == "friend":
        identity_index = _compute_friend_index(data_for_index, user_name)
    elif id_key == "business":
        identity_index = _compute_biz_index(data_for_index, user_name)
    elif id_key == "family":
        identity_index = _compute_family_index(data_for_index, user_name)
    elif id_key == "colleague":
        identity_index = _compute_colleague_index(data_for_index, user_name)
    elif id_key == "service":
        identity_index = _compute_service_index(data_for_index, user_name)
    # lover/unknown fall back to intimacy index

    # For non-lover identities, swap intimacy index and signals
    if id_key != "lover" and identity_index:
        intimacy = identity_index
    identity_signals = _compute_identity_signals(data_for_index, id_key)
    if identity_signals:
        signals = identity_signals

    return {
        "chat_type": "private",
        "contact": contact_name,
        "user_name": user_name,
        "other_name": other_name,
        "date_range": date_range,
        "days": days,
        "stats": {
            "total": total,
            "her_count": her_count,
            "me_count": me_count,
            "daily_avg": daily_avg,
            "her_avg_len": her_avg_len,
            "me_avg_len": me_avg_len,
            "voice_count": voice_count,
            "call_count": call_count,
        },
        "by_type": [{"type": t, "count": c} for t, c in type_counts.most_common()],
        "by_hour": hourly,
        "daily_breakdown": [{"date": d, "her": v["her"], "me": v["me"]} for d, v in sorted(daily_breakdown.items())],
        "reply_stats": reply_stats,
        "topics": topics,
        "recent_messages": recent,
        # New deep analysis fields
        "phases": phases,
        "intimacy": intimacy,
        "signals": signals,
        "dynamics": dynamics,
        "advice": advice,
        "monthly_trend": monthly_trend,
        "identity": identity,
        "identity_index": identity_index,
    }


def analyze_group(group_name: str, since: str, until: str, username: str = "") -> dict:
    """Analyze group chat with community insights."""
    query = username or group_name
    try:
        cmd = f'wx history {_q(query)} --since {since} --until {until} -n 99999 --json'
        messages = _run_wx(cmd)
    except Exception as e:
        return {"error": f"无法读取群「{group_name}」的聊天记录: {e}"}
    if not isinstance(messages, list) or not messages:
        return {"error": f"未找到群「{group_name}」的聊天记录"}

    enriched = _parse_messages(messages)
    if not enriched:
        return {"error": "消息解析失败"}

    total = len(enriched)
    dates = sorted(set(m["date"] for m in enriched))
    date_range = f"{dates[0]} — {dates[-1]}" if dates else f"{since} — {until}"
    active_days = len(dates)
    daily_avg = round(total / max(1, active_days), 1)
    msg_density = round(total / max(1, active_days * 24), 1)  # msgs per hour

    # ═══ Sender analysis ═══
    sender_counter = Counter()
    for m in enriched:
        sender = m.get("sender", "")
        if sender:
            sender_counter[sender] += 1

    total_senders = len(sender_counter)
    top_senders = [{"sender": s, "count": c} for s, c in sender_counter.most_common(15)]

    # Member engagement tiers
    tiers = _compute_engagement_tiers(sender_counter, total)
    tier_labels = {
        "core": "核心 🔥", "active": "活跃 ⭐",
        "regular": "普通 💬", "lurker": "潜水 👀",
    }
    tiers_out = []
    for tier_name in ["core", "active", "regular", "lurker"]:
        members = tiers.get(tier_name, [])
        tier_total = sum(c for _, c in members)
        tiers_out.append({
            "tier": tier_labels.get(tier_name, tier_name),
            "count": len(members),
            "msg_total": tier_total,
            "msg_pct": round(tier_total / max(1, total) * 100, 1),
            "top_members": [name for name, _ in members[:3]],
        })

    # ═══ Message types ═══
    type_counter = Counter(m.get("type", "文本") for m in enriched)
    type_map = {"文本": "text", "图片": "image", "表情": "emoji",
                "链接/文件": "link", "语音": "voice", "系统": "system", "通话": "call"}
    by_type = []
    for raw_type, count in type_counter.most_common():
        by_type.append({
            "type": raw_type, "count": count,
            "pct": round(count / total * 100, 1),
            "key": type_map.get(raw_type, "other"),
        })

    # ═══ Hourly distribution ═══
    hourly = [0] * 24
    for m in enriched:
        hourly[m["hour"]] += 1
    peak_hour = hourly.index(max(hourly))
    peak_labels = {0: "凌晨", 6: "清晨", 9: "早高峰", 12: "午休", 14: "下午茶", 18: "晚餐", 21: "晚高峰", 23: "深夜"}
    peak_label = ""
    for h, label in sorted(peak_labels.items()):
        if peak_hour >= h:
            peak_label = label

    # ═══ Daily trend ═══
    daily_counts = defaultdict(int)
    weekday_counts = defaultdict(int)  # 0=Mon..6=Sun
    for m in enriched:
        daily_counts[m["date"]] += 1
        weekday_counts[m["weekday"]] += 1

    daily_trend = [{"date": d, "count": daily_counts[d]} for d in sorted(daily_counts)]

    # Weekday pattern
    day_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    weekday_trend = []
    for i in range(7):
        active_count = len(set(m["date"] for m in enriched if m["weekday"] == i))
        weekday_trend.append({
            "day": day_names[i],
            "total": weekday_counts.get(i, 0),
            "avg": round(weekday_counts.get(i, 0) / max(1, active_count), 1),
        })

    # ═══ Content features ═══
    text_msgs = [m for m in enriched if m.get("type") == "文本"]
    text_content = [m.get("content", "") for m in text_msgs]
    avg_len = round(sum(len(t) for t in text_content) / max(1, len(text_content)), 1)

    image_count = type_counter.get("图片", 0)
    link_count = type_counter.get("链接/文件", 0)
    emoji_count = type_counter.get("表情", 0)
    voice_count = type_counter.get("语音", 0)

    # ═══ Chat rhythm ═══
    intervals = []
    for i in range(1, len(enriched)):
        gap = _safe_timestamp(enriched[i]) - _safe_timestamp(enriched[i-1])
        if 0 < gap < 3600:
            intervals.append(gap)

    avg_interval = round(sum(intervals) / max(1, len(intervals)))
    burst_threshold = max(1, avg_interval // 4) if avg_interval > 0 else 5
    burst_count = sum(1 for g in intervals if g <= burst_threshold)

    # Quiet hours (hours with < 5% of peak)
    quiet_hours = [h for h in range(24) if hourly[h] < max(hourly) * 0.05]
    quiet_range = _format_hour_ranges(quiet_hours) if quiet_hours else "无明显冷场时段"

    # ═══ Topics ═══
    text_count = sum(1 for m in enriched if m.get("type") == "文本")
    detailed_topics = _extract_detailed_topics(enriched, text_count)

    # Trend comparison: fetch previous equal period
    try:
        period_days = max(1, (datetime.strptime(until_d, "%Y-%m-%d") - datetime.strptime(since, "%Y-%m-%d")).days) if since else 7
        prev_since = (datetime.strptime(since, "%Y-%m-%d") - timedelta(days=period_days)).strftime("%Y-%m-%d")
        prev_until = (datetime.strptime(since, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
        prev_cmd = f'wx history {_q(group_name)} --since {prev_since} --until {prev_until} -n 100 --json'
        prev_raw = _run_wx(prev_cmd)
        prev_enriched = []
        if isinstance(prev_raw, list) and prev_raw:
            prev_enriched = _parse_messages([m for m in prev_raw if isinstance(m, dict)])
        topic_trends = _compute_topic_trends(detailed_topics, prev_enriched)
    except:
        topic_trends = {}

    # ═══ Recent messages ═══
    recent = []
    for m in messages[-15:]:
        recent.append({
            "time": m.get("time", "").split(" ")[-1] if " " in m.get("time", "") else m.get("time", ""),
            "sender": m.get("sender", ""),
            "type": m.get("type", ""),
            "content": m.get("content", "")[:80],
        })

    # ═══ Messages for LLM analysis (up to 100, with full content) ═══
    llm_messages = []
    for m in messages[-100:]:
        llm_messages.append({
            "time": m.get("time", "").split(" ")[-1] if " " in m.get("time", "") else m.get("time", ""),
            "sender": m.get("sender", ""),
            "content": m.get("content", ""),
        })

    return {
        "chat_type": "group",
        "contact": group_name,
        "date_range": date_range,
        "stats": {
            "total": total,
            "daily_avg": daily_avg,
            "active_members": total_senders,
            "active_days": active_days,
            "msg_density": msg_density,
            "peak_hour": peak_hour,
            "peak_label": peak_label,
        },
        "by_type": by_type,
        "by_hour": hourly,
        "top_senders": top_senders,
        # New group dimensions
        "engagement_tiers": tiers_out,
        "daily_trend": daily_trend,
        "weekday_trend": weekday_trend,
        "content_features": {
            "avg_msg_len": avg_len,
            "image_pct": round(image_count / max(1, total) * 100, 1),
            "link_pct": round(link_count / max(1, total) * 100, 1),
            "emoji_pct": round(emoji_count / max(1, total) * 100, 1),
            "voice_pct": round(voice_count / max(1, total) * 100, 1),
        },
        "rhythm": {
            "avg_interval_sec": avg_interval,
            "burst_count": burst_count,
            "quiet_hours": quiet_range,
            "most_active_day": day_names[max(weekday_counts, key=weekday_counts.get)] if weekday_counts else "",
        },
        "topics": [{"topic": t["topic"], "total_hits": t["total_hits"], "top_keywords": t["top_keywords"][:5]} for t in detailed_topics],
        "detailed_topics": detailed_topics,
        "topic_trends": topic_trends,
        "recent_messages": recent,
        "messages": llm_messages,
    }


def _compute_engagement_tiers(sender_counter: Counter, total: int) -> dict:
    """Classify members into engagement tiers."""
    core = []
    active = []
    regular = []
    lurker = []
    for name, count in sender_counter.most_common():
        pct = count / total
        if pct >= 0.10:
            core.append((name, count))
        elif pct >= 0.05:
            active.append((name, count))
        elif pct >= 0.01:
            regular.append((name, count))
        else:
            lurker.append((name, count))
    return {"core": core, "active": active, "regular": regular, "lurker": lurker}


def _format_hour_ranges(hours: list[int]) -> str:
    """Format list of hours into readable ranges, e.g. [2,3,4,5,14,15] → '02-05时, 14-15时'."""
    if not hours:
        return ""
    ranges = []
    start = hours[0]
    end = hours[0]
    for h in hours[1:]:
        if h == end + 1:
            end = h
        else:
            ranges.append((start, end))
            start = end = h
    ranges.append((start, end))
    return ", ".join(f"{s:02d}-{e:02d}时" if s != e else f"{s:02d}时" for s, e in ranges)


# ═══════════════════════════════════════════
#  Topic Keywords
# ═══════════════════════════════════════════

TOPIC_KEYWORDS = {
    "美食": ["吃", "饭", "火锅", "米线", "面", "蛋糕", "菜", "喝", "汤", "肉", "水果", "烧烤", "奶茶", "咖啡",
             "外卖", "食堂", "餐厅", "好吃", "难吃", "饿", "饱", "辣", "甜", "酸", "杨国福", "海底捞"],
    "工作": ["上班", "下班", "加班", "值班", "忙", "店长", "同事", "休息", "请假", "排班", "工资", "辞职",
             "开会", "任务", "客户", "罚款", "好评", "监控"],
    "购物": ["买", "多少钱", "便宜", "贵", "下单", "快递", "到了", "退货", "淘宝", "京东", "拼多多"],
    "情感": ["想你", "爱你", "喜欢", "爱", "在乎", "心疼", "抱抱", "亲", "晚安", "早", "在吗"],
    "健康": ["痛", "不舒服", "累", "困", "睡", "头疼", "肚子", "感冒", "药", "医院", "按摩", "运动"],
    "出行": ["去", "到", "出发", "回", "路", "打车", "公交", "地铁", "开车", "接", "送", "下雨"],
    "娱乐": ["游泳", "健身", "电影", "唱", "玩", "游戏", "抖音", "视频", "拍", "美甲", "化妆"],
    "家庭": ["妈", "爸", "妹妹", "哥", "嫂子", "舅舅", "舅妈", "侄子", "家", "亲戚"],
}


def _extract_topics(text: str) -> list[dict]:
    """Extract topic keywords from text."""
    results = []
    for topic, keywords in TOPIC_KEYWORDS.items():
        hits = []
        for kw in keywords:
            if kw in text:
                hits.append({"keyword": kw, "count": text.count(kw)})
        if hits:
            hits.sort(key=lambda x: x["count"], reverse=True)
            results.append({
                "topic": topic,
                "total_hits": sum(h["count"] for h in hits),
                "top_keywords": [h["keyword"] for h in hits[:5]],
            })
    results.sort(key=lambda x: x["total_hits"], reverse=True)
    return results[:8]


# ═══════════════════════════════════════════
#  Summary Dashboard
# ═══════════════════════════════════════════

TODO_PATTERNS = [
    # (pattern, priority, label_template)
    (r"下次|到时候|改天|回头|晚点|过几天|有空", "中", "约定"),
    (r"帮我|给我|你帮我|你给我|帮我买|给我带", "高", "对方请求"),
    (r"我去|我买|我安排|我来|我弄|我搞定|我处理", "中", "你的承诺"),
    (r"到了没|到了吗|收到没|发货|快递|物流", "中", "物流跟踪"),
    (r"几点|什么时候|几号|星期几|约|见面|碰头", "高", "时间待定"),
    (r"记得|别忘了|别忘了提醒|提醒我|提醒", "高", "提醒事项"),
    (r"要买|想买|下单|买了没|买了不|要不要买", "中", "购物决策"),
    (r"休息|请假|放假|排班|值班|调休", "低", "排班变动"),
    (r"身体|不舒服|疼|难受|感冒|发烧|药|医院", "高", "健康关注"),
    (r"钱|工资|花费|转账|红包|AA|报销", "中", "财务相关"),
]

REPLY_SIGNALS = {
    "unreplied_last": {"signal": "🔴 待回复", "priority": "高", "desc": "对方最后一条消息你还没回"},
    "just_sent": {"signal": "🟢 对方刚发消息", "priority": "高", "desc": "对方几分钟前发了消息"},
    "waiting_you": {"signal": "🟡 对方可能在等回复", "priority": "中", "desc": "对方问了问题或发了需要回应的话"},
    "all_caught_up": {"signal": "✅ 已回复", "priority": "低", "desc": "你已经回复了最新消息"},
}


def _extract_todos(text: str, sender: str) -> list[dict]:
    """Extract todo items from message text with priority."""
    todos = []
    for pattern, priority, label in TODO_PATTERNS:
        matches = re.findall(pattern, text)
        if matches:
            # Find the sentence containing the match
            for m in matches[:2]:
                idx = text.find(m)
                start = max(0, idx - 20)
                end = min(len(text), idx + len(m) + 30)
                snippet = text[start:end].strip()
                if len(snippet) > 60:
                    snippet = snippet[:60] + "..."
                todos.append({
                    "priority": priority,
                    "label": label,
                    "match": m,
                    "snippet": snippet,
                    "from": "对方" if sender == "" else "你",
                })
    # Sort by priority: 高 > 中 > 低
    order = {"高": 0, "中": 1, "低": 2}
    todos.sort(key=lambda x: order.get(x["priority"], 9))
    # Dedup by snippet
    seen = set()
    unique = []
    for t in todos:
        if t["snippet"] not in seen:
            seen.add(t["snippet"])
            unique.append(t)
    return unique[:6]


def _detect_signal(messages: list[dict], user_name: str) -> dict:
    """Detect interaction signal for a private chat."""
    if not messages:
        return {"signal": "⚪ 无最近消息", "priority": "低", "desc": ""}

    last = messages[-1]
    last_sender = last.get("sender", "")
    last_ts = _safe_timestamp(last)
    now_ts = int(datetime.now().timestamp())

    # Is the last message from the other party (unreplied)?
    if last_sender == "":
        minutes_ago = (now_ts - last_ts) // 60 if last_ts else 999
        if minutes_ago < 5:
            return REPLY_SIGNALS["just_sent"]
        return REPLY_SIGNALS["unreplied_last"]

    # Check if other party asked a question or made a request recently
    recent_other = [m for m in messages[-10:] if m.get("sender") == ""]
    for m in reversed(recent_other):
        content = m.get("content", "")
        if any(kw in content for kw in ["?", "？", "吗", "呢", "没", "不", "几点", "怎么"]):
            return REPLY_SIGNALS["waiting_you"]

    return REPLY_SIGNALS["all_caught_up"]


def _generate_reply_hint(messages: list[dict], topics: list[dict]) -> str:
    """Generate a short reply suggestion based on recent context."""
    if not messages:
        return ""
    last = messages[-1]
    content = last.get("content", "")
    last_sender = last.get("sender", "")

    # Only suggest if the other party sent the last message
    if last_sender != "":
        return ""

    # Extract topic hints
    topic_names = [t["topic"] for t in topics[:3]] if topics else []

    if any(kw in content for kw in ["早", "早安", "早上"]):
        return "回复早安 + 问昨晚睡得怎么样"
    if any(kw in content for kw in ["下班", "下班了", "走了"]):
        return "问她今天累不累 + 路上注意安全"
    if any(kw in content for kw in ["烦", "受不了", "气死", "无语"]):
        return "站她旁边一起吐槽，别分析别讲道理"
    if any(kw in content for kw in ["吃", "饿", "饭", "外卖"]):
        return "问她吃了什么 + 好吃吗"
    if any(kw in content for kw in ["睡觉", "睡了", "晚安", "困"]):
        return "说晚安，别追加消息"
    if any(kw in content for kw in ["忙", "加班"]):
        return "说辛苦了，等她忙完再聊"
    if any(kw in content for kw in ["到了", "到家", "回去"]):
        return "确认她安全到家"
    if topic_names:
        return f"接着聊 {'/'.join(topic_names[:2])} 的话题"
    return "回一个表情或简单问候"


def _extract_detailed_topics(enriched: list[dict], total_text_msgs: int) -> list[dict]:
    """Extract detailed topic info: relative percentage, top keywords, sample message."""
    if not enriched or total_text_msgs == 0:
        return []

    all_text = " ".join(m.get("content", "") for m in enriched if m.get("type") == "文本")
    if not all_text.strip():
        return []

    TOPIC_COLORS = {
        "美食": "#f59e0b", "工作": "#f85149", "购物": "#3fb950",
        "情感": "#db61a2", "健康": "#58a6ff", "出行": "#a371f7",
        "娱乐": "#79c0ff", "家庭": "#d2991d",
    }

    results = []
    for topic, keywords in TOPIC_KEYWORDS.items():
        hits = {}
        for kw in keywords:
            c = all_text.count(kw)
            if c > 0:
                hits[kw] = c
        if not hits:
            continue

        total_hits = sum(hits.values())
        sorted_kw = sorted(hits, key=hits.get, reverse=True)

        # Find a representative message (best match for this topic's top keywords)
        sample_msg = ""
        best_score = 0
        for m in enriched:
            if m.get("type") != "文本":
                continue
            content = m.get("content", "")
            score = sum(1 for kw in sorted_kw[:3] if kw in content)
            if score > best_score and len(content) > 4:
                best_score = score
                sample_msg = content[:80]

        results.append({
            "topic": topic,
            "total_hits": total_hits,
            "top_keywords": sorted_kw[:5],
            "sample_msg": sample_msg,
            "color": TOPIC_COLORS.get(topic, "#8b949e"),
        })

    results.sort(key=lambda x: x["total_hits"], reverse=True)

    # Normalize to relative percentages (sum = 100%)
    total_all_hits = sum(r["total_hits"] for r in results)
    for r in results:
        r["pct"] = round(r["total_hits"] / max(1, total_all_hits) * 100, 1)

    return results[:10]


def _compute_topic_trends(current_topics: list[dict], prev_enriched: list[dict]) -> dict:
    """Compare topic density with previous period to detect trends."""
    if not prev_enriched:
        return {}

    prev_all = " ".join(m.get("content", "") for m in prev_enriched if m.get("type") == "文本")
    prev_total = sum(1 for m in prev_enriched if m.get("type") == "文本")

    trends = {}
    for t in current_topics:
        topic = t["topic"]
        keywords = TOPIC_KEYWORDS.get(topic, [])
        prev_hits = sum(prev_all.count(kw) for kw in keywords)
        prev_pct = round(prev_hits / max(1, prev_total) * 100, 1) if prev_total else 0
        curr_pct = t["pct"]
        if prev_pct > 0:
            change = round((curr_pct - prev_pct) / prev_pct * 100)
        else:
            change = 100 if curr_pct > 0 else 0
        trends[topic] = {"prev_pct": prev_pct, "change": change}

    return trends


def _aggregate_global_topics(group_summaries: list[dict], private_summaries: list[dict] = None) -> list[dict]:
    """Aggregate topics across all groups and private chats for a global leaderboard."""
    topic_agg = defaultdict(lambda: {"total_hits": 0, "group_count": 0, "private_count": 0,
                                      "all_keywords": Counter(), "samples": [],
                                      "matching_groups": [], "matching_privates": []})

    for g in (group_summaries or []):
        if not isinstance(g, dict):
            continue
        gname = g.get("name", "")
        for t in g.get("detailed_topics", []):
            if not isinstance(t, dict):
                continue
            name = t.get("topic", "")
            agg = topic_agg[name]
            agg["total_hits"] += t.get("total_hits", 0)
            agg["group_count"] += 1
            for kw in t.get("top_keywords", [])[:3]:
                agg["all_keywords"][kw] += 1
            if t.get("sample_msg"):
                agg["samples"].append(t["sample_msg"])
            if gname not in agg["matching_groups"]:
                agg["matching_groups"].append(gname)

    for p in (private_summaries or []):
        if not isinstance(p, dict):
            continue
        pname = p.get("name", "")
        for t in p.get("topics", []):
            if isinstance(t, str):
                topic_name = t
            elif isinstance(t, dict):
                topic_name = t.get("topic", "")
            else:
                continue
            agg = topic_agg[topic_name]
            agg["total_hits"] += 1
            agg["private_count"] += 1
            if pname not in agg["matching_privates"]:
                agg["matching_privates"].append(pname)

    global_list = []
    for topic, agg in topic_agg.items():
        global_list.append({
            "topic": topic,
            "total_hits": agg["total_hits"],
            "group_count": agg["group_count"],
            "private_count": agg["private_count"],
            "top_keywords": [kw for kw, _ in agg["all_keywords"].most_common(5)],
            "sample_msg": agg["samples"][0] if agg["samples"] else "",
            "matching_groups": agg["matching_groups"],
            "matching_privates": agg["matching_privates"],
        })

    global_list.sort(key=lambda x: x["total_hits"], reverse=True)
    return global_list[:12]


def _compute_msg_trend(name: str, since_d: str, until_d: str, current_count: int) -> dict:
    """Compute message count trend vs previous equivalent period."""
    try:
        since_dt = datetime.strptime(since_d, "%Y-%m-%d")
        until_dt = datetime.strptime(until_d, "%Y-%m-%d")
        span = max(1, (until_dt - since_dt).days + 1)
        prev_until = (since_dt - timedelta(days=1)).strftime("%Y-%m-%d")
        prev_since = (since_dt - timedelta(days=span)).strftime("%Y-%m-%d")
        cmd = f'wx history {_q(name)} --since {prev_since} --until {prev_until} -n 99999 --json'
        prev_raw = _run_wx(cmd)
        prev_count = 0
        if isinstance(prev_raw, list):
            prev_count = len([m for m in prev_raw if isinstance(m, dict)])
        if prev_count == 0:
            return {"label": "🆕", "prev_count": 0, "change_pct": 0}
        change_pct = round((current_count - prev_count) / prev_count * 100)
        if change_pct > 30:
            return {"label": "↑ 增长", "prev_count": prev_count, "change_pct": change_pct}
        elif change_pct < -30:
            return {"label": "↓ 下降", "prev_count": prev_count, "change_pct": change_pct}
        else:
            return {"label": "→ 平稳", "prev_count": prev_count, "change_pct": change_pct}
    except Exception:
        return {"label": "—", "prev_count": 0, "change_pct": 0}


def _compute_activity_label(msg_count: int, days: int) -> str:
    if days <= 0:
        return "💤 冷清"
    avg = msg_count / max(1, days)
    if avg > 200:
        return "🔥 火爆"
    if avg > 80:
        return "🔥 活跃"
    if avg > 20:
        return "💬 一般"
    return "💤 冷清"


def _get_time_range(range_key: str, since: str, until: str) -> tuple:
    """Resolve time range. Returns (since_date, until_date, label)."""
    today = datetime.now().strftime("%Y-%m-%d")
    if range_key == "today":
        return today, today, "今日"
    elif range_key == "3d":
        since_d = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
        return since_d, today, "近3天"
    elif range_key == "7d":
        since_d = (datetime.now() - timedelta(days=6)).strftime("%Y-%m-%d")
        return since_d, today, "近7天"
    else:
        return since or today, until or today, f"{since} — {until}"


def analyze_summary(range_key: str = "7d", since: str = "", until: str = "") -> dict:
    """Generate summary dashboard for all active chats."""
    since_d, until_d, label = _get_time_range(range_key, since, until)

    # Get unread, sessions, and contacts
    unread_raw = _run_wx("wx unread --filter private,group --json")
    sessions_raw = _run_wx("wx sessions --json")
    contacts_raw = _run_wx("wx contacts --json")

    unread_list = unread_raw if isinstance(unread_raw, list) else []
    sessions = sessions_raw if isinstance(sessions_raw, list) else []
    contacts = contacts_raw if isinstance(contacts_raw, list) else []

    # Build unread lookup
    unread_map = {}
    for u in unread_list:
        unread_map[u.get("chat", "")] = u.get("unread", 0)

    # Build session type/username lookup (wx search doesn't return chat_type)
    session_info = {}  # name -> {chat_type, username}
    for s in sessions:
        if isinstance(s, dict) and s.get("chat"):
            session_info[s["chat"]] = {
                "chat_type": s.get("chat_type", ""),
                "username": s.get("username", ""),
            }

    # Gather all known group names from sessions
    known_groups = {name for name, info in session_info.items() if info["chat_type"] == "group"}

    # Track processed names to avoid duplicates
    # ── Phase 1: Collect sessions to process ──
    seen_chats = set()
    sessions_to_process = []  # list of (name, chat_type, username, session_dict)

    for s in sessions:
        if not isinstance(s, dict):
            continue
        name = s.get("chat", "")
        chat_type = s.get("chat_type", "")
        username = s.get("username", "")

        # Skip system/folded entries
        if chat_type in ("folded", "official_account"):
            continue
        if name in ("服务通知", "微信团队", "文件传输助手", "brandsessionholder", "brandservicesessionholder"):
            continue
        if not name:
            continue
        if name in seen_chats:
            continue
        seen_chats.add(name)

        sessions_to_process.append((name, chat_type, username, s))

    # ── Phase 2: Parallel fetch all wx history ──
    # Warm up: prime the wx-cli and SQLite database cache
    if sessions_to_process:
        _run_wx(f'wx history {_q(sessions_to_process[0][0])} --since {since_d} --until {until_d} -n 1 --json')

    def _fetch_history(name):
        cmd = f'wx history {_q(name)} --since {since_d} --until {until_d} -n 50 --json'
        raw = _run_wx(cmd)
        if not isinstance(raw, list):
            return None
        filtered = [m for m in raw if isinstance(m, dict)]
        if not filtered:
            return None
        return _parse_messages(filtered)

    # Fetch all histories in parallel
    session_data = {}  # name -> enriched messages
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(_fetch_history, name): name for name, _, _, _ in sessions_to_process}
        for future in as_completed(futures):
            name = futures[future]
            try:
                result = future.result()
                if result:
                    session_data[name] = result
            except Exception:
                pass

    # ── Phase 3: Process results sequentially ──
    private_summaries = []
    group_summaries = []
    total_private = 0
    total_group = 0
    total_todos = 0
    total_unread = sum(unread_map.values())

    for name, chat_type, username, s in sessions_to_process:
        enriched = session_data.get(name)
        if not enriched:
            continue

        # Infer chat_type if not set by sessions data
        if not chat_type or chat_type not in ("private", "group"):
            senders = {m.get("sender", "") for m in enriched} - {""}
            if len(senders) > 1:
                chat_type = "group"
            elif name in known_groups:
                chat_type = "group"
            else:
                chat_type = "private"

        # All text content
        all_text = " ".join(m.get("content", "") for m in enriched if m.get("type") == "文本")

        if chat_type == "private":
            total_private += 1

            # User name detection
            user_name = "你"
            for m in enriched:
                if m.get("sender") and m["sender"] != "":
                    user_name = m["sender"]
                    break

            # Topics
            topics = _extract_topics(all_text)

            # Todos (from the other party's messages)
            other_msgs = [m for m in enriched if m.get("sender") == ""]
            other_text = " ".join(m.get("content", "") for m in other_msgs if m.get("type") == "文本")
            todos = _extract_todos(other_text, "")

            # Also check own messages for commitments
            my_msgs = [m for m in enriched if m.get("sender") != ""]
            my_text = " ".join(m.get("content", "") for m in my_msgs if m.get("type") == "文本")
            my_todos = _extract_todos(my_text, user_name)
            all_todos = todos + my_todos
            all_todos.sort(key=lambda x: {"高": 0, "中": 1, "低": 2}.get(x.get("priority", "中"), 9))
            total_todos += len(all_todos)

            # Signal
            signal = _detect_signal(enriched, user_name)

            # Reply hint
            reply_hint = _generate_reply_hint(enriched, topics)

            # Recent preview (last 3)
            recent = []
            for m in enriched[-3:]:
                who = name if m.get("sender") == "" else user_name
                recent.append({
                    "who": who,
                    "content": m.get("content", "")[:80],
                    "time": m.get("time", "").split(" ")[-1] if " " in m.get("time", "") else "",
                })

            # Trend: simplified for summary dashboard
            trend = {"label": "—", "prev_count": 0, "change_pct": 0}

            private_summaries.append({
                "name": name,
                "username": username,
                "last_active": s.get("time", ""),
                "last_summary": s.get("summary", "")[:50],
                "unread": unread_map.get(name, 0),
                "msg_count": len(enriched),
                "trend": trend,
                "topics": [t["topic"] for t in topics[:4]],
                "todos": all_todos[:5],
                "signal": signal,
                "reply_hint": reply_hint,
                "recent": recent,
            })

        elif chat_type == "group":
            total_group += 1
            session_time = s.get("time", "")

            # Detailed topics
            text_count = sum(1 for m in enriched if m.get("type") == "文本")
            detailed_topics = _extract_detailed_topics(enriched, text_count)

            # Trend: simplified for summary dashboard
            trends = {}

            # Top speakers
            sender_counter = Counter()
            for m in enriched:
                s = m.get("sender", "")
                if s:
                    sender_counter[s] += 1
            top3 = [s for s, _ in sender_counter.most_common(3)]

            # Peak hour
            hours = [0] * 24
            for m in enriched:
                hours[m["hour"]] += 1
            peak_hour = hours.index(max(hours)) if max(hours) > 0 else 0

            # @me count
            at_me = sum(1 for m in enriched if "@你" in m.get("content", "") or "@Klesen" in m.get("content", ""))

            # Activity label
            dates_set = set(m["date"] for m in enriched)
            activity = _compute_activity_label(len(enriched), len(dates_set))

            group_summaries.append({
                "name": name,
                "username": username,
                "last_active": session_time,
                "unread": unread_map.get(name, 0),
                "msg_count": len(enriched),
                "active_days": len(dates_set),
                "activity": activity,
                "detailed_topics": detailed_topics,
                "topic_trends": trends,
                "top_speakers": top3,
                "peak_hour": peak_hour,
                "peak_label": {0: "凌晨", 6: "清晨", 9: "早高峰", 12: "午休", 14: "下午", 18: "晚餐", 21: "晚高峰", 23: "深夜"}.get(
                    max(k for k in [0, 6, 9, 12, 14, 18, 21, 23] if peak_hour >= k), ""
                ),
                "at_me": at_me,
            })

    # ── Also check contacts not in sessions (for private chats) ──
    # wx new-messages may not have chat_type; use known_groups to filter
    new_msgs_raw = _run_wx("wx new-messages --json")
    new_contacts = set()
    if isinstance(new_msgs_raw, list):
        for nm in new_msgs_raw:
            if isinstance(nm, dict):
                nm_name = nm.get("chat", "")
                # Skip known groups (already handled in sessions loop)
                if nm_name and nm_name not in known_groups:
                    new_contacts.add(nm_name)

    # Merge: sessions cover recent, contacts cover all, new-messages covers very recent
    extra_names = set()
    for nm_name in new_contacts:
        if nm_name not in seen_chats:
            extra_names.add(nm_name)

    # ── Fallback: sample probe unseen contacts (limit 15 for performance) ──
    # Sessions + new-messages covers 95%+ of active chats. This catches edge cases.
    # Removed: 35-word search loop (was spawning 35 subprocesses = ~40s latency)
    fallback_count = 0
    for c in contacts:
        if not isinstance(c, dict):
            continue
        c_name = (c.get("display", "") or "").strip()
        if not c_name or c_name in seen_chats:
            continue
        # Quick probe: any messages in range?
        cmd = f'wx history {_q(c_name)} --since {since_d} --until {until_d} -n 1 --json'
        probe = _run_wx(cmd)
        if isinstance(probe, list) and probe:
            probe = [p for p in probe if isinstance(p, dict)]
            if probe:
                extra_names.add(c_name)
        fallback_count += 1
        if fallback_count >= 15:
            break

    # Process extra names (quick check first)
    checked = 0
    for name in extra_names:
        cmd = f'wx history {_q(name)} --since {since_d} --until {until_d} -n 1 --json'
        msgs = _run_wx(cmd)
        if not isinstance(msgs, list) or not msgs:
            continue
        msgs = [m for m in msgs if isinstance(m, dict)]
        if not msgs:
            continue

        # Detect group chats by checking senders (groups have diverse non-empty senders)
        raw_senders = {m.get("sender", "") for m in msgs}
        non_me_senders = raw_senders - {"", "Klesen"}
        if non_me_senders:
            # This is a group chat not in sessions — process it as a group
            seen_chats.add(name)
            msgs = _run_wx(f'wx history {_q(name)} --since {since_d} --until {until_d} -n 100 --json')
            if not isinstance(msgs, list) or not msgs:
                continue
            msgs = [m for m in msgs if isinstance(m, dict)]
            if not msgs:
                continue
            enriched = _parse_messages(msgs)
            if not enriched:
                continue

            total_group += 1
            text_count = sum(1 for m in enriched if m.get("type") == "文本")
            detailed_topics = _extract_detailed_topics(enriched, text_count)
            sender_counter = Counter()
            for m in enriched:
                s = m.get("sender", "")
                if s:
                    sender_counter[s] += 1
            top3 = [s for s, _ in sender_counter.most_common(3)]
            hours = [0] * 24
            for m in enriched:
                hours[m["hour"]] += 1
            peak_hour = hours.index(max(hours)) if max(hours) > 0 else 0
            at_me = sum(1 for m in enriched if "@你" in m.get("content", "") or "@Klesen" in m.get("content", ""))
            dates_set = set(m["date"] for m in enriched)
            activity = _compute_activity_label(len(enriched), len(dates_set))
            session_time = enriched[-1].get("time", "") if enriched else ""

            group_summaries.append({
                "name": name, "username": "",
                "last_active": session_time,
                "unread": unread_map.get(name, 0),
                "msg_count": len(enriched),
                "active_days": len(dates_set),
                "activity": activity,
                "detailed_topics": detailed_topics,
                "topic_trends": [],
                "top_speakers": top3,
                "peak_hour": peak_hour,
                "peak_label": {0: "凌晨", 6: "清晨", 9: "早高峰", 12: "午休", 14: "下午", 18: "晚餐", 21: "晚高峰", 23: "深夜"}.get(
                    max(k for k in [0, 6, 9, 12, 14, 18, 21, 23] if peak_hour >= k), ""
                ),
                "at_me": at_me,
            })
            continue

        seen_chats.add(name)
        cmd = f'wx history {_q(name)} --since {since_d} --until {until_d} -n 100 --json'
        msgs = _run_wx(cmd)
        if not isinstance(msgs, list) or not msgs:
            continue
        msgs = [m for m in msgs if isinstance(m, dict)]
        if not msgs:
            continue
        enriched = _parse_messages(msgs)
        if not enriched:
            continue

        total_private += 1
        checked += 1
        if checked > 100:
            break

        all_text = " ".join(m.get("content", "") for m in enriched if m.get("type") == "文本")

        user_name = "你"
        for m in enriched:
            if m.get("sender") and m["sender"] != "":
                user_name = m["sender"]
                break

        topics = _extract_topics(all_text)
        other_msgs = [m for m in enriched if m.get("sender") == ""]
        other_text = " ".join(m.get("content", "") for m in other_msgs if m.get("type") == "文本")
        todos = _extract_todos(other_text, "")
        my_msgs = [m for m in enriched if m.get("sender") != ""]
        my_text = " ".join(m.get("content", "") for m in my_msgs if m.get("type") == "文本")
        my_todos = _extract_todos(my_text, user_name)
        all_todos = todos + my_todos
        all_todos.sort(key=lambda x: {"高": 0, "中": 1, "低": 2}.get(x.get("priority", "中"), 9))
        total_todos += len(all_todos)

        signal = _detect_signal(enriched, user_name)
        reply_hint = _generate_reply_hint(enriched, topics)

        recent = []
        for m in enriched[-3:]:
            who = name if m.get("sender") == "" else user_name
            recent.append({
                "who": who,
                "content": m.get("content", "")[:80],
                "time": m.get("time", "").split(" ")[-1] if " " in m.get("time", "") else "",
            })

        # Trend: simplified — extra_names are edge cases
        trend = {"label": "—", "prev_count": 0, "change_pct": 0}

        private_summaries.append({
            "name": name, "username": username,
            "last_active": enriched[-1].get("time", "") if enriched else "",
            "last_summary": "", "unread": unread_map.get(name, 0),
            "msg_count": len(enriched),
            "trend": trend,
            "topics": [t["topic"] for t in topics[:4]],
            "todos": all_todos[:5],
            "signal": signal, "reply_hint": reply_hint,
            "recent": recent,
        })

    # Sort: unread first, then by recency
    private_summaries.sort(key=lambda x: (-x["unread"], x.get("last_active", "")))
    group_summaries.sort(key=lambda x: (-x["unread"], -x["msg_count"]))

    # Global topic leaderboard
    global_topics = _aggregate_global_topics(group_summaries, private_summaries)
    global_topics_groups = _aggregate_global_topics(group_summaries, [])
    global_topics_privates = _aggregate_global_topics([], private_summaries)

    return {
        "time_label": label,
        "since": since_d,
        "until": until_d,
        "overview": {
            "total_private": total_private,
            "total_group": total_group,
            "total_unread": total_unread,
            "total_todos": total_todos,
        },
        "private_summaries": private_summaries,
        "group_summaries": group_summaries,
        "global_topics": global_topics,
        "global_topics_groups": global_topics_groups,
        "global_topics_privates": global_topics_privates,
    }


# ═══════════════════════════════════════════
#  Custom Tag Storage
# ═══════════════════════════════════════════

import os as _os

_TAG_FILE = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "custom_tags.json")


def _load_all_tags() -> dict:
    if _os.path.exists(_TAG_FILE):
        try:
            with open(_TAG_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {}


def _save_all_tags(tags: dict):
    with open(_TAG_FILE, "w") as f:
        json.dump(tags, f, ensure_ascii=False, indent=2)



def _load_custom_tag(contact_name: str) -> dict:
    """Load custom tag for a contact. Returns dict or empty dict."""
    tags = _load_all_tags()
    raw = tags.get(contact_name, {})
    # Backward compat: old format was a string
    if isinstance(raw, str):
        return {"label": raw, "identity_key": ""} if raw else {}
    if isinstance(raw, dict):
        return raw
    return {}


def set_custom_tag(contact_name: str, tag: str, identity_key: str = ""):
    tags = _load_all_tags()
    if tag or identity_key:
        tags[contact_name] = {"label": tag, "identity_key": identity_key}
    else:
        tags.pop(contact_name, None)
    _save_all_tags(tags)



def delete_custom_tag(contact_name: str):
    tags = _load_all_tags()
    tags.pop(contact_name, None)
    _save_all_tags(tags)


def list_custom_tags() -> dict:
    return _load_all_tags()


# ═══════════════════════════════════════════
#  Custom Identity Definitions (user-created)
# ═══════════════════════════════════════════

_CUSTOM_ID_FILE = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "custom_identities.json")


def _load_custom_identities() -> list[dict]:
    """Load user-created custom identities. Each: {key, label, icon, base}"""
    if _os.path.exists(_CUSTOM_ID_FILE):
        try:
            with open(_CUSTOM_ID_FILE, "r") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
        except (json.JSONDecodeError, IOError):
            pass
    return []


def _save_custom_identities(idents: list[dict]):
    with open(_CUSTOM_ID_FILE, "w") as f:
        json.dump(idents, f, ensure_ascii=False, indent=2)


def list_custom_identities() -> list[dict]:
    return _load_custom_identities()


def set_custom_identity(key: str, label: str, icon: str, base: str = "friend"):
    """Save or update a user-created custom identity."""
    idents = _load_custom_identities()
    # Update existing or append
    found = False
    for i, ident in enumerate(idents):
        if ident["key"] == key:
            idents[i] = {"key": key, "label": label, "icon": icon, "base": base}
            found = True
            break
    if not found:
        idents.append({"key": key, "label": label, "icon": icon, "base": base})
    _save_custom_identities(idents)


def delete_custom_identity(key: str):
    idents = _load_custom_identities()
    idents = [i for i in idents if i["key"] != key]
    _save_custom_identities(idents)
