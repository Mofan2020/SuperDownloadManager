// SDM Firefox extension background script.
// Listens to all completed downloads and asks SDM to take them
// over by cancelling the original and re-dispatching through the
// local socket (127.0.0.1:7955/add).

const SDM_DISPATCH = "http://127.0.0.1:7955/add";

async function dispatchToSDM(downloadItem) {
  const params = new URLSearchParams({
    url: downloadItem.finalUrl || downloadItem.url,
    filename: downloadItem.suggestedFilename || downloadItem.filename || "",
    referer: downloadItem.referrer || ""
  });
  try {
    const ctrl = new AbortController();
    const tid = setTimeout(() => ctrl.abort(), 4000);
    const res = await fetch(`${SDM_DISPATCH}?${params.toString()}`, {
      method: "GET",
      signal: ctrl.signal
    });
    clearTimeout(tid);
    if (!res.ok) {
      console.warn("[SDM dispatch] non-2xx response:", res.status);
      return false;
    }
    return true;
  } catch (e) {
    console.warn("[SDM dispatch] failed:", e);
    return false;
  }
}

// Cancel a download and erase it from the history so it doesn't
// pollute the user's "Downloads" list. Returns a Promise<void>.
async function cancelOriginal(id) {
  try {
    await browser.downloads.cancel(id);
    await browser.downloads.erase({ id });
  } catch (e) {
    console.warn("[SDM] cancel/erase failed:", e);
  }
}

browser.downloads.onCreated.addListener(async (item) => {
  // Skip the small files the user clearly opened intentionally
  // (only on `onDeterminingFilename` we get the right hook; this
  // listener just records the start).
  // The actual takeover happens in `onDeterminingFilename`.
});

browser.downloads.onDeterminingFilename.addListener(async (suggestion, suggest) => {
  // We tell the browser we want to keep the filename (so we can
  // observe the download), then cancel and re-dispatch in
  // onChanged below. The cooperative path is to suggest a
  // unique filename rather than refuse.
  const safeName = suggestion.filename || "download.bin";
  suggest({ filename: safeName, conflictAction: "uniquify" });
  // Don't await — fire and forget, the cancel+dispatch path runs
  // asynchronously once the download is created.
  (async () => {
    // Wait for the download to actually start.
    const id = (await browser.downloads.search({ filenameRegex: safeName }))[0]?.id;
    if (id != null) {
      const ok = await dispatchToSDM({ url: safeName, suggestedFilename: safeName });
      if (ok) {
        await cancelOriginal(id);
      }
    }
  })();
});

// Sniff interactions: when content script dispatches "sniff"
// messages, route them to SDM the same way.
browser.runtime.onMessage.addListener(async (msg) => {
  if (msg?.type !== "sniff") return;
  return dispatchToSDM({
    url: msg.url,
    filename: msg.suggestedFilename || "",
    referer: msg.referer || ""
  });
});
