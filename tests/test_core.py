import pytest
import json
import sys
import asyncio
from pathlib import Path

# 让测试能 import app
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as app_module


# ============================================================
# 辅助：构造 provider
# ============================================================
def provider(name="P", models=None, disabled=None):
    return {
        "name": name,
        "base_url": "http://x/v1",
        "api_key": "k",
        "models": models or [],
        "disabled_models": disabled or [],
    }


# ============================================================
# get_enabled_models
# ============================================================
def test_enabled_models_filters_disabled():
    p = provider(models=["a", "b", "c"], disabled=["b"])
    assert app_module.get_enabled_models(p) == ["a", "c"]


def test_enabled_models_no_disabled():
    p = provider(models=["a", "b"])
    assert app_module.get_enabled_models(p) == ["a", "b"]


def test_enabled_models_empty():
    p = provider(models=[])
    assert app_module.get_enabled_models(p) == []


# ============================================================
# is_chat_model / is_free_model
# ============================================================
def test_chat_model_true():
    assert app_module.is_chat_model("deepseek-ai/deepseek-v4-flash") is True


def test_chat_model_false_for_embedding():
    assert app_module.is_chat_model("text-embedding-3") is False


def test_free_model_by_pricing_zero():
    info = {"pricing": {"prompt": "0", "completion": "0"}}
    assert app_module.is_free_model(info) is True


def test_free_model_by_pricing_zero_decimal():
    info = {"pricing": {"prompt": "0.00", "completion": "0E-10"}}
    assert app_module.is_free_model(info) is True


def test_not_free_model():
    info = {"pricing": {"prompt": "0.001", "completion": "0.002"}}
    assert app_module.is_free_model(info) is False


def test_free_model_no_pricing():
    assert app_module.is_free_model({}) is False


# ============================================================
# mask_key
# ============================================================
def test_mask_key_long():
    # 前6字符 "nvapi-" + "****" + 后4字符 "lmno"
    assert app_module.mask_key("nvapi-abcdefghijklmno") == "nvapi-****lmno"


def test_mask_key_short():
    assert app_module.mask_key("short") == "****"


def test_mask_key_empty():
    assert app_module.mask_key("") == ""


# ============================================================
# merge_reasoning (透传，不合并)
# ============================================================
def test_merge_reasoning_preserves_both():
    obj = {"choices": [{"delta": {"reasoning_content": "think", "content": "hi"}}]}
    out = app_module.merge_reasoning(obj)
    assert out["choices"][0]["delta"]["content"] == "hi"
    assert out["choices"][0]["delta"]["reasoning_content"] == "think"


def test_merge_reasoning_only_reasoning():
    obj = {"choices": [{"delta": {"reasoning_content": "think"}}]}
    out = app_module.merge_reasoning(obj)
    assert out["choices"][0]["delta"]["reasoning_content"] == "think"
    assert "content" not in out["choices"][0]["delta"]


def test_merge_reasoning_no_reasoning():
    obj = {"choices": [{"delta": {"content": "hi"}}]}
    out = app_module.merge_reasoning(obj)
    assert out["choices"][0]["delta"]["content"] == "hi"


def test_merge_reasoning_message_key():
    obj = {"choices": [{"message": {"reasoning_content": "think", "content": "hi"}}]}
    out = app_module.merge_reasoning(obj)
    assert out["choices"][0]["message"]["content"] == "hi"
    assert out["choices"][0]["message"]["reasoning_content"] == "think"


def test_merge_reasoning_no_choices():
    obj = {"id": "x"}
    assert app_module.merge_reasoning(obj) == {"id": "x"}



# ============================================================
# compress / restore hermes
# ============================================================
def test_compress_then_restore_roundtrip():
    body = {"messages": [{"role": "user", "content": "mcp_hermes_studio_use_hermes_studio_use_tool"}]}
    compressed = app_module.compress_hermes(body)
    assert "mcp_hsu_" in compressed["messages"][0]["content"]
    s = app_module.restore_hermes_text("__mcp_hsu_tool__")
    assert "mcp_hermes_studio_use_hermes_studio_use_" in s


# ============================================================
# pick_available_models：disabled 过滤
# ============================================================
def test_pick_skips_disabled(monkeypatch):
    monkeypatch.setattr(app_module, "providers", [
        provider(name="NVIDIA", models=["a", "b"], disabled=["b"])
    ])
    monkeypatch.setattr(app_module, "health_status", {
        "NVIDIA||a": {"status": "ok"},
        "NVIDIA||b": {"status": "ok"},
    })
    cands = app_module.pick_available_models()
    models = [m for _, m in cands]
    assert "a" in models
    assert "b" not in models


def test_pick_respects_health(monkeypatch):
    monkeypatch.setattr(app_module, "providers", [
        provider(name="NVIDIA", models=["a", "b"])
    ])
    monkeypatch.setattr(app_module, "health_status", {
        "NVIDIA||a": {"status": "ok"},
        "NVIDIA||b": {"status": "fail"},
    })
    cands = app_module.pick_available_models()
    models = [m for _, m in cands]
    assert "a" in models
    assert "b" not in models


def test_pick_explicit_model_without_prefix_bypasses_health(monkeypatch):
    # 显式指定裸模型名，跳过健康检查，直接透传给上游
    monkeypatch.setattr(app_module, "providers", [
        provider(name="NVIDIA", models=["a", "b"])
    ])
    monkeypatch.setattr(app_module, "health_status", {
        "NVIDIA||a": {"status": "ok"},
        "NVIDIA||b": {"status": "fail"},
    })
    cands = app_module.pick_available_models("b")
    models = [m for _, m in cands]
    assert "b" in models  # 显式指定时不过滤，直接透传


def test_pick_force_bypasses_health(monkeypatch):
    monkeypatch.setattr(app_module, "providers", [
        provider(name="NVIDIA", models=["a", "b"])
    ])
    monkeypatch.setattr(app_module, "health_status", {
        "NVIDIA||a": {"status": "ok"},
        "NVIDIA||b": {"status": "fail"},
    })
    cands = app_module.pick_available_models("b", force=True)
    models = [m for _, m in cands]
    assert "b" in models


def test_pick_returns_empty_when_no_providers(monkeypatch):
    monkeypatch.setattr(app_module, "providers", [])
    monkeypatch.setattr(app_module, "health_status", {})
    assert app_module.pick_available_models() == []


# ============================================================
# 熔断
# ============================================================
def test_circuit_opens_after_threshold(monkeypatch):
    monkeypatch.setattr(app_module, "circuit_breaker", {})
    k = "X||y"
    for _ in range(app_module.CIRCUIT_FAIL_THRESHOLD):
        app_module.record_fail(k)
    assert app_module.is_circuit_open(k) is True


def test_circuit_resets_on_success(monkeypatch):
    monkeypatch.setattr(app_module, "circuit_breaker", {})
    k = "X||y"
    app_module.record_fail(k)
    app_module.record_success(k)
    assert app_module.is_circuit_open(k) is False


# ============================================================
# 质量分
# ============================================================
def test_quality_optimistic_when_no_data(monkeypatch):
    monkeypatch.setattr(app_module, "model_quality", {})
    assert app_module.get_quality_score("missing") == 1.0


def test_quality_tracks_ok_fail(monkeypatch):
    monkeypatch.setattr(app_module, "model_quality", {})
    k = "X||y"
    app_module.update_model_quality(k, {"status": "ok", "latency_ms": 100})
    app_module.update_model_quality(k, {"status": "ok", "latency_ms": 200})
    app_module.update_model_quality(k, {"status": "fail"})
    assert app_module.get_quality_score(k) == pytest.approx(2 / 3)


# ============================================================
# is_1m_model
# ============================================================
def test_is_1m_model_true(monkeypatch):
    monkeypatch.setattr(app_module, "model_details", {})
    # context_limits 里有 1M 的
    assert app_module.is_1m_model("deepseek-ai/deepseek-v4-flash") is True


def test_is_1m_model_false(monkeypatch):
    monkeypatch.setattr(app_module, "model_details", {})
    assert app_module.is_1m_model("sensenova-u1-fast") is False


# ============================================================
# /api/page-diag 前端诊断回传（去重 + 端点契约）
# ============================================================
def test_page_diag_dedup_window():
    t0 = 1000.0
    assert app_module.page_diag_dedup("diag-payload-A", t0) is True
    assert app_module.page_diag_dedup("diag-payload-B", t0 + 1) is True   # 不同内容互不影响
    assert app_module.page_diag_dedup("diag-payload-A", t0 + 30) is False  # 60s 内重复抑制
    assert app_module.page_diag_dedup("diag-payload-A", t0 + 61) is True   # 过窗恢复


def test_page_diag_endpoint_ok():
    from fastapi.testclient import TestClient
    client = TestClient(app_module.app)  # 不进 with：跳过 lifespan/后台任务
    r = client.post("/api/page-diag", json={"kind": "unit-test", "msg": "hello"})
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_page_diag_endpoint_oversize_rejected():
    from fastapi.testclient import TestClient
    client = TestClient(app_module.app)
    r = client.post("/api/page-diag", content=b"x" * 20000)  # httpx 自动带 content-length
    assert r.status_code == 200
    assert r.json()["ok"] is False  # 超 8KB 不读 body、不记日志


# ============================================================
# AUTO_KILL 身份校验（只杀网关，不误杀同端口陌生进程）
# ============================================================
def test_looks_like_gateway():
    assert app_module._looks_like_gateway("/home/u/.venv/bin/python app.py") is True
    assert app_module._looks_like_gateway("/opt/dist/v1.6.1-网关客户端") is True
    assert app_module._looks_like_gateway("/opt/dist/v1.6.1-模型蓄水池") is True
    assert app_module._looks_like_gateway("C:\\app\\Model-Gateway.exe") is True
    assert app_module._looks_like_gateway("C:\\app\\ModelReservoir.exe") is True
    assert app_module._looks_like_gateway("/usr/sbin/nginx: master process") is False
    assert app_module._looks_like_gateway("") is False


def test_pid_is_gateway_excludes_self_and_invalid():
    import os as _os
    assert app_module._pid_is_gateway(_os.getpid()) is False  # 不能杀自己
    assert app_module._pid_is_gateway(0) is False
    assert app_module._pid_is_gateway(-1) is False


def test_pid_is_gateway_identifies_via_info(monkeypatch):
    monkeypatch.setattr(app_module, "_pid_info",
                        lambda pid: "python app.py" if pid == 1234 else "/usr/sbin/cron -f")
    assert app_module._pid_is_gateway(1234) is True
    assert app_module._pid_is_gateway(4321) is False


def _fake_ss(*_a, **_k):
    class R:
        stdout = 'LISTEN 0 4096 127.0.0.1:8000 0.0.0.0:* users:(("x",pid=7777,fd=5))'
    return R()


def test_kill_old_instance_spares_strangers(monkeypatch):
    import os as _os
    monkeypatch.setattr(app_module.sys, "platform", "linux")  # 钉死 ss 分支，三平台同一逻辑
    killed = []
    monkeypatch.setattr(app_module.subprocess, "run", _fake_ss)
    monkeypatch.setattr(app_module, "_pid_info", lambda pid: "nginx worker process")
    monkeypatch.setattr(_os, "kill", lambda pid, sig: killed.append((pid, sig)))
    assert app_module.kill_old_instance(8000) is False
    assert killed == []            # 陌生进程分毫不动


def test_kill_old_instance_kills_gateway(monkeypatch):
    import os as _os
    monkeypatch.setattr(app_module.sys, "platform", "linux")  # 钉死 ss 分支，三平台同一逻辑
    killed = []
    monkeypatch.setattr(app_module.subprocess, "run", _fake_ss)
    monkeypatch.setattr(app_module, "_pid_info", lambda pid: "/u/.venv/bin/python app.py")
    monkeypatch.setattr(_os, "kill", lambda pid, sig: killed.append((pid, sig)))
    assert app_module.kill_old_instance(8000) is True
    assert killed == [(7777, 15)]  # 只杀网关本体，SIGTERM


def _fake_cmds(cmd, **kw):
    """按命令返回三平台各自的探测输出。"""
    class R:
        stdout = ""
    out = R()
    if cmd[0] == "lsof":          # macOS: 一 pid 一行
        out.stdout = "7777\n"
    elif cmd[0] == "ss":          # Linux: ss -ltnpH
        out.stdout = 'LISTEN 0 4096 127.0.0.1:8000 0.0.0.0:* users:(("x",pid=7777,fd=5))'
    elif cmd[0] == "netstat":     # Windows: netstat -ano
        out.stdout = "  TCP    0.0.0.0:8000            0.0.0.0:0              LISTENING       7777"
    return out


def test_kill_old_instance_darwin_branch(monkeypatch):
    import os as _os
    monkeypatch.setattr(app_module.sys, "platform", "darwin")
    killed = []
    monkeypatch.setattr(app_module.subprocess, "run", _fake_cmds)
    monkeypatch.setattr(app_module, "_pid_info", lambda pid: "python app.py")
    monkeypatch.setattr(_os, "kill", lambda pid, sig: killed.append((pid, sig)))
    assert app_module.kill_old_instance(8000) is True
    assert killed == [(7777, 15)]


def test_kill_old_instance_win32_branch(monkeypatch):
    monkeypatch.setattr(app_module.sys, "platform", "win32")
    taskkilled = []

    def fake_run(cmd, **kw):
        if cmd[0] == "taskkill":
            taskkilled.append(cmd)
        return _fake_cmds(cmd, **kw)

    monkeypatch.setattr(app_module.subprocess, "run", fake_run)
    monkeypatch.setattr(app_module, "_pid_info", lambda pid: "模型蓄水池.exe")
    assert app_module.kill_old_instance(8000) is True
    assert taskkilled and taskkilled[0][:2] == ["taskkill", "/PID"]


def test_single_instance_lock_path(tmp_path, monkeypatch):
    """锁路径跨启动方式唯一：Linux 用 XDG_RUNTIME_DIR（缺省回退 /tmp/runtime-uid）。"""
    import sys as _sys
    monkeypatch.setattr(_sys, "platform", "linux")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    p = app_module.single_instance_lock_path()
    assert p == tmp_path / "model-reservoir.lock"
    assert p.parent.is_dir()
    # Windows 无 os.getuid → mock 掉，三平台均可跑且 uid 确定
    monkeypatch.setattr(app_module.os, "getuid", lambda: 1000, raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    p2 = app_module.single_instance_lock_path()
    # Windows 的 Path 用反斜杠 stringify → 归一化后再断言
    s = str(p2).replace("\\", "/")
    assert s.startswith("/tmp/runtime-1000")
    assert s.endswith("model-reservoir.lock")


def test_image_to_argb():
    """SNI IconPixmap 载荷：ARGB 大端字节序。"""
    from PIL import Image
    img = Image.new("RGBA", (2, 1), (0, 0, 0, 0))
    img.putpixel((0, 0), (255, 0, 0, 255))  # 纯红不透明
    w, h, data = app_module.image_to_argb(img)
    assert (w, h) == (2, 1)
    assert data[0:4] == bytes((255, 255, 0, 0))   # A=255 R=255 G=0 B=0
    assert data[4:8] == bytes((0, 0, 0, 0))        # 透明像素全 0


def test_activate_endpoint():
    """/api/activate：第二实例唤起已有实例窗口（无窗口环境返回 ok=False，端点恒 200）。"""
    from fastapi.testclient import TestClient
    client = TestClient(app_module.app)
    r = client.post("/api/activate")
    assert r.status_code == 200
    assert "ok" in r.json()


def test_usage_all_days(monkeypatch, tmp_path):
    """/api/usage?days=0 = "全部"：不截断、不被钳到 1..30；days>30 仍钳到 30。"""
    import time as _time
    f = tmp_path / "usage.jsonl"
    old = _time.time() - 90 * 86400   # 90 天前的记录（旧逻辑30天会被清理/查询截掉）
    recent = _time.time() - 3600
    f.write_text(
        '{"ts": %d, "pt": 10, "ct": 20, "model": "m", "provider": "p"}\n' % old +
        '{"ts": %d, "pt": 1, "ct": 2, "model": "m", "provider": "p"}\n' % recent,
        encoding="utf-8")
    monkeypatch.setattr(app_module, "USAGE_FILE", f)

    # 底层: days<=0 不截断
    assert len(app_module._read_usage_sync(0)) == 2
    assert len(app_module._read_usage_sync(1)) == 1
    # cleanup: 90 天记录在 10 年保留期内 → 不删
    assert app_module._cleanup_usage_sync() == 0
    assert f.exists() and len(f.read_text(encoding="utf-8").splitlines()) == 2

    # 端点: days=0 原样返回全量
    from fastapi.testclient import TestClient
    client = TestClient(app_module.app)
    headers = {"Authorization": "Bearer " + app_module.LOCAL_API_KEY}
    r = client.get("/api/usage?days=0", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["days"] == 0
    assert body["total"]["requests"] == 2
    assert body["total"]["pt"] == 11
    # days>MAX 钳到 30（"近30天"语义不变）
    r2 = client.get("/api/usage?days=999", headers=headers)
    assert r2.json()["days"] == 30

def test_ensure_gtk_im_module(monkeypatch):
    """缺 GTK_IM_MODULE 时按已装模块补齐（中文输入根因修复）；已设则不覆盖。"""
    # 该函数 Linux-only（sys.platform 守卫）；CI 有 mac/win runner，mock 平台
    monkeypatch.setattr(app_module.sys, "platform", "linux")
    # 未设置 + fcitx 模块存在 -> 补 fcitx
    env = {}
    assert app_module.ensure_gtk_im_module(env, exists=lambda p: p.endswith("im-fcitx5.so")) is True
    assert env["GTK_IM_MODULE"] == "fcitx"
    # 只有 ibus -> 补 ibus
    env = {}
    assert app_module.ensure_gtk_im_module(env, exists=lambda p: p.endswith("im-ibus.so")) is True
    assert env["GTK_IM_MODULE"] == "ibus"
    # 什么模块都没有 -> 不瞎设
    env = {}
    assert app_module.ensure_gtk_im_module(env, exists=lambda p: False) is False
    assert "GTK_IM_MODULE" not in env
    # 已设 -> 不覆盖
    env = {"GTK_IM_MODULE": "ibus"}
    assert app_module.ensure_gtk_im_module(env, exists=lambda p: p.endswith(".so")) is False
    assert env["GTK_IM_MODULE"] == "ibus"


def test_ensure_gtk_im_module_file(monkeypatch):
    """打包版根因：PyInstaller 把 GTK_PATH 等指向包内且包里无 immodules.cache，
    必须补 GTK_IM_MODULE_FILE 指向系统缓存，否则 GTK 加载不到任何 IM 模块。"""
    monkeypatch.setattr(app_module.sys, "platform", "linux")
    cache = "/usr/lib/gtk-3.0/3.0.0/immodules.cache"
    # 缓存存在 -> 补 GTK_IM_MODULE_FILE（即使 GTK_IM_MODULE 已设，两变量独立补齐）
    env = {"GTK_IM_MODULE": "fcitx"}
    assert app_module.ensure_gtk_im_module(env, exists=lambda p: p == cache) is False
    assert env["GTK_IM_MODULE_FILE"] == cache
    # 两变量都缺 -> 一次补齐
    env = {}
    def exists(p):
        return p.endswith("im-fcitx5.so") or p == cache
    assert app_module.ensure_gtk_im_module(env, exists=exists) is True
    assert env["GTK_IM_MODULE"] == "fcitx"
    assert env["GTK_IM_MODULE_FILE"] == cache
    # 模块在但缓存文件不在（无 GTK 缓存的机器）-> 补 MODULE 不补 FILE
    env = {}
    assert app_module.ensure_gtk_im_module(env, exists=lambda p: p.endswith(".so")) is True
    assert env["GTK_IM_MODULE"] == "fcitx"
    assert "GTK_IM_MODULE_FILE" not in env
    # 彻底什么都没有 -> 不瞎设
    env = {}
    assert app_module.ensure_gtk_im_module(env, exists=lambda p: False) is False
    assert "GTK_IM_MODULE" not in env and "GTK_IM_MODULE_FILE" not in env
    # 已设不覆盖
    env = {"GTK_IM_MODULE_FILE": "/custom/immodules.cache"}
    app_module.ensure_gtk_im_module(env, exists=lambda p: True)
    assert env["GTK_IM_MODULE_FILE"] == "/custom/immodules.cache"


def test_call_log_no_100_cap():
    """调用记录不再 100 条截断（1 万条兜底），且每条带 ts 供相对时间显示。"""
    assert app_module.CALL_LOG_MAX >= 10000
    assert app_module.call_log.maxlen == app_module.CALL_LOG_MAX
    # ts 由 append 点写入：每个 time 行的下一行必须紧跟 ts 行（防回归）
    import inspect
    lines = inspect.getsource(app_module).splitlines()
    time_idx = [i for i, l in enumerate(lines) if '"time": time.strftime("%H:%M:%S"),' in l]
    assert len(time_idx) >= 9
    for i in time_idx:
        assert i + 1 < len(lines) and '"ts": time.time()' in lines[i + 1], f"call_log append 缺 ts: line {i}"


def test_infer_modality_rules():
    """模态能力自动辨别：家族知识库 + 显式覆盖 + 兜底。"""
    im = app_module.infer_modality
    # 默认纯文本
    assert im("meta/llama2-70b") == "text"
    # 向量模型防带偏（不吃 gemini/embedding 命名的生成/全模态规则）
    assert im("google/gemini-embedding-001") == "text"
    assert im("text-embedding-3-small") == "text"
    # 图像生成
    assert im("dall-e-3") == "image_gen"
    assert im("black-forest-labs/FLUX.1-schnell") == "image_gen"
    assert im("stabilityai/stable-diffusion-xl-base-1.0") == "image_gen"
    # 视频生成
    assert im("sora-2") == "video_gen"
    assert im("kling-v1.6") == "video_gen"
    # 音频生成
    assert im("gpt-4o-mini-tts") == "audio_gen"
    assert im("CosyVoice2-0.5B") == "audio_gen"
    # 音频理解
    assert im("whisper-large-v3") == "audio_in"
    assert im("SenseVoiceSmall") == "audio_in"
    # 图像理解
    assert im("Qwen/Qwen3-VL-8B-Instruct") == "vision"
    assert im("meta/llama-3.2-11b-vision-instruct") == "vision"
    assert im("microsoft/phi-3-vision-128k-instruct") == "vision"
    # 全模态
    assert im("nvidia/nemotron-3-nano-omni-30b-a3b-reasoning") == "omni"
    assert im("gpt-4o") == "omni"
    assert im("google/gemini-2.5-flash") == "omni"
    # 生成规则先于全模态（tts 不是 omni）
    assert im("gpt-4o-mini-tts") != "omni"
    # gpt-4o-mini 只是图像理解，不是全模态
    assert im("gpt-4o-mini") == "vision"
    # 家族知识库：纠正 v1 的误判（均为用户实盘在用的模型）
    assert im("deepseek-ai/deepseek-v4-flash") == "vision"   # V4 Flash：原生多模态视觉
    assert im("deepseek-ai/deepseek-v4-pro") == "vision"     # V4 Pro 正式版：支持图像推理
    assert im("google/gemma-3-12b-it") == "vision"
    assert im("google/gemma-4-31b-it") == "omni"
    assert im("Qwen/Qwen3.5-122B-A10B") == "vision"
    assert im("moonshotai/kimi-k3") == "omni"
    assert im("moonshotai/kimi-k2.6") == "vision"
    assert im("mimo-v2.5") == "omni"
    assert im("mimo-v2.5-pro") == "omni"                      # 官方模型卡：V2.5 全系输入含图像/视频/音频
    assert im("MiniMax/MiniMax-M3") == "vision"
    assert im("z-ai/glm-5.2") == "text"
    assert im("mistralai/mistral-large-3-675b-instruct-2512") == "vision"
    assert im("sensenova-u1-fast") == "image_gen"
    # 显式覆盖（models_meta.modalities）优先于一切规则：字符串 & 对象两种写法
    monkeypatch_key = "my-legacy-model"
    app_module.MODEL_MODALITIES[monkeypatch_key] = "video_gen"
    try:
        assert im(monkeypatch_key) == "video_gen"
        # 对象写法可精确到单个维度（模态 + 深度思考）
        app_module.MODEL_MODALITIES[monkeypatch_key] = {"modality": "omni", "reasoning": True}
        assert im(monkeypatch_key) == "omni"
        assert app_module.infer_reasoning(monkeypatch_key) is True
        # 非法覆盖值被忽略，回退自动判定
        app_module.MODEL_MODALITIES[monkeypatch_key] = "banana"
        assert im(monkeypatch_key) == "text"
    finally:
        del app_module.MODEL_MODALITIES[monkeypatch_key]
    # 合法类别集合
    for m in ("text", "vision", "audio_in", "image_gen", "audio_gen", "video_gen", "omni"):
        assert m in app_module.MODALITY_CATEGORIES


def test_v1_models_modality_field(monkeypatch):
    """/v1/models 每项带 modality 字段；路由组=router。"""
    # 测试环境 DATA_DIR 无 providers.json → providers 为空，注入假数据
    monkeypatch.setattr(app_module, "providers", [
        {"name": "P1", "models": ["deepseek-v4-flash", "Qwen/Qwen3-VL-8B-Instruct", "sora-2", "whisper-large-v3"],
         "base_url": "http://x", "api_key": "k", "disabled_models": []},
    ], raising=False)
    monkeypatch.setattr(app_module, "ROUTERS", {"myroute": ["deepseek-v4-flash"]}, raising=False)
    # 清缓存强制重建
    app_module._models_cache["data"] = None
    from fastapi.testclient import TestClient
    client = TestClient(app_module.app)
    headers = {"Authorization": "Bearer " + app_module.LOCAL_API_KEY}
    resp = client.get("/v1/models", headers=headers)
    assert resp.status_code == 200
    data = resp.json()["data"]
    by_id = {d["id"]: d for d in data}
    assert by_id["P1/deepseek-v4-flash"]["modality"] == "vision"
    assert by_id["P1/Qwen/Qwen3-VL-8B-Instruct"]["modality"] == "vision"
    assert by_id["P1/sora-2"]["modality"] == "video_gen"
    assert by_id["P1/whisper-large-v3"]["modality"] == "audio_in"
    assert by_id["myroute"]["modality"] == "router"
    # 深度思考字段：每项都带且为布尔
    assert by_id["P1/deepseek-v4-flash"]["reasoning"] is True
    assert by_id["myroute"]["reasoning"] is False
    # 每项都必须有 modality（合法类别）与 reasoning（布尔）
    for d in data:
        assert d.get("modality") in app_module.MODALITY_CATEGORIES, d
        assert isinstance(d.get("reasoning"), bool), d
    # 复位缓存，避免污染其他用例
    app_module._models_cache["data"] = None


def test_infer_reasoning_rules():
    """深度思考能力辨别：思考型家族为真，普通模型为假。"""
    ir = app_module.infer_reasoning
    # 思考型
    assert ir("deepseek-v4-flash") is True
    assert ir("deepseek-ai/deepseek-v4-pro") is True
    assert ir("Qwen/Qwen3.5-397B-A17B") is True
    assert ir("moonshotai/kimi-k3") is True
    assert ir("z-ai/glm-5.3-flash") is True
    assert ir("mimo-v2.6-pro") is True
    assert ir("nvidia/nemotron-3-super-120b-a12b") is True
    assert ir("openai/gpt-oss-20b") is True
    assert ir("deepseek-r1") is True
    assert ir("some/thinking-model") is True
    # 非思考型
    assert ir("google/gemma-3-12b-it") is False
    assert ir("meta/llama-3.2-11b-vision-instruct") is False
    assert ir("Qwen/Qwen3-VL-8B-Instruct") is False
    assert ir("mistralai/mistral-large-3-675b-instruct-2512") is False
    assert ir("meta/llama2-70b") is False


def test_infer_capabilities_shape():
    """infer_capabilities 返回 {modality, reasoning} 且模态值合法。"""
    caps = app_module.infer_capabilities("Qwen/Qwen3.5-27B")
    assert caps == {"modality": "vision", "reasoning": True}
    assert set(app_module.infer_capabilities("sensenova-u1-fast")) == {"modality", "reasoning"}


def test_model_capability_endpoint(monkeypatch, tmp_path):
    """/api/model-capability 落盘人工覆盖，清空后回落自动判定。"""
    from fastapi.testclient import TestClient
    monkeypatch.setattr(app_module, "META_FILE", tmp_path / "models_meta.json")
    saved = dict(app_module.MODEL_MODALITIES)
    try:
        client = TestClient(app_module.app)
        headers = {"Authorization": "Bearer " + app_module.LOCAL_API_KEY}
        # 非法类别被拒
        r = client.post("/api/model-capability", headers=headers,
                        json={"model": "foo-bar", "modality": "banana"})
        assert r.status_code == 200 and r.json()["ok"] is False
        # 写入覆盖（模态 + 深度思考）
        r = client.post("/api/model-capability", headers=headers,
                        json={"model": "foo-bar", "modality": "omni", "reasoning": True})
        assert r.json()["ok"] is True
        assert r.json()["data"] == {"modality": "omni", "reasoning": True}
        assert app_module.infer_modality("foo-bar") == "omni"
        assert app_module.infer_reasoning("foo-bar") is True
        # 落盘可读
        on_disk = json.loads((tmp_path / "models_meta.json").read_text(encoding="utf-8"))
        assert on_disk["modalities"]["foo-bar"]["modality"] == "omni"
        # 清空覆盖 → 回落自动判定
        r = client.post("/api/model-capability", headers=headers, json={"model": "foo-bar"})
        assert r.json()["ok"] is True
        assert app_module.infer_modality("foo-bar") == "text"
        assert "foo-bar" not in app_module.MODEL_MODALITIES
    finally:
        app_module.MODEL_MODALITIES.clear()
        app_module.MODEL_MODALITIES.update(saved)


def test_autostart_desktop_content_source(monkeypatch):
    """源码运行：Exec 应为 python + app.py。"""
    monkeypatch.delattr(app_module.sys, "frozen", raising=False)
    txt = app_module._autostart_desktop_content()
    assert txt.startswith("[Desktop Entry]\n")
    assert "app.py" in txt
    assert "NoDisplay=true" in txt


def test_autostart_desktop_content_frozen(monkeypatch):
    """打包运行：Exec 只能是二进制本体，不能带 onefile 临时目录里的 app.py 参数。"""
    monkeypatch.setattr(app_module.sys, "frozen", True, raising=False)
    monkeypatch.setattr(app_module.sys, "executable", "/opt/reservoir/v1.6.2-模型蓄水池")
    txt = app_module._autostart_desktop_content()
    assert 'Exec=env GATEWAY_AUTO_KILL=1 "/opt/reservoir/v1.6.2-模型蓄水池"' in txt
    assert "_MEI" not in txt
    assert ".py" not in txt


# ============================================================
# 公告 / 在线更新：远端地址、平台分派、兜底文案
# ============================================================
def test_remote_sources_point_to_own_repo():
    """公告/版本/预设的默认远端都指向本仓库，不再指向上游作者的 gitee
    （否则 fork 出去的软件，公告与更新地址仍被别人左右）"""
    for url in (app_module.DEFAULT_ANNOUNCEMENT_URL,
                app_module.VERSION_CHECK_URL,
                app_module.PRESET_REMOTE_URL):
        assert url.startswith("https://raw.githubusercontent.com/chenshifanjian/model-reservoir/")
        assert "gitee.com" not in url


def test_fallback_announcement_is_markdown_with_version():
    text = app_module.FALLBACK_ANNOUNCEMENT
    assert text.strip()
    assert text.lstrip().startswith("#")
    assert app_module.APP_VERSION in text


def test_download_url_for_platform(monkeypatch):
    data = {
        "download_url": "https://ex/all",
        "download_url_linux": "https://ex/linux",
        "download_url_windows": "https://ex/win",
        "download_url_macos": "https://ex/mac",
    }
    monkeypatch.setattr(app_module.sys, "platform", "linux")
    assert app_module.platform_key() == "linux"
    assert app_module.download_url_for_platform(data) == "https://ex/linux"

    monkeypatch.setattr(app_module.sys, "platform", "win32")
    assert app_module.platform_key() == "windows"
    assert app_module.download_url_for_platform(data) == "https://ex/win"

    monkeypatch.setattr(app_module.sys, "platform", "darwin")
    assert app_module.platform_key() == "macos"
    assert app_module.download_url_for_platform(data) == "https://ex/mac"

    # 只给通用地址时，三端都拿通用地址；都没有则空串
    assert app_module.download_url_for_platform({"download_url": "https://ex/all"}) == "https://ex/all"
    assert app_module.download_url_for_platform({}) == ""


def test_hot_update_only_on_windows_frozen(monkeypatch):
    monkeypatch.setattr(app_module.sys, "platform", "linux")
    monkeypatch.setattr(app_module.sys, "frozen", False, raising=False)
    assert app_module.hot_update_supported() is False

    monkeypatch.setattr(app_module.sys, "platform", "win32")
    assert app_module.hot_update_supported() is False  # 源码模式不给热更
    monkeypatch.setattr(app_module.sys, "frozen", True, raising=False)
    assert app_module.hot_update_supported() is True


def test_version_json_matches_app_version_and_has_assets():
    """仓库根 version.json 是客户端更新源：版本号必须跟 APP_VERSION 同步，
    三端下载地址必须齐备（缺一个就会让那个平台的用户看到空按钮）"""
    p = Path(__file__).resolve().parent.parent / "version.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["version"] == app_module.APP_VERSION
    for plat in ("windows", "linux", "macos"):
        url = data["download_url_" + plat]
        assert url.startswith(
            "https://github.com/chenshifanjian/model-reservoir/releases/download/v" + data["version"] + "/")
    assert data["release_notes"].strip()
    assert data["release_page"].startswith("https://github.com/chenshifanjian/model-reservoir/releases")


# ============================================================
# 对外模型名写法：统一为「供应商/模型名」（斜杠分隔）
# ============================================================
def test_local_model_id_uses_slash():
    assert app_module.local_model_id("SenseNova", "kimi-k3") == "SenseNova/kimi-k3"
    assert app_module.local_model_id("mimo_token_plan", "mimo-v2.6-flash") == "mimo_token_plan/mimo-v2.6-flash"
    # 模型名自带斜杠：只加一层供应商前缀，模型名原样保留
    assert app_module.local_model_id("魔搭", "deepseek-ai/DeepSeek-V4-Pro") == "魔搭/deepseek-ai/DeepSeek-V4-Pro"
    # 供应商名自带连字符也不能被拆开
    assert app_module.local_model_id("示例甲-通用", "gpt-x-4o") == "示例甲-通用/gpt-x-4o"


def test_model_id_matches_accepts_three_forms():
    p, m = "SenseNova", "kimi-k3"
    assert app_module.model_id_matches("kimi-k3", p, m)              # 裸模型名
    assert app_module.model_id_matches("SenseNova/kimi-k3", p, m)    # 标准写法
    assert app_module.model_id_matches("SenseNova-kimi-k3", p, m)    # 历史写法（兼容旧客户端）
    assert not app_module.model_id_matches("Other/kimi-k3", p, m)
    assert not app_module.model_id_matches("kimi-k4", p, m)
    assert not app_module.model_id_matches("", p, m)
    assert not app_module.model_id_matches(None, p, m)


def test_pick_accepts_slash_prefixed_model(monkeypatch):
    monkeypatch.setattr(app_module, "providers", [provider(name="SenseNova", models=["kimi-k3"])])
    monkeypatch.setattr(app_module, "health_status", {})
    cands = app_module.pick_available_models("SenseNova/kimi-k3")
    assert [(p["name"], m) for p, m in cands] == [("SenseNova", "kimi-k3")]


def test_pick_accepts_legacy_hyphen_prefixed_model(monkeypatch):
    monkeypatch.setattr(app_module, "providers", [provider(name="SenseNova", models=["kimi-k3"])])
    monkeypatch.setattr(app_module, "health_status", {})
    cands = app_module.pick_available_models("SenseNova-kimi-k3")
    assert [(p["name"], m) for p, m in cands] == [("SenseNova", "kimi-k3")]


def test_pick_slash_prefix_scopes_to_named_provider(monkeypatch):
    """同名模型挂在两个供应商下时，带供应商前缀的写法必须只命中那一个"""
    monkeypatch.setattr(app_module, "providers", [
        provider(name="甲", models=["kimi-k3"]),
        provider(name="乙", models=["kimi-k3"]),
    ])
    monkeypatch.setattr(app_module, "health_status", {})
    cands = app_module.pick_available_models("甲/kimi-k3")
    assert [(p["name"], m) for p, m in cands] == [("甲", "kimi-k3")]


def test_models_endpoint_ids_use_slash_and_roundtrip(monkeypatch):
    """界面「本地输出模型管理」的列表直接取 /v1/models 的 id：
    每个 id 必须是「供应商/模型名」，且原样贴回去必须能命中同一供应商。"""
    from fastapi.testclient import TestClient

    fake = [
        provider(name="SenseNova", models=["kimi-k3"]),
        provider(name="魔搭", models=["deepseek-ai/DeepSeek-V4-Pro"]),
    ]
    monkeypatch.setattr(app_module, "providers", fake)
    monkeypatch.setattr(app_module, "ROUTERS", {})
    monkeypatch.setattr(app_module, "health_status", {})
    app_module._models_cache["data"] = None

    client = TestClient(app_module.app)  # 不进 with：跳过 lifespan/后台任务
    res = client.get("/v1/models", headers={"Authorization": f"Bearer {app_module.LOCAL_API_KEY}"})
    assert res.status_code == 200
    ids = [m["id"] for m in res.json()["data"]]
    assert ids == ["SenseNova/kimi-k3", "魔搭/deepseek-ai/DeepSeek-V4-Pro"]
    # 生成规则 == 唯一拼法，且每个 id 回填后仍命中它自己的供应商
    for item in res.json()["data"]:
        assert item["id"] == app_module.local_model_id(item["owned_by"], item["id"].split("/", 1)[1])
        assert app_module.model_id_matches(item["id"], item["owned_by"], item["id"].split("/", 1)[1])
    app_module._models_cache["data"] = None


# ============================================================
# 监控页「模型可用性」：声明过的模型一个都不能少（v1.7.2 修复）
# 旧实现把「探过但一次都没成功」的模型整行删掉 → 112 个模型只列 55 个，
# 且「服务异常（<50%）」筛选永远筛不到 0% 的那批。
# ============================================================
def test_classify_probe_failure_maps_codes():
    f = app_module.classify_probe_failure
    assert f(429, "fail") == "触发限流"
    assert f(404, "fail") == "上游无此模型"
    assert f(410, "fail") == "模型已下线"
    assert f(403, "fail") == "无权限 / 不在套餐"
    assert f(400, "fail") == "请求被拒（不支持该调用）"
    assert f(503, "fail") == "上游故障"
    assert f(418, "fail") == "HTTP 418"
    assert f(None, "error", "httpx.ReadTimeout") == "探测超时"
    assert f(None, "error", "boom") == "探测异常"
    assert f(None, "error") == "探测异常"


def test_stability_rows_keep_never_succeeded_models():
    allowed = ["A||ok模型", "A||死模型", "B||没探过"]
    stats = {
        "A||ok模型": {"ok": 2, "fail": 0, "error": 0, "total": 2, "latencies": [100, 140]},
        "A||死模型": {"ok": 0, "fail": 2, "error": 0, "total": 2, "latencies": []},
    }
    health = {
        "A||ok模型": {"status": "ok", "code": 200},
        "A||死模型": {"status": "fail", "code": 410, "detail": "gone"},
        "B||没探过": {"status": "ok", "code": 200},
    }
    rows = app_module.build_stability_rows(stats, allowed, health)
    # 条数 == 声明数：不隐藏任何模型
    assert len(rows) == len(allowed) == 3
    # 排序：可用率高的在前 → 0% 沉底 → 未巡检的垫底
    assert [(r["provider"], r["model"]) for r in rows] == [("A", "ok模型"), ("A", "死模型"), ("B", "没探过")]
    assert rows[0]["availability"] == 100.0 and rows[0]["last_reason"] is None
    dead = rows[1]
    assert dead["availability"] == 0.0
    assert dead["last_code"] == 410 and dead["last_reason"] == "模型已下线"
    assert dead["fail"] == 2 and dead["ok"] == 0
    unprobed = rows[2]
    assert unprobed["checks"] == 0 and unprobed["availability"] is None  # 页面显示 "--" 而不是 0.0%


async def _fake_read_history(hours=24):
    return [{"time": 1.0, "data": {
        "A||m1": {"status": "ok", "latency_ms": 100},
        "A||m2": {"status": "fail", "latency_ms": 30, "code": 410, "detail": "gone"},
    }}]


def test_stability_endpoint_lists_every_declared_model(monkeypatch):
    """接口层：条数 == 提供商声明去重后的模型数（含从没成功过的）"""
    from fastapi.testclient import TestClient

    fake = [provider(name="A", models=["m1", "m2"]), provider(name="B", models=["m3"])]
    monkeypatch.setattr(app_module, "providers", fake)
    # 内存里还没有巡检结果（应用刚启动）→ 原因必须能从历史记录里兜底
    monkeypatch.setattr(app_module, "health_status", {})
    monkeypatch.setattr(app_module, "read_history", _fake_read_history)
    app_module._stability_cache.clear()

    client = TestClient(app_module.app)  # 不进 with：跳过 lifespan/后台任务
    res = client.get("/api/stability?hours=24", headers={"Authorization": f"Bearer {app_module.LOCAL_API_KEY}"})
    assert res.status_code == 200
    data = res.json()
    assert len(data) == 3
    m2 = [r for r in data if r["model"] == "m2"][0]
    assert m2["checks"] == 1 and m2["ok"] == 0 and m2["availability"] == 0.0
    assert m2["last_code"] == 410 and m2["last_reason"] == "模型已下线"
    m3 = [r for r in data if r["model"] == "m3"][0]
    assert m3["checks"] == 0 and m3["availability"] is None and m3["last_reason"] is None
    app_module._stability_cache.clear()


def test_stability_endpoint_prefers_live_health_over_history(monkeypatch):
    """内存里的实时巡检结果比历史记录新，优先采用"""
    from fastapi.testclient import TestClient

    monkeypatch.setattr(app_module, "providers", [provider(name="A", models=["m2"])])
    monkeypatch.setattr(app_module, "health_status", {"A||m2": {"status": "fail", "code": 429, "detail": "rate"}})
    monkeypatch.setattr(app_module, "read_history", _fake_read_history)
    app_module._stability_cache.clear()

    client = TestClient(app_module.app)
    res = client.get("/api/stability?hours=24", headers={"Authorization": f"Bearer {app_module.LOCAL_API_KEY}"})
    assert res.status_code == 200
    m2 = res.json()[0]
    assert m2["last_code"] == 429 and m2["last_reason"] == "触发限流"
    app_module._stability_cache.clear()


# ============================================================
# 免费/付费判定：证据分级（2026-10-15）
# ============================================================
# 背景回归：旧版「仅免费」直接渲染配置位 free_only（添加时前端写死 true），
# deepseek 这种按量计费的上游也被标"仅免费"。现在按证据分级，没证据就不许说免费。
ORACLE = {"tail": {"deepseek-chat": [0.0000027, 0.000011], "demo-free": [0, 0]}}


def _prov(models, base_url="https://api.example.com/v1", **kw):
    return {"name": "P", "base_url": base_url, "api_key": "k",
            "models": models, "disabled_models": [], **kw}


def test_classify_upstream_pricing_zero_is_free():
    v = app_module.classify_model_free("x", upstream_pricing={"prompt": "0", "completion": "0"}, oracle=ORACLE)
    assert v["status"] == "free" and v["source"] == "upstream"


def test_classify_upstream_pricing_paid():
    v = app_module.classify_model_free("deepseek/deepseek-chat",
                                       upstream_pricing={"prompt": "0.0027", "completion": "0.011"})
    assert v["status"] == "paid" and v["source"] == "upstream" and v["conflict"] is False


def test_classify_upstream_vs_oracle_conflict_flagged():
    # 上游说免费、价目源说付费 → 以上游为准，但打冲突标记（前端会提示）
    v = app_module.classify_model_free("m", upstream_pricing={"prompt": "0", "completion": "0"},
                                       oracle={"tail": {"m": [0.01, 0.02]}})
    assert v["status"] == "free" and v["conflict"] is True


def test_classify_name_suffix_free():
    for mid in ("llama-3.3-70b:free", "vendor/model-free", "abc_free"):
        v = app_module.classify_model_free(mid)
        assert v["status"] == "free" and v["source"] == "name", mid


def test_classify_free_prefix_not_false_positive():
    # 只看末段后缀："free-" 开头的普通模型名不算免费
    v = app_module.classify_model_free("free-model-x")
    assert v["status"] == "unknown"


def test_is_local_base_url():
    for u in ("http://127.0.0.1:8000", "http://localhost/v1", "http://192.168.1.5:9000",
              "http://10.1.2.3", "http://172.16.0.9", "http://172.31.255.1", "http://nas.local"):
        assert app_module.is_local_base_url(u) is True, u
    for u in ("https://api.deepseek.com", "https://llm.example.com/",
              "http://172.32.0.1", "https://integrate.api.nvidia.com/v1"):
        assert app_module.is_local_base_url(u) is False, u


def test_classify_local_deploy_is_free():
    v = app_module.classify_model_free("qwen/qwen3.8-27b", base_url="http://192.168.1.5:9000/v1")
    assert v["status"] == "free" and v["source"] == "local"


def test_classify_oracle_hit():
    v = app_module.classify_model_free("deepseek/deepseek-chat", oracle=ORACLE)
    assert v["status"] == "paid" and v["source"] == "oracle"
    assert app_module.classify_model_free("demo-free", oracle=ORACLE)["status"] == "free"


def test_classify_unknown_when_no_evidence():
    v = app_module.classify_model_free("brand-new-model")
    assert v["status"] == "unknown" and v["source"] == "none" and v["price"] is None


def test_summarize_all_free_badge():
    s = app_module.summarize_provider_free(_prov(["a-free", "b-free"]), oracle={"tail": {}})
    assert s["badge"] == "free" and s["free"] == 2 and s["unknown"] == 0


def test_summarize_paid_wins_over_free():
    s = app_module.summarize_provider_free(_prov(["a-free", "deepseek-chat"]), oracle=ORACLE)
    assert s["badge"] == "paid" and s["paid"] == 1 and s["free"] == 1


def test_summarize_unknown_when_no_evidence():
    s = app_module.summarize_provider_free(_prov(["brand-new", "another"]), oracle={"tail": {}})
    assert s["badge"] == "unknown" and s["unknown"] == 2


def test_summarize_disabled_models_not_counted():
    p = _prov(["a-free", "paid-model"], disabled_models=["paid-model"])
    s = app_module.summarize_provider_free(p, oracle={"tail": {"paid-model": [1, 1]}})
    assert s["badge"] == "free" and s["models_total"] == 1


def test_summarize_override_wins():
    p = _prov(["brand-new"], free_status_override="free")
    s = app_module.summarize_provider_free(p, oracle={"tail": {}})
    assert s["badge"] == "override:free"


def test_deepseek_never_labeled_free():
    """回归锚点：deepseek 没有任何免费证据 → 必须是未核实，绝不能是仅免费。"""
    p = _prov(["deepseek-chat", "deepseek-reasoner"], base_url="https://api.deepseek.com")
    s = app_module.summarize_provider_free(p, oracle={"tail": {}})
    assert s["badge"] == "unknown" and s["free"] == 0


def test_fetch_models_with_pricing_returns_upstream_prices(monkeypatch):
    class _Resp:
        status_code = 200

        def json(self):
            return {"data": [
                {"id": "m-free", "pricing": {"prompt": "0", "completion": "0"}},
                {"id": "m-paid", "pricing": {"prompt": "0.01", "completion": "0.02"}},
                {"id": "m-no-price"},
            ]}

    class _Client:
        async def get(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(app_module, "http_client", _Client())
    ids, pricing = asyncio.run(app_module.fetch_models_with_pricing("http://x/v1", "k", free_only=False))
    assert ids == ["m-free", "m-paid", "m-no-price"]
    assert pricing["m-paid"]["prompt"] == "0.01" and "m-no-price" not in pricing


def test_fetch_models_keeps_list_only_signature(monkeypatch):
    class _Resp:
        status_code = 200

        def json(self):
            return {"data": [{"id": "a"}, {"id": "b"}]}

    class _Client:
        async def get(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(app_module, "http_client", _Client())
    assert asyncio.run(app_module.fetch_models("http://x/v1", "k")) == ["a", "b"]


def test_price_cache_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "DATA_DIR", tmp_path)
    app_module._write_price_cache({"tail": {"deepseek-chat": [2.7e-6, 1.1e-5]},
                                   "fetched_at": 123.0, "source": "openrouter"})
    got = app_module._read_price_cache()
    assert got["tail"]["deepseek-chat"] == [2.7e-6, 1.1e-5] and got["fetched_at"] == 123.0


def test_price_cache_corrupt_file_is_safe(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "DATA_DIR", tmp_path)
    (tmp_path / "pricing_cache.json").write_text("{oops", encoding="utf-8")
    got = app_module._read_price_cache()
    assert got["tail"] == {} and got["fetched_at"] == 0.0


# ============================================================
# 模型管理弹窗：available-models 每模型免费判定（2026-10-17）
# ============================================================
def test_available_models_returns_per_model_verdict(monkeypatch):
    """弹窗接口要给每个模型一份判定，不是只给名字列表。"""
    class _Resp:
        status_code = 200

        def json(self):
            return {"data": [
                {"id": "m-free", "pricing": {"prompt": "0", "completion": "0"}},
                {"id": "deepseek-chat"},
                {"id": "brand-new-model"},
            ]}

    class _Client:
        async def get(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(app_module, "http_client", _Client())
    monkeypatch.setattr(app_module, "providers", [_prov(["m-free"])])
    monkeypatch.setattr(app_module, "save_providers", lambda *a, **k: None)
    # 价目源命中 deepseek-chat
    monkeypatch.setattr(app_module, "_read_price_cache",
                        lambda: {"tail": {"deepseek-chat": [2.7e-6, 1.1e-5]}, "fetched_at": 1.0, "source": "openrouter"})

    import asyncio
    res = asyncio.run(app_module.get_available_models("P"))
    assert res["ok"] is True
    assert res["models"] == ["m-free", "deepseek-chat", "brand-new-model"]
    free = res["free"]
    assert free["m-free"]["status"] == "free" and free["m-free"]["source"] == "upstream"
    assert free["deepseek-chat"]["status"] == "paid" and free["deepseek-chat"]["source"] == "oracle"
    assert free["brand-new-model"]["status"] == "unknown"


def test_available_models_persists_upstream_pricing(monkeypatch):
    """上游给了 pricing 就存进 provider，下次判定不用重拉。"""
    class _Resp:
        status_code = 200

        def json(self):
            return {"data": [{"id": "a", "pricing": {"prompt": "0", "completion": "0"}}]}

    class _Client:
        async def get(self, *a, **k):
            return _Resp()

    saved = {}
    monkeypatch.setattr(app_module, "http_client", _Client())
    p = _prov(["a"])
    monkeypatch.setattr(app_module, "providers", [p])
    monkeypatch.setattr(app_module, "save_providers", lambda provs: saved.setdefault("called", True))
    import asyncio
    asyncio.run(app_module.get_available_models("P"))
    assert p["model_pricing"]["a"]["prompt"] == "0"
    assert saved.get("called") is True


def test_available_models_404_for_unknown_provider(monkeypatch):
    from fastapi import HTTPException
    monkeypatch.setattr(app_module, "providers", [])
    import asyncio
    with pytest.raises(HTTPException):
        asyncio.run(app_module.get_available_models("不存在的"))


def test_available_models_empty_upstream(monkeypatch):
    class _Resp:
        status_code = 200

        def json(self):
            return {"data": []}

    class _Client:
        async def get(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(app_module, "http_client", _Client())
    monkeypatch.setattr(app_module, "providers", [_prov([])])
    import asyncio
    res = asyncio.run(app_module.get_available_models("P"))
    assert res["models"] == [] and res["free"] == {}
