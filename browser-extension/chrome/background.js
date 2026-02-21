// Service worker — handles fetch to localhost so CORS is never an issue.
chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message.type !== "import") return;

  const params = new URLSearchParams({ lcsc_id: message.lcscId });
  if (message.description) params.set("description", message.description);

  fetch(`http://localhost:7777/import?${params}`)
    .then((r) => r.json())
    .then((data) => sendResponse(data))
    .catch((err) => sendResponse({ success: false, error: err.message }));

  return true; // keep the message channel open for the async response
});
