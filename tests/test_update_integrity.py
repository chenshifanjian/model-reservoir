"""更新链完整性：校验不过必须拒绝，任何一步都不能把坏包换上去。"""
import asyncio
import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app as app_module


# ============================================================
# 纯校验函数
# ============================================================
def test_size_mismatch_rejected():
    """截断的下载（字节数不够）必须拒绝 —— 这是最常见的一种坏包。"""
    with pytest.raises(ValueError) as e:
        app_module._assert_download_ok("a" * 64, 1024, "a" * 64, 2048)
    assert "大小不符" in str(e.value)


def test_sha_mismatch_rejected():
    """大小对但内容不对（传错文件 / 被替换）也必须拒绝。"""
    with pytest.raises(ValueError) as e:
        app_module._assert_download_ok("b" * 64, 2048, "a" * 64, 2048)
    assert "SHA256 不符" in str(e.value)


def test_sha_compare_is_case_insensitive():
    """version.json 里大小写混写不该误伤。"""
    app_module._assert_download_ok("ABCDEF" + "0" * 58, 10, "abcdef" + "0" * 58, 10)


def test_matching_meta_passes():
    app_module._assert_download_ok("c" * 64, 999, "c" * 64, 999)


def test_missing_meta_passes():
    """老版 version.json 没有 artifacts：放行（不能因为字段缺失就锁死更新）。"""
    app_module._assert_download_ok("d" * 64, 123, "", 0)


# ============================================================
# 文件头粗筛
# ============================================================
def test_html_error_page_rejected(tmp_path):
    """下到 404 页面 / 反代错误页时，绝对不能当更新包换上。"""
    p = tmp_path / "fake.exe"
    p.write_bytes(b"<!DOCTYPE html><html>404</html>")
    assert app_module._looks_like_executable(str(p)) is False


def test_truncated_single_byte_rejected(tmp_path):
    p = tmp_path / "one.bin"
    p.write_bytes(b"\x7f")
    assert app_module._looks_like_executable(str(p)) is False


def test_missing_file_rejected(tmp_path):
    assert app_module._looks_like_executable(str(tmp_path / "nope")) is False


def test_real_binary_of_this_platform_accepted(tmp_path):
    """用当前解释器当样本：Linux 是 ELF，macOS 是 Mach-O，Windows 是 PE。"""
    head = Path(sys.executable).read_bytes()[:4]
    p = tmp_path / "bin"
    p.write_bytes(head + b"\x00" * 32)
    assert app_module._looks_like_executable(str(p)) is True


# ============================================================
# check_update 把 artifacts 透出来
# ============================================================
class _FakeResp:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload

    def json(self):
        return self._payload


class _FakeHTTP:
    """http_client 在 lifespan 里才创建，测试里直接塞一个最小实现。"""

    def __init__(self, payload):
        self._payload = payload

    async def get(self, url, **kw):
        return _FakeResp(self._payload)


def test_check_update_surfaces_artifact_meta(monkeypatch):
    payload = {
        "version": "9.9.9",
        "min_version": "",
        "release_notes": "x",
        "download_url_windows": "https://e/w.exe",
        "download_url_linux": "https://e/l",
        "download_url_macos": "https://e/m.zip",
        "artifacts": {
            app_module.platform_key(): {"sha256": "AB" * 32, "size": 12345},
        },
    }

    monkeypatch.setattr(app_module, "http_client", _FakeHTTP(payload))
    r = asyncio.run(app_module.check_update())
    assert r["sha256"] == "ab" * 32
    assert r["size"] == 12345


def test_check_update_without_artifacts_is_empty(monkeypatch):
    """老 version.json 没有 artifacts 时返回空值，而不是 KeyError。"""
    payload = {
        "version": "9.9.9",
        "min_version": "",
        "download_url_windows": "https://e/w.exe",
        "download_url_linux": "https://e/l",
        "download_url_macos": "https://e/m.zip",
    }

    monkeypatch.setattr(app_module, "http_client", _FakeHTTP(payload))
    r = asyncio.run(app_module.check_update())
    assert r["sha256"] == ""
    assert r["size"] == 0


# ============================================================
# _do_download 端到端：坏包不落盘
# ============================================================
class _FakeStream:
    def __init__(self, body, status=200):
        self.status_code = status
        self.headers = {"content-length": str(len(body))}
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def aiter_bytes(self, chunk_size=0):
        yield self._body


class _FakeClient:
    body = b""
    status = 200

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def stream(self, method, url, **kw):
        return _FakeStream(_FakeClient.body, _FakeClient.status)


def _run_download(monkeypatch, tmp_path, body, sha, size):
    monkeypatch.setattr(app_module, "DATA_DIR", tmp_path)
    monkeypatch.setattr(app_module.httpx, "AsyncClient", _FakeClient)
    _FakeClient.body = body
    _FakeClient.status = 200
    app_module._update_download_state.update({
        "downloading": True, "progress": 0, "total": 0,
        "done": False, "error": None, "file": None,
    })
    asyncio.run(app_module._do_download("https://e/x", sha, size))
    return dict(app_module._update_download_state)


def test_download_accepts_good_package(monkeypatch, tmp_path):
    body = b"MZ" + b"\x00" * 100
    st = _run_download(monkeypatch, tmp_path, body,
                       hashlib.sha256(body).hexdigest(), len(body))
    assert st["done"] is True
    assert st["error"] is None
    assert Path(st["file"]).read_bytes() == body


def test_download_discards_truncated(monkeypatch, tmp_path):
    """声明 500 字节、实际只到 200 字节：必须报错且不留文件。"""
    body = b"MZ" + b"\x00" * 100
    st = _run_download(monkeypatch, tmp_path, body, "", 500)
    assert st["done"] is False
    assert "大小不符" in st["error"]
    assert st["file"] is None
    assert list(tmp_path.iterdir()) == []


def test_download_discards_sha_mismatch(monkeypatch, tmp_path):
    body = b"MZ" + b"\x00" * 100
    st = _run_download(monkeypatch, tmp_path, body, "f" * 64, len(body))
    assert st["done"] is False
    assert "SHA256 不符" in st["error"]
    assert list(tmp_path.iterdir()) == []


# ============================================================
# 发版脚本：资产名必须与 release 上的一致，否则 version.json 里的 URL 直接 404
# ============================================================
def test_release_asset_names_match_published_files():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import gen_version_artifacts as gva

    assert gva.ASSET_NAME["windows"].format(tag="v1.9.0") == "ModelReservoir-v1.9.0-windows.exe"
    assert gva.ASSET_NAME["linux"].format(tag="v1.9.0") == "ModelReservoir-v1.9.0-linux-x86_64"
    assert gva.ASSET_NAME["macos"].format(tag="v1.9.0") == "ModelReservoir-v1.9.0-macos.zip"
