"""服务就绪校验：别开一个永远「无法访问」的死窗口。

这条以前内联在启动流程里、测不到，所以它的失败分支从没被跑过：
5 秒等不到就照常开窗，用户看到空窗口而日志里没有任何报错。
"""
import http.server
import socket
import socketserver
import threading
import time

import app as app_module


class _NeverSpeaksHTTP(socketserver.BaseRequestHandler):
    """接受 TCP 连接但永远不回话 —— 最贴近真实故障：端口通了，HTTP 没起。"""

    def handle(self):
        try:
            self.request.settimeout(0.2)
            self.request.recv(64)
            time.sleep(0.5)     # 拖过客户端超时
        except Exception:
            pass


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def test_dead_port_returns_non_zero_status_fast():
    port = _free_port()
    t0 = time.time()
    st = app_module.wait_until_ready(port, timeout=1.5)
    assert st != 200, "死端口不该被判成就绪"
    assert time.time() - t0 < 4, "1.5s 超时却等了很久"


def test_listener_that_never_speaks_http_is_not_ready():
    """端口通、HTTP 不通 —— 单看 `port_in_use` 会误判成就绪，这正是老实现的坑。"""
    srv = socketserver.TCPServer(("127.0.0.1", 0), _NeverSpeaksHTTP)
    srv.daemon_threads = True
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        # 确认 TCP 层确实通（否则这个测试什么都没证明）
        with socket.socket() as s:
            s.settimeout(1)
            assert s.connect_ex(("127.0.0.1", port)) == 0, "TCP 应该是通的"
        assert app_module.probe_http(port, timeout=0.8) != 200

        t0 = time.time()
        st = app_module.wait_until_ready(port, timeout=2.0)
        assert st != 200, "端口通但 HTTP 没起 → 必须判为未就绪"
        assert time.time() - t0 < 5
    finally:
        srv.shutdown()
        srv.server_close()


def test_real_http_server_is_ready():
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert app_module.wait_until_ready(port, timeout=5.0) == 200
    finally:
        srv.shutdown()
        srv.server_close()


def test_probe_http_returns_zero_on_dead_port():
    assert app_module.probe_http(_free_port(), timeout=0.5) == 0


def test_probe_http_reports_non_200_status():
    """4xx 也要如实返回 —— 不能因为不是 200 就吞成 0，否则分不清「没起」和「起了但报错」。"""
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert app_module.probe_http(port, timeout=1.5) == 503
    finally:
        srv.shutdown()
        srv.server_close()
