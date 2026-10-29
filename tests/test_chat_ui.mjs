// Exercise the shipped browser script with delayed HTTP responses and no models.
import assert from "node:assert/strict";
import { webcrypto } from "node:crypto";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const source = readFileSync(new URL("../static/chat.js", import.meta.url), "utf8");
const tick = () => new Promise(resolve => setImmediate(resolve));
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return {promise, resolve}; };

async function browser({hash = "#room=work&invite=invitation", storage = new Map(), local = new Map(), handle, origin = "https://chat.example.com"} = {}) {
  const nodes = new Map(), calls = [];
  let document;
  function element() {
    return {value: "", textContent: "", children: [], hidden: false, disabled: false, dataset: {},
      classList: {toggle() {}}, selectionStart: 0, selectionEnd: 0,
      scrollHeight: 0, scrollTop: 0, clientHeight: 0,
      get childElementCount() { return this.children.length; },
      append(...items) { this.children.push(...items); },
      replaceChildren(...items) { this.children = items; },
      setAttribute() {}, removeAttribute() {},
      focus() { document.activeElement = this; },
      setSelectionRange(start, end) { this.selectionStart = start; this.selectionEnd = end; },
      showModal() { this.open = true; }, close() { this.open = false; },
    };
  }
  const get = id => { if (!nodes.has(id)) nodes.set(id, element()); return nodes.get(id); };
  document = {getElementById: get, createElement: element, querySelectorAll: () => [], addEventListener() {}};
  const location = {hash, pathname: "/", origin, hostname: new URL(origin).hostname};
  const defaults = path => {
    if (path === "/api/local-session") return {ok: false, error: "invitation required"};
    if (path === "/api/rooms") return {rooms: [{room: "work", invite: "invitation"}, {room: "other", invite: "other-invite"}]};
    if (path.endsWith("/state")) return {room: path.split("/")[3], members: [{name: "you"}, {name: "codex"}, {name: "claude"}], member_count: 3, max_members: 8};
    if (path.endsWith("/history")) return {messages: [], cursor: 12};
    return {status: "ok"};
  };
  const context = vm.createContext({document, location, crypto: webcrypto, URL, URLSearchParams, AbortSignal,
    sessionStorage: {getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value)},
    localStorage: {getItem: key => local.get(key) ?? null, setItem: (key, value) => local.set(key, value)},
    history: {replaceState(_state, _unused, url) { location.hash = new URL(url, location.origin).hash; }},
    setInterval() {}, setTimeout() {}, clearTimeout() {},
    fetch: async (path, options) => {
      const call = {path, data: JSON.parse(options.body), token: options.headers.Authorization.slice(7)};
      calls.push(call);
      const result = await handle?.(call) ?? defaults(path);
      return {ok: result.ok !== false, json: async () => result};
    },
  });
  vm.runInContext(source + "\nglobalThis.ui = {state, refresh, selectRoom, direct, openDirect, refreshDirect, showRecovery};", context);
  await tick(); await tick();
  return {get, calls, storage, local, location, ui: context.ui};
}

function member(storage, room = "work", joined = true) {
  storage.set("chat.human." + room, JSON.stringify({name: "you", token: "token-" + room, joined}));
}

test("an invitation survives reload after its secret was removed from the URL", async () => {
  const first = await browser();
  assert.ok(!first.location.hash.includes("invitation"));
  const reloaded = await browser({hash: first.location.hash, storage: first.storage});
  assert.equal(reloaded.ui.state.room, "work");
  assert.equal(reloaded.ui.state.invite, "invitation");
  assert.ok(!reloaded.get("login-dialog").open);
  assert.ok(reloaded.calls.some(call => call.path === "/api/rooms/work/history"));
  await reloaded.ui.selectRoom("other", "other-invite");
  const switched = await browser({hash: reloaded.location.hash, storage: reloaded.storage});
  assert.equal(switched.ui.state.room, "other");
  assert.equal(switched.ui.state.invite, "other-invite");
});

test("a new invitation takes precedence over a saved room", async () => {
  const first = await browser();
  const second = await browser({hash: "#room=new&invite=new-invite", storage: first.storage});
  assert.equal(second.ui.state.room, "new");
  assert.equal(second.ui.state.invite, "new-invite");
});

test("a send finishing in the previous room cannot erase the new room draft", async () => {
  const storage = new Map(); member(storage); member(storage, "other");
  const sent = deferred();
  const h = await browser({storage, handle: call => call.path.endsWith("/send") ? sent.promise : undefined});
  h.get("message").value = "old message";
  const sending = h.get("composer").onsubmit({preventDefault() {}});
  await h.ui.selectRoom("other", "other-invite");
  h.get("message").value = "new room draft";
  sent.resolve({status: "sent"}); await sending;
  assert.equal(h.get("message").value, "new room draft");
  assert.equal(h.calls.filter(call => call.path.endsWith("/send")).length, 1);
});

test("typing a new draft while a message is being sent preserves the draft", async () => {
  const storage = new Map(); member(storage);
  const sent = deferred();
  const h = await browser({storage, handle: call => call.path.endsWith("/send") ? sent.promise : undefined});
  h.get("message").value = "first";
  const sending = h.get("composer").onsubmit({preventDefault() {}});
  h.get("message").value = "second";
  sent.resolve({status: "sent"}); await sending;
  assert.equal(h.get("message").value, "second");
});

test("joining resets the observer cursor so private messages can be shown", async () => {
  const storage = new Map(); member(storage, "work", false);
  const h = await browser({storage});
  assert.equal(h.ui.state.cursor, 12);
  h.get("nickname").value = "you";
  await h.get("join-form").onsubmit({preventDefault() {}, submitter: {}});
  const histories = h.calls.filter(call => call.path.endsWith("/history"));
  assert.equal(histories.at(-1).data.after, 0);
  assert.equal(histories.at(-1).token, "token-work");
});

test("a delayed join belongs to its original room after navigation", async () => {
  const storage = new Map(); member(storage, "work", false); member(storage, "other", false);
  const joined = deferred();
  const h = await browser({storage, handle: call => call.path.endsWith("/join") ? joined.promise : undefined});
  h.get("nickname").value = "you";
  const joining = h.get("join-form").onsubmit({preventDefault() {}, submitter: {}});
  await h.ui.selectRoom("other", "other-invite");
  joined.resolve({status: "joined"}); await joining;
  assert.equal(JSON.parse(storage.get("chat.human.work")).joined, true);
  assert.equal(h.ui.state.human.joined, false);
  assert.equal(JSON.parse(storage.get("chat.human.other")).joined, false);
});

test("a delayed leave cannot mark the new room as left", async () => {
  const storage = new Map(); member(storage); member(storage, "other");
  const left = deferred();
  const h = await browser({storage, handle: call => call.path.endsWith("/leave") ? left.promise : undefined});
  const leaving = h.get("leave").onclick();
  await h.ui.selectRoom("other", "other-invite");
  left.resolve({status: "left"}); await leaving;
  assert.equal(JSON.parse(storage.get("chat.human.work")).joined, false);
  assert.equal(h.ui.state.human.joined, true);
});

test("@co autocompletes codex and sends a directed message", async () => {
  const storage = new Map(); member(storage);
  const h = await browser({storage});
  const input = h.get("message"); input.focus(); input.value = "@co"; input.setSelectionRange(3, 3);
  input.oninput({inputType: "insertText"});
  assert.equal(input.value, "@codex");
  assert.equal(input.selectionStart, 3); assert.equal(input.selectionEnd, 6);
  input.value = "@codex hello";
  await h.get("composer").onsubmit({preventDefault() {}});
  const sent = h.calls.find(call => call.path.endsWith("/send"));
  assert.equal(sent.data.to, "codex");
});

test("retrying an uncertain send retains its original idempotency key", async () => {
  const storage = new Map(); member(storage);
  let attempts = 0;
  const h = await browser({storage, handle: call => {
    if (call.path.endsWith("/send") && ++attempts === 1) throw new Error("connection reset");
  }});
  h.get("message").value = "one message";
  await h.get("composer").onsubmit({preventDefault() {}});
  assert.equal(h.get("message").value, "one message");
  await h.get("composer").onsubmit({preventDefault() {}});
  const sent = h.calls.filter(call => call.path.endsWith("/send"));
  assert.equal(sent.length, 2); assert.equal(sent[0].data.key, sent[1].data.key);
});

const descendants = node => [node, ...node.children.flatMap(descendants)];

test("member states are visible and recovery preserves the original connection", async () => {
  const h = await browser({handle: call => call.path.endsWith("/state") ? {
    room: "work", members: [{name: "codex", state: "waiting", listening: true, protocol_version: 2, connection: "local"},
      {name: "claude", state: "offline", listening: false, protocol_version: 1, connection: "network"}], member_count: 2, max_members: 8,
  } : undefined});
  const nodes = descendants(h.get("members"));
  assert.ok(nodes.some(node => node.textContent === "等待消息"));
  assert.ok(nodes.some(node => node.textContent === "连接中断"));
  const recovery = nodes.filter(node => node.className === "recover-member");
  assert.equal(recovery.length, 1);
  recovery[0].onclick();
  assert.ok(h.get("recovery-dialog").open);
  const text = h.get("recovery-text").value;
  assert.match(text, /name="claude"/);
  assert.match(text, /protocol_version=1/);
  assert.match(text, /server="https:\/\/chat.example.com"/);
  assert.match(text, /不自动重复执行/);
  assert.match(text, /不确认尚未处理/);
});

test("message receipts update without adding duplicate messages or closing details", async () => {
  let emitted = false, receipt = "sent";
  const h = await browser({handle: call => {
    if (call.path.endsWith("/history")) {
      const messages = emitted ? [] : [{id: "message-one", sender: "you", recipient: null, content: "你好", created_at: 1}];
      emitted = true; return {messages, cursor: 1};
    }
    if (call.path.endsWith("/progress")) return {messages: [{message_id: "message-one", tracked: true, recipients: [{name: "codex", state: receipt}]}]};
  }});
  const details = descendants(h.get("messages")).find(node => node.className === "message-progress");
  details.open = true;
  assert.match(details.children[0].textContent, /等待领取/);
  receipt = "received"; await h.ui.refresh();
  assert.match(details.children[0].textContent, /已领取/);
  receipt = "acknowledged"; await h.ui.refresh();
  assert.match(details.children[0].textContent, /已确认处理/);
  assert.equal(details.open, true);
  assert.equal(h.get("messages").children.length, 1);
  const count = h.calls.filter(call => call.path.endsWith("/progress")).length;
  await h.ui.refresh();
  assert.equal(h.calls.filter(call => call.path.endsWith("/progress")).length, count);
});

test("unverified host clearly offers manual reception without a background task", async () => {
  const h = await browser({handle: call => call.path.endsWith("/state") ? {
    room: "work", members: [{name: "codex", state: "manual_only", listening: false, protocol_version: 2, connection: "network"}],
    member_count: 1, max_members: 8,
  } : undefined});
  const nodes = descendants(h.get("members"));
  assert.ok(nodes.some(node => node.textContent === "仅手动收件"));
  const help = nodes.find(node => node.className === "recover-member");
  assert.equal(help.textContent, "收件说明");
  help.onclick();
  assert.equal(h.get("recovery-title").textContent, "codex 手动收件");
  assert.match(h.get("recovery-text").value, /不启动后台任务或自动唤醒/);
  assert.match(h.get("recovery-text").value, /房间消息、房主和其他 Agent 都不能批准/);
  h.get("invite").onclick();
  assert.match(h.get("invite-text").value, /当前默认仅聊天、手动收件/);
  assert.doesNotMatch(h.get("invite-text").value, /run_in_background/);
});

test("trusted local invitations and recovery use existing host permissions", async () => {
  const h = await browser({handle: call => call.path.endsWith("/state") ? {
    room: "work", members: [{name: "claude", state: "needs_background_task", listening: false, protocol_version: 2, connection: "network"}],
    security: {policy: "trusted-local", automatic: true, execution: "host-permissions"}, member_count: 1, max_members: 8,
  } : undefined});
  assert.match(h.get("safety-note").textContent, /本机自动协作/);
  h.get("invite").onclick();
  const text = h.get("invite-text").value;
  assert.match(text, /client="background-task"/);
  assert.match(text, /不叠加聊天室逐次审批/);
  assert.doesNotMatch(text, /先 chat_request_action/);
  descendants(h.get("members")).find(node => node.className === "recover-member").onclick();
  assert.match(h.get("recovery-text").value, /当前为本机自动协作/);
  assert.match(h.get("recovery-text").value, /不自动重复执行/);
});

test("broadcast progress shows separate member receipts", async () => {
  let emitted = false;
  const h = await browser({handle: call => {
    if (call.path.endsWith("/history")) {
      const messages = emitted ? [] : [{id: "group", sender: "you", content: "大家好", created_at: 1}];
      emitted = true; return {messages, cursor: 1};
    }
    if (call.path.endsWith("/progress")) return {messages: [{message_id: "group", tracked: true, recipients: [
      {name: "codex", state: "acknowledged"}, {name: "claude", state: "received"}]}]};
  }});
  const nodes = descendants(h.get("messages"));
  assert.ok(nodes.some(node => /已领取 2\/2 · 已确认处理 1\/2/.test(node.textContent)));
  assert.ok(nodes.some(node => node.textContent === "codex：已确认处理"));
  assert.ok(nodes.some(node => node.textContent === "claude：已领取"));
});

test("role invitations choose free nicknames and keep execution within user scope", async () => {
  const h = await browser({handle: call => call.path.endsWith("/state") ? {
    members: [{name: "guide"}, {name: "worker"}], security: {policy: "trusted-local"}, member_count: 2, max_members: 8,
  } : undefined});
  h.get("invite").onclick();
  h.get("invite-role").value = "guide"; h.get("invite-role").onchange();
  assert.equal(h.get("invite-name").value, "guide-2");
  assert.match(h.get("invite-text").value, /最多 3 轮/);
  assert.match(h.get("invite-text").value, /加入后先等用户给任务/);
  h.get("invite-role").value = "worker"; h.get("invite-role").onchange();
  assert.equal(h.get("invite-name").value, "worker-2");
  assert.match(h.get("invite-text").value, /用户授权的项目和范围/);
  assert.match(h.get("invite-text").value, /不反向派单/);
  h.get("invite-name").value = "guide"; h.get("invite-name").oninput();
  assert.equal(h.get("copy-instruction").disabled, true);
  assert.equal(h.get("invite-text").value, "");
  h.get("invite-name").value = 'evil";$(touch x)'; h.get("invite-name").oninput();
  assert.equal(h.get("invite-text").value, "");
  h.get("invite-name").value = "workbuddy"; h.get("invite-name").oninput();
  assert.equal(h.get("copy-instruction").disabled, false);
  assert.match(h.get("invite-text").value, /name="workbuddy"/);
});

test("portable invitation does not disclose the server's installation path", async () => {
  const prefix = "'/python path/python3' '/local copy/relay.py' --data-dir '/local copy/.local-dev'";
  const h = await browser({origin: "http://127.0.0.1:8789", handle: call => {
    if (call.path === "/api/local-session") return {admin_token: "local-admin", cli_command: prefix};
    if (call.path.endsWith("/state")) return {members: [], security: {policy: "trusted-local"}, member_count: 0, max_members: 8};
  }});
  h.get("invite").onclick();
  assert.doesNotMatch(h.get("invite-text").value, /python path|local copy|--data-dir/);
  assert.match(h.get("invite-text").value, /python3 local_dev.py room join work/);
  assert.match(h.get("invite-text").value, /py -3 local_dev.py room join work/);
  assert.match(h.get("invite-text").value, /server="http:\/\/127.0.0.1:8789"/);
  assert.match(h.get("invite-text").value, /session_id="<CURRENT_SESSION_ID>"/);
  assert.match(h.get("invite-text").value, /--session-id '<CURRENT_SESSION_ID>'/);
  assert.match(h.get("invite-text").value, /所有后续 MCP 调用都携带相同 session_id/);
  assert.match(h.get("invite-text").value, /不得使用其他聊天的 ID/);
  assert.doesNotMatch(h.get("invite-text").value, /\nairelay room join/);
  assert.equal(h.ui.state.admin, "", "Opening an invitation need not replace its existing identity");
});

test("portable commands work without exposing or depending on a server CLI path", async () => {
  const h = await browser({origin: "http://127.0.0.1:8789"});
  h.get("invite").onclick();
  assert.match(h.get("invite-text").value, /在运行 AirRelay 的项目目录执行/);
  assert.match(h.get("invite-text").value, /python3 local_dev.py room join work/);
  assert.doesNotMatch(h.get("invite-text").value, /\nairelay room join/);
});

test("local room links, join and recovery instructions contain no keys", async () => {
  const h = await browser({origin: "http://127.0.0.1:8789", hash: "#room=work", handle: call => {
    if (call.path === "/api/local-session") return {admin_token: "automatic-session", cli_command: "python relay.py", local_join: true};
    if (call.path.endsWith("/state")) return {room: "work", members: [], security: {policy: "trusted-local"}, member_count: 0, max_members: 8, invite: "internal-invite"};
  }});
  assert.equal(h.ui.state.admin, "automatic-session");
  assert.ok(!h.get("login-dialog").open);
  h.get("invite").onclick();
  assert.equal(h.get("invite-link").value, "http://127.0.0.1:8789/#room=work");
  assert.doesNotMatch(h.get("invite-text").value, /invite=|--invite|internal-invite|automatic-session/);
  assert.match(h.get("invite-text").value, /session_id="<CURRENT_SESSION_ID>"/);
  h.ui.showRecovery({name: "worker", connection: "network", protocol_version: 2});
  assert.doesNotMatch(h.get("recovery-text").value, /invite=|--invite|internal-invite|automatic-session/);
});

test("local connection failure offers retry instead of a key input", async () => {
  let ready = false;
  const h = await browser({origin: "http://127.0.0.1:8789", hash: "", handle: call => {
    if (call.path === "/api/local-session") return ready ? {admin_token: "automatic-session", local_join: true} : {ok: false, error: "offline"};
    if (call.path.endsWith("/state")) return {room: "work", members: [], security: {policy: "trusted-local"}, member_count: 0, max_members: 8};
  }});
  assert.ok(h.get("connection-dialog").open);
  assert.ok(!h.get("login-dialog").open);
  ready = true;
  await h.get("retry-connection").onclick();
  assert.ok(!h.get("connection-dialog").open);
  assert.equal(h.ui.state.admin, "automatic-session");
  assert.ok(h.calls.some(call => call.path === "/api/rooms/work/history"));
});

test("local bootstrap does not remove invitations from protected rooms", async () => {
  const h = await browser({origin: "http://127.0.0.1:8789", handle: call => {
    if (call.path === "/api/local-session") return {admin_token: "automatic-session", cli_command: "python relay.py", local_join: true};
    if (call.path.endsWith("/state")) return {room: "work", members: [], security: {policy: "chat-only"}, member_count: 0, max_members: 8};
  }});
  h.get("invite").onclick();
  assert.match(h.get("invite-text").value, /--invite='invitation'/);
});

test("file preview shows startup help without loading the chat runtime", () => {
  const source = readFileSync(new URL("../static/bootstrap.js", import.meta.url), "utf8");
  const help = {hidden: true}, app = {hidden: false}, scripts = [];
  const document = {getElementById: id => id === "file-help" ? help : app,
    createElement: () => ({}), head: {append: script => scripts.push(script)}};
  vm.runInNewContext(source, {document, location: {protocol: "file:"}});
  assert.equal(help.hidden, false);
  assert.equal(app.hidden, true);
  assert.equal(scripts.length, 0);
  vm.runInNewContext(source, {document, location: {protocol: "http:"}});
  assert.equal(scripts.length, 1);
  assert.equal(scripts[0].src, "./chat.js");
});

test("invitation values starting with a dash stay attached to the CLI option", async () => {
  const h = await browser({hash: "#room=work&invite=-abc123"});
  h.get("invite").onclick();
  assert.match(h.get("invite-text").value, /--invite='-abc123'/);
  assert.doesNotMatch(h.get("invite-text").value, /--invite\s/);
});

test("clicking a member opens an isolated sidebar and sends only to that peer", async () => {
  const storage = new Map(); member(storage);
  const h = await browser({storage, handle: call => call.path.endsWith('/history') && call.data.peer ? {
    messages: [{id:'direct-1', sender:'codex', recipient:'you', content:'private answer', created_at:1}], cursor:1,
  } : undefined});
  const button = descendants(h.get('members')).find(n => n.className === 'member-target' && n.textContent === 'codex');
  button.onclick(); await tick(); await tick();
  assert.equal(h.get('direct-panel').hidden, false);
  assert.equal(h.get('direct-name').textContent, 'codex');
  assert.equal(h.get('message').value, '');
  assert.ok(descendants(h.get('direct-messages')).some(n => n.textContent === 'private answer'));
  assert.ok(!descendants(h.get('messages')).some(n => n.textContent === 'private answer'));
  h.get('direct-message').value = '请检查修改';
  await h.get('direct-composer').onsubmit({preventDefault(){}});
  const send = h.calls.find(c => c.path.endsWith('/send'));
  assert.equal(send.data.to, 'codex'); assert.equal(send.token, 'token-work');
  assert.equal(send.data.content, '请检查修改');
});

test("late sidebar history and sends cannot overwrite another peer's draft", async () => {
  const storage = new Map(); member(storage);
  const oldHistory = deferred(), oldSend = deferred();
  let blockHistory = true;
  const h = await browser({storage, handle: c => {
    if (c.path.endsWith('/history') && c.data.peer === 'codex' && blockHistory) return oldHistory.promise;
    if (c.path.endsWith('/send')) return oldSend.promise;
  }});
  h.ui.openDirect('codex');
  h.get('direct-message').value = 'first';
  const sending = h.get('direct-composer').onsubmit({preventDefault(){}});
  h.ui.openDirect('claude'); h.get('direct-message').value = 'second';
  oldHistory.resolve({messages:[{id:'old',sender:'codex',recipient:'you',content:'late',created_at:1}],cursor:10});
  blockHistory = false; oldSend.resolve({status:'sent'});
  await sending; await tick(); await tick();
  assert.equal(h.get('direct-name').textContent,'claude');
  assert.equal(h.get('direct-message').value,'second');
  assert.ok(!descendants(h.get('direct-messages')).some(n=>n.textContent==='late'));
  h.ui.openDirect('codex');
  assert.equal(h.get('direct-message').value,'');
});

test("sidebar retry reuses its key and changing rooms clears the private view", async () => {
  const storage = new Map(); member(storage); member(storage,'other'); let sends=0;
  const h = await browser({storage, handle:c => { if(c.path.endsWith('/send') && ++sends===1) throw new Error('timeout'); }});
  h.ui.openDirect('codex'); await tick();
  h.get('direct-message').value='retry';
  await h.get('direct-composer').onsubmit({preventDefault(){}});
  await h.get('direct-composer').onsubmit({preventDefault(){}});
  const calls=h.calls.filter(c=>c.path.endsWith('/send'));
  assert.equal(calls[0].data.key,calls[1].data.key);
  await h.ui.selectRoom('other','other-invite');
  assert.equal(h.get('direct-panel').hidden,true);
  assert.equal(h.get('direct-messages').children.length,0);
});

test("local owner collaboration view uses owner credentials and does not claim messages", async () => {
  const storage = new Map([['chat.admin','owner-token']]); member(storage);
  let sent=false;
  const h=await browser({storage,handle:c=>{
    if(c.path.endsWith('/state')) return {members:[{name:'you'},{name:'codex'},{name:'claude'}],collaboration_view:true,member_count:3,max_members:8};
    if(c.path.endsWith('/activity')) { const messages=sent?[]:[{id:'collab',seq:123,sender:'claude',recipient:'codex',content:'reviewed',created_at:1}]; sent=true; return {messages,cursor:123}; }
    if(c.path.endsWith('/activityprogress')) return {messages:[{message_id:'collab',tracked:true,recipients:[{name:'codex',state:'received'}]}]};
  }});
  assert.equal(h.get('timeline-title').textContent,'协作记录');
  assert.ok(descendants(h.get('messages')).some(n=>n.textContent==='#123'));
  assert.ok(descendants(h.get('messages')).some(n=>n.textContent==='claude → codex'));
  assert.ok(descendants(h.get('messages')).some(n=>n.textContent.includes('已领取')));
  assert.equal(h.calls.find(c=>c.path.endsWith('/activity')).token,'owner-token');
  assert.equal(h.calls.find(c=>c.path.endsWith('/activityprogress')).token,'owner-token');
  assert.ok(!h.calls.some(c=>/\/(receive|ack)$/.test(c.path)));
});

test("room close, reopen, delete and restore stay scoped to the selected room", async () => {
  const storage = new Map([["chat.admin", "owner"]]); member(storage);
  let roomState = "open";
  const h = await browser({storage, handle: call => {
    if (call.path === "/api/rooms") return {room_management: true, rooms: (roomState === "deleted") === !!call.data.include_deleted ? [{room: "work", room_state: roomState, member_count: 2}] : []};
    if (call.path.endsWith("/state")) return {room: "work", room_state: roomState, room_management: true, members: [{name: "you"}, {name: "codex"}], member_count: 2, max_members: 8};
    const action = call.path.split("/").at(-1);
    if (["close", "reopen", "delete", "restore"].includes(action)) { roomState = {close: "closed", reopen: "open", delete: "deleted", restore: "closed"}[action]; return {room_state: roomState}; }
  }});
  h.get("close-room").onclick();
  assert.equal(roomState, "open", "Opening a confirmation must not close the room");
  await h.get("room-action-form").onsubmit({preventDefault() {}});
  assert.equal(roomState, "closed");
  assert.ok(h.get("composer").hidden); assert.ok(h.get("invite").disabled);
  await h.get("reopen-room").onclick();
  assert.equal(roomState, "open"); assert.ok(!h.get("composer").hidden);
  h.get("delete-room").onclick();
  await h.get("room-action-form").onsubmit({preventDefault() {}});
  assert.equal(h.ui.state.room, ""); assert.ok(!h.get("show-deleted").hidden);
  assert.ok(storage.get("chat.human.work"), "Recoverable deletion keeps the browser's original member identity");
  await h.get("show-deleted").onclick();
  assert.equal(h.ui.state.rooms[0].room_state, "deleted");
  await h.ui.selectRoom("work");
  assert.ok(!h.get("restore-room").hidden); assert.ok(h.get("composer").hidden);
  await h.get("restore-room").onclick();
  assert.equal(roomState, "closed"); assert.equal(h.ui.state.room, "");
});

test("switching rooms while a confirmation is open never deletes the new room", async () => {
  const storage = new Map([["chat.admin", "owner"]]);
  const h = await browser({storage});
  h.get("delete-room").onclick();
  await h.ui.selectRoom("other", "other-invite");
  await h.get("room-action-form").onsubmit({preventDefault() {}});
  assert.ok(h.calls.some(call => call.path === "/api/rooms/work/delete"));
  assert.ok(!h.calls.some(call => call.path === "/api/rooms/other/delete"));
  assert.equal(h.ui.state.room, "other");
});

test("invitation uses each installation's actual room address without personal paths", async () => {
  for (const origin of ["http://127.0.0.1:8795", "http://localhost:9010"]) {
    const h = await browser({origin, hash: "#room=work", handle: call => {
      if (call.path === "/api/local-session") return {admin_token: "local", local_join: true, cli_command: "C:\\Users\\someone\\Python.exe C:\\Users\\someone\\AirRelay\\relay.py"};
      if (call.path.endsWith("/state")) return {room: "work", security: {policy: "trusted-local"}, members: [], member_count: 0, max_members: 8};
    }});
    h.get("invite").onclick();
    assert.ok(h.get("invite-text").value.includes(`server="${origin}"`));
    assert.doesNotMatch(h.get("invite-text").value, /someone|\/Users\/|C:\\|8789|--data-dir|invite=/);
    h.ui.showRecovery({name: "worker", connection: "network", protocol_version: 2});
    assert.doesNotMatch(h.get("recovery-text").value, /someone|\/Users\/|C:\\|--data-dir/);
    assert.match(h.get("recovery-text").value, /py -3 local_dev.py/);
  }
});

function localOwner({rooms = [], deleted = [], members = []} = {}) {
  const joined = new Set(members);
  return call => {
    if (call.path === "/api/local-session") return {admin_token: "automatic-session", local_join: true};
    if (call.path === "/api/rooms" && call.data.action === "create") { rooms.push(call.data.room); return {room: call.data.room}; }
    if (call.path === "/api/rooms") return {rooms: (call.data.include_deleted ? deleted : rooms).map(room => ({room})), room_management: true};
    if (call.path.endsWith("/humanjoin")) { joined.add(call.data.name); return {status: "joined"}; }
    if (call.path.endsWith("/state")) return {members: [...joined].map(name => ({name})), security: {policy: "trusted-local"}, member_count: joined.size, max_members: 8};
  };
}

test("first local visit creates a room and joins the owner without dialogs", async () => {
  const h = await browser({origin: "http://127.0.0.1:8789", hash: "", handle: localOwner()});
  for (let i = 0; i < 5; i++) await tick();
  assert.ok(h.calls.some(call => call.data.action === "create" && call.data.room === "work"));
  assert.equal(h.ui.state.room, "work");
  const join = h.calls.find(call => call.path === "/api/rooms/work/humanjoin");
  assert.equal(join.data.name, "you");
  assert.equal(h.ui.state.human.joined, true);
  assert.ok(!h.get("create-dialog").open && !h.get("join-dialog").open);
  assert.equal(JSON.parse(h.local.get("chat.human.work")).name, "you", "the local owner keeps its identity across tabs");
  assert.ok(!h.storage.has("chat.human.work"));
  const reopened = await browser({origin: "http://127.0.0.1:8789", hash: "", local: h.local, handle: localOwner({rooms: ["work"], members: ["you"]})});
  for (let i = 0; i < 5; i++) await tick();
  assert.ok(!reopened.calls.some(call => call.path.endsWith("/humanjoin")), "a saved identity is reused, not rejoined");
});

test("auto-join picks a free name and never recreates deleted rooms", async () => {
  const taken = await browser({origin: "http://127.0.0.1:8789", hash: "", handle: localOwner({rooms: ["work"], members: ["you"]})});
  for (let i = 0; i < 5; i++) await tick();
  assert.equal(taken.calls.find(call => call.path.endsWith("/humanjoin")).data.name, "you-2");
  const emptied = await browser({origin: "http://127.0.0.1:8789", hash: "", handle: localOwner({deleted: ["old"]})});
  for (let i = 0; i < 5; i++) await tick();
  assert.ok(!emptied.calls.some(call => call.data.action === "create"));
  assert.equal(emptied.ui.state.room, "");
});

test("network visitors are not joined automatically", async () => {
  const h = await browser();
  for (let i = 0; i < 5; i++) await tick();
  assert.ok(!h.calls.some(call => /\/(human)?join$/.test(call.path)));
});

test("room names with Chinese characters are URL-encoded in API paths", async () => {
  const h = await browser({origin: "http://127.0.0.1:8789", hash: "", handle: localOwner({rooms: ["新项目"]})});
  for (let i = 0; i < 5; i++) await tick();
  assert.equal(h.ui.state.room, "新项目");
  assert.ok(h.calls.some(call => call.path === "/api/rooms/%E6%96%B0%E9%A1%B9%E7%9B%AE/state"));
  assert.ok(!h.calls.some(call => call.path.includes("新项目")));
});

test("a room only remembered by the tab is skipped once it no longer exists", async () => {
  const storage = new Map([["chat.room", "gone"]]);
  const h = await browser({origin: "http://127.0.0.1:8789", hash: "", storage, handle: localOwner({rooms: ["work"]})});
  for (let i = 0; i < 5; i++) await tick();
  assert.equal(h.ui.state.room, "work");
  assert.ok(!h.calls.some(call => call.path.startsWith("/api/rooms/gone/")));
});
