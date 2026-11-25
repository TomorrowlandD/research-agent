// Run against agents.web in a disposable workspace using a configured model.
const assert = require("node:assert/strict");
const {randomUUID} = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const {chromium} = require("playwright");

async function main(url, workspace) {
  if (!url || !workspace || new URL(url).hostname !== "127.0.0.1") {
    throw new Error("Usage: node web_session_browser.cjs http://127.0.0.1:PORT /temporary/workspace");
  }
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 900, height: 700}});
    await page.goto(url);
    const id = randomUUID();
    for (const allow of [true, false]) {
      const name = `trial-${id}-${allow}.txt`;
      const target = path.join(workspace, name);
      const marker = allow ? "ALLOW-OK" : "DENY-OK";
      assert.equal(fs.existsSync(target), false);
      const prompt = `请调用一次 write_file，向当前工作区的 ${name} 写入 P31。根据工具的实际结果回复：成功时只回复 ALLOW-OK，被拒绝时只回复 DENY-OK。被拒绝后直接结束，不要重试或改用其他工具。`;
      await page.getByLabel("任务输入").fill(prompt);
      await page.getByRole("button", {name: "发送任务"}).click();
      const card = page.locator(".approval").filter({hasText: name});
      await card.waitFor({timeout: 90000});
      assert.equal(fs.existsSync(target), false);
      await card.getByRole("button", {name: allow ? "批准" : "拒绝"}).click();
      await page.waitForFunction(() => document.querySelector("#status").textContent === "已完成",
                                 null, {timeout: 90000});
      const final = await page.locator(".entry").last().locator("p").innerText();
      assert.equal(final.trim(), marker);
      assert.equal(fs.existsSync(target), allow);
      if (allow) assert.equal(fs.readFileSync(target, "utf8").trim(), "P31");
      console.log(JSON.stringify({allow, final: final.trim(), fileExists: fs.existsSync(target)}));
    }
    await page.setViewportSize({width: 390, height: 800});
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  } finally {
    await browser.close();
  }
}

main(process.argv[2], process.argv[3]).catch(error => { console.error(error); process.exitCode = 1; });
