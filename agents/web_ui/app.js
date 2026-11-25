"use strict";
const historyBox = document.querySelector("#history");
const statusLine = document.querySelector("#status");
const promptBox = document.querySelector("#prompt");
const form = document.querySelector("#composer");
const send = document.querySelector("#send");
const socket = new WebSocket("ws://" + location.host + "/session");
let activeOutput = null;
let busy = true;

function availability(running, status) {
  busy = running;
  send.disabled = running || socket.readyState !== WebSocket.OPEN;
  statusLine.textContent = status;
}
function entry(label, text) {
  document.querySelector("#empty")?.remove();
  const section = document.createElement("article");
  section.className = "entry";
  const heading = document.createElement("h2");
  heading.textContent = label;
  const body = document.createElement("p");
  body.textContent = text;
  section.append(heading, body);
  historyBox.append(section);
  return body;
}
function approval(event) {
  activeOutput = null;
  const card = document.createElement("section");
  card.className = "approval";
  const label = document.createElement("strong");
  label.textContent = event.label;
  const message = document.createElement("pre");
  message.textContent = event.message;
  card.append(label, message);
  for (const [text, allow] of [["批准", true], ["拒绝", false]]) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = text;
    if (!allow) button.className = "secondary";
    button.onclick = () => {
      if (socket.readyState !== WebSocket.OPEN) return;
      socket.send(JSON.stringify({type:"decision", id:event.id, allow}));
      for (const item of card.querySelectorAll("button")) item.remove();
      label.textContent = event.label + (allow ? " · 已批准" : " · 已拒绝");
      statusLine.textContent = historyBox.querySelector(".approval button") ? "等待审批" : "运行中";
    };
    card.append(button);
  }
  historyBox.append(card);
  statusLine.textContent = "等待审批";
}
form.onsubmit = (event) => {
  event.preventDefault();
  const text = promptBox.value.trim();
  if (!text || busy || socket.readyState !== WebSocket.OPEN) return;
  socket.send(JSON.stringify({type:"prompt", text}));
  entry("你", text);
  activeOutput = null;
  promptBox.value = "";
  availability(true, "运行中");
  historyBox.scrollTop = historyBox.scrollHeight;
};
promptBox.onkeydown = (event) => {
  if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
    event.preventDefault();
    form.requestSubmit();
  }
};
socket.onmessage = ({data}) => {
  const event = JSON.parse(data);
  if (event.type === "ready" || event.type === "complete") {
    activeOutput = null;
    availability(false, event.type === "ready" ? "已连接" : "已完成");
  } else if (event.type === "output") {
    if (!activeOutput) activeOutput = entry("ResearchAgent", "");
    activeOutput.append(document.createTextNode(event.text));
  } else if (event.type === "approval") {
    approval(event);
  } else if (event.type === "failed" || event.type === "error") {
    entry("会话提示", event.message);
    if (event.type === "failed") availability(false, "任务中断");
  }
  historyBox.scrollTop = historyBox.scrollHeight;
};
socket.onclose = ({code}) => {
  const reason = code === 4001 ? "会话因空闲已关闭" : "连接已断开";
  availability(true, reason + "，刷新可开始新会话");
  for (const item of historyBox.querySelectorAll("button")) item.disabled = true;
};
