browser.runtime.onMessage.addListener((message, sender) => {
  if (message?.type !== "getReviews" || !sender.url) return;

  const page = new URL(sender.url);
  if (page.origin !== "https://github.com" || page.pathname !== "/notifications") return;

  return browser.runtime.sendNativeMessage("github_inbox_reviews", {
    viewer: message.viewer,
    pullRequests: message.pullRequests,
  }).catch(() => ({
    error: "Local helper unavailable. Run host.py --install, then reload the inbox.",
  }));
});
