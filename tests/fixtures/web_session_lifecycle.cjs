// Linux acceptance driver. Identify each MCP process tree while its owning tab is alive.
const fs = require("node:fs");
const assert = require("node:assert/strict");
const {chromium} = require("playwright");
const [url, server, output] = process.argv.slice(2);
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

function processInfo(pid) {
  try {
    const stat = fs.readFileSync(`/proc/${pid}/stat`, "utf8");
    const fields = stat.slice(stat.lastIndexOf(")") + 2).split(" ");
    return {pid, ppid:Number(fields[1]), start:fields[19], state:fields[0],
            name:stat.slice(stat.indexOf("(") + 1, stat.lastIndexOf(")"))};
  } catch (error) {
    if (["ENOENT", "ESRCH"].includes(error.code)) return null;
    throw error;
  }
}

function capture(exclude) {
  const all = fs.readdirSync("/proc").filter(x => /^\d+$/.test(x)).map(Number).map(processInfo).filter(Boolean);
  const roots = all.filter(p => p.ppid === Number(server) && p.pid !== process.pid && !exclude.has(p.pid));
  assert.equal(roots.length, 1, "expected one new MCP process tree");
  const tree = [...roots];
  for (const parent of tree) tree.push(...all.filter(p => p.ppid === parent.pid));
  assert.ok(tree.some(p => /chrome|chromium|headless/i.test(p.name)), "no browser launched");
  return tree;
}
function stillPresent(tree) {
  return tree.filter(p => processInfo(p.pid)?.start === p.start);
}
async function until(check, message, limit = 30000) {
  const deadline = Date.now() + limit;
  while (!await check()) {
    if (Date.now() >= deadline) throw new Error(message);
    await sleep(80);
  }
}

async function main() {
  const browser = await chromium.launch();
  const context = await browser.newContext();
  const evidence = {started_utc:new Date().toISOString(), idle_timeout_seconds:4};
  try {
    async function start(label) {
      const tab = await context.newPage();
      await tab.goto(url);
      await tab.getByLabel("任务输入").fill(label);
      await tab.getByRole("button", {name:"发送任务"}).click();
      const card = tab.locator(".approval").filter({hasText:"browser_navigate"});
      await card.getByRole("button", {name:"批准"}).click();
      await until(async () => (await (await fetch(url + "/probe")).json()).includes(label), "navigation failed");
      return tab;
    }
    const survivor = await start("survivor");
    const active = capture(new Set());
    const exclude = new Set(active.map(p => p.pid));
    for (const kind of ["idle", "crash"]) {
      const tab = await start(kind);
      if (kind === "idle") await tab.locator(".approval").filter({hasText:"browser_snapshot"}).waitFor();
      const owned = capture(exclude);
      const startTime = Date.now();
      if (kind === "idle") {
        await tab.getByText("会话因空闲已关闭，刷新可开始新会话").waitFor();
      } else {
        const cdp = await context.newCDPSession(tab);
        const crashed = tab.waitForEvent("crash");
        cdp.send("Page.crash").catch(() => {});
        await crashed;
      }
      try {
        await until(() => stillPresent(owned).length === 0, kind + " cleanup timed out", 75000);
      } catch (error) {
        console.error("remaining process identities", stillPresent(owned).map(p => processInfo(p.pid)));
        throw error;
      }
      const survivorPids = stillPresent(active).map(p => p.pid);
      assert.equal(survivorPids.length, active.length, "active session lost a process");
      evidence[kind] = {before:owned, remaining:stillPresent(owned), survivor_pids:survivorPids,
                        elapsed_ms:Date.now()-startTime};
      await tab.close();
    }
    await fetch(url + "/release", {method:"POST"});
    await survivor.locator(".approval").filter({hasText:"browser_snapshot"}).getByRole("button",{name:"批准"}).click();
    await survivor.getByText("已完成", {exact:true}).waitFor();
    assert.equal((await survivor.locator(".entry").last().locator("p").innerText()).trim(), "SESSION-PROBE");
    await until(() => stillPresent(active).length === 0, "survivor child did not exit after completing its task");
    evidence.survivor = {before:active, final:"SESSION-PROBE", remaining:stillPresent(active)};
    fs.writeFileSync(output, JSON.stringify(evidence, null, 2) + "\n");
    console.log("Both target process trees exited; survivor returned SESSION-PROBE.");
  } finally {
    await browser.close();
  }
}
main().catch(error => {console.error(error); process.exitCode = 1;});
