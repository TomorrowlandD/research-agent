const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");
const {chromium} = require("playwright");
const [url, home] = process.argv.slice(2);

async function check() {
  const browser = await chromium.launch();
  try {
    const page = await browser.newPage();
    await page.goto(url);
    async function submit(prompt, count) {
      await page.getByLabel("任务输入").fill(prompt);
      await page.getByRole("button", {name:"发送任务"}).click();
      await page.waitForFunction(n => document.querySelectorAll(".turn-trace").length === n, count);
    }
    await submit("包含委派和缓存", 1);
    const panel = page.locator(".turn-trace").first();
    await panel.locator("summary").first().click();
    const id = await panel.getAttribute("data-trace-id");
    const raw = fs.readFileSync(path.join(home, ".research", "traces", id + ".jsonl"), "utf8")
                  .trim().split("\n").map(line => JSON.parse(line));
    assert.equal(raw.length, 4);
    assert.equal(raw.filter(r => !r.ok).length, 1);
    const candidates = raw.filter(r => ["web_fetch", "web_search"].includes(r.tool_name));
    assert.equal(candidates.length, 2);
    assert.equal(candidates.filter(r => r.cache_hit).length, 1);
    assert.match(await panel.locator(".trace-cache").innerText(), /1\/2（50%）/);
    assert.equal(await panel.locator(".trace-span").count(), raw.length);
    const delegate = raw.find(r => r.tool_name === "agent" && r.agent_role === "main");
    assert.ok(delegate);
    assert.deepEqual(candidates.map(r => [r.agent_role, r.parent_span_id]), Array(2).fill(["explore", delegate.span_id]));
    const longest = Math.max(...raw.map(r => r.latency_ms));
    for (const row of raw) {
      const node = panel.locator(`[data-span-id="${row.span_id}"]`);
      const line = node.locator(":scope > details > summary");
      assert.equal(await node.evaluate(n => n.parentElement.closest(".trace-span")?.dataset.spanId ?? null),
                   row.parent_span_id);
      assert.equal(await line.locator(".trace-tool").innerText(), row.tool_name);
      assert.equal(await line.locator(".trace-role").innerText(), row.agent_role);
      assert.equal(await line.locator(".trace-duration").innerText(), row.latency_ms + " ms");
      assert.deepEqual(await line.locator("meter").evaluate(m => [m.value, m.max]), [row.latency_ms, longest]);
      assert.equal(await line.locator(".trace-hit").count(), Number(row.cache_hit));
      assert.equal(await line.locator(".trace-failure").count(), Number(!row.ok));
      await line.click();
      const fields = await node.locator(":scope > details > dl dd").allTextContents();
      assert.deepEqual(fields, [row.span_id, row.parent_span_id || "—", row.timestamp,
                                row.source_type, row.ok ? "成功" : "失败", String(row.output_chars)]);
    }
    await submit("空任务", 2);
    const empty = page.locator(".turn-trace").last();
    await empty.locator("summary").click();
    assert.notEqual(await empty.getAttribute("data-trace-id"), id);
    assert.match(await empty.innerText(), /没有已记录的工具调用/);
    assert.equal(await panel.locator(".trace-span").count(), 4);
    await page.setViewportSize({width:390, height:800});
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    console.log("4 spans match JSONL; 2 child fetches; 1/2 cache hits; 1 failed tool; separate empty turn.");
  } finally {
    await browser.close();
  }
}
check().catch(error => {console.error(error); process.exitCode = 1;});
