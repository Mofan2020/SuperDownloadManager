// SDM Safari Web Extension content script — same shape as Firefox.
(function () {
  "use strict";
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
      "pointer-events: auto"
    ].join(";");
    btn.addEventListener("click", async (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      if (!currentTarget) return;
      const url = currentTarget.src || currentTarget.href || "";
      if (!url) return;
      try {
        const res = await browser.runtime.sendMessage({
          type: "sniff",
          url,
          suggestedFilename: currentTarget.download || "",
          referer: location.href
        });
        if (res) {
          btn.textContent = "✓ Sent";
          setTimeout(() => { btn.style.display = "none"; }, 700);
        }
      } catch (e) {
        btn.textContent = "✗";
      }
    });
    document.body.appendChild(btn);
    return btn;
  }

  function sniffable(el) {
    if (!el) return false;
    if (el.tagName === "A" && el.href && /\.(zip|dmg|pkg|iso|exe|pdf|mp4|mkv|mp3|flac|rar|7z|tar|gz)$/i.test(el.href)) return true;
    if (el.tagName === "VIDEO" || el.tagName === "AUDIO") {
      const src = el.src || el.querySelector("source")?.src;
      return Boolean(src);
    }
    return false;
  }

  document.addEventListener("mouseover", (ev) => {
    const t = ev.target;
    if (!sniffable(t)) return;
    currentTarget = t;
    const r = t.getBoundingClientRect();
    const b = ensureButton();
    b.style.left = `${r.right + 6}px`;
    b.style.top  = `${r.top + 2}px`;
    b.style.display = "block";
  }, true);

  document.addEventListener("mouseout", (ev) => {
    if (currentTarget === ev.target) {
      currentTarget = null;
      if (btn) btn.style.display = "none";
    }
  }, true);
})();
