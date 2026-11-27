// Run with Node 22 against a running Vite dev server and a local Chromium debugging endpoint.
// node tests/interactions.mjs http://127.0.0.1:5173 http://127.0.0.1:9222
import assert from 'node:assert/strict';
import { setTimeout as sleep } from 'node:timers/promises';
const [app='http://127.0.0.1:5173', browser='http://127.0.0.1:9222'] = process.argv.slice(2);
const target = await fetch(browser + '/json/new?about:blank', { method:'PUT' }).then(r=>r.json());
const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((resolve,reject)=>{ ws.onopen=resolve; ws.onerror=reject; });
const pending = new Map();
const errors = [];
let seq=0;
ws.onmessage=({data})=>{
  const msg=JSON.parse(data);
  if (msg.method==='Runtime.exceptionThrown') errors.push(msg.params.exceptionDetails.text);
  if (msg.method==='Runtime.consoleAPICalled' && msg.params.type==='error') errors.push(msg.params.args.map(a=>a.value).join(' '));
  if (!msg.id) return;
  const {resolve,reject}=pending.get(msg.id);
  pending.delete(msg.id);
  if (msg.error) reject(new Error(msg.error.message)); else resolve(msg.result);
};
function call(method,params={}) {
  return new Promise((resolve,reject)=>{ pending.set(++seq,{resolve,reject}); ws.send(JSON.stringify({id:seq,method,params})); });
}
async function evaluate(expression) {
  const r=await call('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});
  if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description ?? r.exceptionDetails.text);
  return r.result.value;
}
async function until(condition) {
  for (let i=0;i<100;i++) {
    if (await evaluate(`Boolean(${condition})`)) return;
    await sleep(25);
  }
  assert.fail(condition);
}
async function key(key,modifiers=0) {
  const fields={key,modifiers,windowsVirtualKeyCode:({Enter:13,Escape:27,ArrowDown:40})[key]};
  if (key==='Enter') fields.text='\r';
  await call('Input.dispatchKeyEvent',{type:'keyDown',...fields});
  await call('Input.dispatchKeyEvent',{type:'keyUp',...fields,text:''});
}
async function type(value) { await call('Input.insertText',{text:value}); }
async function click(text) { await evaluate('btn('+JSON.stringify(text)+').click()'); }
async function input(value) { await evaluate('ta().focus(); ta().select()'); await type(value); }
try {
  await call('Runtime.enable');
  await call('Emulation.setDeviceMetricsOverride',{width:1280,height:720,deviceScaleFactor:1,mobile:false});
  await call('Page.navigate',{url:app});
  await until("document.querySelector('textarea')");
  await evaluate(`(async()=>{
    const url=performance.getEntriesByType('resource').find(e=>e.name.includes('/src/store/useAppStore.ts')).name;
    window.store=(await import(url)).useAppStore;
    window.state=()=>store.getState();
    window.current=()=>state().sessions.find(s=>s.id===state().activeSessionId);
    window.btn=t=>Array.from(document.querySelectorAll('button')).find(b=>b.textContent.trim()===t);
    window.side=t=>Array.from(document.querySelectorAll('aside button')).find(b=>b.textContent.startsWith(t)).click();
    window.ta=()=>document.querySelector('textarea');
    window.list=()=>document.querySelector('[role=listbox]');
    window.query=()=>document.querySelector('input[placeholder*=轨迹]');
  })()`);

  // Reuse the same button reference within one event turn to exercise the store guard.
  await evaluate("window.before=current().traces.length;const approve=btn('允许执行');approve.click();approve.click();state().resolveApproval(current().id,'req-pytest-001','rejected')");
  await until("current().traces.length===before+1 && current().messages[1].approval.status==='approved'");

  await click('轨迹');
  await evaluate('query().focus()'); await type('does-not-match');
  await until("document.querySelector('main').innerText.includes('暂无匹配的轨迹事件')");
  await evaluate("side('搭建个人主页')");
  await until("query()?.value==='' && document.querySelectorAll('[id^=trace-row]').length===2");
  await evaluate("side('Markdown 转 LaTeX')");
  await until("document.querySelector('main').innerText.includes('暂无轨迹事件')");
  await click('新建任务'); await click('轨迹');
  for (const mode of ['时长','轮次','调用']) {
    await click(mode);
    await until("!ta() && document.querySelector('main').innerText.includes('暂无轨迹事件') && !/NaN|Infinity/.test(document.querySelector('main').innerHTML)");
  }
  console.log('PASS duplicate approval, search isolation, no matches and empty traces in all modes');
  await click('对话');
  await input('   '); await key('Enter');
  await until("!current() && document.querySelector('button[title=发送]').disabled");
  await input('拼音');
  await evaluate("ta().dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',isComposing:true,bubbles:true,cancelable:true}));ta().dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',keyCode:229,bubbles:true,cancelable:true}))");
  await until("!current() && ta().value==='拼音'");
  await key('Enter',8); await type('第二行');
  await until("!current() && ta().value.includes('\\n')");
  for (const trigger of ['/', '@']) {
    await input(trigger); await until("list()?.querySelector('[role=option]')");
    await evaluate("ta().dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',isComposing:true,bubbles:true}))");
    await until("!!list() && !current()");
    await key('ArrowDown'); await key('Enter');
    await until("!list() && !current() && ta().value.length>2");
    await input(trigger); await key('Escape'); await until('!list() && !current()');
    await input(trigger);
    await call('Input.dispatchMouseEvent',{type:'mousePressed',x:700,y:15,button:'left',clickCount:1});
    await call('Input.dispatchMouseEvent',{type:'mouseReleased',x:700,y:15,button:'left',clickCount:1});
    await until('!list() && !current()');
  }
  console.log('PASS IME composing/229, Shift+Enter, whitespace, / and @ selection, Esc and outside click');

  await input('isolation-A'); await key('Enter');
  await until("current()?.status==='running' && current().messages.length===2");
  await evaluate("window.a=current().id;window.partial=current().messages[1].content;side('搭建个人主页')");
  await until("!document.querySelector('main').innerText.includes('isolation-A')");
  await evaluate("window.b=current().id;window.bBefore=JSON.stringify(current());document.querySelector('[aria-label^=上下文已用]').click()");
  await until("!!document.querySelector('[aria-label=上下文用量明细]')");
  await click('新建任务');
  await until("!current() && !document.querySelector('[aria-label=上下文用量明细]')");
  await until("state().sessions.find(s=>s.id===a).status==='idle'");
  await until("state().sessions.find(s=>s.id===a).messages[1].content.length>partial.length && JSON.stringify(state().sessions.find(s=>s.id===b))===bBefore && !document.querySelector('main').innerText.includes('isolation-A')");
  await input('/'); await until('!!list()'); await evaluate("side('isolation-A')");
  await until("!list() && ta().value==='' && current().id===a");
  await input('stop-mid-stream'); await key('Enter');
  await until("current().status==='running' && current().messages.length===4");
  await evaluate("document.querySelector('[title=停止当前回合]').click(); state().stopRun();window.stopped=JSON.stringify(current())");
  await sleep(1100);
  await until("current().status==='stopped' && JSON.stringify(current())===stopped && current().messages.filter(m=>m.role==='system_notice').length===1 && !current().messages[3].checkpoint");
  console.log('PASS A/B/new isolation during mock stream, overlay unmount, stop blocks later chunks');

  // Inject deliberately unbroken fixtures into this disposable tab, then exercise normal UI controls.
  await evaluate(`side('排查多项目'); const s=current();const fixture=structuredClone(s);
    const long='x'.repeat(6000);const tool=fixture.messages[1].toolCalls;
    tool[0].args=JSON.stringify({value:long});tool[0].output=long;tool[1].diff.filePath='/tmp/'+long;
    tool[1].diff.lines=[{type:'add',content:long}];fixture.messages[0].content=long;
    fixture.messages[1].content=long;fixture.traces[0].resultPreview=long;fixture.traces[0].detail=long;
    store.setState({sessions:state().sessions.map(x=>x.id===s.id?fixture:x)});
    window.chatScroll=()=>document.querySelector('main .overflow-y-auto');
    window.bounds=()=>[document.querySelector('header').getBoundingClientRect().top,ta().getBoundingClientRect().top];
    window.fits=()=>[document.documentElement,document.body].every(el=>el.scrollWidth<=1280 && el.scrollHeight<=720);
  `);
  await evaluate("Array.from(document.querySelectorAll('main button')).find(b=>b.textContent.includes('read_file')).click()");
  for (const toggle of ['收起侧边栏','展开侧边栏']) {
    await until("fits() && chatScroll().scrollWidth<=chatScroll().clientWidth && chatScroll().scrollHeight>chatScroll().clientHeight");
    const pos=await evaluate('bounds()');
    await evaluate("chatScroll().scrollTop=chatScroll().scrollHeight");
    assert.deepEqual(await evaluate('bounds()'),pos);
    await evaluate('document.querySelector('+JSON.stringify('[title="'+toggle+'"]')+').click()');
  }
  await click('轨迹'); await evaluate("document.querySelector('[id^=trace-row] button').click()");
  await until("fits() && Array.from(document.querySelectorAll('main .overflow-y-auto')).every(e=>e.scrollWidth<=e.clientWidth)");
  assert.deepEqual(errors,[]);
  console.log('PASS 1280x720 long JSON/path/code, sidebar toggle, stationary header/composer, trace layout and console');
} finally {
  ws.close();
  await fetch(browser+'/json/close/'+target.id);
}
