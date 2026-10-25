// SDM 浏览器接管扩展 —— 后台脚本（Chrome / Edge，Manifest V3）。
//
// 干的事：把浏览器本来要下的文件截下来，交给 SDM 下载器去下。
//
// 三个关键决定：
//
// 1. **先取消浏览器的下载，再派单。**
//    `downloads.onDeterminingFilename` 在下载**开始之前**触发，
//    在这里 `return { cancel: true }`，浏览器就不会把文件下到本地盘
//    ——不然同一个文件会被下两遍。
//    派单失败（应用没开、token 不对、协议不支持）时用
//    `downloads.download()` 把下载还给浏览器：
//    **用户的文件不能凭空消失**。
//
// 2. **blob: / data: 一律放行。**
//    这两种协议浏览器自己能下，而且下载器拿它们没法干活
//    （没有可重放的 HTTP 请求）。以前这里没判断，扩展把浏览器的
//    下载取消了、Python 又拒绝接单——用户的文件直接没了。
//
// 3. **按住 Alt（macOS 是 Option）点击时不接管。**
//    逃生舱：用户想用浏览器自己的下载器时，按住 Alt 点一下就行。
//
// **stdio 原生宿主是主通道**（契约 §4.1），回环 HTTP 只是降级。

const SDM_APP_ID = "com.skyc8266.superdownloadmanager";
const SDM_HTTP_ENDPOINT = "http://127.0.0.1:7955/add";
const TOKEN_STORAGE_KEY = "sdmPairToken";

// -- 协议过滤 --------------------------------------------------------
//
// 不支持的协议：直接放行给浏览器，既不派单也不取消。
const UNSUPPORTED_SCHEMES = ["blob:", "data:"];
// 支持的协议：只有这两种下载器才拿得动。
const SUPPORTED_SCHEMES = ["http:", "https:"];

// Alt 逃生舱的记录保留多久。
//
// 从「按下 Alt 点击」到「下载真正开始」中间可能隔一两秒
// （重定向、慢服务器）。太短会漏，太长又会误伤——用户按 Alt 点了 A，
// 半分钟后正常点了 B，结果 B 也不接管了。
const ALT_SKIP_TTL_MS = 15000;

// 请求缓存（webRequest 观察到的上下文）的保留时间与上限。
const REQUEST_TTL_MS = 30000;
const REQUEST_CACHE_MAX = 500;

// POST 体缓存的上限。太大的一次上传不该塞进扩展存储。
const MAX_POST_BODY_BYTES = 64 * 1024;

/** 最近一次 webRequest 观察到的请求上下文：url -> 上下文。 */
const requestCache = new Map();
/** Alt 点击过的链接：url -> 时间戳。 */
const altClicks = new Map();
/** 各标签页最近一次 Alt 点击：tabId -> { ts, documentUrl }。 */
const altClicksByTab = new Map();
/** 内容脚本截到的 POST 体：`<url> <method>` -> body。 */
const postBodies = new Map();

// -- 小工具 ----------------------------------------------------------

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

/** 这个协议是不是浏览器自己能搞定、下载器接不了的。 */
function isUnsupportedScheme(url) {
  return UNSUPPORTED_SCHEMES.indexOf(schemeOf(url)) >= 0;
}

/** 这个协议下载器能不能接。 */
function isSupportedScheme(url) {
  return SUPPORTED_SCHEMES.indexOf(schemeOf(url)) >= 0;
}

function basename(path) {
  const text = String(path || "");
  const cut = Math.max(text.lastIndexOf("/"), text.lastIndexOf("\\"));
  return cut >= 0 ? text.slice(cut + 1) : text;
}

function now() {
  return Date.now();
}

// -- 配对 ------------------------------------------------------------

async function getToken() {
  try {
    const stored = await chrome.storage.local.get(TOKEN_STORAGE_KEY);
    return stored[TOKEN_STORAGE_KEY] || "";
  } catch (err) {
    return "";
  }
}

async function setToken(token) {
  try {
    await chrome.storage.local.set({ [TOKEN_STORAGE_KEY]: token });
  } catch (err) {
    /* 存不进去就下次再配对 */
  }
}

/**
 * 跟应用要配对 token。
 *
 * token 是长期凭据（契约 §4.2）：扩展第一次连上应用时用 `{"op":"pair"}`
 * 换一个，之后每条消息都带上。应用侧不匹配就拒绝。
 *
 * **只在原生宿主上配对。** 回环 HTTP 不开 `/pair`——那个端口带着
 * `Access-Control-Allow-Origin: *`，任何网页都能 fetch 到它，
 * 在那儿发 token 等于把 token 贴到网上。
 */
async function ensurePaired() {
  const existing = await getToken();
  if (existing) return existing;
  let response = null;
  try {
    response = await chrome.runtime.sendNativeMessage(SDM_APP_ID, { op: "pair" });
  } catch (err) {
    // 原生宿主没装 / 没启动
    return "";
  }
  if (response && response.ok && response.token) {
    await setToken(response.token);
    return response.token;
  }
  return "";
}

// -- 传输 ------------------------------------------------------------

/**
 * 把一条消息送到应用。
 *
 * 原生宿主优先；宿主不可用时退到回环 HTTP。
 * 两条路都失败才算失败。
 */
async function sendToApp(message) {
  if (message.op !== "pair") {
    const token = message.token || (await ensurePaired());
    if (!token) {
      return { ok: false, error: "not_paired" };
    }
    message.token = token;
  }

  try {
    const response = await chrome.runtime.sendNativeMessage(SDM_APP_ID, message);
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
    return { ok: false, error: "no_native_host" };
  }
  return sendViaHttp(message);
}

/** 降级通道：回环 HTTP。 */
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

// -- 浏览器上下文 ----------------------------------------------------

/** 读 URL 对应的 Cookie，拼成请求头的样子。 */
async function readCookie(url) {
  if (!chrome.cookies || !chrome.cookies.getAll) return "";
  try {
    const cookies = await chrome.cookies.getAll({ url });
    if (!cookies || !cookies.length) return "";
    return cookies.map((c) => `${c.name}=${c.value}`).join("; ");
  } catch (err) {
    return "";
  }
}

function pruneCaches() {
  const cutoff = now() - REQUEST_TTL_MS;
  for (const [key, value] of requestCache) {
    if (value.ts < cutoff) requestCache.delete(key);
  }
  for (const [key, value] of altClicks) {
    if (value < cutoff) altClicks.delete(key);
  }
  if (requestCache.size > REQUEST_CACHE_MAX) {
    const excess = requestCache.size - REQUEST_CACHE_MAX;
    const keys = Array.from(requestCache.keys()).slice(0, excess);
    keys.forEach((key) => requestCache.delete(key));
  }
}

/**
 * 观察请求，攒下 Cookie / UA / Referer / POST 体。
 *
 * **MV3 里不能要 `webRequestBlocking`。** Chrome 从 88 起把这个权限
 * 收窄成「只有策略安装的扩展能用」，普通扩展带这个权限直接装不上。
 * 好在我们要的只是**读**请求头——`onBeforeSendHeaders` 不带
 * `blocking` 也能拿到 `requestHeaders`。要改请求头才需要 blocking，
 * 我们不改。
 *
 * Cookie 拿不到（Chrome 72 起 `onBeforeSendHeaders` 不给 Cookie），
 * 所以另外用 `cookies` 权限去读。
 */
function observeRequests() {
  chrome.webRequest.onBeforeSendHeaders.addListener(
    (details) => {
      pruneCaches();
      const headers = {};
      for (const header of details.requestHeaders || []) {
        headers[String(header.name).toLowerCase()] = header.value;
      }
      requestCache.set(details.url, {
        method: details.method || "GET",
        userAgent: headers["user-agent"] || "",
        referer: headers["referer"] || "",
        tabId: details.tabId,
        documentUrl: details.documentUrl || "",
        initiator: details.initiator || "",
        ts: now(),
      });
    },
    { urls: ["<all_urls>"] },
    ["requestHeaders"]
  );
}

// -- 逃生舱 ----------------------------------------------------------

/**
 * 判断这次下载该不该放行给浏览器。
 *
 * 两条依据：
 * 1. 内容脚本报告的「刚才那一下按着 Alt」——按住 Alt（macOS 是
 *    Option）点击时不接管，这是用户明确表达「我自己要下」的信号。
 * 2. 请求的发起方（initiator）——我们自己发的救援下载不能再接管，
 *    否则会无限循环：接管 → 失败 → 救援 → 又被接管。
 */
function isEscapeHatch(url, info) {
  const stamp = altClicks.get(url);
  if (stamp && now() - stamp < ALT_SKIP_TTL_MS) {
    return true;
  }
  if (info) {
    if (info.initiator && info.initiator.indexOf("chrome-extension://") === 0) {
      return true;
    }
    const tabState = altClicksByTab.get(info.tabId);
    if (
      tabState &&
      now() - tabState.ts < ALT_SKIP_TTL_MS &&
      tabState.documentUrl === info.documentUrl
    ) {
      return true;
    }
  }
  return false;
}

// -- 接管 ------------------------------------------------------------

/**
 * 接管一次下载：派单给应用。
 *
 * 失败了就**把下载还给浏览器**。取消浏览器下载是为了避免下两遍，
 * 不是为了把用户的文件弄丢——应用没开、token 不对、协议不支持，
 * 这些都不该让用户什么都拿不到。
 */
async function captureDownload(item, url) {
  const info = requestCache.get(url) || requestCache.get(item.url) || null;
  const cookie = await readCookie(url);
  const message = {
    op: "add",
    url: url,
    filename: basename(item.suggestedFilename || item.filename || ""),
    referer: item.referrer || (info && info.referer) || "",
    cookie: cookie,
    user_agent: (info && info.userAgent) || "",
    method: (info && info.method) || "GET",
    post_data: lookupPostBody(url, (info && info.method) || "GET"),
  };
  const response = await sendToApp(message);
  if (response && response.ok) {
    setBadge("✓", "#16a34a");
    return true;
  }
  setBadge("!", "#dc2626");
  await rescue(item, url, info);
  return false;
}

/** 找内容脚本截到的 POST 体。 */
function lookupPostBody(url, method) {
  const key = `${url} ${String(method || "GET").toUpperCase()}`;
  return postBodies.get(key) || "";
}

/** 把下载还给浏览器——派单失败时的保险。 */
async function rescue(item, url, info) {
  try {
    const options = { url: url, conflictAction: "uniquify" };
    const headers = {};
    if (info && info.userAgent) headers["User-Agent"] = info.userAgent;
    if (item.referrer) headers["Referer"] = item.referrer;
    if (Object.keys(headers).length) options.headers = headers;
    await chrome.downloads.download(options);
  } catch (err) {
    console.warn("[SDM] 救援下载失败:", err);
  }
}

function setBadge(text, color) {
  try {
    chrome.action.setBadgeText({ text: text });
    if (color) chrome.action.setBadgeBackgroundColor({ color: color });
    setTimeout(() => chrome.action.setBadgeText({ text: "" }), 3000);
  } catch (err) {
    /* 没有 action API 的浏览器忽略 */
  }
}

// -- 事件接线 --------------------------------------------------------

/**
 * 下载开始前拦一道。
 *
 * **这个监听器必须同步。** Chrome 的 `onDeterminingFilename` 不接受
 * Promise——返回 Promise 会被当成「没意见」，下载照常进行。
 * 所以这里只做同步能做的判断（协议、Alt 逃生舱），派单放到后台。
 */
function wireDownloads() {
  chrome.downloads.onDeterminingFilename.addListener((item) => {
    const url = item.finalUrl || item.url || "";

    // 不支持的协议：放行。不派单，也不取消。
    if (isUnsupportedScheme(url)) return undefined;
    if (!isSupportedScheme(url)) return undefined;

    // Alt 逃生舱：放行。
    const info = requestCache.get(url) || requestCache.get(item.url) || null;
    if (isEscapeHatch(url, info)) return undefined;

    captureDownload(item, url);
    return { cancel: true };
  });
}

/** 右键菜单：手动「发送到下载器」。 */
function wireContextMenu() {
  chrome.contextMenus.create({
    id: "sdm-send-to-downloader",
    title: "发送到下载器（SDM）",
    contexts: ["link", "image", "video", "audio", "selection"],
  });
  chrome.contextMenus.onClicked.addListener((info, tab) => {
    const url = info.linkUrl || info.srcUrl || info.pageUrl || "";
    if (!url || !isSupportedScheme(url)) return;
    dispatchManually(url, tab && tab.url ? tab.url : info.pageUrl || "");
  });
}

/** 手动派单（右键菜单 / 工具栏弹窗）。 */
async function dispatchManually(url, referer) {
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
  const response = await sendToApp(message);
  if (response && response.ok) {
    setBadge("✓", "#16a34a");
  } else {
    setBadge("!", "#dc2626");
  }
  return response;
}

function wireMessages() {
  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (!msg) return undefined;

    // 内容脚本报告「按着 Alt 点了一下」
    if (msg.type === "sdm:altClick") {
      // altKey 明确带过来：内容脚本已经判断过用户是不是按着
      // Alt（macOS 上是 Option）点的，这里只认这一个信号。
      if (msg.altKey && msg.url) altClicks.set(msg.url, now());
      const tabId = sender && sender.tab ? sender.tab.id : -1;
      altClicksByTab.set(tabId, { ts: now(), documentUrl: msg.documentUrl || "" });
      return undefined;
    }

    // 内容脚本截到的 POST 体
    if (msg.type === "sdm:postCandidate") {
      const body = String(msg.body || "");
      if (body && body.length <= MAX_POST_BODY_BYTES) {
        const key = `${msg.url} ${String(msg.method || "GET").toUpperCase()}`;
        postBodies.set(key, body);
      }
      return undefined;
    }

    // 弹窗 / 选项页问状态
    if (msg.type === "sdm:status") {
      ensurePaired().then((token) => {
        sendResponse({ paired: Boolean(token), token: token ? "********" : "" });
      });
      return true;
    }

    // 弹窗 / 选项页 / 悬停按钮要手动派单
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
  chrome.runtime.onInstalled.addListener(() => {
    try {
      chrome.contextMenus.removeAll(() => {
        wireContextMenu();
      });
    } catch (err) {
      wireContextMenu();
    }
    ensurePaired();
  });
  chrome.runtime.onStartup.addListener(() => {
    ensurePaired();
  });
}

observeRequests();
wireDownloads();
wireMessages();
wireStartup();
try {
  wireContextMenu();
} catch (err) {
  /* 菜单建不了不影响下载接管 */
}
ensurePaired();
