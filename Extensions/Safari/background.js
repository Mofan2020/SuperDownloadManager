// SDM Safari Web Extension —— 后台脚本。
//
// **Safari 只做手动派单，不做自动接管。**
//
// 原因不是偷懒：Safari 的 WebExtension API 没有 `downloads.onDeterminingFilename`
// 这种「下载开始前拦一道」的钩子（契约 §4.4.5 明确要求这一点如实标注）。
// 硬做的话只能等下载已经开始了再取消——那时候文件已经在路上了，
// 取消 + 擦除的窗口里用户会丢文件。所以 Safari 这边只有两条路：
// 右键菜单「发送到下载器」和工具栏按钮。
//
// 传输：优先走 Safari 的原生消息（`sendNativeMessage`）——但注意
// **Safari 的原生消息不是 Chrome 那种宿主**。Safari 的
// `sendNativeMessage(appId, message)` 是发给应用包里的
// **Safari Web Extension Handler**（一个 macOS App Extension，
// 由应用自己实现 `beginRequest(with:)`）。本仓库里的
// `sdm/ipc/native_host.py` 是 Chrome 协议的宿主，Safari 用不上。
// Handler 没实现时，退到回环 HTTP 通道。

const SDM_APP_ID = "com.skyc8266.superdownloadmanager";
const SDM_HTTP_ENDPOINT = "http://127.0.0.1:7955/add";
const TOKEN_STORAGE_KEY = "sdmPairToken";

// -- 协议过滤 --------------------------------------------------------
//
// blob: / data: 下载器接不了（没有可重放的 HTTP 请求）。
// 手动派单时也要挡住，不然用户点了没反应还以为坏了。
const UNSUPPORTED_SCHEMES = ["blob:", "data:"];
const SUPPORTED_SCHEMES = ["http:", "https:"];

// Alt 逃生舱：按住 Alt（macOS 上是 Option）点过的链接不接管，
// 留给浏览器自己下。
const ALT_SKIP_TTL_MS = 15000;

/** 内容脚本报告的「按着 Alt 点过的链接」。 */
const altClicks = new Map();

function schemeOf(url) {
  const text = String(url || "").trim().toLowerCase();
  for (const scheme of UNSUPPORTED_SCHEMES) {
    if (text.startsWith(scheme)) return scheme;
  }
  try {
    return new URL(text).protocol;
  } catch (err) {
    return "";
  }
}

function isUnsupportedScheme(url) {
  return UNSUPPORTED_SCHEMES.indexOf(schemeOf(url)) >= 0;
}

function isSupportedScheme(url) {
  return SUPPORTED_SCHEMES.indexOf(schemeOf(url)) >= 0;
}

function now() {
  return Date.now();
}

// -- 配对 ------------------------------------------------------------

async function getToken() {
  try {
    const stored = await browser.storage.local.get(TOKEN_STORAGE_KEY);
    return stored[TOKEN_STORAGE_KEY] || "";
  } catch (err) {
    return "";
  }
}

async function setToken(token) {
  try {
    await browser.storage.local.set({ [TOKEN_STORAGE_KEY]: token });
  } catch (err) {
    /* 忽略 */
  }
}

async function ensurePaired() {
  const existing = await getToken();
  if (existing) return existing;
  try {
    const response = await browser.runtime.sendNativeMessage(SDM_APP_ID, { op: "pair" });
    if (response && response.ok && response.token) {
      await setToken(response.token);
      return response.token;
    }
  } catch (err) {
    // Safari Web Extension Handler 没装 / 不响应
  }
  return "";
}

// -- 传输 ------------------------------------------------------------

async function sendToApp(message) {
  if (message.op !== "pair") {
    const token = message.token || (await ensurePaired());
    if (!token) {
      return { ok: false, error: "not_paired" };
    }
    message.token = token;
  }

  try {
    const response = await browser.runtime.sendNativeMessage(SDM_APP_ID, message);
    if (response && response.ok) return response;
    // 别的错误（协议不支持、token 不对）不换通道重试——换了也一样错，
    // 而且「协议不支持」必须原样传回去：扩展靠它决定放行给浏览器。
    // 只有「应用没起来」才值得再走 HTTP 通道（那台机器上可能只开着
    // GUI，守护进程没跑）。
    if (response && response.error !== "app_unavailable") return response;
  } catch (err) {
    // 原生宿主没装 / 不可用 → 落到 HTTP 通道
  }

  if (message.op === "pair") {
    return { ok: false, error: "no_native_handler" };
  }
  return sendViaHttp(message);
}

async function sendViaHttp(message) {
  const token = message.token || "";
  if (!token) {
    return { ok: false, error: "not_paired" };
  }
  const body = {
    url: message.url || "",
    filename: message.filename || "",
    referer: message.referer || "",
    cookie: message.cookie || "",
    user_agent: message.user_agent || "",
    method: message.method || "GET",
    post_data: message.post_data || "",
  };
  try {
    const response = await fetch(SDM_HTTP_ENDPOINT, {
      method: "POST",
      headers: {
        "Content-Type": "application/json; charset=utf-8",
        "X-SDM-Token": token,
      },
      body: JSON.stringify(body),
    });
    const data = await response.json();
    if (data && data.ok) {
      const result = data.result || {};
      return { ok: true, task_id: result.task_id || "", url: result.url || body.url };
    }
    return { ok: false, error: (data && data.error) || "http_error" };
  } catch (err) {
    return { ok: false, error: "http_error" };
  }
}

// -- 派单 ------------------------------------------------------------

/**
 * 手动派单。
 *
 * Alt 逃生舱在这里也生效：用户按着 Alt 点过这个链接，说明他想用
 * Safari 自己的下载器——这时候不抢。
 */
async function dispatchManually(url, referer) {
  if (!url) {
    return { ok: false, error: "missing_url" };
  }
  if (isUnsupportedScheme(url)) {
    return { ok: false, error: "unsupported_scheme" };
  }
  if (!isSupportedScheme(url)) {
    return { ok: false, error: "unsupported_scheme" };
  }
  const altStamp = altClicks.get(url);
  if (altStamp && now() - altStamp < ALT_SKIP_TTL_MS) {
    return { ok: false, error: "alt_pressed" };
  }

  const message = {
    op: "add",
    url: url,
    filename: "",
    referer: referer || "",
    cookie: await readCookie(url),
    user_agent: "",
    method: "GET",
    post_data: "",
  };
  return sendToApp(message);
}

async function readCookie(url) {
  if (!browser.cookies || !browser.cookies.getAll) return "";
  try {
    const cookies = await browser.cookies.getAll({ url: url });
    if (!cookies || !cookies.length) return "";
    return cookies.map((c) => `${c.name}=${c.value}`).join("; ");
  } catch (err) {
    return "";
  }
}

// -- 事件接线 --------------------------------------------------------

function wireContextMenu() {
  browser.contextMenus.create({
    id: "sdm-send-to-downloader",
    title: "发送到下载器（SDM）",
    contexts: ["link", "image", "video", "audio", "selection"],
  });
  browser.contextMenus.onClicked.addListener((info, tab) => {
    const url = info.linkUrl || info.srcUrl || "";
    if (!url) return;
    const referer = (tab && tab.url) || info.pageUrl || "";
    dispatchManually(url, referer).then((response) => {
      notify(response);
    });
  });
}

function notify(response) {
  try {
    if (response && response.ok) {
      browser.runtime.sendMessage({ type: "sdm:notify", text: "已发送到下载器" });
    } else {
      const text = response && response.error ? response.error : "发送失败";
      browser.runtime.sendMessage({ type: "sdm:notify", text: `发送失败：${text}` });
    }
  } catch (err) {
    /* 没有弹窗打开时忽略 */
  }
}

function wireMessages() {
  browser.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (!msg) return undefined;

    // 内容脚本报告「按着 Alt 点了一下」
    if (msg.type === "sdm:altClick") {
      if (msg.altKey && msg.url) altClicks.set(msg.url, now());
      return undefined;
    }

    // 弹窗问状态
    if (msg.type === "sdm:status") {
      ensurePaired().then((token) => {
        sendResponse({ paired: Boolean(token) });
      });
      return true;
    }

    // 弹窗要派单
    if (msg.type === "sdm:send") {
      dispatchManually(msg.url || "", msg.referer || "").then((response) => {
        sendResponse(response);
      });
      return true;
    }

    return undefined;
  });
}

function wireStartup() {
  browser.runtime.onInstalled.addListener(() => {
    try {
      const removing = browser.contextMenus.removeAll();
      if (removing && typeof removing.then === "function") {
        removing.then(wireContextMenu, wireContextMenu);
      } else {
        wireContextMenu();
      }
    } catch (err) {
      wireContextMenu();
    }
    ensurePaired();
  });
}

wireMessages();
wireStartup();
try {
  wireContextMenu();
} catch (err) {
  /* 菜单建不了不影响别的 */
}
ensurePaired();
