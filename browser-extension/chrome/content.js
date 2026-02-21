// Injected into every lcsc.com page.
// - On product detail pages: adds a button next to the LCSC part number.
// - On search / category pages: adds an inline button to every product row.

const BTN_CLASS   = "kicad-import-btn";
const FLOATING_ID = "kicad-import-floating";

// ── helpers ───────────────────────────────────────────────────────────────────

function lcscIdFromUrl(url) {
  // Match C+digits before .html, preceded by _ or / to avoid false positives.
  // Handles both: /product-detail/Category_Brand_C178367.html
  //           and: /product-detail/C2980306.html
  const m = url.match(/[/_](C\d+)\.html/);
  return m ? m[1] : null;
}

const SKIP_TAGS = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "META", "HEAD", "TEMPLATE"]);

// Finds the innermost *visible* rendered element whose trimmed text is exactly `text`.
// Skips script/style/meta tags where the ID might appear as JSON data.
function findTextElement(text) {
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      const p = node.parentElement;
      if (!p || SKIP_TAGS.has(p.tagName)) return NodeFilter.FILTER_SKIP;
      // Must be a visible, rendered element
      const style = window.getComputedStyle(p);
      if (style.display === "none" || style.visibility === "hidden") return NodeFilter.FILTER_SKIP;
      return node.textContent.trim() === text
        ? NodeFilter.FILTER_ACCEPT
        : NodeFilter.FILTER_SKIP;
    },
  });
  const node = walker.nextNode();
  return node ? node.parentElement : null;
}

// Scrape manufacturer, MPN, key attributes from the current LCSC detail page.
// Falls back gracefully — always returns an object, empty strings if nothing found.
function scrapePartInfo(lcscId) {
  const info = { manufacturer: "", mpn: "", attributes: [] };

  // ── 1. URL parsing (fast, works for most parts) ───────────────────────────
  // URL pattern: /product-detail/Category_Mfr_MPN_Attrs_C12345.html
  //           or /product-detail/Category_Mfr-MPN_C12345.html
  const pathMatch = location.pathname.match(/\/product-detail\/(.+?)[/_]C\d+\.html/);
  if (pathMatch) {
    const segs = pathMatch[1].split("_");
    if (segs.length >= 3) {
      // e.g. ["Solid-Polymer-Electrolytic-Capacitor", "PANASONIC", "25SVPF330M", "330uF-25V"]
      info.manufacturer = segs[segs.length - 2].replace(/-/g, " ");
      info.mpn          = segs[segs.length - 1]; // last before C-id might be key attrs
      // If last segment looks like a spec (contains digits + units) treat it as attrs
      if (/\d/.test(info.mpn) && segs.length >= 4) {
        info.attributes.push(info.mpn.replace(/-/g, " "));
        info.mpn = segs[segs.length - 2];
        info.manufacturer = segs[segs.length - 3].replace(/-/g, " ");
      }
    } else if (segs.length === 2) {
      // e.g. ["General-Purpose-Transistors", "onsemi-MMBT2222ALT1G"]
      const hi = segs[1].indexOf("-");
      if (hi > 0) {
        info.manufacturer = segs[1].slice(0, hi);
        info.mpn          = segs[1].slice(hi + 1);
      }
    }
  }

  // ── 2. DOM scraping (more accurate when available) ────────────────────────

  // Manufacturer — LCSC puts it near the top of the product page
  const mfgEl = (
    document.querySelector('[class*="manufactor"] a') ||
    document.querySelector('[class*="brand"] a') ||
    document.querySelector('[class*="manufacturer"] a') ||
    document.querySelector('[class*="mfr-name"]') ||
    document.querySelector('[class*="manufactor"]')
  );
  if (mfgEl?.textContent.trim()) {
    info.manufacturer = mfgEl.textContent.trim();
  }

  // MPN — try common selectors
  const mpnEl = (
    document.querySelector('[class*="product-mpn"]') ||
    document.querySelector('[class*="mfr-part"]') ||
    document.querySelector('[class*="mpn"]')
  );
  if (mpnEl?.textContent.trim()) {
    info.mpn = mpnEl.textContent.trim();
  }

  // Key attributes — LCSC renders them in a table under "Key Attributes"
  // Try to find the section header then its table
  const allText = [...document.querySelectorAll("h2, h3, h4, div, span")];
  const keyAttrHeader = allText.find(
    (el) =>
      el.children.length === 0 &&
      /key\s*attr/i.test(el.textContent)
  );

  const attrTable = keyAttrHeader
    ? keyAttrHeader.closest("section, div")?.querySelector("table") ||
      keyAttrHeader.nextElementSibling
    : document.querySelector('[class*="key-attr"] table') ||
      document.querySelector('[class*="keyAttr"] table') ||
      document.querySelector('[class*="key_attr"] table');

  if (attrTable) {
    attrTable.querySelectorAll("tr").forEach((row) => {
      const cells = row.querySelectorAll("td");
      if (cells.length >= 2) {
        const key = cells[0].textContent.trim().replace(/:$/, "");
        const val = cells[1].textContent.trim();
        if (key && val) info.attributes.push(`${key}: ${val}`);
      }
    });
  }

  // Build the description string
  const parts = [];
  if (info.manufacturer && info.mpn) parts.push(`${info.manufacturer} ${info.mpn}`);
  else if (info.mpn) parts.push(info.mpn);
  parts.push(`LCSC: ${lcscId}`);
  if (info.attributes.length > 0) parts.push(info.attributes.slice(0, 6).join(" | "));

  info.description = parts.join(" · ");
  return info;
}

function sendImport(lcscId, description, onResult) {
  chrome.runtime.sendMessage({ type: "import", lcscId, description }, onResult);
}

// ── button factory ────────────────────────────────────────────────────────────

function makeButton(lcscId, compact = false) {
  const btn = document.createElement("button");
  btn.className = BTN_CLASS;
  btn.dataset.lcscId = lcscId;
  btn.textContent = compact ? "→ KiCad" : "Import to KiCad";

  Object.assign(btn.style, {
    background: "#2d6a4f",
    color: "#fff",
    border: "none",
    borderRadius: "4px",
    padding: compact ? "3px 8px" : "6px 12px",
    fontSize: compact ? "11px" : "12px",
    fontWeight: "600",
    cursor: "pointer",
    whiteSpace: "nowrap",
    lineHeight: "1.4",
    transition: "background 0.2s",
    flexShrink: "0",
    verticalAlign: "middle",
    display: "inline-block",
  });

  btn.addEventListener("mouseenter", () => {
    if (!btn.disabled) btn.style.background = "#1b4332";
  });
  btn.addEventListener("mouseleave", () => {
    if (!btn.disabled) btn.style.background = "#2d6a4f";
  });

  btn.addEventListener("click", (e) => {
    e.preventDefault();
    e.stopPropagation();

    btn.textContent = "…";
    btn.disabled = true;
    btn.style.background = "#888";
    btn.style.cursor = "default";

    const partInfo = scrapePartInfo(lcscId);
    sendImport(lcscId, partInfo.description, (data) => {
      if (chrome.runtime.lastError || !data) {
        setButtonState(btn, "error", compact ? "✗ offline" : "✗ Server offline", compact);
        return;
      }
      if (data.success) {
        setButtonState(btn, "ok", compact ? "✓" : "✓ Imported", compact);
        showSuccessToast(data.symbol_name || lcscId, lcscId);
      } else {
        setButtonState(btn, "error", compact ? "✗" : "✗ Failed", compact);
        console.error("[easyeda2kicad]", data.stderr || data.error);
      }
    });
  });

  return btn;
}

function setButtonState(btn, state, label, compact) {
  btn.textContent = label;
  btn.style.cursor = "default";

  if (state === "ok") {
    btn.style.background = "#1b4332";
  } else {
    btn.style.background = "#9b2226";
    setTimeout(() => {
      btn.textContent = compact ? "→ KiCad" : "Import to KiCad";
      btn.disabled = false;
      btn.style.background = "#2d6a4f";
      btn.style.cursor = "pointer";
    }, 4000);
  }
}

// ── success toast ─────────────────────────────────────────────────────────────

function showSuccessToast(symbolName, lcscId) {
  document.getElementById("kicad-toast")?.remove();

  const toast = document.createElement("div");
  toast.id = "kicad-toast";
  Object.assign(toast.style, {
    position: "fixed",
    bottom: "24px",
    right: "24px",
    zIndex: "1000000",
    background: "#1b4332",
    color: "#fff",
    borderRadius: "8px",
    padding: "16px 20px",
    boxShadow: "0 4px 20px rgba(0,0,0,0.35)",
    fontFamily: "system-ui, sans-serif",
    fontSize: "14px",
    lineHeight: "1.5",
    maxWidth: "340px",
    display: "flex",
    flexDirection: "column",
    gap: "10px",
    animation: "kicad-slidein 0.25s ease",
  });

  // inject keyframe once
  if (!document.getElementById("kicad-anim")) {
    const style = document.createElement("style");
    style.id = "kicad-anim";
    style.textContent = `
      @keyframes kicad-slidein {
        from { opacity:0; transform: translateY(12px); }
        to   { opacity:1; transform: translateY(0); }
      }
    `;
    document.head.appendChild(style);
  }

  // Header row
  const header = document.createElement("div");
  header.style.cssText = "display:flex; align-items:center; gap:8px; font-size:15px; font-weight:700;";
  header.innerHTML = `<span style="font-size:18px">✓</span> Imported to KiCad`;
  toast.appendChild(header);

  // Symbol name row + copy button
  const nameRow = document.createElement("div");
  nameRow.style.cssText = "display:flex; align-items:center; gap:8px;";

  const nameBox = document.createElement("code");
  nameBox.textContent = symbolName;
  Object.assign(nameBox.style, {
    background: "rgba(255,255,255,0.15)",
    borderRadius: "4px",
    padding: "3px 8px",
    fontSize: "13px",
    flex: "1",
    overflow: "hidden",
    textOverflow: "ellipsis",
    whiteSpace: "nowrap",
  });

  const copyBtn = document.createElement("button");
  copyBtn.textContent = "Copy";
  Object.assign(copyBtn.style, {
    background: "rgba(255,255,255,0.2)",
    color: "#fff",
    border: "none",
    borderRadius: "4px",
    padding: "3px 10px",
    fontSize: "12px",
    cursor: "pointer",
    flexShrink: "0",
  });
  copyBtn.addEventListener("click", () => {
    navigator.clipboard.writeText(symbolName).then(() => {
      copyBtn.textContent = "✓ Copied";
      setTimeout(() => (copyBtn.textContent = "Copy"), 2000);
    });
  });

  nameRow.appendChild(nameBox);
  nameRow.appendChild(copyBtn);
  toast.appendChild(nameRow);

  // LCSC ID sub-line
  const sub = document.createElement("div");
  sub.style.cssText = "font-size:12px; opacity:0.75;";
  sub.textContent = `LCSC: ${lcscId}  ·  library: easyeda2kicad`;
  toast.appendChild(sub);

  // Close button (×)
  const closeBtn = document.createElement("button");
  closeBtn.textContent = "×";
  Object.assign(closeBtn.style, {
    position: "absolute",
    top: "10px",
    right: "12px",
    background: "none",
    border: "none",
    color: "#fff",
    fontSize: "18px",
    cursor: "pointer",
    lineHeight: "1",
    opacity: "0.7",
  });
  closeBtn.addEventListener("click", () => toast.remove());
  toast.style.position = "fixed"; // ensure absolute child works
  toast.appendChild(closeBtn);

  document.body.appendChild(toast);

  // Auto-dismiss after 9 seconds
  setTimeout(() => toast?.remove(), 9000);
}

// ── position tracker ──────────────────────────────────────────────────────────
// The button lives on document.body (outside React's tree) but uses RAF to
// continuously match the viewport position of the part-name element so it
// appears inline next to it without being subject to React re-renders.

let rafId = null;

function findPartNameElement() {
  // LCSC puts the MPN in a prominent heading at the top of the detail page.
  const candidates = [
    document.querySelector("h1"),
    document.querySelector('[class*="product-title"]'),
    document.querySelector('[class*="productTitle"]'),
    document.querySelector('[class*="part-name"]'),
    document.querySelector('[class*="partName"]'),
    document.querySelector('[class*="detail"] h2'),
  ];
  for (const el of candidates) {
    if (!el) continue;
    const text = el.textContent.trim();
    if (text.length < 2 || text.length > 200) continue;
    const r = el.getBoundingClientRect();
    if (r.width > 0 && r.height > 0) return el;
  }
  return null;
}

function startTracking(btn) {
  if (rafId) cancelAnimationFrame(rafId);

  function tick() {
    if (!document.getElementById(FLOATING_ID)) return; // cleaned up
    const anchor = findPartNameElement();
    if (anchor) {
      const r   = anchor.getBoundingClientRect();
      const top = r.top + r.height / 2 - btn.offsetHeight / 2;
      const leftOfAnchor = r.right + 12;

      if (leftOfAnchor + btn.offsetWidth + 16 <= window.innerWidth) {
        // Enough room to the right of the heading
        btn.style.top    = `${Math.max(8, top)}px`;
        btn.style.left   = `${leftOfAnchor}px`;
        btn.style.bottom = "auto";
        btn.style.right  = "auto";
      } else {
        // Not enough room — sit just below the heading, left-aligned
        btn.style.top    = `${r.bottom + 8}px`;
        btn.style.left   = `${r.left}px`;
        btn.style.bottom = "auto";
        btn.style.right  = "auto";
      }
    }
    rafId = requestAnimationFrame(tick);
  }

  rafId = requestAnimationFrame(tick);
}

// ── cleanup ───────────────────────────────────────────────────────────────────

function cleanup() {
  if (rafId) { cancelAnimationFrame(rafId); rafId = null; }
  document.getElementById(FLOATING_ID)?.remove();
  document.querySelectorAll(`.${BTN_CLASS}`).forEach((b) => b.remove());
}

// ── detail page ───────────────────────────────────────────────────────────────

function addFloatingButton(lcscId) {
  if (document.getElementById(FLOATING_ID)) return;

  const btn = makeButton(lcscId);
  btn.id = FLOATING_ID;
  Object.assign(btn.style, {
    position: "fixed",
    // Default: bottom-right until the anchor element is found
    bottom: "24px",
    right:  "24px",
    zIndex: "999999",
    boxShadow: "0 2px 8px rgba(0,0,0,0.25)",
  });

  document.body?.appendChild(btn);
  startTracking(btn);
}

function handleDetailPage() {
  const lcscId = lcscIdFromUrl(location.href);
  if (!lcscId) return;
  if (document.getElementById(FLOATING_ID)) return;
  addFloatingButton(lcscId);
}

// ── search / category pages ───────────────────────────────────────────────────

function handleListPage() {
  document.querySelectorAll('a[href*="product-detail"]').forEach((link) => {
    const lcscId = lcscIdFromUrl(link.href);
    if (!lcscId) return;

    const row = link.closest("tr, li, [class*='product'], [class*='item'], [class*='row']");
    const anchor = row || link.parentElement;

    if (anchor.querySelector(`.${BTN_CLASS}`)) return;

    const btn = makeButton(lcscId, true);
    btn.style.marginLeft = "6px";
    link.insertAdjacentElement("afterend", btn);
  });
}

// ── router ────────────────────────────────────────────────────────────────────

function run() {
  if (location.pathname.includes("/product-detail/")) {
    handleDetailPage();
  } else {
    handleListPage();
  }
}

// ── SPA navigation detection ──────────────────────────────────────────────────

function onNavigate() {
  cleanup();
  setTimeout(run, 300);
}

const _push = history.pushState.bind(history);
history.pushState = function (...args) { _push(...args); onNavigate(); };

const _replace = history.replaceState.bind(history);
history.replaceState = function (...args) { _replace(...args); onNavigate(); };

window.addEventListener("popstate", onNavigate);

// ── MutationObserver — catches lazy-loaded content, upgrades floating→inline ──

let debounce;
const observer = new MutationObserver(() => {
  clearTimeout(debounce);
  debounce = setTimeout(run, 400);
});

observer.observe(document.documentElement, { childList: true, subtree: true });

run();
