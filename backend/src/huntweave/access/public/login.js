"use strict";
const form = document.getElementById("login-form");
const input = document.getElementById("access-key");
const error = document.getElementById("error");
const button = form.querySelector("button");
form.addEventListener("submit", async (event) => {
  event.preventDefault();
  button.disabled = true;
  error.textContent = "";
  let body = JSON.stringify({ access_key: input.value });
  input.value = "";
  try {
    const pending = fetch("/auth/login", { method: "POST", headers: { "Content-Type": "application/json" }, body, credentials: "same-origin" });
    body = "";
    const response = await pending;
    if (response.ok) { window.location.replace("/"); return; }
    const messages = { invalid_access_key: "密钥不正确。", rate_limited: "尝试过于频繁，请稍后再试。", origin_invalid: "访问地址与服务端配置不一致，请使用配置的入口。", access_key_missing: "服务端尚未配置可用密钥。" };
    const data = await response.json();
    error.textContent = messages[data.reason_code] || "暂时无法登录，请检查服务状态。";
  } catch { error.textContent = "连接失败，请稍后重试。"; }
  finally { body = ""; button.disabled = false; }
});
