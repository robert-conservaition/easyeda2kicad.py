// Service worker — handles all fetches to the local server so CORS and
// mixed-content are never an issue, and so a killed content-script context
// can't break an in-flight request.
//
// Use 127.0.0.1 (not "localhost"): the server binds IPv4 only, and on some
// systems "localhost" resolves to IPv6 (::1) first, which fails with
// "Failed to fetch".
const SERVER = "http://127.0.0.1:7777";

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  let url;

  if (message.type === "import") {
    const params = new URLSearchParams({ lcsc_id: message.lcscId });
    if (message.description) params.set("description", message.description);
    if (message.inStock)     params.set("in_stock",    message.inStock);
    url = `${SERVER}/import?${params}`;
  } else if (message.type === "update-all") {
    url = `${SERVER}/update-all`;
  } else if (message.type === "update-progress") {
    url = `${SERVER}/update-progress`;
  } else if (message.type === "status") {
    url = `${SERVER}/status`;
  } else {
    return; // not ours
  }

  fetch(url)
    .then((r) => r.json())
    .then((data) => sendResponse(data))
    .catch((err) => sendResponse({ success: false, error: err.message }));

  return true; // keep the message channel open for the async response
});
