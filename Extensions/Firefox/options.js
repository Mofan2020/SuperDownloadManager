// SDM 扩展选项页：配对 token 的查看 / 手工填写。
//
// 正常情况下不需要动这里——扩展启动时会自动用 Native Messaging
// 配对。只有在浏览器里没装原生宿主（Firefox 常见）、
// 只能走回环 HTTP 通道时，才需要把应用里的 token 粘进来。

const api = typeof browser !== "undefined" ? browser : chrome;
const TOKEN_STORAGE_KEY = "sdmPairToken";

const tokenInput = document.getElementById("token");
const statusBox = document.getElementById("status");

function showStatus(text, isError) {
  statusBox.textContent = text;
  statusBox.style.color = isError ? "#dc2626" : "#16a34a";
}

async function refresh() {
  const stored = await api.storage.local.get(TOKEN_STORAGE_KEY);
  const token = stored[TOKEN_STORAGE_KEY] || "";
  tokenInput.value = token;
  if (token) {
    showStatus("已配对（回环 HTTP 通道可用）。", false);
  } else {
    showStatus("尚未配对。装了原生宿主的话扩展会自动配对。", true);
  }
}

document.getElementById("save").addEventListener("click", async () => {
  const token = tokenInput.value.trim();
  if (!token) {
    await api.storage.local.remove(TOKEN_STORAGE_KEY);
    showStatus("已清除配对 token。", false);
    return;
  }
  await api.storage.local.set({ [TOKEN_STORAGE_KEY]: token });
  showStatus("已保存。", false);
});

document.getElementById("clear").addEventListener("click", async () => {
  await api.storage.local.remove(TOKEN_STORAGE_KEY);
  tokenInput.value = "";
  showStatus("已清除配对 token。", false);
});

refresh();
