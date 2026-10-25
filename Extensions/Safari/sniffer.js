// SDM 内容脚本：悬停按钮 + Alt 逃生舱上报 + POST 体截获。
//
// 三件事：
//
// 1. **悬停按钮**——鼠标停在媒体 / 文件链接上时冒一个小按钮，
//    点一下就发给下载器。这是自动接管之外的另一条路。
//
// 2. **Alt 逃生舱上报**——按住 Alt（macOS 是 Option）点击时，
//    立刻告诉后台「这个链接别接管」。
//    **必须在这里上报，不能等到后台去问**：后台的
//    `downloads.onDeterminingFilename` 是同步回调，等不了异步往返；
//    等它问完，下载早就开始了。
//
// 3. **POST 体截获**——Manifest V3 里 `webRequest` 拿不到请求体
//    （`requestBody` 要 `blocking` 权限，而 MV3 已经不给普通扩展了）。
//    所以在这里钩住 `fetch` / `XMLHttpRequest`，把带 body 的请求
//    记下来，后台派单时一并带走——有些站点（比如上传接口返回文件）
//    没有 body 就下不到东西。

(function () {
  "use strict";

  const api = typeof browser !== "undefined" ? browser : chrome;
  const POST_BODY_MAX = 64 * 1024;

  // -- 1. Alt 逃生舱 ------------------------------------------------

  function linkUrlOf(target) {
    if (!target) return "";
    const anchor = target.closest ? target.closest("a[href]") : null;
    if (anchor) return anchor.href || "";
    if (target.tagName === "A" && target.href) return target.href;
    if (target.src) return target.src;
    return "";
  }

  function reportAltClick(url) {
    if (!url) return;
    try {
      api.runtime.sendMessage({
        type: "sdm:altClick",
        // altKey 一起带上：后台只认这个标记，不自己猜。
        altKey: true,
        url: url,
        documentUrl: location.href,
      });
    } catch (err) {
      /* 后台不在线就算了 */
    }
  }

  // 捕获阶段先跑：页面自己的处理器可能 stopPropagation，我们要在
  // 它之前知道用户按没按 Alt。
  document.addEventListener(
    "click",
    (event) => {
      if (!event.altKey) return;
      reportAltClick(linkUrlOf(event.target));
    },
    true
  );

  document.addEventListener(
    "pointerdown",
    (event) => {
      if (!event.altKey) return;
      reportAltClick(linkUrlOf(event.target));
    },
    true
  );

  // -- 2. POST 体截获 ------------------------------------------------

  function reportPost(url, method, body) {
    if (!url) return;
    let text = "";
    try {
      if (typeof body === "string") {
        text = body;
      } else if (body && typeof body.text === "function") {
        // FormData / URLSearchParams：能变成字符串就带走
        text = String(body);
      }
    } catch (err) {
      text = "";
    }
    if (!text || text.length > POST_BODY_MAX) return;
    try {
      api.runtime.sendMessage({
        type: "sdm:postCandidate",
        url: url,
        method: String(method || "GET").toUpperCase(),
        body: text,
      });
    } catch (err) {
      /* 忽略 */
    }
  }

  try {
    const originalFetch = window.fetch;
    if (originalFetch) {
      window.fetch = function (input, init) {
        try {
          const url =
            typeof input === "string" ? input : (input && input.url) || "";
          const method =
            (init && init.method) || (input && input.method) || "GET";
          if (init && init.body && String(method).toUpperCase() !== "GET") {
            reportPost(url, method, init.body);
          }
        } catch (err) {
          /* 忽略 */
        }
        return originalFetch.apply(this, arguments);
      };
    }
  } catch (err) {
    /* 页面冻结了 fetch 就没法截 */
  }

  try {
    const originalOpen = XMLHttpRequest.prototype.open;
    const originalSend = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function (method, url) {
      this.__sdmMethod = method;
      this.__sdmUrl = typeof url === "string" ? url : (url && url.href) || "";
      return originalOpen.apply(this, arguments);
    };
    XMLHttpRequest.prototype.send = function (body) {
      try {
        if (body) reportPost(this.__sdmUrl, this.__sdmMethod, body);
      } catch (err) {
        /* 忽略 */
      }
      return originalSend.apply(this, arguments);
    };
  } catch (err) {
    /* 忽略 */
  }

  // -- 3. 悬停按钮 --------------------------------------------------

  let btn = null;
  let currentTarget = null;

  function ensureButton() {
    if (btn) return btn;
    btn = document.createElement("div");
    btn.id = "__sdm_hover_btn__";
    btn.textContent = "↓ SDM";
    btn.style.cssText = [
      "position: fixed",
      "z-index: 2147483647",
      "padding: 4px 8px",
      "background: #8B5CF6",
      "color: white",
      "font: 12px/1 -apple-system, sans-serif",
      "border-radius: 6px",
      "box-shadow: 0 2px 8px rgba(0,0,0,0.3)",
      "cursor: pointer",
      "user-select: none",
      "display: none",
      "pointer-events: auto",
    ].join(";");
    btn.addEventListener("click", async (event) => {
      event.preventDefault();
      event.stopPropagation();
      if (!currentTarget) return;
      const url = currentTarget.src || currentTarget.href || "";
      if (!url) return;
      try {
        const res = await api.runtime.sendMessage({
          type: "sdm:send",
          url: url,
          referer: location.href,
        });
        if (res && res.ok) {
          btn.textContent = "✓ 已发送";
          setTimeout(() => {
            btn.style.display = "none";
          }, 700);
        } else {
          btn.textContent = "✗ 失败";
          setTimeout(() => {
            btn.style.display = "none";
          }, 1500);
        }
      } catch (err) {
        btn.textContent = "✗ 失败";
      }
    });
    document.body.appendChild(btn);
    return btn;
  }

  function sniffable(el) {
    if (!el) return false;
    if (
      el.tagName === "A" &&
      el.href &&
      /\.(zip|dmg|pkg|iso|exe|pdf|mp4|mkv|mp3|flac|rar|7z|tar|gz)$/i.test(el.href)
    ) {
      return true;
    }
    if (el.tagName === "VIDEO" || el.tagName === "AUDIO") {
      const src = el.src || (el.querySelector && el.querySelector("source")?.src);
      return Boolean(src);
    }
    return false;
  }

  document.addEventListener(
    "mouseover",
    (event) => {
      const target = event.target;
      if (!sniffable(target)) return;
      currentTarget = target;
      const rect = target.getBoundingClientRect();
      const button = ensureButton();
      button.style.left = `${rect.right + 6}px`;
      button.style.top = `${rect.top + 2}px`;
      button.style.display = "block";
    },
    true
  );

  document.addEventListener(
    "mouseout",
    (event) => {
      if (currentTarget === event.target) {
        currentTarget = null;
        if (btn) btn.style.display = "none";
      }
    },
    true
  );
})();
