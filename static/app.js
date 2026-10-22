(() => {
if (location.protocol === "file:") {
  location.replace("http://127.0.0.1:8765/");
  return;
}
const $ = (id) => document.getElementById(id);
const labels = {queued:"待处理", running:"执行中", waiting_review:"待反馈", stalled:"需要处理", closed:"已结束"};
let token = sessionStorage.getItem("relay-token") || "";
const fragment = new URLSearchParams(location.hash.slice(1));
if (fragment.has("token")) {
  token = fragment.get("token");
  sessionStorage.setItem("relay-token", token);
  history.replaceState(null, "", location.pathname);
}
let selected = sessionStorage.getItem("relay-selected") || null;
let current = null, setup = null, renderKey = "", listKey = "", refreshBusy = false;
const drafts = new Map();
const initialKey = () => crypto.randomUUID();
let createKey = initialKey(), feedbackKey = initialKey();
function toast(message) {
  $("toast").textContent = message;
  $("toast").hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => $("toast").hidden = true, 3500);
}
async function api(path, data) {
  const response = await fetch(path, {method:data === undefined ? "GET" : "POST", cache:"no-store",
    headers:{"Authorization":"Bearer " + token, ...(data === undefined ? {} : {"Content-Type":"application/json"})},
    ...(data === undefined ? {} : {body:JSON.stringify(data)})});
  const result = await response.json();
  if (!response.ok) {
    if (response.status === 401 && !$("login-dialog").open) $("login-dialog").showModal();
    throw new Error(result.error || "请求失败");
  }
  return result;
}
function shortTime(timestamp) {
  return new Intl.DateTimeFormat("zh-CN", {timeZone:"Asia/Shanghai", month:"2-digit", day:"2-digit",hour:"2-digit",minute:"2-digit"}).format(new Date(timestamp*1000));
}
function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
async function refresh() {
  if (refreshBusy || !token) return;
  refreshBusy = true;
  try {
    const state = await api("/api/state");
    $("connection").textContent = "在线";
    $("connection").title = "本地中转服务在线";
    $("connection").classList.remove("offline");
    $("bound-title").textContent = state.binding.thread_title || "未绑定";
    $("bound-title").title = state.binding.thread_id || "";
    $("last-poll").textContent = state.last_poll ? shortTime(state.last_poll) : "等待首次收件";
    $("interval-note").textContent = "约 " + (state.binding.interval_minutes || "?") + " 分钟";
    if (!state.tasks.some(t => t.id === selected)) selected = state.tasks[0]?.id || null;
    if (selected) sessionStorage.setItem("relay-selected", selected);
    const nextListKey = JSON.stringify(state.tasks.map(t=>[t.id,t.title])) + selected;
    if (nextListKey !== listKey) {
      listKey = nextListKey;
      $("task-select").replaceChildren();
      for (const task of state.tasks) {
        const option = element("option", "", task.title);
        option.value = task.id;
        $("task-select").append(option);
      }
      $("task-select").value = selected || "";
    }
    $("empty-state").hidden = Boolean(selected);
    $("task-view").hidden = !selected;
    $("task-bar").hidden = !selected;
    if (selected) {
      const chosen = selected;
      const snapshot = await api("/api/tasks/" + chosen);
      if (chosen === selected) renderTask(snapshot);
    }
  } catch (error) {
    $("connection").textContent = "未连接";
    $("connection").title = error.message;
    $("connection").classList.add("offline");
    if (!$("login-dialog").open) toast(error.message);
  } finally { refreshBusy = false; }
}
function renderTask(snapshot) {
  current = snapshot;
  const task = snapshot.task;
  const key = task.id + task.status + snapshot.messages.map(m=>m.id).join("");
  if (key === renderKey) return;
  const timeline = $("timeline");
  const changedTask = timeline.dataset.task !== task.id;
  const atBottom = changedTask || timeline.scrollHeight - timeline.scrollTop - timeline.clientHeight < 80;
  renderKey = key;
  $("task-title").textContent = task.title;
  $("task-status").textContent = labels[task.status];
  $("task-status").className = "status " + task.status;
  $("task-status").title = "第 " + task.round + " / " + task.max_rounds + " 轮";
  timeline.replaceChildren();
  timeline.dataset.task = task.id;
  for (const message of snapshot.messages) {
    const isCodex = message.sender === "codex";
    const card = element("article", "message " + (isCodex ? "codex" : "peer"));
    const meta = element("div", "message-meta");
    meta.append(element("strong", "", isCodex ? "Codex" : message.sender === "user" ? "你" : "另一边 AI"),
      element("time", "", shortTime(message.created_at)));
    if (message.round > 1) meta.append(element("span", "", "第 " + message.round + " 轮"));
    card.append(meta, element("div", "message-content", message.content));
    timeline.append(card);
  }
  const notes = {
    queued:"等待 Codex 收件",
    running:"Codex 正在处理…",
    waiting_review:task.round >= task.max_rounds ? "已达到本任务的轮数上限" : "可以补充意见，继续下一轮",
    stalled:"执行状态待确认，请检查 Codex 聊天后在更多操作中处理",
    closed:"任务已结束"
  };
  $("status-note").textContent = notes[task.status];
  $("feedback-form").hidden = task.status !== "waiting_review" || task.round >= task.max_rounds;
  $("close-task").hidden = task.status === "closed";
  $("retry").hidden = task.status !== "stalled";
  if (changedTask) $("feedback").value = drafts.get(task.id) || "";
  if (atBottom) timeline.scrollTop = timeline.scrollHeight;
}
$("task-select").onchange = () => {
  selected = $("task-select").value;
  sessionStorage.setItem("relay-selected", selected);
  $("feedback").value = drafts.get(selected) || "";
  renderKey = "";
  feedbackKey = initialKey();
  $("task-menu").open = false;
  refresh();
};
$("feedback").oninput = () => { if (selected) drafts.set(selected, $("feedback").value); };
function openCreate() { $("task-menu").open = false; $("create-dialog").showModal(); }
$("new-task").onclick = openCreate;
$("empty-create").onclick = openCreate;
for (const button of document.querySelectorAll("[data-dismiss]")) button.onclick = () => $(button.dataset.dismiss).close();
document.addEventListener("click", event => {
  if (!$("task-menu").contains(event.target)) $("task-menu").open = false;
});
$("login-dialog").addEventListener("cancel", event => event.preventDefault());
$("login-form").onsubmit = async (event) => {
  event.preventDefault(); token = $("token").value.trim();
  try {
    await api("/api/state"); sessionStorage.setItem("relay-token", token); $("login-dialog").close();
    $("token").value = ""; $("login-error").textContent = ""; refresh();
  } catch (error) { $("login-error").textContent = error.message; }
};
$("create-form").onsubmit = async (event) => {
  event.preventDefault();
  const button = event.submitter || event.target.querySelector('button[type="submit"]');
  button.disabled = true;
  try {
    const result = await api("/api/tasks", {title:$("title").value,content:$("content").value,
      max_rounds:Number($("max-rounds").value),idempotency_key:createKey});
    selected = result.task_id; renderKey = ""; createKey = initialKey();
    event.target.reset();
    event.target.querySelector("details").open = false;
    $("create-dialog").close(); toast("任务已发送"); await refresh();
  } catch (error) { toast(error.message); } finally { button.disabled = false; }
};
$("feedback-form").onsubmit = async (event) => {
  event.preventDefault();
  const button = event.submitter || event.target.querySelector('button[type="submit"]');
  button.disabled = true;
  try {
    const answer = current.messages.filter(m=>m.kind === "result").at(-1);
    await api("/api/tasks/" + selected + "/feedback", {content:$("feedback").value,
      reply_to:answer.id,idempotency_key:feedbackKey});
    feedbackKey = initialKey(); $("feedback").value = ""; drafts.delete(selected);
    toast("意见已发送"); await refresh();
  } catch (error) { toast(error.message); } finally { button.disabled = false; }
};
$("close-task").onclick = async () => {
  $("task-menu").open = false;
  try { await api("/api/tasks/" + selected + "/close", {}); await refresh(); } catch (error) { toast(error.message); }
};
$("retry").onclick = async () => {
  $("task-menu").open = false;
  if (!confirm("请先确认原来的 Codex 执行已经停止且没有回传。重新排队可能再次执行已发生的文件修改。继续？")) return;
  try { await api("/api/tasks/" + selected + "/retry", {}); await refresh(); } catch (error) { toast(error.message); }
};
async function copy(value) { try { await navigator.clipboard.writeText(value); toast("已复制"); } catch { toast("请手动选择并复制内容"); } }
$("copy-task-id").onclick = () => { $("task-menu").open = false; copy(selected); };
$("setup").onclick = async () => {
  try {
    setup = await api("/api/setup");
    $("mcp-config").textContent = JSON.stringify(setup.mcp,null,2);
    $("other-ai").textContent = setup.instructions; $("setup-dialog").showModal();
  } catch (error) { toast(error.message); }
};
$("copy-config").onclick = () => copy(JSON.stringify(setup.mcp,null,2));
$("copy-instructions").onclick = () => copy(setup.instructions);
$("export").onclick = () => {
  $("task-menu").open = false;
  const text = "# " + current.task.title + "\n\n任务 ID：" + selected + "\n\n" + current.messages.map(m=>
    "## " + m.sender + " · 第 " + m.round + " 轮 · " + shortTime(m.created_at) + "\n\n" + m.content).join("\n\n");
  const url = URL.createObjectURL(new Blob([text],{type:"text/markdown;charset=utf-8"}));
  const anchor = document.createElement("a"); anchor.href = url; anchor.download = "relay-" + selected.slice(0,8) + ".md"; anchor.click();
  setTimeout(()=>URL.revokeObjectURL(url),1000);
};
if (!token) $("login-dialog").showModal();
refresh();
setInterval(refresh,4000);
})();
