const statusText = document.getElementById("statusText");
const scopeSelect = document.getElementById("scopeSelect");
const exportJsonBtn = document.getElementById("exportJsonBtn");

function setStatus(message) {
  statusText.textContent = message;
}

function toTimestampParts(date) {
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}${pad(date.getMonth() + 1)}${pad(date.getDate())}_${pad(date.getHours())}${pad(date.getMinutes())}${pad(date.getSeconds())}`;
}

function mapSameSite(value) {
  if (value === "no_restriction") return "None";
  if (value === "lax") return "Lax";
  if (value === "strict") return "Strict";
  return "Lax";
}

function getCurrentTab() {
  return new Promise((resolve, reject) => {
    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
      if (chrome.runtime.lastError) {
        reject(new Error(chrome.runtime.lastError.message));
        return;
      }
      resolve(tabs && tabs[0] ? tabs[0] : null);
    });
  });
}

function getCookies(filter) {
  return new Promise((resolve, reject) => {
    chrome.cookies.getAll(filter, (cookies) => {
      if (chrome.runtime.lastError) {
        reject(new Error(chrome.runtime.lastError.message));
        return;
      }
      resolve(Array.isArray(cookies) ? cookies : []);
    });
  });
}

async function collectCookiesByScope(scope) {
  if (scope === "current") {
    const tab = await getCurrentTab();
    const url = tab && tab.url ? tab.url : "";
    if (!url.startsWith("http://") && !url.startsWith("https://")) {
      throw new Error("当前标签页不是网页，无法按当前站点读取 Cookie");
    }
    const cookies = await getCookies({ url });
    return { cookies, sourceUrl: url };
  }

  const domains = [".adobe.com", "firefly.adobe.com", "account.adobe.com"];
  const all = [];
  for (const domain of domains) {
    const cookies = await getCookies({ domain });
    all.push(...cookies);
  }

  const unique = [];
  const seen = new Set();
  for (const item of all) {
    const key = `${item.domain}|${item.path}|${item.name}`;
    if (seen.has(key)) continue;
    seen.add(key);
    unique.push(item);
  }
  return { cookies: unique, sourceUrl: "https://firefly.adobe.com/" };
}

function toPlaywrightLikeCookies(cookies) {
  return cookies.map((item) => ({
    name: item.name,
    value: item.value,
    domain: item.domain,
    path: item.path || "/",
    expires: typeof item.expirationDate === "number" ? item.expirationDate : -1,
    httpOnly: Boolean(item.httpOnly),
    secure: Boolean(item.secure),
    sameSite: mapSameSite(item.sameSite)
  }));
}

function buildCookieHeader(cookies) {
  const parts = [];
  for (const item of cookies) {
    const name = String(item.name || "").trim();
    if (!name) continue;
    parts.push(`${name}=${String(item.value || "")}`);
  }
  return parts.join("; ");
}

function downloadJson(filename, data) {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  chrome.downloads.download({
    url,
    filename,
    saveAs: true
  });
  setTimeout(() => URL.revokeObjectURL(url), 3000);
}

async function generatePayload() {
  const scope = scopeSelect.value;
  const { cookies } = await collectCookiesByScope(scope);
  const normalizedCookies = toPlaywrightLikeCookies(cookies);
  const cookieHeader = buildCookieHeader(normalizedCookies);
  const now = new Date();
  const fileTs = toTimestampParts(now);

  const payload = { cookie: cookieHeader };

  const fileName = `cookie_${fileTs}.json`;
  return {
    payload,
    fileName,
    cookieCount: normalizedCookies.length,
    cookieHeader
  };
}

exportJsonBtn.addEventListener("click", async () => {
  try {
    setStatus("正在读取 Cookie...");
    const { payload, fileName, cookieCount, cookieHeader } = await generatePayload();
    if (!cookieCount) {
      setStatus("未读取到 Cookie，请先登录 Adobe/Firefly 后重试");
      return;
    }
    downloadJson(fileName, payload);
    setStatus(`导出成功：${cookieCount} 条 Cookie`);
  } catch (error) {
    setStatus(`导出失败：${error.message || error}`);
  }
});
