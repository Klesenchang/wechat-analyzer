"""
LLM 调用模块 — OpenAI 兼容接口
支持 DeepSeek、OpenAI、OpenRouter 等所有 OpenAI 兼容 API
"""

import json
import os
import time
import urllib.request
import urllib.error

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
USAGE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "usage_stats.json")

def _load_usage():
    try:
        with open(USAGE_PATH, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {
            "total_calls": 0,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_cost_est": 0.0,
            "by_function": {},
            "calls": [],
        }

def _save_usage(stats):
    with open(USAGE_PATH, "w") as f:
        json.dump(stats, f, indent=2, ensure_ascii=False)

_usage_stats = None

def _get_usage():
    global _usage_stats
    if _usage_stats is None:
        _usage_stats = _load_usage()
    return _usage_stats

def _track_usage(caller, input_tokens, output_tokens):
    s = _get_usage()
    s["total_calls"] += 1
    s["total_input_tokens"] += input_tokens
    s["total_output_tokens"] += output_tokens
    s["total_cost_est"] += (input_tokens / 1_000_000 * 0.28) + (output_tokens / 1_000_000 * 1.10)
    fn = s["by_function"].setdefault(caller, {"calls": 0, "input_tokens": 0, "output_tokens": 0})
    fn["calls"] += 1
    fn["input_tokens"] += input_tokens
    fn["output_tokens"] += output_tokens
    s["calls"].append({
        "caller": caller,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    })
    if len(s["calls"]) > 100:
        s["calls"] = s["calls"][-100:]
    _save_usage(s)

def get_usage_stats():
    return _get_usage()

def reset_usage_stats():
    empty = {
        "total_calls": 0, "total_input_tokens": 0, "total_output_tokens": 0,
        "total_cost_est": 0.0, "by_function": {}, "calls": [],
    }
    _save_usage(empty)
    global _usage_stats
    _usage_stats = empty
    return empty


def _load_config():
    with open(CONFIG_PATH, "r") as f:
        return json.load(f)


def _save_config(cfg):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def get_config():
    """Return current LLM config (raw — no masking, for frontend display)."""
    cfg = _load_config()
    return cfg.get("llm", {})


def update_config(**kwargs):
    """Update LLM config fields. Returns updated config."""
    cfg = _load_config()
    llm = cfg.setdefault("llm", {})
    for k, v in kwargs.items():
        if v is not None and k in {"provider", "model", "api_key", "base_url",
                                     "enabled", "max_tokens", "temperature"}:
            llm[k] = v
    _save_config(cfg)
    return get_config()


def is_available():
    """Check if LLM is configured and enabled."""
    cfg = _load_config().get("llm", {})
    return bool(cfg.get("enabled") and cfg.get("api_key"))


def chat(system_prompt, user_prompt, temperature=None, max_tokens=None, caller="unknown"):
    """
    Send a chat request to the configured LLM.
    Returns (success: bool, content: str, error: str).
    caller: human-readable name of the calling function for usage tracking
    """
    cfg = _load_config().get("llm", {})
    if not cfg.get("enabled"):
        return False, "", "LLM 未启用，请在设置中配置并启用"
    if not cfg.get("api_key"):
        return False, "", "API Key 未配置"

    base_url = cfg.get("base_url", "https://api.deepseek.com/v1").rstrip("/")
    model = cfg.get("model", "deepseek-chat")
    api_key = cfg["api_key"]
    temp = temperature if temperature is not None else cfg.get("temperature", 0.7)
    max_tok = max_tokens if max_tokens is not None else cfg.get("max_tokens", 1024)

    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temp,
        "max_tokens": max_tok,
    }).encode("utf-8")

    # Rough input token estimate: 1 token ≈ 3 chars for Chinese
    est_input = max(1, (len(system_prompt) + len(user_prompt)) // 3)
    t0 = time.time()

    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
            data = json.loads(raw)
            content = data["choices"][0]["message"]["content"]

            # Track usage
            usage = data.get("usage", {})
            in_tok = usage.get("prompt_tokens", est_input)
            out_tok = usage.get("completion_tokens", max(1, len(content) // 3))
            _track_usage(caller, in_tok, out_tok)

            return True, content.strip(), ""
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        return False, "", f"HTTP {e.code}: {err_body[:300]}"
    except Exception as e:
        return False, "", str(e)


# ── High-level analysis helpers ──


def analyze_signal(messages, contact_name="联系人", user_name=""):
    """
    Deep analysis of chat signals: emotional state, needs, risks, suggestions.
    messages: list of {sender, content, time}
    contact_name: name of the contact being analyzed
    user_name: name of the backend user
    Returns dict or None.
    """
    if not is_available():
        return None

    convo, total, preamble = _format_private_convo(messages, contact_name, user_name, 30)

    system = f"""{preamble}你是一位专业的沟通分析师。分析以上对话：
- 标记为「对方」的消息来自联系人
- 标记为其他名字的消息来自用户（后台使用人）

请分析：
1. 对方的情绪状态和潜在需求
2. 对话中的风险点或冲突信号
3. 用户应不应该回复、用什么语气

用 JSON 格式回复：
{{
  "emotion": "对方的情绪状态（1-2句话）",
  "needs": "对方可能的需求或期待（1-2句话）",
  "risk": "当前对话中的风险点或冲突信号（无则填'无明显风险'）",
  "suggestions": ["给用户的具体回复建议1", "建议2", "建议3"],
  "should_reply": true/false,
  "reply_tone": "建议用户采用的回复语气"
}}"""

    ok, content, err = chat(system, f"分析以下对话：\n\n{convo}", caller="信号分析")
    if not ok:
        return {"error": err}

    try:
        # Try to parse JSON, be lenient
        content = content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[-1].rsplit("```", 1)[0]
        return json.loads(content)
    except json.JSONDecodeError:
        return {"raw": content}


def suggest_reply(messages, style="自然", identity="lover", contact_name="联系人", user_name=""):
    """
    AI-powered reply suggestion — identity-aware.
    messages: list of {sender, content, time}
    contact_name: name of the contact being analyzed
    user_name: name of the backend user
    """
    if not is_available():
        return None

    convo, total, preamble = _format_private_convo(messages, contact_name, user_name, 30)

    IDENTITY_VOICE = {
        "lover":     "对话双方是恋爱关系。回复要亲密自然，可以撒娇和表达关心。",
        "friend":    "对话双方是朋友关系。回复要轻松随意，可以开玩笑和吐槽。",
        "colleague": "对话双方是同事关系。回复要专业高效，保持职场分寸。",
        "family":    "对话双方是家人关系。回复要温暖亲切，带点唠叨和牵挂。",
        "business":  "对话双方是商业合作关系。回复要正式专业，体现尊重和效率。",
        "service":   "对方是客服/服务人员，用户是客户。回复要礼貌得体。",
    }
    voice = IDENTITY_VOICE.get(identity, IDENTITY_VOICE["lover"])

    system = f"""{preamble}你是聊天回复助手。{voice}
请帮「我」生成3条回复「{contact_name}」的建议。
风格倾向：{style}
用 JSON 格式回复：
{{
  "replies": [
    {{"text": "回复内容", "style": "风格标签", "reason": "为什么这样回"}}
  ],
  "context_note": "当前对话状态的一句话总结"
}}
回复要短（20-80字），符合微信聊天习惯。"""

    ok, content, err = chat(system, f"根据上下文给出回复建议：\n\n{convo}", caller="回复建议")
    if not ok:
        return {"error": err}

    try:
        content = content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[-1].rsplit("```", 1)[0]
        return json.loads(content)
    except json.JSONDecodeError:
        return {"raw": content}


def analyze_chat_insight(messages, chat_name, identity="lover", user_name=""):
    """
    Identity-aware relationship insight — 6 dimensions (3 universal + 3 identity-specific).
    """
    if not is_available():
        return None

    convo, total, preamble = _format_private_convo(messages, chat_name, user_name, 100)

    # ── Dimension definitions ──
    DIMS = {
        "lover": {
            "role": "亲密关系分析师",
            "universal": [
                "互动节奏：聊天频率、回复速度、深夜占比、主动发起比例",
                "关系温度：亲昵词密度、情绪信号（关心/冲突/调侃）、阶段趋势",
                "发展趋势：升温/降温/稳定迹象、风险信号、关系拐点",
            ],
            "specific": [
                "情感深度：直接表达vs间接暗示、情绪共鸣程度、脆弱面展示",
                "权力动态：谁发起话题、谁主导节奏、谁更常妥协",
                "未来信号：长期承诺暗示、共同计划、'我们'语言的使用",
            ],
        },
        "friend": {
            "role": "社交关系分析师",
            "universal": [
                "互动节奏：聊天频率和间隔、群聊vs私聊模式、回复习惯",
                "关系温度：轻松度和调侃度、话题广度和多样性、信任表现",
                "发展趋势：走近/疏远迹象、共同活动频率变化",
            ],
            "specific": [
                "兴趣共鸣：共同话题重叠度、互相推荐分享的频率",
                "互惠平衡：付出/索取比、帮忙和求助的对称性",
                "圈子融合：共同好友提及频率、群聊互动模式",
            ],
        },
        "colleague": {
            "role": "职场沟通分析师",
            "universal": [
                "互动节奏：工作日响应速度、会议/同步频率、沟通时段分布",
                "关系温度：专业尊重度、协作顺畅度、正式vs随意语气",
                "发展趋势：协作加深/收尾迹象、角色变化",
            ],
            "specific": [
                "信息同步：消息清晰度、是否有漏接或误解、确认和反馈模式",
                "边界感：工作时间外沟通频率、公事私事的分明程度",
                "依赖模式：单向求助vs双向协作、谁更常发起工作话题",
            ],
        },
        "family": {
            "role": "家庭关系分析师",
            "universal": [
                "互动节奏：联系频率和间隔、通话vs文字偏好、联系时段",
                "关系温度：关怀表达密度、情感直接/含蓄程度、温暖度",
                "发展趋势：回暖/疏远迹象、家庭事件影响",
            ],
            "specific": [
                "责任分担：家务/赡养/育儿话题、任务分配和协调",
                "代际动态：长辈vs平辈vs晚辈的互动差异和尊重模式",
                "生活参与：日常琐事分享频率、重大决定的商议程度、对彼此生活的了解深度",
            ],
        },
        "business": {
            "role": "商业合作分析师",
            "universal": [
                "互动节奏：响应速度和沟通密度、沟通时段分布",
                "关系温度：信任程度、合作意愿、正式vs随意的平衡",
                "发展趋势：合作推进/搁置/收尾迹象",
            ],
            "specific": [
                "利益对齐：共赢信号vs博弈信号、价值交换的平衡度",
                "专业匹配：能力互补程度、资源匹配、专业度评估",
                "风险评估：违约/摩擦信号、沟通中的不确定性和保留态度",
            ],
        },
        "service": {
            "role": "客户服务分析师",
            "universal": [
                "互动节奏：响应速度、服务频次、互动密度变化",
                "关系温度：满意度信号、礼貌度和尊重度、投诉/表扬",
                "发展趋势：增长/流失迹象、复购/续费意愿",
            ],
            "specific": [
                "问题解决：一次解决率、反复沟通的问题类型、解决效率",
                "主动服务：提醒/关怀/跟进频率、超出预期的服务行为",
                "客户粘性：推荐意愿、忠诚度信号、对竞品的提及",
            ],
        },
    }

    dims = DIMS.get(identity, DIMS["lover"])

    system = f"""{preamble}你是{dims['role']}。按以下6个维度进行系统分析：

【通用维度】
1. {dims['universal'][0]}
2. {dims['universal'][1]}
3. {dims['universal'][2]}

【专属维度】
4. {dims['specific'][0]}
5. {dims['specific'][1]}
6. {dims['specific'][2]}

要求：
- 每个维度写1-2句话，基于实际对话内容，不要空洞
- 最后给出2-3条具体可操作的建议
- 不要复述维度名称作为标题，直接写出分析内容
- 用「•」开头分条输出，建议部分用「💡」开头
- 总共不超过20行"""

    ok, content, err = chat(system, convo, caller="关系洞察")
    if not ok:
        return {"error": err}
    return {"insight": content.strip()}


def generate_advice_ai(messages, contact_name, intimacy, signals, dynamics, phases, user_name=""):
    """
    AI-powered relationship advice generator.
    """
    if not is_available():
        return None

    convo, total, preamble = _format_private_convo(messages, contact_name, user_name, 400)

    # Analysis summary
    intimacy_score = intimacy.get("total", 0)
    intimacy_label = intimacy.get("label", "")
    phase_label = phases.get("label", "")
    phase_desc = phases.get("description", "")
    signal_summary = signals.get("summary", "")
    her_start_pct = dynamics.get("initiator", {}).get("her_pct", 50)
    late_pct = dynamics.get("late_night_pct", 0)

    summary = f"""分析数据：
- 亲密度：{intimacy_score}分（{intimacy_label}）
- 关系阶段：{phase_label} — {phase_desc}
- 信号摘要：{signal_summary}
- 她发起对话占比：{her_start_pct}%
- 深夜聊天占比：{late_pct}%"""

    system = f"""你是「{contact_name}」的关系沟通顾问。用户的聊天对象叫「{contact_name}」。
根据聊天记录和分析数据，给出3-5条具体可操作的关系发展建议。
每条建议要：有洞察力（不只是常识）、给出具体话术或行动、按优先级排序。

用 JSON 数组格式回复，不要 Markdown 代码块：
[
  {{"priority": 1, "title": "建议标题（6字以内）", "detail": "详细说明（30-80字）", "icon": "单个emoji", "topics": ["具体的可执行话术或行动1", "话术2"]}}
]
priority 从 1 开始递增。"""

    user = f"{summary}\n\n{preamble}聊天记录（共{total}条）：\n{convo}"

    ok, content, err = chat(system, user, temperature=0.8, max_tokens=2048, caller="阶段洞察")
    if not ok:
        return None

    try:
        content = content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        advice = json.loads(content)
        if isinstance(advice, list) and len(advice) > 0:
            # Ensure required fields and limit to 5
            result = []
            for a in advice[:5]:
                result.append({
                    "priority": a.get("priority", len(result) + 1),
                    "title": a.get("title", ""),
                    "detail": a.get("detail", ""),
                    "icon": a.get("icon", "💡"),
                    "topics": a.get("topics", [])[:3],
                })
            return result
    except (json.JSONDecodeError, Exception):
        pass

    return None


def generate_phase_insight(messages, contact_name, identity, phases, intimacy, user_name=""):
    """
    AI-generated phase insight — 3 universal dimensions + 1 suggestion.
    """
    if not is_available():
        return None

    convo, total, preamble = _format_private_convo(messages, contact_name, user_name, 200)

    phase_label = phases.get("label", "")
    intimacy_score = intimacy.get("total", 0)
    intimacy_label = intimacy.get("label", "")

    IDENTITY_CONTEXT = {
        "lover": "亲密关系",
        "friend": "朋友关系",
        "colleague": "同事关系",
        "family": "家庭关系",
        "business": "商业合作关系",
        "service": "客户服务关系",
    }
    rel_context = IDENTITY_CONTEXT.get(identity, "人际关系")

    # Universal dimension interpretations per identity
    UNIV_DIMS = {
        "lover": ("聊天频率和回复速度", "亲昵度和情绪温度", "升温还是降温"),
        "friend": ("聊天频率和互动间隔", "轻松度和话题广度", "走近还是疏远"),
        "colleague": ("沟通频率和响应速度", "专业尊重和协作顺畅度", "协作加深还是收尾"),
        "family": ("联系频率和通话偏好", "关怀密度和情感温度", "回暖还是疏远"),
        "business": ("响应速度和沟通密度", "信任度和合作意愿", "推进还是搁置"),
        "service": ("响应速度和服务频次", "满意度和礼貌度", "增长还是流失"),
    }
    d1, d2, d3 = UNIV_DIMS.get(identity, UNIV_DIMS["lover"])

    system = f"""{preamble}你是{rel_context}分析师。从3个维度给出2-3句话的状态洞察：

1. 互动节奏（{d1}）
2. 关系温度（{d2}）
3. 发展趋势（{d3}）

参考数据：阶段「{phase_label}」，指数 {intimacy_score}分（{intimacy_label}）。

要求：
- 不罗列维度名，用自然段落写出
- 基于对话细节，不要泛泛而谈
- 结尾给1条简短的具体建议
- 直接输出，3-4句话即可"""

    ok, content, err = chat(system, convo, temperature=0.8, max_tokens=512, caller="话题分析")
    if not ok:
        return None
    return content.strip()


# ═══════════════════════════════════════════
#  Message Sampling Helper
# ═══════════════════════════════════════════

def _sample_convo(messages, max_n=200):
    """Sample messages evenly and format for LLM input. Returns (convo_str, total)."""
    total = len(messages)
    sample_n = min(max_n, total)
    if total <= sample_n:
        sampled = messages
    else:
        step = total / sample_n
        sampled = [messages[int(i * step)] for i in range(sample_n)]

    lines = []
    for m in sampled:
        if not isinstance(m, dict):
            continue
        content = m.get("content", "")
        if not content:
            t = m.get("type", "")
            if t == "语音": content = "[语音]"
            elif t == "图片": content = "[图片]"
            elif t == "表情": content = "[表情]"
            elif t == "通话": content = "[通话]"
            else: continue
        sender = m.get("sender", "") or "?"
        time_str = m.get("time", "")[-8:] if m.get("time") else ""
        lines.append(f"[{time_str}] {sender}: {content[:120]}")
    return "\n".join(lines), total


def _format_private_convo(messages, contact_name, user_name, max_n=200):
    """
    Format private chat conversation with clear identity labels.
    - sender="" or sender=contact_name → labeled as contact_name
    - sender=user_name → labeled as "我"
    Returns (convo_str, total, preamble_str).
    """
    total = len(messages)
    sample_n = min(max_n, total)
    if total <= sample_n:
        sampled = messages
    else:
        step = total / sample_n
        sampled = [messages[int(i * step)] for i in range(sample_n)]

    lines = []
    for m in sampled:
        if not isinstance(m, dict):
            continue
        content = m.get("content", "")
        if not content:
            t = m.get("type", "")
            if t == "语音": content = "[语音]"
            elif t == "图片": content = "[图片]"
            elif t == "表情": content = "[表情]"
            elif t == "通话": content = "[通话]"
            else: continue
        raw_sender = m.get("sender", "") or ""
        # Map to clear labels
        if not raw_sender or raw_sender == contact_name:
            label = contact_name
        elif raw_sender == user_name:
            label = "我"
        else:
            label = raw_sender
        time_str = m.get("time", "")[-8:] if m.get("time") else ""
        lines.append(f"[{time_str}] {label}: {content[:120]}")

    preamble = f"对话身份：「{contact_name}」是联系人，「我」是用户（后台使用人 {user_name}）。\n"
    return "\n".join(lines), total, preamble


# ═══════════════════════════════════════════
#  Private Chat Dimensions (new)
# ═══════════════════════════════════════════

def analyze_topics_ai(messages, chat_name, user_name=""):
    """Topic exploration: recent topics, shifts, what the other party cares about."""
    if not is_available():
        return None
    convo, total, preamble = _format_private_convo(messages, chat_name, user_name, 150)
    system = f"""{preamble}你是对话分析师。总结：
1. 最近主要在聊什么话题（列3-5个）
2. 话题有什么变化趋势（哪些变热、哪些变冷）
3. 对方最近特别关注什么（1-2句话）
直接输出中文，用「•」分条，不超过12行。"""
    ok, content, err = chat(system, convo, temperature=0.7, max_tokens=512, caller="趋势追踪")
    if not ok: return None
    return {"topics_insight": content.strip()}


def extract_todos_ai(messages, chat_name, user_name=""):
    """Extract actionable TODOs: promises, commitments, pending items."""
    if not is_available():
        return None
    convo, total, preamble = _format_private_convo(messages, chat_name, user_name, 200)
    system = f"""{preamble}你是行动项提取助手。提取对话中所有待办/承诺/约定。
优先级：
1. **「我」的任务** — 用户需要做的事情（优先提取，放最前面）
2. **「对方」的任务** — 联系人的承诺/待办（作为提醒，放后面）
3. **「双方」的约定** — 需要共同完成的事

格式（JSON数组，不要代码块）：
[{{"priority":"高/中/低","who":"我/对方/双方","item":"具体待办事项","context":"出自哪句对话"}}]
排序：先我、后对方、再双方。没有待办则返回空数组 []。"""
    ok, content, err = chat(system, convo, temperature=0.5, max_tokens=512, caller="待办提取")
    if not ok: return None
    try:
        content = content.strip()
        if content.startswith("```"): content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        todos = json.loads(content)
        if isinstance(todos, list):
            return {"todos": todos[:8]}
    except: pass
    return {"todos": []}


def track_emotion_ai(messages, chat_name, user_name=""):
    """Emotion timeline: recent mood swings, key trigger events."""
    if not is_available():
        return None
    convo, total, preamble = _format_private_convo(messages, chat_name, user_name, 200)
    system = f"""{preamble}你是情绪分析师。分析：
1. 对方近期的情绪起伏趋势（是越来越开心/越来越累/波动大）
2. 有哪些关键事件触发了情绪变化（如有）
3. 当前情绪状态判断
直接输出中文，2-4句话，有洞察力。"""
    ok, content, err = chat(system, convo, temperature=0.7, max_tokens=512, caller="情绪追踪")
    if not ok: return None
    return {"emotion_insight": content.strip()}


# ═══════════════════════════════════════════
#  Group Chat Dimensions (all 6)
# ═══════════════════════════════════════════

def group_topic_leaderboard(messages, chat_name):
    """Group topic leaderboard: hot topics, trends, representative messages."""
    if not is_available():
        return None
    convo, total = _sample_convo(messages, 200)
    system = f"""你是群聊分析师。「{chat_name}」的聊天记录如下。
输出 JSON 数组（不要代码块），每个话题一个对象：
[
  {{"rank": 1, "topic": "话题名", "pct": 30, "trend": "↑", "color": "#58a6ff",
    "keywords": ["关键词1","关键词2"], "sample": "一句代表性发言原文"}}
]
前5个话题，pct加起来=100，trend用 ↑↓→。"""
    ok, content, err = chat(system, convo, temperature=0.7, max_tokens=768, caller="群话题榜")
    if not ok: return None
    try:
        content = content.strip()
        if content.startswith("```"): content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        topics = json.loads(content)
        if isinstance(topics, list):
            return {"topic_leaderboard": topics}
    except: pass
    return {"topic_leaderboard": content.strip()}


def group_member_profile(messages, chat_name):
    """Group member profile: active members, speech styles, engagement tiers."""
    if not is_available():
        return None
    convo, total = _sample_convo(messages, 200)
    system = f"""你是社群分析师。「{chat_name}」的聊天记录如下。
输出 JSON 数组（不要代码块），每个成员一个对象：
[
  {{"name": "成员名", "tier": "核心", "msg_share": 35, "style": "话痨/段子手/潜水/技术宅/气氛担当",
    "icon": "🔥", "color": "#f85149", "desc": "一句话描述发言风格"}}
]
取最活跃的3-5人，tier: 核心/活跃/偶尔冒泡。msg_share是百分比推测值。icon选一个匹配的emoji。color用亮色系区分。"""
    ok, content, err = chat(system, convo, temperature=0.7, max_tokens=512, caller="群成员画像")
    if not ok: return None
    try:
        content = content.strip()
        if content.startswith("```"): content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        members = json.loads(content)
        if isinstance(members, list):
            return {"member_profile": members}
    except: pass
    return {"member_profile": content.strip()}


def group_vibe_check(messages, chat_name):
    """Group vibe: emotional tone, conflict signals, harmony level."""
    if not is_available():
        return None
    convo, total = _sample_convo(messages, 200)
    system = f"""你是社群氛围分析师。「{chat_name}」的聊天记录如下。
输出 JSON 对象（不要代码块）：
{{
  "mood": "轻松/吐槽/欢乐/严肃/火药味",
  "mood_emoji": "😄",
  "score": 8,
  "conflict": false,
  "conflict_detail": "",
  "description": "一句话氛围总结",
  "color": "#3fb950"
}}
score 1-10分。color: 绿色系=和谐，黄色系=一般，红色系=紧张。"""
    ok, content, err = chat(system, convo, temperature=0.7, max_tokens=512, caller="群氛围评估")
    if not ok: return None
    try:
        content = content.strip()
        if content.startswith("```"): content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        vibe = json.loads(content)
        if isinstance(vibe, dict):
            return {"vibe_check": vibe}
    except: pass
    return {"vibe_check": content.strip()}


def group_signal_radar(messages, chat_name):
    """Signal radar: important announcements, valuable links, key decisions, follow-ups."""
    if not is_available():
        return None
    convo, total = _sample_convo(messages, 300)
    system = f"""你是信息雷达。「{chat_name}」的聊天记录如下。
扫描对话，提取对群成员有价值的信息，用 JSON 数组输出（不要代码块）：

[
  {{"type": "通知", "icon": "📢", "priority": "高", "content": "活动/聚会/安排等具体信息摘要", "color": "#f85149"}},
  {{"type": "分享", "icon": "🔗", "priority": "中", "content": "有人推荐的链接/文章/视频摘要", "color": "#d2991d"}},
  {{"type": "求助", "icon": "❓", "priority": "高", "content": "有人提出的问题或求助，需要回应", "color": "#3fb950"}},
  {{"type": "亮点", "icon": "💡", "priority": "低", "content": "有趣的讨论、段子、金句、八卦", "color": "#a371f7"}},
  {{"type": "待办", "icon": "📌", "priority": "中", "content": "有人提起但未落实的事项", "color": "#58a6ff"}}
]

规则：
- 只提取真的有用的信息，不要硬凑
- content 要具体（包含谁、什么事），不能太笼统
- 最多6条，宁缺毋滥，没发现就返回 []
- 重点关注最近的消息（对话末尾）"""

    ok, content, err = chat(system, convo, temperature=0.5, max_tokens=768, caller="群信号雷达")
    if not ok: return None
    try:
        content = content.strip()
        if content.startswith("```"): content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        signals = json.loads(content)
        if isinstance(signals, list):
            return {"signal_radar": signals}
    except: pass
    return {"signal_radar": content.strip()}


def group_my_trace(messages, chat_name):
    """My trace: messages @me, conversations I participated in, highlights I missed."""
    if not is_available():
        return None
    convo, total = _sample_convo(messages, 200)
    system = f"""你是个人足迹追踪器。「{chat_name}」的聊天记录如下。「我」是用户的发言。
输出 JSON 数组（不要代码块），每条足迹一个对象：
[
  {{"type": "@我", "icon": "📌", "content": "谁@了我、说了什么", "time": "12:30", "color": "#f85149"}},
  {{"type": "参与", "icon": "💬", "content": "我参与了什么话题", "time": "11:00", "color": "#58a6ff"}},
  {{"type": "错过", "icon": "👀", "content": "我可能错过的重要讨论", "time": "09:00", "color": "#8b949e"}}
]
最多6条，没有则返回空数组 []。"""
    ok, content, err = chat(system, convo, temperature=0.7, max_tokens=512, caller="群内足迹")
    if not ok: return None
    try:
        content = content.strip()
        if content.startswith("```"): content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        traces = json.loads(content)
        if isinstance(traces, list):
            return {"my_trace": traces}
    except: pass
    return {"my_trace": content.strip()}


def group_role_map(messages, chat_name):
    """Role map: identify opinion leaders, vibe-setters, info sources, lurkers."""
    if not is_available():
        return None
    convo, total = _sample_convo(messages, 200)
    system = f"""你是社群角色分析师。「{chat_name}」的聊天记录如下。
输出 JSON 数组（不要代码块），每个角色一个对象：
[
  {{"role": "意见领袖", "icon": "👑", "members": ["成员名"], "color": "#d2991d", "desc": "一句话说明为什么"}},
  {{"role": "气氛组", "icon": "🎭", "members": ["成员名"], "color": "#db61a2", "desc": "一句话说明为什么"}},
  {{"role": "信息源", "icon": "📡", "members": ["成员名"], "color": "#58a6ff", "desc": "一句话说明为什么"}},
  {{"role": "潜水员", "icon": "🥷", "members": ["成员名"], "color": "#8b949e", "desc": "一句话说明为什么"}}
]
每类最多列2人，某类没有可省略。"""
    ok, content, err = chat(system, convo, temperature=0.7, max_tokens=512, caller="群角色地图")
    if not ok: return None
    try:
        content = content.strip()
        if content.startswith("```"): content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        roles = json.loads(content)
        if isinstance(roles, list):
            return {"role_map": roles}
    except: pass
    return {"role_map": content.strip()}


def group_batch_analysis(messages, chat_name):
    """Batch all 6 group dimensions into a single LLM call — saves tokens and latency."""
    if not is_available():
        return None
    convo, total = _sample_convo(messages, 300)
    system = f"""你是群聊分析师。「{chat_name}」的聊天记录如下。
一次输出6个维度的分析结果，用JSON格式（不要代码块）：

{{
  "topic_leaderboard": [
    {{"rank":1,"topic":"话题名","pct":35,"trend":"↑","color":"#58a6ff","keywords":["关键词"],"sample":"代表性发言"}}
  ],
  "member_profile": [
    {{"name":"成员名","tier":"核心","msg_share":35,"style":"话痨","icon":"🔥","color":"#f85149","desc":"发言风格描述"}}
  ],
  "vibe_check": {{"mood":"活跃","mood_emoji":"😄","score":8,"description":"氛围总结","conflict":false,"conflict_detail":"","color":"#3fb950"}},
  "signal_radar": [
    {{"type":"通知","icon":"📢","priority":"高","content":"信息摘要","color":"#f85149"}}
  ],
  "my_trace": [
    {{"type":"@我","icon":"📌","content":"足迹描述","time":"12:30","color":"#f85149"}}
  ],
  "role_map": [
    {{"role":"意见领袖","icon":"👑","members":["成员名"],"color":"#d2991d","desc":"说明"}}
  ]
}}

规则：话题取前5、成员取3-5人、信号≤6条、足迹≤6条、角色每类≤2人。宁缺毋滥，无数据返回空数组[]。"""
    ok, content, err = chat(system, convo, temperature=0.7, max_tokens=2048, caller="群批量分析")
    if not ok: return None
    try:
        content = content.strip()
        if content.startswith("```"): content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        return json.loads(content)
    except:
        return None
