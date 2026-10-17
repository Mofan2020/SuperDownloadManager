// SDM Safari Web Extension background script.
// Same dispatch path as Firefox — Safari just doesn't have
// download interception, so we only do "user clicks our hover
// button" and rely on manual download triggering otherwise.

const SDM_DISPATCH = "http://127.0.0.1:7955/add";

async function dispatchToSDM(message) {
  const params = new URLSearchParams({
    url: message.url || "",
    filename: message.suggestedFilename || "",
    referer: message.referer || ""
  });
  try {
    const ctrl = new AbortController();
    const tid = setTimeout(() => ctrl.abort(), 4000);
    const res = await fetch(`${SDM_DISPATCH}?${params.toString()}`, {
      method: "GET",
      signal: ctrl.signal
    });
    clearTimeout(tid);
    return res.ok;
  } catch (e) {
    return false;
  }
}

browser.runtime.onMessage.addListener(async (msg) => {
  if (msg?.type === "sniff") {
    return await dispatchToSDM(msg);
  }
  return false;
});
