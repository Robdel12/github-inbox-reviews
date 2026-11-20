const badgeClass = "github-inbox-review";
const batchSize = 25;
const cacheLifetimeMs = 60_000;
const statuses = {
  APPROVED: ["✓", "You approved"],
  CHANGES_REQUESTED: ["!", "You requested changes"],
  COMMENTED: ["…", "You left a review comment"],
  DISMISSED: ["×", "Your review was dismissed"],
  UNREVIEWED: ["○", "You haven’t submitted a review"],
  UNAVAILABLE: ["?", "Review history unavailable"],
};
const cache = new Map();
let scheduled = false;
let loading = false;

function pullRequestKey(link) {
  const url = new URL(link.href, location.href);
  if (url.origin !== "https://github.com") return null;
  const match = /^\/([\w-]+)\/([\w.-]+)\/pull\/([1-9][0-9]*)(?:\/|$)/.exec(url.pathname);
  return match?.slice(1).join("/").toLowerCase() ?? null;
}

function notificationLinks() {
  const inbox = document.querySelector("main, [role=main]") ?? document;
  const rows = new Map();
  for (const link of inbox.querySelectorAll('a[href*="/pull/"]')) {
    const key = pullRequestKey(link);
    if (!key || !link.textContent.trim()) continue;

    const row = link.closest("li, [role=listitem], .notification-list-item") ?? link;
    if (!rows.has(row) || link.matches(".js-notification-target")) rows.set(row, { link, key });
  }
  return Array.from(rows.values());
}

function notificationMetadata(link, key) {
  const reference = key.replace(/\/([0-9]+)$/, "#$1");
  return Array.from(link.querySelectorAll("span, div, p, small")).findLast(
    (element) => element.textContent.replace(/\s+/g, "").toLowerCase() === reference,
  );
}

function createBadge(link, key) {
  const badge = document.createElement("span");
  badge.className = badgeClass;
  badge.dataset.pullRequest = key;
  badge.setAttribute("role", "img");

  const metadata = notificationMetadata(link, key);
  if (metadata) {
    metadata.append(badge);
  } else {
    badge.dataset.leading = "";
    link.prepend(badge);
  }
  return badge;
}

function showBadge(link, key, review) {
  let badge = link.querySelector(`.${badgeClass}`);
  if (!review) {
    badge?.remove();
    return;
  }

  const [symbol, verdict] = statuses[review.state];
  const details = [review.error ?? verdict];
  if (review.dismissed && review.state !== "DISMISSED") details.push("Review dismissed");
  if (review.head === "changed") details.push("Head commit changed since your review");
  if (review.head === "same") details.push("Same head commit as your review");
  if (review.head === "unknown") details.push("Reviewed commit unavailable");
  if (review.requestedAgain) details.push("Review requested again");
  const label = details.join(" · ");

  if (badge?.dataset.pullRequest !== key) {
    badge?.remove();
    badge = createBadge(link, key);
  }
  if (badge.title === label && badge.dataset.state === review.state) return;

  const activity = [];
  if (review.head === "changed") activity.push("+");
  if (review.requestedAgain === true) activity.push("↻");
  badge.dataset.activity = activity.join(" ");
  badge.dataset.symbol = symbol;
  badge.dataset.state = review.state;
  badge.toggleAttribute("data-dismissed", review.dismissed === true);
  badge.title = label;
  badge.setAttribute("aria-label", label);
}

async function updateInbox() {
  if (location.pathname !== "/notifications") return;
  const viewer = document.querySelector('meta[name="user-login"]')?.content.toLowerCase();
  if (!viewer) return;

  const links = notificationLinks();
  const visibleLinks = new Set(links.map(({ link }) => link));
  for (const badge of document.querySelectorAll(`.${badgeClass}`)) {
    if (!visibleLinks.has(badge.closest("a"))) badge.remove();
  }

  const pending = new Set();
  const now = Date.now();
  for (const { link, key } of links) {
    const cached = cache.get(`${viewer}:${key}`);
    showBadge(link, key, cached?.review);
    if (!cached || cached.expires <= now) pending.add(key);
  }
  if (loading || pending.size === 0) return;

  loading = true;
  const pullRequests = Array.from(pending).slice(0, batchSize);
  let result;
  try {
    result = await browser.runtime.sendMessage({ type: "getReviews", viewer, pullRequests });
  } catch {
    result = { error: "Review history unavailable. Reload the inbox to try again." };
  }
  const expires = Date.now() + cacheLifetimeMs;
  for (const key of pullRequests) {
    const review = result?.reviews?.[key] ?? { state: "UNAVAILABLE", error: result?.error };
    cache.set(`${viewer}:${key}`, { review, expires });
  }
  loading = false;
  scheduleUpdate();
}

function scheduleUpdate() {
  if (scheduled) return;
  scheduled = true;
  requestAnimationFrame(() => {
    scheduled = false;
    void updateInbox();
  });
}

new MutationObserver(scheduleUpdate).observe(document.body, {
  childList: true,
  subtree: true,
  attributes: true,
  attributeFilter: ["href"],
});
window.addEventListener("focus", scheduleUpdate);
window.addEventListener("pageshow", scheduleUpdate);
scheduleUpdate();
