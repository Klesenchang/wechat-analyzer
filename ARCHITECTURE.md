# 微信聊天分析器 — 系统架构文档

## 概述

微信聊天分析器（WeChat Chat Analyzer）是一个本地运行的 Web 应用，通过 `wx-cli` 命令行工具读取本地微信数据库，对私聊和群聊进行多维度统计分析，并可选接入 LLM（大语言模型）进行 AI 增强分析。

- **运行环境**: macOS，Python 3.11+，Flask
- **数据来源**: wx-cli（本地微信数据库读取工具）
- **默认端口**: 8899
- **路径**: `~/Desktop/chat-analyzer/`

---

## 一、项目结构

```
chat-analyzer/
├── server.py            # Flask 后端，路由 + API（551 行）
├── analyzer.py          # 核心分析引擎（2700 行）
├── llm.py               # LLM 调用模块（836 行）
├── config.json          # 服务器配置（密码、昵称、网络等）
├── usage_stats.json     # LLM 用量统计持久化
├── custom_tags.json     # 联系人自定义身份标签
├── password.txt         # 密码明文备份
├── templates/
│   └── index.html       # 单页前端（~3800 行，vanilla JS + CSS）
└── .hermes/plans/       # 开发计划文档
```

---

## 二、架构总览

```
┌──────────────────────────────────────────────────────┐
│                    浏览器 (Browser)                    │
│  ┌────────────────────────────────────────────────┐  │
│  │         index.html (SPA, ~3800 行)              │  │
│  │  • 液态玻璃 UI 主题                              │  │
│  │  • Chart.js 图表渲染                             │  │
│  │  • html2canvas 截图                             │  │
│  │  • pinyin-pro 拼音搜索                           │  │
│  │  • 启动动画 (Canvas 粒子系统)                     │  │
│  └──────────────────┬─────────────────────────────┘  │
│                     │ HTTP (localhost:8899)           │
└─────────────────────┼────────────────────────────────┘
                      │
┌─────────────────────┼────────────────────────────────┐
│              Flask (server.py, 551行)                 │
│  ┌──────────────────┴─────────────────────────────┐  │
│  │  路由层                                          │  │
│  │  /             → 主页 (index.html)               │  │
│  │  /login        → 密码登录页                      │  │
│  │  /api/contacts → 联系人搜索                      │  │
│  │  /api/analyze  → 聊天分析                        │  │
│  │  /api/summary  → 首页概览                        │  │
│  │  /api/tags     → 身份标签 CRUD                   │  │
│  │  /api/config   → LLM 配置                        │  │
│  │  /api/usage    → 用量统计                        │  │
│  │  /api/settings → 服务器设置                      │  │
│  │  /api/llm/*    → AI 分析 (11 个端点)             │  │
│  └──────────────────┬─────────────────────────────┘  │
└─────────────────────┼────────────────────────────────┘
                      │
        ┌─────────────┼─────────────┐
        ▼             ▼             ▼
┌──────────────┐ ┌──────────┐ ┌──────────┐
│  analyzer.py │ │  llm.py  │ │ wx-cli   │
│  分析引擎     │ │ LLM 调用 │ │ 微信数据  │
│  (2700行)    │ │ (836行)  │ │ (外部CLI)│
└──────────────┘ └──────────┘ └──────────┘
```

---

## 三、各模块详解

### 3.1 server.py — Flask 后端

**职责**: HTTP 路由、会话管理、密码认证、配置持久化

**核心路由（17 个 API + 2 个页面）:**

| 路由 | 方法 | 功能 |
|------|------|------|
| `/` | GET | 返回 index.html 主页（需登录） |
| `/login` | GET/POST | 密码登录页 |
| `/logout` | GET | 登出 |
| `/api/contacts` | GET | 搜索联系人/群聊 |
| `/api/analyze` | POST | 分析私聊或群聊 |
| `/api/summary` | GET | 首页概览（15 秒缓存） |
| `/api/tags` | GET/POST/DELETE | 联系人身份标签 |
| `/api/config` | GET/POST | LLM 配置 |
| `/api/usage` | GET/POST | LLM 用量统计 |
| `/api/settings` | GET/POST | 密码/昵称/网络设置 |
| `/api/llm/signal` | POST | 私聊信号分析 |
| `/api/llm/reply` | POST | 回复建议 |
| `/api/llm/insight` | POST | 关系洞察 |
| `/api/llm/topics` | POST | 话题分析 |
| `/api/llm/todos` | POST | 待办提取 |
| `/api/llm/emotion-track` | POST | 情绪追踪 |
| `/api/llm/group-topics` | POST | 群话题榜 |
| `/api/llm/group-members` | POST | 群成员画像 |
| `/api/llm/group-vibe` | POST | 群氛围评估 |
| `/api/llm/group-signals` | POST | 群信号雷达 |
| `/api/llm/group-trace` | POST | 我的群内足迹 |
| `/api/llm/group-roles` | POST | 群角色地图 |
| `/api/llm/group-all` | POST | 群批量分析（合并 6 维度） |

**关键设计:**
- `login_required` 装饰器：统一认证拦截，API 返回 401，页面重定向登录
- `config.json` 持久化：密码、昵称、局域网开关、密码开关
- 局域网 IP 检测：通过 `ifconfig` 自动获取
- 首页概览 15 秒缓存，减少重复 wx-cli 调用

---

### 3.2 analyzer.py — 核心分析引擎

**职责**: 通过 wx-cli 读取微信数据，执行统计分析

**对外 API（7 个公共函数）:**

| 函数 | 功能 |
|------|------|
| `list_contacts(query)` | 搜索联系人/群聊，返回匹配列表 |
| `analyze_private(name, since, until)` | 私聊全维度分析 |
| `analyze_group(name, since, until)` | 群聊全维度分析 |
| `analyze_summary(range_key, since, until)` | 首页概览（多联系人汇总） |
| `set_custom_tag(contact, tag)` | 设置身份标签 |
| `delete_custom_tag(contact)` | 删除身份标签 |
| `list_custom_tags()` | 列出所有标签 |

**数据流:**
```
wx-cli 命令 → subprocess → JSON 解析 → 统计分析 → dict 返回
```

**wx-cli 缓存机制:**
- 30 秒 TTL 的进程内缓存（`_wx_cache`）
- 缓存 key 为完整命令字符串
- 超过 200 条自动清空

**私聊分析维度（`analyze_private`）:**

| 维度 | 说明 |
|------|------|
| 基础统计 | 消息数、字数、发送者占比、时间分布 |
| 回复分析 | 回复速度、首发言人、对话轮次 |
| 关键词分析 | 情感/饮食/约会等预定义词库统计 |
| 关系阶段 | 4 阶段检测（`_detect_phases`） |
| 亲密度指数 | 0-100 综合评分（`_compute_intimacy`） |
| 情绪信号 | 好感/关心/冲突/未来/调侃 5 维度 |
| 互动动态 | 月度趋势、活跃时段、对话模式 |
| AI 建议 | 基于数据的可执行建议 |
| 身份检测 | 自动识别恋人/朋友/同事等关系 |
| 性别检测 | 基于内容 + 名字的性别推断 |

**群聊分析维度（`analyze_group`）:**

| 维度 | 说明 |
|------|------|
| 基础统计 | 消息数、成员数、活跃度 |
| 成员活跃榜 | Top 20 成员排名 + 占比 |
| 时间分布 | 按小时/星期热力图数据 |
| @提及分析 | 被 @ 最多的成员 |
| 媒体统计 | 图片/视频/语音/表情包计数 |

---

### 3.3 llm.py — LLM 调用模块

**职责**: 封装 OpenAI 兼容 API 调用，提供 AI 分析能力

**支持的 Provider:**
- DeepSeek（默认）
- OpenAI
- OpenRouter
- 自定义（任意 OpenAI 兼容 API）

**配置持久化**: `config.json`，通过 `get_config()` / `update_config()` 读写

**用量追踪:**
- 每次 LLM 调用自动记录到 `usage_stats.json`
- 按功能分类：总调用次数、输入/输出 Token、预估费用（DeepSeek 定价）
- 可通过 API 查询和清零

**私聊 AI 分析（6 个函数）:**

| 函数 | 说明 |
|------|------|
| `analyze_signal` | 关系信号解读（好感/冲突/需求等） |
| `suggest_reply` | 回复建议（支持风格和身份参数） |
| `analyze_chat_insight` | 综合关系洞察 |
| `analyze_topics_ai` | 话题聚类分析 |
| `extract_todos_ai` | 待办事项提取 |
| `track_emotion_ai` | 情绪时间线追踪 |

**群聊 AI 分析（7 个函数）:**

| 函数 | 说明 |
|------|------|
| `group_topic_leaderboard` | 话题排行榜 |
| `group_member_profile` | 成员画像 |
| `group_vibe_check` | 群氛围评估 |
| `group_signal_radar` | 群信号雷达图数据 |
| `group_my_trace` | 我的发言足迹 |
| `group_role_map` | 群角色地图（谁是什么角色） |
| `group_batch_analysis` | 批量分析（合并 6 维度为 1 次调用） |

**内部架构:**
```
_openai_chat(messages, ...) → HTTP Request → JSON Response
    ↓
_track_usage(caller, in_tokens, out_tokens) → 用量统计
    ↓
各分析函数 (组装 prompt → 调用 → 解析响应)
```

---

### 3.4 index.html — 前端 SPA

**技术栈:**
- 纯 HTML/CSS/JS，零框架依赖
- Chart.js（CDN）— 图表渲染
- html2canvas（CDN）— 截图功能
- pinyin-pro（CDN）— 拼音搜索

**UI 架构:**

```
┌──────────────────────────────────────┐
│  Header (标题 + 设置齿轮)             │
├──────────────────────────────────────┤
│  Panel (搜索框 + 日期 + 分析按钮)      │
├──────────────────────────────────────┤
│  Error Box / Spinner                 │
├──────────────────────────────────────┤
│  Results (分析报告动态渲染区)          │
├──────────────────────────────────────┤
│  Quick Nav (顶部 + 截图悬浮按钮)       │
└──────────────────────────────────────┘

Settings Modal (液态玻璃弹窗):
├── Settings Tabs (5 标签单行)
├── 👤 个人信息 (昵称设置)
├── 🤖 大模型 API (Provider/URL/Model/Key)
├── 🔐 安全设置 (密码开关/修改)
├── 📊 用量统计 (统计卡片 + 明细表格)
└── 🌐 网络设置 (局域网开关/IP 信息)
```

**设计系统:**
- 主题：液态毛玻璃（`backdrop-filter: blur` + 半透明背景 + 多层阴影）
- 色板：深色底 (#090b10) + 蓝紫渐变光晕 + 高饱和色彩点缀
- 启动动画: Canvas 粒子系统，三层彩色粒子扩散 + 玻璃碎片 + 鼠标交互

**JS 核心函数分类:**

| 类别 | 主要函数 |
|------|---------|
| 联系人 | `loadContacts()` `preloadContacts()` `filterContacts()` |
| 分析 | `startAnalyze()` `analyzePrivate()` `analyzeGroup()` |
| 渲染 | `renderPrivate()` `renderGroup()` `renderSummary()` `renderTopicList()` |
| 图表 | `drawActivityChart()` `drawMemberChart()` `drawEmotionChart()` |
| AI | `loadAIAnalysis()` `loadSignalAnalysis()` `loadReplySuggestion()` |
| 设置 | `toggleSettings()` `switchSettingsTab()` `saveConfig()` `saveProfile()` |
| 截图 | `captureScreenshot()` |
| 动画 | Canvas 粒子系统 IIFE（~300 行） |

---

## 四、数据流

### 4.1 分析请求完整流程

```
1. 用户在浏览器选择联系人 + 日期 → 点击"开始分析"
2. 前端 POST /api/analyze { contact, since, until, chat_type }
3. Flask 路由 → analyzer.analyze_private() 或 analyze_group()
4. analyzer 调用 wx-cli 获取聊天记录 JSON
   ├── wx history <contact> --since <date> --until <date> --json
   └── 缓存 30 秒，避免重复调用
5. 统计计算：消息数/字数/时间分布/关键词/回复速度等
6. 返回结构化 dict → Flask jsonify → 前端
7. 前端渲染：HTML 卡片 + Chart.js 图表
8. 用户可点击 AI 分析按钮，前端 POST /api/llm/* 
   → llm.py 调用 OpenAI API → 返回 AI 分析结果 → 前端渲染
```

### 4.2 联系人搜索流程

```
1. 前端搜索框输入 → GET /api/contacts?q=xxx
2. analyzer.list_contacts(query)
3. 并行调用:
   ├── wx sessions -n 9999 → 私聊列表 (~388 人)
   └── wx sessions -n 9999 → 群聊列表 + wx contacts 补充
4. 拼音匹配 (pinyin-pro) + 模糊匹配
5. 私聊/群聊分开标记 → 返回前端 → 下拉建议渲染
```

---

## 五、配置与状态

### 5.1 config.json

```json
{
  "password": "xxx",
  "password_enabled": true,
  "user_nickname": "Klesen",
  "lan_enabled": true,
  "provider": "deepseek",
  "base_url": "https://api.deepseek.com/v1",
  "model": "deepseek-chat",
  "api_key": "sk-xxx"
}
```

### 5.2 usage_stats.json

```json
{
  "total_calls": 244,
  "total_input_tokens": 407000,
  "total_output_tokens": 58000,
  "total_cost_est": 0.18,
  "by_function": { "analyze_topics_ai": {...}, ... }
}
```

### 5.3 custom_tags.json

```json
{ "张雅芸": "lover", "周锐": "colleague" }
```

---

## 六、外部依赖

| 依赖 | 用途 | 类型 |
|------|------|------|
| wx-cli | 读取本地微信数据库 | 外部 CLI 工具 |
| Flask | Web 框架 | Python pip |
| Chart.js 4.x | 图表渲染 | CDN |
| html2canvas | 页面截图 | CDN |
| pinyin-pro | 拼音搜索 | CDN |
| OpenAI 兼容 API | LLM 分析 | HTTP API |

---

## 七、启动方式

```bash
cd ~/Desktop/chat-analyzer
python server.py
# 输出:
# 🔐 微信聊天分析器已启动 (密码认证: 已启用, 局域网: 已启用)
#    本机访问: http://localhost:8899
#    局域网访问: http://192.168.x.x:8899
```

---

## 八、安全模型

- 密码认证：可选开关，默认启用
- 会话管理：Flask session（服务端签名 cookie）
- API 保护：`@login_required` 装饰器拦截所有 /api/* 路由
- 局域网访问：可配置仅限本机 (127.0.0.1) 或允许局域网 (0.0.0.0)
- API Key：存储在 config.json 本地文件，前端显示为遮蔽字符
