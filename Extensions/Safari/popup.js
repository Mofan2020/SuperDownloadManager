// SDM Safari 扩展的弹窗：手动把链接发给下载器。
//
// Safari 没有自动接管（契约 §4.4.5），所以工具栏按钮的作用是
// 「让用户把当前页面的链接手动发出去」——不能自动猜哪个链接是要下的。

const api = typeof browser !== "undefined" ? browser : chrome;

const urlInput = document.getElementById("url");
const statusBox = document.getElementById("status");
const sendButton = document.getElementById("send");

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
      showStatus("未配对：应用里的原生消息没响应，只能走回环 HTTP 通道。", true);
    }
  } catch (err) {
    showStatus("后端没响应。", true);
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

fillCurrentUrl();
showPairingStatus();
