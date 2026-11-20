"use strict";

const orderInput = document.getElementById("orderId");
const tokenInput = document.getElementById("pickupToken");
const result = document.getElementById("resultSection");
const errorBox = document.getElementById("errorBox");
const collectButton = document.getElementById("queryBtn");
const copyButton = document.getElementById("copyBtn");
const openButton = document.getElementById("openLinkBtn");
const content = document.getElementById("cardContent");
const params = new URLSearchParams(window.location.hash.slice(1));
const buttonLabel = collectButton.textContent.trim();
let link = "";

if (params.has("pickup_token")) {
  orderInput.value = params.get("order_id") ?? "";
  tokenInput.value = params.get("pickup_token");
  history.replaceState(null, "", window.location.pathname);
}

function showError(message) {
  errorBox.textContent = message;
  errorBox.style.display = "block";
}

async function collect() {
  const orderId = orderInput.value.trim();
  const pickupToken = tokenInput.value.trim();
  errorBox.style.display = "none";
  result.style.display = "none";
  content.textContent = "";
  link = "";
  if (!orderId || !pickupToken) {
    showError("请输入订单编号和订单专属提货凭证。");
    (orderId ? tokenInput : orderInput).focus();
    return;
  }

  collectButton.disabled = true;
  collectButton.textContent = "正在验证提货凭证…";
  try {
    const response = await fetch("/api/pickup/collect", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({order_id: orderId, pickup_token: pickupToken}),
    });
    const {ok, data, error} = await response.json();
    if (!ok) {
      showError(error || "提取失败，请确认订单编号和提货凭证。");
      return;
    }
    document.getElementById("metaTitle").textContent = data.title;
    document.getElementById("metaStatus").textContent = "已提取";
    document.getElementById("metaTime").textContent = data.shipped_at || "—";
    document.getElementById("watermark").textContent = "订单尾号 " + orderId.slice(-6);
    content.textContent = data.cards.map(({kind, payload}) =>
      kind === "image" ? payload.replace(/^\d+\|/, "") : payload,
    ).join("\n");
    link = content.textContent.match(/https?:\/\/[^\s<>]+/)?.[0] || "";
    openButton.style.display = link ? "inline-block" : "none";
    result.style.display = "block";
  } catch {
    showError("网络连接异常，请稍后重试。");
  } finally {
    collectButton.disabled = false;
    collectButton.textContent = buttonLabel;
  }
}

collectButton.addEventListener("click", collect);
for (const input of [orderInput, tokenInput]) {
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !collectButton.disabled) {
      collect();
    }
  });
}
copyButton.addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(content.textContent);
    copyButton.textContent = "已复制";
  } catch {
    showError("复制失败，请手动选择并复制发货内容。");
  }
});
openButton.addEventListener("click", () => {
  if (link) {
    window.open(link, "_blank", "noopener,noreferrer");
  }
});
