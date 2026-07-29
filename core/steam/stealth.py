"""Playwright 页内 init_script（弱化常见自动化检测点）。"""

from __future__ import annotations

import json
import random
import string

from config import (
    PLAYWRIGHT_NAVIGATOR_APP_CODE_NAME,
    PLAYWRIGHT_NAVIGATOR_APP_NAME,
    PLAYWRIGHT_NAVIGATOR_APP_NAME_RANDOM,
    PLAYWRIGHT_NAVIGATOR_APP_VERSION,
    PLAYWRIGHT_NAVIGATOR_BRANDS,
    PLAYWRIGHT_NAVIGATOR_DEVICE_MEMORY,
    PLAYWRIGHT_NAVIGATOR_HARDWARE_CONCURRENCY,
    PLAYWRIGHT_NAVIGATOR_LANGUAGES,
    PLAYWRIGHT_NAVIGATOR_PLATFORM,
    PLAYWRIGHT_NAVIGATOR_PLUGINS_LENGTH,
    PLAYWRIGHT_NAVIGATOR_PRODUCT,
    PLAYWRIGHT_NAVIGATOR_PRODUCT_SUB,
    PLAYWRIGHT_NAVIGATOR_VENDOR,
    PLAYWRIGHT_NAVIGATOR_VENDOR_SUB,
    PLAYWRIGHT_STEALTH_CANVAS_NOISE,
    PLAYWRIGHT_STEALTH_MODIFY_WEB_UK,
    PLAYWRIGHT_STEALTH_WEBGL_NOISE,
    PLAYWRIGHT_STEALTH_MUTE_CONSOLE,
    PLAYWRIGHT_STEALTH_PATCH_DEVTOOLS,
    PLAYWRIGHT_STEALTH_PATCH_NAVIGATOR_PRODUCT,
    PLAYWRIGHT_WEBGL_RENDERER,
    PLAYWRIGHT_WEBGL_VENDOR,
)
from .window_metrics import get_runtime_window_metrics

_AUTOMATION_STEALTH = """(() => {
  const patch = () => {
    try {
      Object.defineProperty(navigator, 'webdriver', {
        get: () => false,
        configurable: true,
      });
    } catch (e) {}

    try {
      if (!window.chrome) {
        window.chrome = {};
      }
      if (!window.chrome.runtime) {
        window.chrome.runtime = {};
      }
    } catch (e) {}

    try {
      const q = navigator.permissions && navigator.permissions.query;
      if (typeof q === 'function') {
        navigator.permissions.query = (parameters) =>
          parameters && parameters.name === 'notifications'
            ? Promise.resolve({
                state:
                  typeof Notification !== 'undefined'
                    ? Notification.permission
                    : 'default',
                onchange: null,
              })
            : q.call(navigator.permissions, parameters);
      }
    } catch (e) {}

  };
  patch();
  document.addEventListener('DOMContentLoaded', patch, { once: true });
})();"""

_DEVTOOLS_PATCH = """(() => {
  try {
    Object.defineProperty(window, 'devtools', {
      get: function() { return { opened: false }; },
      configurable: true,
    });
  } catch (e) {}
})();"""

_MUTE_CONSOLE = """(() => {
  try {
    const noop = function() {};
    console.log = noop;
    console.warn = noop;
    console.error = noop;
    console.clear = noop;
  } catch (e) {}
})();"""


def _clear_web_uk_js() -> str:
  if not PLAYWRIGHT_STEALTH_MODIFY_WEB_UK:
    return ""
  return """(() => {
  const chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789';
  const mutate = (raw) => {
    try {
      if (typeof raw !== 'string' || !raw.length) return raw;
      const ch = chars[Math.floor(Math.random() * chars.length)];
      return raw.slice(0, -1) + ch;
    } catch (e) {
      return raw;
    }
  };

  const patch = () => {
    try {
      if (window.localStorage) {
        const v = window.localStorage.getItem('WEB_UK');
        if (v) {
          window.localStorage.setItem('WEB_UK', mutate(v));
        }
      }
    } catch (e) {}
    try {
      if (window.sessionStorage) {
        const v = window.sessionStorage.getItem('WEB_UK');
        if (v) {
          window.sessionStorage.setItem('WEB_UK', mutate(v));
        }
      }
    } catch (e) {}
  };
  patch();
  document.addEventListener('DOMContentLoaded', patch, { once: true });
})();"""

# 稀疏扰动 RGBA，使指纹脚本读到的哈希随运行变化；toDataURL/toBlob 先经 getImageData 回写再导出。
_CANVAS_NOISE = """(() => {
  if (window.__cshelperCanvasNoise) return;
  window.__cshelperCanvasNoise = true;
  const jitter = (u8) => {
    try {
      const n = u8.length;
      for (let i = 0; i < n; i += 4) {
        if (Math.random() * 12000 < 1) {
          u8[i] ^= 1;
        }
      }
    } catch (e) {}
  };
  try {
    const g = CanvasRenderingContext2D.prototype.getImageData;
    CanvasRenderingContext2D.prototype.getImageData = function (sx, sy, sw, sh) {
      const img = g.call(this, sx, sy, sw, sh);
      jitter(img.data);
      return img;
    };
  } catch (e) {}
  try {
    const u = HTMLCanvasElement.prototype.toDataURL;
    HTMLCanvasElement.prototype.toDataURL = function (...a) {
      const c = this.getContext('2d');
      if (c && this.width > 0 && this.height > 0) {
        try {
          const img = c.getImageData(0, 0, this.width, this.height);
          c.putImageData(img, 0, 0);
        } catch (e) {}
      }
      return u.apply(this, a);
    };
  } catch (e) {}
  try {
    const b = HTMLCanvasElement.prototype.toBlob;
    if (typeof b === 'function') {
      HTMLCanvasElement.prototype.toBlob = function (...a) {
        const c = this.getContext('2d');
        if (c && this.width > 0 && this.height > 0) {
          try {
            const img = c.getImageData(0, 0, this.width, this.height);
            c.putImageData(img, 0, 0);
          } catch (e) {}
        }
        return b.apply(this, a);
      };
    }
  } catch (e) {}
})();"""

# WebGL(2) readPixels：仅在输出为 Uint8Array（典型 UNSIGNED_BYTE）时扰动，避免破坏 FLOAT/整数格式读回。
_WEBGL_NOISE = """(() => {
  if (window.__cshelperWebglNoise) return;
  window.__cshelperWebglNoise = true;
  const jitterU8 = (u8) => {
    try {
      const n = u8.length;
      for (let i = 0; i < n; i += 4) {
        if (Math.random() * 12000 < 1) {
          u8[i] ^= 1;
        }
      }
    } catch (e) {}
  };
  const wrapReadPixels = (proto) => {
    if (!proto || typeof proto.readPixels !== 'function') return;
    const rp = proto.readPixels;
    proto.readPixels = function (x, y, w, h, format, type, pixels) {
      rp.call(this, x, y, w, h, format, type, pixels);
      try {
        if (pixels && pixels instanceof Uint8Array) {
          jitterU8(pixels);
        }
      } catch (e) {}
    };
  };
  try {
    wrapReadPixels(WebGLRenderingContext.prototype);
  } catch (e) {}
  try {
    if (typeof WebGL2RenderingContext !== 'undefined') {
      wrapReadPixels(WebGL2RenderingContext.prototype);
    }
  } catch (e) {}
})();"""


def _navigator_languages_js() -> str:
  if PLAYWRIGHT_NAVIGATOR_LANGUAGES is None:
    return ""
  langs_lit = json.dumps(PLAYWRIGHT_NAVIGATOR_LANGUAGES, ensure_ascii=False)
  return f"""(() => {{
  const patch = () => {{
    try {{
      Object.defineProperty(navigator, 'languages', {{
        get: () => Object.freeze({langs_lit}),
        configurable: true,
      }});
    }} catch (e) {{}}
  }};
  patch();
  document.addEventListener('DOMContentLoaded', patch, {{ once: true }});
}})();"""


def _screen_window_metrics_js() -> str:
  metrics = get_runtime_window_metrics()
  props: list[tuple[str, int | float | None, str]] = [
    ("availWidth", metrics["screen_avail_width"], "screen"),
    ("availHeight", metrics["screen_avail_height"], "screen"),
    ("innerWidth", metrics["window_inner_width"], "window"),
    ("innerHeight", metrics["window_inner_height"], "window"),
    ("outerWidth", metrics["window_outer_width"], "window"),
    ("outerHeight", metrics["window_outer_height"], "window"),
    ("devicePixelRatio", metrics["window_device_pixel_ratio"], "window"),
  ]
  lines: list[str] = []
  for key, val, target in props:
    if val is None:
      continue
    lit = json.dumps(val, ensure_ascii=False)
    if target == "screen":
      lines.append(
        f"    try {{ Object.defineProperty(window.screen, {json.dumps(key)}, "
        f"{{ get: () => {lit}, configurable: true }}); }} catch (e) {{}}"
      )
    else:
      lines.append(
        f"    try {{ Object.defineProperty(window, {json.dumps(key)}, "
        f"{{ get: () => {lit}, configurable: true }}); }} catch (e) {{}}"
      )
  if not lines:
    return ""
  inner = "\n".join(lines)
  return f"""(() => {{
  const patch = () => {{
{inner}
  }};
  patch();
  document.addEventListener('DOMContentLoaded', patch, {{ once: true }});
}})();"""


def _random_app_name() -> str:
  n = random.randint(4, 10)
  chars = string.ascii_letters + string.digits
  return "".join(random.choice(chars) for _ in range(n))


def _navigator_product_info_js() -> str:
  if not PLAYWRIGHT_STEALTH_PATCH_NAVIGATOR_PRODUCT:
    return ""
  app_name_val = (
    _random_app_name()
    if PLAYWRIGHT_NAVIGATOR_APP_NAME_RANDOM
    else PLAYWRIGHT_NAVIGATOR_APP_NAME
  )
  props: list[tuple[str, str | int | float | None]] = [
    ("vendor", PLAYWRIGHT_NAVIGATOR_VENDOR),
    ("vendorSub", PLAYWRIGHT_NAVIGATOR_VENDOR_SUB),
    ("product", PLAYWRIGHT_NAVIGATOR_PRODUCT),
    ("productSub", PLAYWRIGHT_NAVIGATOR_PRODUCT_SUB),
    ("appName", app_name_val),
    ("appCodeName", PLAYWRIGHT_NAVIGATOR_APP_CODE_NAME),
    ("appVersion", PLAYWRIGHT_NAVIGATOR_APP_VERSION),
    ("platform", PLAYWRIGHT_NAVIGATOR_PLATFORM),
    ("hardwareConcurrency", PLAYWRIGHT_NAVIGATOR_HARDWARE_CONCURRENCY),
    ("deviceMemory", PLAYWRIGHT_NAVIGATOR_DEVICE_MEMORY),
  ]
  lines: list[str] = []
  for key, val in props:
    if val is None:
      continue
    lit = json.dumps(val, ensure_ascii=False)
    lines.append(
      f"    try {{ Object.defineProperty(navigator, {json.dumps(key)}, "
      f"{{ get: () => {lit}, configurable: true }}); }} catch (e) {{}}"
    )
  if PLAYWRIGHT_NAVIGATOR_PLUGINS_LENGTH is not None:
    plugins_len_lit = json.dumps(PLAYWRIGHT_NAVIGATOR_PLUGINS_LENGTH, ensure_ascii=False)
    lines.append(
      "    try {\n"
      "      const _len = " + plugins_len_lit + ";\n"
      "      const fakePlugins = {\n"
      "        length: _len,\n"
      "        item: (i) => (i >= 0 && i < _len ? {} : null),\n"
      "        namedItem: () => null,\n"
      "      };\n"
      "      Object.defineProperty(navigator, 'plugins', {\n"
      "        get: () => fakePlugins,\n"
      "        configurable: true,\n"
      "      });\n"
      "    } catch (e) {}"
    )
  if PLAYWRIGHT_NAVIGATOR_BRANDS is not None:
    brands_lit = json.dumps(PLAYWRIGHT_NAVIGATOR_BRANDS, ensure_ascii=False)
    lines.append(
      "    try {\n"
      "      const _brands = " + brands_lit + ";\n"
      "      const uaData = {\n"
      "        brands: _brands,\n"
      "        mobile: false,\n"
      "        platform: navigator.platform || '',\n"
      "        getHighEntropyValues: async (hints) => {\n"
      "          const out = {};\n"
      "          try {\n"
      "            const req = Array.isArray(hints) ? hints : [];\n"
      "            if (req.includes('brands')) out.brands = _brands;\n"
      "            if (req.includes('fullVersionList')) out.fullVersionList = _brands;\n"
      "            if (req.includes('platform')) out.platform = navigator.platform || '';\n"
      "            if (req.includes('mobile')) out.mobile = false;\n"
      "          } catch (e) {}\n"
      "          return out;\n"
      "        },\n"
      "      };\n"
      "      Object.defineProperty(navigator, 'userAgentData', {\n"
      "        get: () => uaData,\n"
      "        configurable: true,\n"
      "      });\n"
      "    } catch (e) {}"
    )
  if not lines:
    return ""
  inner = "\n".join(lines)
  return f"""(() => {{
  const patch = () => {{
{inner}
  }};
  patch();
  document.addEventListener('DOMContentLoaded', patch, {{ once: true }});
}})();"""


# 新增：覆写 WebGL renderer/vendor（仅 getParameter）
def _webgl_renderer_info_js() -> str:
  if PLAYWRIGHT_WEBGL_VENDOR is None or PLAYWRIGHT_WEBGL_RENDERER is None:
    return ""
  vendor_lit = json.dumps(PLAYWRIGHT_WEBGL_VENDOR, ensure_ascii=False)
  renderer_lit = json.dumps(PLAYWRIGHT_WEBGL_RENDERER, ensure_ascii=False)
  return f"""(() => {{
  if (window.__cshelperWebglRendererPatched) return;
  window.__cshelperWebglRendererPatched = true;

  const patch = (proto) => {{
    try {{
      if (!proto || typeof proto.getParameter !== 'function') return;
      const orig = proto.getParameter;
      proto.getParameter = function (param) {{
        try {{
          if (param === 37445) return {vendor_lit};
          if (param === 37446) return {renderer_lit};
        }} catch (e) {{}}
        return orig.call(this, param);
      }};
    }} catch (e) {{}}
  }};

  try {{
    patch(WebGLRenderingContext.prototype);
  }} catch (e) {{}}
  try {{
    if (typeof WebGL2RenderingContext !== 'undefined') {{
      patch(WebGL2RenderingContext.prototype);
    }}
  }} catch (e) {{}}
}})();"""


def playwright_stealth_init_js() -> str:
  # parts: list[str] = [_AUTOMATION_STEALTH]
  parts: list[str] = []
  clear_web_uk_js = _clear_web_uk_js()
  if clear_web_uk_js:
    parts.append(clear_web_uk_js)
  # langs_js = _navigator_languages_js()
  # if langs_js:
  #   parts.append(langs_js)
  metrics_js = _screen_window_metrics_js()
  if metrics_js:
    parts.append(metrics_js)
  # nav_js = _navigator_product_info_js()
  # if nav_js:
  #   parts.append(nav_js)
  webgl_info_js = _webgl_renderer_info_js()
  if webgl_info_js:
    parts.append(webgl_info_js)
  if PLAYWRIGHT_STEALTH_PATCH_DEVTOOLS:
      parts.append(_DEVTOOLS_PATCH)
  if PLAYWRIGHT_STEALTH_MUTE_CONSOLE:
      parts.append(_MUTE_CONSOLE)
  # if PLAYWRIGHT_STEALTH_CANVAS_NOISE:
  #     parts.append(_CANVAS_NOISE)
  if PLAYWRIGHT_STEALTH_WEBGL_NOISE:
      parts.append(_WEBGL_NOISE)
  return "\n".join(parts)
