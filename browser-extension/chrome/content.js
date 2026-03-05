// Injected into every lcsc.com page.
// - On product detail pages: adds a button next to the LCSC part number.
// - On search / category pages: adds an inline button to every product row.

const BTN_CLASS      = "kicad-import-btn";
const FLOATING_ID    = "kicad-import-floating";
const UPDATE_BAR_ID  = "kicad-update-bar";

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

// Scrape manufacturer, MPN, key attributes, description, and stock from the
// current LCSC detail page.  Falls back gracefully — always returns an object.
function scrapePartInfo(lcscId) {
  const info = { manufacturer: "", mpn: "", attributes: [], description: "", inStock: "" };

  // ── 1. URL parsing (fast, works for most parts) ───────────────────────────
  const pathMatch = location.pathname.match(/\/product-detail\/(.+?)[/_]C\d+\.html/);
  if (pathMatch) {
    const segs = pathMatch[1].split("_");
    if (segs.length >= 3) {
      info.manufacturer = segs[segs.length - 2].replace(/-/g, " ");
      info.mpn          = segs[segs.length - 1];
      if (/\d/.test(info.mpn) && segs.length >= 4) {
        info.attributes.push(info.mpn.replace(/-/g, " "));
        info.mpn = segs[segs.length - 2];
        info.manufacturer = segs[segs.length - 3].replace(/-/g, " ");
      }
    } else if (segs.length === 2) {
      const hi = segs[1].indexOf("-");
      if (hi > 0) {
        info.manufacturer = segs[1].slice(0, hi);
        info.mpn          = segs[1].slice(hi + 1);
      }
    }
  }

  // ── 2. DOM scraping ────────────────────────────────────────────────────────

  // Manufacturer
  const mfgEl = (
    document.querySelector('[class*="manufactor"] a') ||
    document.querySelector('[class*="brand"] a') ||
    document.querySelector('[class*="manufacturer"] a') ||
    document.querySelector('[class*="mfr-name"]') ||
    document.querySelector('[class*="manufactor"]')
  );
  if (mfgEl?.textContent.trim()) info.manufacturer = mfgEl.textContent.trim();

  // MPN
  const mpnEl = (
    document.querySelector('[class*="product-mpn"]') ||
    document.querySelector('[class*="mfr-part"]') ||
    document.querySelector('[class*="mpn"]')
  );
  if (mpnEl?.textContent.trim()) info.mpn = mpnEl.textContent.trim();

  // ── 3. Key Attributes table ────────────────────────────────────────────────
  const allEls = [...document.querySelectorAll("h2, h3, h4, div, span")];
  const keyAttrHeader = allEls.find(
    (el) => el.children.length === 0 && /key\s*attr/i.test(el.textContent)
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

  // ── 4. In Stock count from LCSC detail page ────────────────────────────────
  // LCSC shows stock near the "Add to Cart" section.
  const stockCandidates = [
    '[class*="stock"] [class*="num"]',
    '[class*="stock"] [class*="count"]',
    '[class*="StockNum"]',
    '[class*="stock-num"]',
    '[class*="inventory"] [class*="num"]',
    '[class*="available"] [class*="num"]',
  ];
  for (const sel of stockCandidates) {
    const el = document.querySelector(sel);
    if (el) {
      const num = el.textContent.trim().replace(/[^\d]/g, "");
      if (num) { info.inStock = num; break; }
    }
  }
  // Fallback: walk visible text nodes looking for "In Stock" label + adjacent number
  if (!info.inStock) {
    const stockLabel = [...document.querySelectorAll("span, td, div, p")].find(
      (el) => el.children.length === 0 && /^in\s*stock$/i.test(el.textContent.trim())
    );
    if (stockLabel) {
      const sib = stockLabel.nextElementSibling || stockLabel.parentElement?.nextElementSibling;
      const num = sib?.textContent.trim().replace(/[^\d]/g, "");
      if (num) info.inStock = num;
    }
  }

  // ── 5. Build the description string ───────────────────────────────────────
  // Prefer a clean product description element from the page if available.
  const descEl = (
    document.querySelector('[class*="product-intro"] [class*="desc"]') ||
    document.querySelector('[class*="ProductInfo"] [class*="desc"]') ||
    document.querySelector('[class*="goods-intro"] p') ||
    document.querySelector('[class*="detail-desc"]')
  );
  if (descEl?.textContent.trim().length > 10) {
    info.description = descEl.textContent.trim();
  }

  // If no element found, build from key attributes (best available on detail pages)
  if (!info.description && info.attributes.length > 0) {
    const head = [info.manufacturer, info.mpn].filter(Boolean).join(" ");
    info.description = [head, ...info.attributes.slice(0, 8)].filter(Boolean).join(", ");
  }

  // Last resort: manufacturer + MPN
  if (!info.description) {
    info.description = [info.manufacturer, info.mpn, `LCSC: ${lcscId}`]
      .filter(Boolean).join(" ");
  }

  return info;
}

// Matches price-like cell values (unit prices, price ranges, currency symbols).
const PRICE_RE = /^[$€£¥]|^\d+\.\d{3,}|[\d.]+\s*~\s*[\d.]+|\/(pc|pcs|piece|ea)\b/i;

// Scrape description and stock count from the JLCPCB/LCSC parts table row that
// contains `el` (the element holding the LCSC part number text).
function scrapeJlcpcbRow(el) {
  const info = { description: "", inStock: "" };

  const row = el.closest("tr");
  if (!row) return info;

  const table = row.closest("table");
  if (!table) return info;

  // Detect column positions from the thead.
  let descIdx = -1, stockIdx = -1;
  const headerRow = table.querySelector("thead tr");
  if (headerRow && headerRow !== row) {
    [...headerRow.querySelectorAll("th, td")].forEach((th, i) => {
      const t = th.textContent.trim().toLowerCase();
      if (descIdx  < 0 && /desc/i.test(t))              descIdx  = i;
      // "qty"/"quantity" alone usually means a price-tier quantity break, NOT stock.
      // Only match columns explicitly labelled "stock" or "in stock".
      if (stockIdx < 0 && /^stock$|in[\s-]?stock/i.test(t)) stockIdx = i;
    });
  }

  // Use :scope > td to avoid picking up cells from nested price-tier tables.
  const cells = [...row.querySelectorAll(":scope > td")];

  if (descIdx  >= 0 && cells[descIdx]) {
    const val = cells[descIdx].textContent.trim();
    if (!PRICE_RE.test(val)) info.description = val;
  }
  if (stockIdx >= 0 && cells[stockIdx]) {
    info.inStock = cells[stockIdx].textContent.trim().replace(/[^\d]/g, "");
  }

  // Fallback heuristics when header detection didn't find the columns.
  if (!info.description || !info.inStock) {
    for (const cell of cells) {
      const t = cell.textContent.trim();
      if (!t || cell.querySelector("img, button, input")) continue;
      if (PRICE_RE.test(t)) continue; // skip price cells
      // Stock: a bare integer, must be a plausible quantity (>=4 digits, or >999).
      // This filters out package codes like 0402/0603/0805 and small MOQ values.
      const digits = t.replace(/\D/g, "");
      const PACKAGE_RE = /^(0201|0402|0603|0805|1206|1210|1812|2010|2512|SOT|SOP|QFP|QFN|DIP|TO-)/i;
      if (!info.inStock && /^\d[\d,\s]*$/.test(t) && digits.length >= 4 && !PACKAGE_RE.test(t)) {
        info.inStock = digits;
      // Description: longer text that isn't an LCSC ID or starts with a digit
      } else if (!info.description && t.length > 15 && !/^C\d+$/.test(t) && !/^\d/.test(t)) {
        info.description = t;
      }
    }
  }

  return info;
}

function sendImport(lcscId, description, inStock, onResult) {
  try {
    chrome.runtime.sendMessage({ type: "import", lcscId, description, inStock }, onResult);
  } catch (e) {
    // Extension context was invalidated (e.g. extension reloaded while page was open).
    // Tell the user to reload the page.
    onResult({ success: false, error: "Extension context invalidated — please reload the page (F5) and try again." });
  }
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

    // Use pre-scraped row data (JLCPCB / LCSC list) when available,
    // else scrape the current page (LCSC detail page).
    const partInfo    = scrapePartInfo(lcscId);
    const description = btn.dataset.description ?? partInfo.description;
    const inStock     = btn.dataset.inStock     ?? partInfo.inStock ?? "";
    sendImport(lcscId, description, inStock, (data) => {
      if (chrome.runtime.lastError || !data) {
        setButtonState(btn, "error", compact ? "✗ offline" : "✗ Server offline", compact);
        return;
      }
      if (data.success) {
        setButtonState(btn, "ok", compact ? "✓" : "✓ Imported", compact);
        showSuccessToast(data.symbol_name || lcscId, lcscId);
      } else {
        setButtonState(btn, "error", compact ? "✗" : "✗ Failed", compact);
        const errMsg = data.stderr || data.error || "";
        console.error("[easyeda2kicad]", errMsg);
        showErrorToast(lcscId, errMsg);
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

// ── error toast ───────────────────────────────────────────────────────────────

function showErrorToast(lcscId, rawError) {
  document.getElementById("kicad-toast")?.remove();

  const toast = document.createElement("div");
  toast.id = "kicad-toast";
  Object.assign(toast.style, {
    position: "fixed",
    bottom: "24px",
    right: "24px",
    zIndex: "1000000",
    background: "#7f1d1d",
    color: "#fff",
    borderRadius: "8px",
    padding: "16px 20px",
    boxShadow: "0 4px 20px rgba(0,0,0,0.35)",
    fontFamily: "system-ui, sans-serif",
    fontSize: "14px",
    lineHeight: "1.5",
    maxWidth: "360px",
    display: "flex",
    flexDirection: "column",
    gap: "8px",
    animation: "kicad-slidein 0.25s ease",
  });

  // Keyframe (shared — only injected once)
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

  // Header
  const header = document.createElement("div");
  header.style.cssText = "display:flex; align-items:center; gap:8px; font-size:15px; font-weight:700;";
  header.innerHTML = `<span style="font-size:18px">✗</span> Import failed`;
  toast.appendChild(header);

  // Clean up the first meaningful line from stderr/error
  const firstLine = (rawError || "")
    .split("\n")
    .map((l) => l.replace(/^\s*\[ERROR\]\s*/i, "").trim())
    .find((l) => l.length > 0) || "Unknown error";

  const msgEl = document.createElement("div");
  msgEl.style.cssText = "font-size:12px; opacity:0.9; word-break:break-word;";
  msgEl.textContent = firstLine;
  toast.appendChild(msgEl);

  const sub = document.createElement("div");
  sub.style.cssText = "font-size:11px; opacity:0.65;";
  sub.textContent = `LCSC: ${lcscId}`;
  toast.appendChild(sub);

  // Close button
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
  toast.style.position = "fixed";
  toast.appendChild(closeBtn);

  document.body.appendChild(toast);
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
  document.getElementById(UPDATE_BAR_ID)?.remove();
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
  injectUpdateAllBar();
  document.querySelectorAll('a[href*="product-detail"]').forEach((link) => {
    const lcscId = lcscIdFromUrl(link.href);
    if (!lcscId) return;

    const row = link.closest("tr, li, [class*='product'], [class*='item'], [class*='row']");
    const anchor = row || link.parentElement;

    if (anchor.querySelector(`.${BTN_CLASS}`)) return;

    // Pre-scrape description + stock from the product row (same logic as JLCPCB).
    // scrapeJlcpcbRow walks up to the nearest <tr> and reads from table headers.
    const rowInfo = row ? scrapeJlcpcbRow(link) : { description: "", inStock: "" };
    const btn = makeButton(lcscId, true);
    if (rowInfo.description) btn.dataset.description = rowInfo.description;
    if (rowInfo.inStock)     btn.dataset.inStock     = rowInfo.inStock;

    btn.style.marginLeft = "6px";
    link.insertAdjacentElement("afterend", btn);
  });
}

// ── JLCPCB parts page ────────────────────────────────────────────────────────
// JLCPCB's component library (jlcpcb.com/parts) shows LCSC part numbers in
// each row. We find them and inject compact import buttons next to each one.

const LCSC_ID_RE = /^C\d{4,}$/;

function handleJlcpcbPage() {
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      const p = node.parentElement;
      if (!p || SKIP_TAGS.has(p.tagName)) return NodeFilter.FILTER_SKIP;
      const style = window.getComputedStyle(p);
      if (style.display === "none" || style.visibility === "hidden") return NodeFilter.FILTER_SKIP;
      return LCSC_ID_RE.test(node.textContent.trim())
        ? NodeFilter.FILTER_ACCEPT
        : NodeFilter.FILTER_SKIP;
    },
  });

  const nodes = [];
  let node;
  while ((node = walker.nextNode())) nodes.push(node);

  nodes.forEach((textNode) => {
    const lcscId = textNode.textContent.trim();
    const el = textNode.parentElement;

    // Skip if a button is already right next to this element
    if (el.nextElementSibling?.classList.contains(BTN_CLASS)) return;
    if (el.parentElement?.querySelector(`.${BTN_CLASS}`)) return;

    // Scrape description + stock from the surrounding table row up-front so
    // the data is available immediately when the user clicks the button.
    const rowInfo = scrapeJlcpcbRow(el);
    const btn = makeButton(lcscId, true);
    if (rowInfo.description) btn.dataset.description = rowInfo.description;
    if (rowInfo.inStock)     btn.dataset.inStock     = rowInfo.inStock;

    btn.style.marginLeft = "8px";
    btn.style.verticalAlign = "middle";
    el.insertAdjacentElement("afterend", btn);
  });
}

// ── Update All bar (LCSC list / search pages only) ────────────────────────────

function injectUpdateAllBar() {
  if (document.getElementById(UPDATE_BAR_ID)) return;

  const bar = document.createElement("div");
  bar.id = UPDATE_BAR_ID;
  Object.assign(bar.style, {
    position:   "fixed",
    top:        "0",
    left:       "0",
    right:      "0",
    zIndex:     "1000001",
    background: "#1b4332",
    color:      "#fff",
    padding:    "7px 16px",
    display:    "flex",
    alignItems: "center",
    gap:        "12px",
    fontFamily: "system-ui, sans-serif",
    fontSize:   "13px",
    boxShadow:  "0 2px 8px rgba(0,0,0,0.25)",
  });

  const label = document.createElement("span");
  label.textContent = "easyeda2kicad";
  label.style.cssText = "font-weight:700; opacity:0.85; flex-shrink:0;";

  const btn = document.createElement("button");
  btn.textContent = "Update All in KiCad Library";
  Object.assign(btn.style, {
    background:   "#2d6a4f",
    color:        "#fff",
    border:       "1px solid rgba(255,255,255,0.3)",
    borderRadius: "4px",
    padding:      "4px 12px",
    fontSize:     "12px",
    fontWeight:   "600",
    cursor:       "pointer",
    flexShrink:   "0",
  });

  const statusSpan = document.createElement("span");
  statusSpan.style.cssText = "font-size:12px; opacity:0.8; flex:1;";

  const closeBtn = document.createElement("button");
  closeBtn.textContent = "×";
  Object.assign(closeBtn.style, {
    background: "none",
    border:     "none",
    color:      "#fff",
    fontSize:   "20px",
    cursor:     "pointer",
    opacity:    "0.65",
    lineHeight: "1",
    padding:    "0 4px",
    flexShrink: "0",
  });
  closeBtn.addEventListener("click", () => bar.remove());

  btn.addEventListener("click", () => {
    btn.disabled = true;
    btn.textContent = "Updating…";
    statusSpan.textContent = "";

    fetch("http://localhost:7777/update-all")
      .then((r) => r.json())
      .then((data) => {
        btn.disabled = false;
        if (data.success) {
          btn.textContent = "✓ Done";
          btn.style.background = "#155724";
          statusSpan.textContent =
            `Updated ${data.updated} / ${data.total} parts` +
            (data.failed > 0 ? ` (${data.failed} failed)` : "");
        } else {
          btn.textContent = "✗ Failed";
          btn.style.background = "#7f1d1d";
          statusSpan.textContent = data.error || "Update failed";
        }
      })
      .catch(() => {
        btn.disabled = false;
        btn.textContent = "✗ Server offline";
        btn.style.background = "#7f1d1d";
        statusSpan.textContent = "Is the server running?";
      });
  });

  bar.appendChild(label);
  bar.appendChild(btn);
  bar.appendChild(statusSpan);
  bar.appendChild(closeBtn);
  document.body.appendChild(bar);
}

// ── router ────────────────────────────────────────────────────────────────────

function run() {
  if (location.hostname.includes("jlcpcb.com")) {
    handleJlcpcbPage();
  } else if (location.pathname.includes("/product-detail/")) {
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
