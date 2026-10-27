// SDM Safari 扩展的弹窗：手动把链接发给下载器。
//
// Safari 没有自动接管（契约 §4.4.5），所以工具栏按钮的作用是
// 「让用户把当前页面的链接手动发出去」——不能自动猜哪个链接是要下的。

const api = typeof browser !== "undefined" ? browser : chrome;

const urlInput = document.getElementById("url");
const statusBox = document.getElementById("status");
const sendButton = document.getElementById("send");
const tokenInput = document.getElementById("token");
const tokenButton = document.getElementById("saveToken");

/** 配对 token 的本地存储键（跟 background.js 里的一致）。 */
const TOKEN_STORAGE_KEY = "sdmPairToken";

function showStatus(text, isError) {
  statusBox.textContent = text;
  statusBox.style.color = isError ? "#dc2626" : "#16a34a";
}

/** 拿当前标签页的 URL 当默认值。 */
async function fillCurrentUrl() {
  try {
    const tabs = await api.tabs.query({ active: true, currentWindow: true });
    if (tabs && tabs.length && tabs[0].url) {
      urlInput.value = tabs[0].url;
      urlInput.select();
    }
  } catch (err) {
    /* 没有 tabs 权限就留空 */
  }
}

async function showPairingStatus() {
  try {
    const status = await api.runtime.sendMessage({ type: "sdm:status" });
    if (status && status.paired) {
      showStatus("已配对。", false);
    } else {
      showStatus("未配对：把应用里的配对 token 粘到下面。", true);
    }
  } catch (err) {
    showStatus("后端没响应（应用开着吗？）。", true);
  }
}

sendButton.addEventListener("click", async () => {
  const url = urlInput.value.trim();
  if (!url) {
    showStatus("先填个链接。", true);
    return;
  }
  sendButton.disabled = true;
  showStatus("发送中…", false);
  try {
    const response = await api.runtime.sendMessage({
      type: "sdm:send",
      url: url,
      referer: "",
    });
    if (response && response.ok) {
      showStatus("已发送到下载器。", false);
    } else {
      const reason = response && response.error ? response.error : "未知错误";
      showStatus(`发送失败：${reason}`, true);
    }
  } catch (err) {
    showStatus("发送失败：后端没响应。", true);
  } finally {
    sendButton.disabled = false;
  }
});

// -- 配对 token ----------------------------------------------------
//
// Safari 没有原生消息宿主，扩展只能走回环 HTTP 通道——而那个通道
// **不开 `/pair`**（端口带着 `Access-Control-Allow-Origin: *`，
// 任何网页都能调，把 token 暴露给网页等于把「谁都能往下载器塞
// 任务」的门打开）。所以 token 只能由用户从应用里复制过来。

async function loadToken() {
  try {
    const stored = await api.storage.local.get(TOKEN_STORAGE_KEY);
    const token = stored[TOKEN_STORAGE_KEY] || "";
    if (tokenInput) tokenInput.value = token;
    return token;
  } catch (err) {
    return "";
  }
}

if (tokenButton) {
  tokenButton.addEventListener("click", async () => {
    const token = (tokenInput?.value || "").trim();
    try {
      if (!token) {
        await api.storage.local.remove(TOKEN_STORAGE_KEY);
        showStatus("已清除配对 token。", false);
        return;
      }
      await api.storage.local.set({ [TOKEN_STORAGE_KEY]: token });
      showStatus("token 已保存。现在可以发链接了。", false);
    } catch (err) {
      showStatus("保存失败：浏览器不让扩展写本地存储。", true);
    }
  });
}

fillCurrentUrl();
loadToken().then(showPairingStatus);
