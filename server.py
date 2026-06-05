"""
Flask 后端 — 提供 API 和前端页面（含密码认证）
"""

from flask import Flask, request, jsonify, send_from_directory, session, redirect, url_for
from functools import wraps
import os, json, time

from analyzer import list_contacts, analyze_private, analyze_group, analyze_summary
from analyzer import set_custom_tag as _set_tag, delete_custom_tag as _del_tag, list_custom_tags as _list_tags
from analyzer import _load_custom_tag
from llm import get_config as get_llm_config, update_config as update_llm_config
from llm import is_available as llm_available, _load_config, analyze_signal, suggest_reply, analyze_chat_insight
from llm import analyze_topics_ai, extract_todos_ai, track_emotion_ai
from llm import group_topic_leaderboard, group_member_profile, group_vibe_check
from llm import group_signal_radar, group_my_trace, group_role_map


def _check_llm_result(result):
    """Check LLM result for errors and return proper (ok, data, error)."""
    if result is None:
        return jsonify({"ok": False, "error": "LLM 调用失败：模型无返回"}), 400
    if isinstance(result, dict) and "error" in result:
        return jsonify({"ok": False, "error": result["error"]}), 400
    return jsonify({"ok": True, "data": result})


from llm import group_batch_analysis
from llm import get_usage_stats, reset_usage_stats

app = Flask(__name__, static_folder=None)
app.secret_key = os.urandom(24).hex()

TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")

# Config path: CLI arg --config overrides default
import sys as _sys
_CONFIG_PATH_OVERRIDE = None
for arg in _sys.argv[1:]:
    if arg.startswith('--config='):
        _CONFIG_PATH_OVERRIDE = arg.split('=', 1)[1]
        break
    if arg == '--config':
        # next arg is the path (if not last)
        continue  # handled below
# Also check for --config PATH (space-separated)
for i, arg in enumerate(_sys.argv):
    if arg == '--config' and i + 1 < len(_sys.argv):
        _CONFIG_PATH_OVERRIDE = _sys.argv[i + 1]
        break
CONFIG_PATH = _CONFIG_PATH_OVERRIDE or os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

# ── Password config (persisted in config.json, fallback to hardcoded) ──
DEFAULT_PASSWORD = "admin"

def _detect_lan_ip():
    """Detect LAN IP address."""
    import subprocess, re
    try:
        result = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=5)
        ips = re.findall(r'inet (\d+\.\d+\.\d+\.\d+)', result.stdout)
        return next((ip for ip in ips if ip.startswith("192.168.") or ip.startswith("10.") or ip.startswith("172.")), "未知")
    except:
        return "未知"


def _load_server_config():
    """Load server settings from config.json."""
    try:
        with open(CONFIG_PATH, "r") as f:
            cfg = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        cfg = {}
    return {
        "password": cfg.get("password", DEFAULT_PASSWORD),
        "password_enabled": cfg.get("password_enabled", False),
        "user_nickname": cfg.get("user_nickname", ""),
        "lan_enabled": cfg.get("lan_enabled", False),
    }

def _save_server_config(password=None, password_enabled=None, user_nickname=None, lan_enabled=None):
    """Save server settings to config.json."""
    try:
        with open(CONFIG_PATH, "r") as f:
            cfg = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        cfg = {}
    if password is not None:
        cfg["password"] = password
    if password_enabled is not None:
        cfg["password_enabled"] = password_enabled
    if user_nickname is not None:
        cfg["user_nickname"] = user_nickname
    if lan_enabled is not None:
        cfg["lan_enabled"] = lan_enabled
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)

def _password_required():
    return _load_server_config()["password_enabled"]

def _get_password():
    return _load_server_config()["password"]

def get_user_nickname():
    """Get the configured user nickname."""
    return _load_server_config().get("user_nickname", "")

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not _password_required():
            return f(*args, **kwargs)
        if not session.get("logged_in"):
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "error": "未登录"}), 401
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return decorated


@app.route("/login", methods=["GET", "POST"])
def login_page():
    if not _password_required():
        return redirect(url_for("index"))
    error = ""
    if request.method == "POST":
        pw = request.form.get("password", "")
        if pw == _get_password():
            session["logged_in"] = True
            return redirect(url_for("index"))
        error = "密码错误"
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>登录 — 微信聊天分析器</title>
  <link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect x='2' y='3' width='24' height='20' rx='5' fill='%2358a6ff'/%3E%3Cpolygon points='6,25 3,30 10,25' fill='%2358a6ff'/%3E%3Crect x='9' y='13' width='2.5' height='6' rx='1' fill='%230d1117'/%3E%3Crect x='13.5' y='10' width='2.5' height='9' rx='1' fill='%230d1117'/%3E%3Crect x='18' y='8' width='2.5' height='11' rx='1' fill='%230d1117'/%3E%3C/svg%3E"/>
  <style>
    * {{ margin:0; padding:0; box-sizing:border-box; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, 'SF Pro', sans-serif;
      background: #090b10; color: #e6edf3; min-height: 100vh;
      display: flex; align-items: center; justify-content: center;
    }}
    #bg-fluid {{
      position: fixed; inset: 0; width: 100vw; height: 100vh;
      z-index: 0; display: block; pointer-events: none; opacity: 0.55;
    }}
    .login-box {{
      position: relative; z-index: 1;
      background: rgba(255,255,255,0.04);
      backdrop-filter: blur(20px); -webkit-backdrop-filter: blur(20px);
      border: 1px solid rgba(255,255,255,0.08); border-radius: 16px;
      padding: 48px 40px; width: 360px; text-align: center;
      box-shadow: 0 4px 32px rgba(0,0,0,0.3), 0 1px 0 rgba(255,255,255,0.04) inset;
    }}
    .login-box h1 {{
      font-size: 1.5rem; font-weight: 700; margin-bottom: 8px;
      color: #7eb8ff; letter-spacing: -0.01em;
    }}
    .login-box p {{ color: #8b929a; font-size: 0.85rem; margin-bottom: 28px; }}
    .login-box input {{
      width: 100%; padding: 12px 16px; border: 1px solid rgba(255,255,255,0.08);
      border-radius: 10px; background: rgba(255,255,255,0.04); color: #e8eaed;
      font-size: 0.95rem; outline: none; text-align: center;
      transition: border-color 0.2s, box-shadow 0.2s;
      font-family: inherit;
    }}
    .login-box input:focus {{
      border-color: rgba(126,184,255,0.5);
      box-shadow: 0 0 0 3px rgba(126,184,255,0.1);
    }}
    .login-box input:focus-visible {{ outline: 2px solid #7eb8ff; outline-offset: 2px; }}
    .login-box button {{
      width: 100%; margin-top: 16px; padding: 12px; border: none;
      border-radius: 10px; background: rgba(126,184,255,0.18); color: #7eb8ff;
      font-size: 0.95rem; font-weight: 600; cursor: pointer;
      transition: background 0.2s, box-shadow 0.2s;
      font-family: inherit; letter-spacing: 0.01em;
      border: 1px solid rgba(126,184,255,0.3);
    }}
    .login-box button:hover {{
      background: rgba(126,184,255,0.28);
      box-shadow: 0 0 20px rgba(126,184,255,0.12);
    }}
    .login-box button:focus-visible {{ outline: 2px solid #fff; outline-offset: 2px; }}
    .error {{ color: #f85149; font-size: 0.85rem; margin-top: 12px; }}
  </style>
</head>
<body>
<canvas id="bg-fluid"></canvas>
  <div class="login-box">
    <h1>
      <svg width="24" height="24" viewBox="0 0 32 32" style="vertical-align:middle;margin-right:6px;">
        <rect x="2" y="3" width="24" height="20" rx="5" fill="#58a6ff"/>
        <polygon points="6,25 3,30 10,25" fill="#58a6ff"/>
        <rect x="9" y="13" width="2.5" height="6" rx="1" fill="#0d1117"/>
        <rect x="13.5" y="10" width="2.5" height="9" rx="1" fill="#0d1117"/>
        <rect x="18" y="8" width="2.5" height="11" rx="1" fill="#0d1117"/>
      </svg>
      微信聊天分析器
    </h1>
    <p>请输入密码以继续</p>
    <form method="post">
      <input type="password" name="password" placeholder="输入密码" autofocus />
      <button type="submit">登 录</button>
    </form>
    {f'<div class="error">{error}</div>' if error else ''}
  </div>

  <script>
  (function(){{
    var VS='attribute vec2 position;void main(){{gl_Position=vec4(position,0.0,1.0);}}';
    var FS='precision highp float;\\n'+
      'uniform vec2 u_resolution;uniform float u_time;uniform vec2 u_mouse;\\n'+
      'vec3 palette(float t,vec3 a,vec3 b,vec3 c,vec3 d){{return a+b*cos(6.28318*(c*t+d));}}\\n'+
      'void main(){{\\n'+
      '  vec2 uv=gl_FragCoord.xy/u_resolution.xy;\\n'+
      '  vec2 p=uv*2.0-1.0;p.x*=u_resolution.x/u_resolution.y;\\n'+
      '  vec2 m=u_mouse*2.0-1.0;m.x*=u_resolution.x/u_resolution.y;\\n'+
      '  float md=length(p-m);\\n'+
      '  float ripple=sin(md*12.0-u_time*3.5)*exp(-md*3.5)*0.06;\\n'+
      '  p+=ripple;\\n'+
      '  vec2 p0=p;\\n'+
      '  for(float i=1.0;i<4.0;i++){{\\n'+
      '    p.x+=0.08/i*sin(i*2.8*p.y+u_time*0.35)+0.04;\\n'+
      '    p.y+=0.08/i*cos(i*2.2*p.x+u_time*0.28)-0.04;\\n'+
      '  }}\\n'+
      '  float r=length(p);float ang=atan(p.y,p.x);\\n'+
      '  vec3 a=vec3(0.04,0.05,0.08);\\n'+
      '  vec3 b=vec3(0.02,0.03,0.04);\\n'+
      '  vec3 c=vec3(1.0,1.0,1.0);\\n'+
      '  vec3 d=vec3(0.1,0.2,0.5);\\n'+
      '  vec3 col=palette(r*1.4+p0.x*0.4+u_time*0.08,a,b,c,d);\\n'+
      '  float disp=sin(r*22.0-u_time*1.3+ang*1.8)*0.5+0.5;\\n'+
      '  col+=vec3(disp*0.012,disp*0.008,disp*0.015);\\n'+
      '  float hi=pow(sin(p.x*3.5+p.y*2.8+u_time)*0.5+0.5,7.0);\\n'+
      '  col+=hi*0.06;\\n'+
      '  vec3 base=vec3(0.035,0.035,0.04);\\n'+
      '  col=mix(base,col,0.82);\\n'+
      '  gl_FragColor=vec4(col,1.0);\\n'+
      '}}';

    var mouse={{x:0.5,y:0.5}};
    window.addEventListener('mousemove',function(e){{mouse.x=e.clientX/innerWidth;mouse.y=e.clientY/innerHeight}});

    var canvas=document.getElementById('bg-fluid');
    var gl=canvas.getContext('webgl',{{alpha:false,antialias:true}});
    if(!gl){{canvas.style.display='none';return;}}

    function mkShader(type,src){{var s=gl.createShader(type);gl.shaderSource(s,src);gl.compileShader(s);return s;}}
    var prog=gl.createProgram();
    gl.attachShader(prog,mkShader(gl.VERTEX_SHADER,VS));
    gl.attachShader(prog,mkShader(gl.FRAGMENT_SHADER,FS));
    gl.linkProgram(prog);gl.useProgram(prog);
    var buf=gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER,buf);
    gl.bufferData(gl.ARRAY_BUFFER,new Float32Array([-1,-1,1,-1,-1,1,-1,1,1,-1,1,1]),gl.STATIC_DRAW);
    var pos=gl.getAttribLocation(prog,'position');
    gl.enableVertexAttribArray(pos);gl.vertexAttribPointer(pos,2,gl.FLOAT,false,0,0);
    var lRes=gl.getUniformLocation(prog,'u_resolution');
    var lT=gl.getUniformLocation(prog,'u_time');
    var lM=gl.getUniformLocation(prog,'u_mouse');

    function resize(){{
      var d=Math.min(window.devicePixelRatio||1,2);
      canvas.width=innerWidth*d;canvas.height=innerHeight*d;
      gl.viewport(0,0,canvas.width,canvas.height);
    }}
    window.addEventListener('resize',resize);resize();

    var t0=Date.now();
    (function loop(){{
      var t=(Date.now()-t0)/1000;
      gl.uniform2f(lRes,canvas.width,canvas.height);
      gl.uniform1f(lT,t);
      gl.uniform2f(lM,mouse.x,1-mouse.y);
      gl.drawArrays(gl.TRIANGLES,0,6);
      requestAnimationFrame(loop);
    }})();
  }})();
  </script>
</body>
</html>"""


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login_page"))


@app.route("/")
@login_required
def index():
    response = send_from_directory(TEMPLATE_DIR, "index.html")
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.route("/vendor/<path:filename>")
@login_required
def vendor_files(filename):
    return send_from_directory(os.path.join(TEMPLATE_DIR, "vendor"), filename)


@app.route("/api/contacts")
@login_required
def api_contacts():
    query = request.args.get("q", "")
    try:
        contacts = list_contacts(query)
        return jsonify({"ok": True, "contacts": contacts})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/analyze", methods=["POST"])
@login_required
def api_analyze():
    data = request.get_json() or {}
    contact = data.get("contact", "").strip()
    username = data.get("username", "").strip()
    since = data.get("since", "").strip()
    until = data.get("until", "").strip()
    chat_type = data.get("chat_type", "").strip()

    if not contact:
        return jsonify({"ok": False, "error": "请选择联系人或群聊"}), 400
    if not since or not until:
        return jsonify({"ok": False, "error": "请选择时间范围"}), 400

    try:
        if chat_type == "group":
            result = analyze_group(contact, since, until, username=username)
        else:
            result = analyze_private(contact, since, until)

        if "error" in result:
            return jsonify({"ok": False, "error": result["error"]}), 404

        return _check_llm_result(result)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── Summary cache ──
_summary_cache = {"key": "", "data": None, "ts": 0}
_SUMMARY_CACHE_TTL = 15  # seconds


@app.route("/api/summary")
@login_required
def api_summary():
    range_key = request.args.get("range", "7d")
    since = request.args.get("since", "")
    until = request.args.get("until", "")
    cache_key = f"{range_key}|{since}|{until}"
    now = time.time()

    # Return cached result if fresh
    if _summary_cache["key"] == cache_key and _summary_cache["data"]:
        if now - _summary_cache["ts"] < _SUMMARY_CACHE_TTL:
            return jsonify({"ok": True, "data": _summary_cache["data"], "cached": True})

    try:
        result = analyze_summary(range_key, since, until)
        _summary_cache["key"] = cache_key
        _summary_cache["data"] = result
        _summary_cache["ts"] = now
        return _check_llm_result(result)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/tags", methods=["GET", "POST", "DELETE"])
@login_required
def api_tags():
    if request.method == "GET":
        return jsonify({"ok": True, "tags": _list_tags()})
    elif request.method == "POST":
        data = request.get_json() or {}
        contact = data.get("contact", "").strip()
        tag = data.get("tag", "").strip()
        identity_key = data.get("identity_key", "").strip()
        if not contact:
            return jsonify({"ok": False, "error": "缺少联系人"}), 400
        _set_tag(contact, tag, identity_key)
        return jsonify({"ok": True})
    elif request.method == "DELETE":
        data = request.get_json() or {}
        contact = data.get("contact", "").strip()
        if not contact:
            return jsonify({"ok": False, "error": "缺少联系人"}), 400
        _del_tag(contact)
        return jsonify({"ok": True})


@app.route("/api/custom-identities", methods=["GET", "POST", "DELETE"])
@login_required
def api_custom_identities():
    """Manage user-created custom identities that persist across contacts."""
    from analyzer import list_custom_identities, set_custom_identity, delete_custom_identity
    if request.method == "GET":
        return jsonify({"ok": True, "identities": list_custom_identities()})
    elif request.method == "POST":
        data = request.get_json() or {}
        key = data.get("key", "").strip()
        label = data.get("label", "").strip()
        icon = data.get("icon", "🏷️")
        base = data.get("base", "friend")
        if not key or not label:
            return jsonify({"ok": False, "error": "缺少身份标识"}), 400
        set_custom_identity(key, label, icon, base)
        return jsonify({"ok": True})
    elif request.method == "DELETE":
        data = request.get_json() or {}
        key = data.get("key", "").strip()
        if not key:
            return jsonify({"ok": False, "error": "缺少身份标识"}), 400
        delete_custom_identity(key)
        return jsonify({"ok": True})


# ── LLM Config ──


@app.route("/api/config", methods=["GET", "POST"])
@login_required
def api_config():
    if request.method == "GET":
        return jsonify({"ok": True, "config": get_llm_config()})
    else:
        data = request.get_json() or {}
        raw_cfg = update_llm_config(**data)
        # Return raw config (not masked) so frontend doesn't lose the api_key on round-trip
        return jsonify({"ok": True, "config": _load_config().get("llm", {})})


# ── Usage Stats ──


@app.route("/api/usage", methods=["GET", "POST"])
@login_required
def api_usage():
    if request.method == "POST":
        data = request.get_json() or {}
        if data.get("action") == "reset":
            return jsonify({"ok": True, "data": reset_usage_stats()})
        return jsonify({"ok": False, "error": "未知操作"}), 400
    return jsonify({"ok": True, "data": get_usage_stats()})


# ── Server Settings (password) ──


@app.route("/api/settings", methods=["GET", "POST"])
@login_required
def api_settings():
    if request.method == "GET":
        cfg = _load_server_config()
        return jsonify({"ok": True, "password_enabled": cfg["password_enabled"],
                        "password": "***", "user_nickname": cfg["user_nickname"],
                        "lan_enabled": cfg.get("lan_enabled", True),
                        "lan_ip": _detect_lan_ip()})
    else:
        data = request.get_json() or {}
        new_pw = data.get("password", "").strip()
        enabled = data.get("password_enabled")
        nickname = data.get("user_nickname")
        lan = data.get("lan_enabled")
        if new_pw:
            if len(new_pw) < 3:
                return jsonify({"ok": False, "error": "密码至少3位"}), 400
            _save_server_config(password=new_pw)
        if enabled is not None:
            _save_server_config(password_enabled=bool(enabled))
        if nickname is not None:
            _save_server_config(user_nickname=nickname.strip())
        if lan is not None:
            _save_server_config(lan_enabled=bool(lan))
        cfg = _load_server_config()
        return jsonify({"ok": True, "password_enabled": cfg["password_enabled"],
                        "password": "***", "user_nickname": cfg["user_nickname"],
                        "lan_enabled": cfg.get("lan_enabled", True),
                        "lan_ip": _detect_lan_ip()})


# ── LLM Analysis ──


@app.route("/api/llm/signal", methods=["POST"])
@login_required
def api_llm_signal():
    if not llm_available():
        return jsonify({"ok": False, "error": "LLM 未配置或未启用，请先在设置中配置"}), 400
    data = request.get_json() or {}
    messages = data.get("messages", [])
    contact_name = data.get("chat_name", "联系人")
    user_name = data.get("user_name", "")
    if not messages:
        return jsonify({"ok": False, "error": "请提供聊天消息"}), 400
    result = analyze_signal(messages, contact_name, user_name)
    return _check_llm_result(result)


@app.route("/api/llm/reply", methods=["POST"])
@login_required
def api_llm_reply():
    if not llm_available():
        return jsonify({"ok": False, "error": "LLM 未配置或未启用，请先在设置中配置"}), 400
    data = request.get_json() or {}
    messages = data.get("messages", [])
    style = data.get("style", "自然")
    identity = data.get("identity", "lover")
    contact_name = data.get("chat_name", "联系人")
    user_name = data.get("user_name", "")
    if not messages:
        return jsonify({"ok": False, "error": "请提供聊天消息"}), 400
    result = suggest_reply(messages, style, identity, contact_name, user_name)
    return _check_llm_result(result)


@app.route("/api/llm/insight", methods=["POST"])
@login_required
def api_llm_insight():
    if not llm_available():
        return jsonify({"ok": False, "error": "LLM 未配置或未启用，请先在设置中配置"}), 400
    data = request.get_json() or {}
    messages = data.get("messages", [])
    chat_name = data.get("chat_name", "未知")
    identity = data.get("identity", "lover")
    user_name = data.get("user_name", "")
    if not messages:
        return jsonify({"ok": False, "error": "请提供聊天消息"}), 400
    result = analyze_chat_insight(messages, chat_name, identity, user_name)
    return _check_llm_result(result)


# ── Private Chat Extended Dimensions ──

@app.route("/api/llm/topics", methods=["POST"])
@login_required
def api_llm_topics():
    if not llm_available():
        return jsonify({"ok": False, "error": "LLM 未配置"}), 400
    data = request.get_json() or {}
    messages = data.get("messages", [])
    chat_name = data.get("chat_name", "未知")
    if not messages:
        return jsonify({"ok": False, "error": "请提供聊天消息"}), 400
    result = analyze_topics_ai(messages, chat_name, data.get("user_name", ""))
    return _check_llm_result(result)


@app.route("/api/llm/todos", methods=["POST"])
@login_required
def api_llm_todos():
    if not llm_available():
        return jsonify({"ok": False, "error": "LLM 未配置"}), 400
    data = request.get_json() or {}
    messages = data.get("messages", [])
    chat_name = data.get("chat_name", "未知")
    if not messages:
        return jsonify({"ok": False, "error": "请提供聊天消息"}), 400
    result = extract_todos_ai(messages, chat_name, data.get("user_name", ""))
    return _check_llm_result(result)


@app.route("/api/llm/emotion-track", methods=["POST"])
@login_required
def api_llm_emotion_track():
    if not llm_available():
        return jsonify({"ok": False, "error": "LLM 未配置"}), 400
    data = request.get_json() or {}
    messages = data.get("messages", [])
    chat_name = data.get("chat_name", "未知")
    if not messages:
        return jsonify({"ok": False, "error": "请提供聊天消息"}), 400
    result = track_emotion_ai(messages, chat_name, data.get("user_name", ""))
    return _check_llm_result(result)


# ── Group Chat Dimensions ──

@app.route("/api/llm/group-topics", methods=["POST"])
@login_required
def api_llm_group_topics():
    if not llm_available():
        return jsonify({"ok": False, "error": "LLM 未配置"}), 400
    data = request.get_json() or {}
    messages = data.get("messages", [])
    chat_name = data.get("chat_name", "未知")
    if not messages:
        return jsonify({"ok": False, "error": "请提供聊天消息"}), 400
    result = group_topic_leaderboard(messages, chat_name)
    return _check_llm_result(result)


@app.route("/api/llm/group-members", methods=["POST"])
@login_required
def api_llm_group_members():
    if not llm_available():
        return jsonify({"ok": False, "error": "LLM 未配置"}), 400
    data = request.get_json() or {}
    messages = data.get("messages", [])
    chat_name = data.get("chat_name", "未知")
    if not messages:
        return jsonify({"ok": False, "error": "请提供聊天消息"}), 400
    result = group_member_profile(messages, chat_name)
    return _check_llm_result(result)


@app.route("/api/llm/group-vibe", methods=["POST"])
@login_required
def api_llm_group_vibe():
    if not llm_available():
        return jsonify({"ok": False, "error": "LLM 未配置"}), 400
    data = request.get_json() or {}
    messages = data.get("messages", [])
    chat_name = data.get("chat_name", "未知")
    if not messages:
        return jsonify({"ok": False, "error": "请提供聊天消息"}), 400
    result = group_vibe_check(messages, chat_name)
    return _check_llm_result(result)


@app.route("/api/llm/group-signals", methods=["POST"])
@login_required
def api_llm_group_signals():
    if not llm_available():
        return jsonify({"ok": False, "error": "LLM 未配置"}), 400
    data = request.get_json() or {}
    messages = data.get("messages", [])
    chat_name = data.get("chat_name", "未知")
    if not messages:
        return jsonify({"ok": False, "error": "请提供聊天消息"}), 400
    result = group_signal_radar(messages, chat_name)
    return _check_llm_result(result)


@app.route("/api/llm/group-trace", methods=["POST"])
@login_required
def api_llm_group_trace():
    if not llm_available():
        return jsonify({"ok": False, "error": "LLM 未配置"}), 400
    data = request.get_json() or {}
    messages = data.get("messages", [])
    chat_name = data.get("chat_name", "未知")
    if not messages:
        return jsonify({"ok": False, "error": "请提供聊天消息"}), 400
    result = group_my_trace(messages, chat_name)
    return _check_llm_result(result)


@app.route("/api/llm/group-roles", methods=["POST"])
@login_required
def api_llm_group_roles():
    if not llm_available():
        return jsonify({"ok": False, "error": "LLM 未配置"}), 400
    data = request.get_json() or {}
    messages = data.get("messages", [])
    chat_name = data.get("chat_name", "未知")
    if not messages:
        return jsonify({"ok": False, "error": "请提供聊天消息"}), 400
    result = group_role_map(messages, chat_name)
    return _check_llm_result(result)


@app.route("/api/llm/group-all", methods=["POST"])
@login_required
def api_llm_group_all():
    """Batch all 6 group dimensions into a single LLM call."""
    if not llm_available():
        return jsonify({"ok": False, "error": "LLM 未配置"}), 400
    data = request.get_json() or {}
    messages = data.get("messages", [])
    chat_name = data.get("chat_name", "未知")
    if not messages:
        return jsonify({"ok": False, "error": "请提供聊天消息"}), 400
    result = group_batch_analysis(messages, chat_name)
    if result is None:
        return jsonify({"ok": False, "error": "LLM 分析失败"}), 500
    return _check_llm_result(result)


if __name__ == "__main__":
    lan_ip = _detect_lan_ip()
    pw_status = "已启用" if _password_required() else "已关闭"
    lan_cfg = _load_server_config().get("lan_enabled", True)
    host = "0.0.0.0" if lan_cfg else "127.0.0.1"
    lan_status = "已启用" if lan_cfg else "已关闭"
    print(f"🔐 微信聊天分析器已启动 (密码认证: {pw_status}, 局域网: {lan_status})")
    print(f"   本机访问: http://localhost:8899")
    if lan_cfg:
        print(f"   局域网访问: http://{lan_ip}:8899")
    app.run(host=host, port=8899, debug=True)
