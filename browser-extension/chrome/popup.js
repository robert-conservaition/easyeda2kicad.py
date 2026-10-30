// LCSC top-level category pages. Selecting one and clicking "Open on LCSC"
// navigates straight to that category's browse/filter page on lcsc.com.
const CATEGORIES = [
  ["Resistors",                 "https://www.lcsc.com/category/501.html"],
  ["Capacitors",                "https://www.lcsc.com/category/495.html"],
  ["Inductors, Coils, Chokes",  "https://www.lcsc.com/category/498.html"],
  ["Connectors",                "https://www.lcsc.com/category/10.html"],
  ["Diodes & Transistors",      "https://www.lcsc.com/category/12.html"],
  ["Integrated Circuits (ICs)", "https://www.lcsc.com/category/17.html"],
  ["Circuit Protection",        "https://www.lcsc.com/category/8.html"],
];

// ── Category dropdown ─────────────────────────────────────────────────────────

const categorySelect = document.getElementById("category");
const btnBrowse       = document.getElementById("btn-browse");

CATEGORIES.forEach(([name, url]) => {
  const opt = document.createElement("option");
  opt.value       = url;
  opt.textContent = name;
  categorySelect.appendChild(opt);
});

btnBrowse.addEventListener("click", () => {
  chrome.tabs.create({ url: categorySelect.value });
});

// ── Library status & Update All ───────────────────────────────────────────────
// Use 127.0.0.1 (not "localhost"): the server binds IPv4 only, and "localhost"
// can resolve to IPv6 (::1) first on some systems → "Failed to fetch".

const SERVER       = "http://127.0.0.1:7777";
const statusRows   = document.getElementById("status-rows");
const btnUpdateAll = document.getElementById("btn-update-all");
const updateMsg    = document.getElementById("update-msg");

function renderStatusRows(items) {
  statusRows.innerHTML = items
    .map(([key, val, cls]) =>
      `<div class="row">
        <span class="key">${key}</span>
        <span class="val${cls ? " " + cls : ""}">${val}</span>
      </div>`
    )
    .join("");
}

function fetchStatus() {
  statusRows.innerHTML = '<div style="color:#999;font-size:12px;">Loading…</div>';
  fetch(`${SERVER}/status`, { signal: AbortSignal.timeout(4000) })
    .then((r) => r.json())
    .then((data) => {
      if (!data.success) {
        renderStatusRows([["Status", "Error", "offline"]]);
        return;
      }
      renderStatusRows([
        ["Server",        "● Online",                    "online"],
        ["Library",       data.library_found ? "Found" : "Not found",
                          data.library_found ? "" : "offline"],
        ["Total Symbols", String(data.total_symbols || 0), ""],
        ["LCSC Symbols",  String(data.lcsc_symbols  || 0), ""],
        ["Last Modified", data.last_modified || "—",       ""],
      ]);
    })
    .catch(() => {
      renderStatusRows([["Server", "● Offline", "offline"]]);
    });
}

document.getElementById("btn-refresh").addEventListener("click", fetchStatus);

function pollUpdate() {
  fetch(`${SERVER}/update-progress`, { signal: AbortSignal.timeout(5000) })
    .then((r) => r.json())
    .then((p) => {
      const pct = p.total ? Math.round((100 * p.done) / p.total) : 0;
      if (p.running) {
        btnUpdateAll.textContent = `Updating ${p.done}/${p.total}`;
        updateMsg.textContent = `${pct}% done${p.failed ? ` · ${p.failed} failed` : ""}`;
        updateMsg.style.color = "#555";
        setTimeout(pollUpdate, 2000);
      } else {
        btnUpdateAll.disabled = false;
        btnUpdateAll.textContent = "Update All Parts";
        if (p.total) {
          updateMsg.textContent = `✓ Updated ${p.updated} / ${p.total} parts${p.failed ? ` (${p.failed} failed)` : ""}`;
          updateMsg.style.color = "#1b4332";
        } else { updateMsg.textContent = ""; }
        fetchStatus();
      }
    })
    .catch(() => { setTimeout(pollUpdate, 3000); });
}

btnUpdateAll.addEventListener("click", () => {
  btnUpdateAll.disabled = true;
  btnUpdateAll.textContent = "Starting…";
  updateMsg.textContent = "Re-importing every part — runs in the background.";
  updateMsg.style.color = "#555";

  fetch(`${SERVER}/update-all`, { signal: AbortSignal.timeout(10000) })
    .then((r) => r.json())
    .then((data) => {
      if (!data.success) {
        btnUpdateAll.disabled = false;
        btnUpdateAll.textContent = "Update All Parts";
        updateMsg.textContent = `✗ ${data.error || "Update failed"}`;
        updateMsg.style.color = "#9b2226";
        return;
      }
      if (data.started === false) {
        btnUpdateAll.disabled = false;
        btnUpdateAll.textContent = "Update All Parts";
        updateMsg.textContent = data.message || "Nothing to update.";
        return;
      }
      pollUpdate();
    })
    .catch(() => {
      btnUpdateAll.disabled = false;
      btnUpdateAll.textContent = "Update All Parts";
      updateMsg.textContent = "✗ Server offline";
      updateMsg.style.color = "#9b2226";
    });
});

// Resume progress display if a job is already running when the popup opens.
fetch(`${SERVER}/update-progress`, { signal: AbortSignal.timeout(4000) })
  .then((r) => r.json())
  .then((p) => { if (p.running) { btnUpdateAll.disabled = true; pollUpdate(); } })
  .catch(() => {});

fetchStatus();
