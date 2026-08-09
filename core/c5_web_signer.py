"""Generate C5 website request signatures without automating a C5 page.

The official signer is an encrypted WebAssembly asset.  It is evaluated in a
local-only Edge/Chrome page, so C5 never sees a Playwright-controlled browser.
"""

from __future__ import annotations

import base64
import http.server
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import requests

from config import CACHE_DIR
from core.market_external_browser import resolve_system_browser_executable

_ASSET_URL = "https://img.zbt.com/b/static/r/8a8110c"
_ASSET_CACHE = "c5_web_signer.bin"
_ASSET_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
_AES_KEY = "Kf8Vx9Qw3Rp2Nm7Hj5Lz1Bc6Yu4Gt0Se"
_AES_IV = "9Xm3Kl8Vq2Rt7Nz1"

_INIT_JS = r"""async ({b64, keyText, ivText}) => {
  const raw = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
  const encoder = new TextEncoder();
  const key = await crypto.subtle.importKey(
    'raw', encoder.encode(keyText), {name: 'AES-CBC'}, false, ['decrypt']
  );
  const wasm = await crypto.subtle.decrypt(
    {name: 'AES-CBC', iv: encoder.encode(ivText)}, key, raw
  );
  let instance;
  const imports = {wbg: {
    __wbg_log_fd9bb94dca9f855e: () => {},
    __wbindgen_init_externref_table: () => {
      const table = instance.exports.__wbindgen_export_0;
      const offset = table.grow(4);
      table.set(0, undefined);
      table.set(offset, undefined);
      table.set(offset + 1, null);
      table.set(offset + 2, true);
      table.set(offset + 3, false);
    }
  }};
  instance = (await WebAssembly.instantiate(wasm, imports)).instance;
  instance.exports.__wbindgen_start();
  const decoder = new TextDecoder();
  window.__cs2thC5Sign = ({pathname, method, timestamp, accessToken}) => {
    let memory;
    const put = value => {
      const bytes = encoder.encode(String(value));
      const pointer = instance.exports.__wbindgen_malloc(bytes.length, 1) >>> 0;
      memory = new Uint8Array(instance.exports.memory.buffer);
      memory.set(bytes, pointer);
      return [pointer, bytes.length];
    };
    const path = put(pathname);
    const verb = put(String(method).toUpperCase());
    const stamp = put(String(timestamp));
    const token = accessToken ? put(accessToken) : [0, 0];
    const result = instance.exports.sg(
      path[0], path[1], verb[0], verb[1], stamp[0], stamp[1], token[0], token[1]
    );
    memory = new Uint8Array(instance.exports.memory.buffer);
    const pointer = result[0] >>> 0;
    const length = result[1] >>> 0;
    const signature = decoder.decode(memory.subarray(pointer, pointer + length));
    instance.exports.__wbindgen_free(pointer, length, 1);
    return signature;
  };
  return true;
}"""


class _LocalHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        body = b"<!doctype html><meta charset=utf-8><title>CS2TH C5 signer</title>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *args) -> None:
        return


def _load_asset() -> bytes:
    path = Path(CACHE_DIR) / _ASSET_CACHE
    try:
        if path.is_file() and time.time() - path.stat().st_mtime <= _ASSET_MAX_AGE_SECONDS:
            data = path.read_bytes()
            if len(data) > 4096:
                return data
    except OSError:
        pass
    response = requests.get(_ASSET_URL, timeout=20)
    response.raise_for_status()
    data = bytes(response.content)
    if len(data) <= 4096:
        raise RuntimeError("C5GAME 官方签名资源返回异常")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    except OSError:
        pass
    return data


class C5WebSigner:
    """A short-lived local signer reused for all C5 requests in one collection."""

    def __init__(self) -> None:
        self._server = None
        self._thread = None
        self._process = None
        self._playwright = None
        self._browser = None
        self._page = None
        self._profile: Path | None = None

    def __enter__(self) -> "C5WebSigner":
        from playwright.sync_api import sync_playwright

        executable = resolve_system_browser_executable()
        if executable is None:
            raise RuntimeError("未找到本机 Chrome / Edge，无法生成 C5GAME 请求签名")
        asset = _load_asset()
        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _LocalHandler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self._profile = Path(tempfile.mkdtemp(prefix="cs2th-c5-sign-"))
        self._process = subprocess.Popen(
            [
                str(executable),
                f"--user-data-dir={self._profile}",
                "--remote-debugging-port=0",
                "--headless=new",
                "--no-first-run",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        port_file = self._profile / "DevToolsActivePort"
        deadline = time.monotonic() + 12.0
        while time.monotonic() < deadline and not port_file.is_file():
            if self._process.poll() is not None:
                break
            time.sleep(0.08)
        if not port_file.is_file():
            self.close()
            raise RuntimeError("C5GAME 本地签名浏览器启动失败")
        port = port_file.read_text(encoding="utf-8").splitlines()[0]
        self._playwright = sync_playwright().start()
        last_connect_error: Exception | None = None
        for attempt in range(1, 6):
            try:
                self._browser = self._playwright.chromium.connect_over_cdp(
                    f"http://127.0.0.1:{port}"
                )
                last_connect_error = None
                break
            except Exception as exc:  # noqa: BLE001
                last_connect_error = exc
                if self._process.poll() is not None:
                    break
                time.sleep(0.2 * attempt)
        if self._browser is None:
            raise RuntimeError(
                f"C5GAME 本地签名浏览器连接失败：{last_connect_error or 'unknown'}"
            )
        context = self._browser.contexts[0]
        self._page = context.pages[0] if context.pages else context.new_page()
        local_port = self._server.server_address[1]
        self._page.goto(f"http://127.0.0.1:{local_port}/", wait_until="domcontentloaded")
        self._page.evaluate(
            _INIT_JS,
            {
                "b64": base64.b64encode(asset).decode("ascii"),
                "keyText": _AES_KEY,
                "ivText": _AES_IV,
            },
        )
        return self

    def sign(self, pathname: str, method: str, timestamp: str, token: str) -> str:
        if self._page is None:
            raise RuntimeError("C5GAME 本地签名器尚未启动")
        value = self._page.evaluate(
            "args => window.__cs2thC5Sign(args)",
            {
                "pathname": pathname,
                "method": method,
                "timestamp": str(timestamp),
                "accessToken": str(token or ""),
            },
        )
        signature = str(value or "").strip()
        if token and not signature:
            raise RuntimeError("C5GAME 官方签名资源未生成有效签名")
        return signature

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()

    def close(self) -> None:
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
            self._playwright = None
        if self._process is not None and self._process.poll() is None:
            try:
                self._process.terminate()
                self._process.wait(timeout=3)
            except Exception:
                try:
                    self._process.kill()
                except Exception:
                    pass
        self._process = None
        if self._server is not None:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:
                pass
            self._server = None
        if self._profile is not None:
            try:
                shutil.rmtree(self._profile, ignore_errors=True)
            except Exception:
                pass
            self._profile = None
