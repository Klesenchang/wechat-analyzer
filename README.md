# wechat-analyzer · [微信聊天分析器 →](https://klesenchang.github.io/wechat-analyzer/) 

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-macOS-lightgrey.svg)](#前置条件)
[![Python](https://img.shields.io/badge/python-3.8%2B-blue)](https://www.python.org)
[![Flask](https://img.shields.io/badge/Flask-3.x-black)](https://flask.palletsprojects.com)
[![GitHub Pages](https://img.shields.io/badge/GitHub%20Pages-%E9%A1%B9%E7%9B%AE%E4%BB%8B%E7%BB%8D-blue?logo=github)](https://klesenchang.github.io/wechat-analyzer/)

> 基于 wx-cli 的本地微信聊天分析工具。所有数据在本地计算，**无需上传**。

---

## 功能

- **私聊分析** — 信号解读、回复建议、关系洞察、话题挖掘、待办提取、情绪追踪（6 维 AI）
- **群聊透视** — 话题排行榜、成员画像、氛围评估、信号雷达、个人足迹、角色地图（6 维结构化渲染）
- **身份识别** — 自动识别恋人/朋友/同事/家人/商务/服务 6 种关系身份，切换对应分析维度
- **关系指标体系** — 亲密度指数、响应速度、深夜活跃度、消息比等
- **AI 洞察** — AI-generated 优先 + 规则兜底，支持 DeepSeek / OpenAI 兼容接口

---

## 前置条件

### 1. 系统与微信

- macOS（依赖 WeChat.app 本地数据库）
- 微信已安装并登录

### 2. 安装 wx-cli

```bash
# npm 安装（推荐）
npm install -g @jackwener/wx-cli

# 或 curl 一键安装
curl -fsSL https://raw.githubusercontent.com/jackwener/wx-cli/main/install.sh | bash

# 验证
wx --version
```

### 3. macOS 初始化（只需一次）

```bash
# 签名微信（WeChat 更新后需重做）
codesign --force --deep --sign - /Applications/WeChat.app

# 清理旧 TCC 授权
for s in ScreenCapture Camera Microphone AppleEvents \
         SystemPolicyDocumentsFolder SystemPolicyDownloadsFolder SystemPolicyDesktopFolder; do
  tccutil reset "$s" com.tencent.xinWeChat
done

# 重启微信，等待完全登录
killall WeChat && open /Applications/WeChat.app

# 初始化密钥
sudo wx init

# 验证
wx sessions
```

### 4. Python 环境

```bash
pip install flask requests
```

### 5. 配置 config.json

```json
{
  "db_dir": "/Users/你的用户名/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/你的用户名_哈希/db_storage",
  "llm": {
    "api_key": "your-api-key",
    "base_url": "https://api.deepseek.com/v1",
    "enabled": true,
    "model": "deepseek-chat",
    "provider": "deepseek"
  },
  "user_nickname": "你的微信昵称"
}
```

---

## 启动

```bash
cd wechat-analyzer
python3 server.py
# → http://localhost:8899 (默认无密码，可在设置中开启)
```

---

## 技术栈

| 层次 | 技术 | 说明 |
|------|------|------|
| 数据引擎 | wx-cli (Rust) | 本地微信数据库读取，daemon 持久缓存 |
| 后端 | Python Flask | API 路由、LLM 集成、身份识别算法 |
| AI 引擎 | DeepSeek / OpenAI | 兼容接口，12+ 分析维度，用量追踪 |
| 前端 | HTML + CSS + JS | 暗色主题，WebGL 背景，Chart.js 图表 |
| 搜索 | pinyin-pro | 汉字 / 全拼 / 首字母模糊搜索 |

---

## 身份识别系统

| 身份 | 关键信号 | 分析维度 |
|------|----------|----------|
| 💕 恋人 | 亲昵词密度 >5%、深夜 >25%、日均 >20 | 情感深度、权力动态、未来信号 |
| 👨‍👩‍👧 家人 | 家庭词 >10、关怀 > 亲昵 ×2、通话 >5% | 责任分担、代际动态、生活参与 |
| 🏢 同事 | 工作词 >5、工作日 >70%、办公时段 >50% | 信息同步、边界感、依赖模式 |
| 💼 商务 | 商业词 >5、链接 >20%、正式语气 | 利益对齐、专业匹配、风险评估 |
| 📞 服务 | 服务词 >3、对方发起 >60% | 问题解决、主动服务、客户粘性 |
| 🤝 朋友 | 兜底识别、轻松短句 | 兴趣共鸣、互惠平衡、圈子融合 |

---

## 隐私说明

- **所有计算在本地完成**，数据不出设备
- API Key 等配置保存在 `config.json`，与技能包分离
- 不收集任何用户数据

---

## 许可证

MIT License
