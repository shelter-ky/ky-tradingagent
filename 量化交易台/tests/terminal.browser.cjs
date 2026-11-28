// All prices and account values in these browser tests are synthetic.
const assert = require("node:assert/strict");
const { readFile } = require("node:fs/promises");
const path = require("node:path");
const { after, before, test } = require("node:test");
const { chromium } = require("playwright");

const panel = path.resolve(__dirname, "..");
const origin = "http://127.0.0.1:18888";
const start = new Date("2025-01-02T16:00:00Z");
let browser;

before(async () => {
  browser = await chromium.launch({ executablePath: process.env.CHROME_EXECUTABLE || undefined });
});
after(async () => { if (browser) await browser.close(); });

function fixture() {
  const dates = Array.from({length: 70}, (_, day) => new Date(Date.UTC(2025, 0, day + 1)).toISOString().slice(0, 10));
  const daily = dates.map((_, i) => [i === 0 ? 140 : i === 1 ? 70 : 105, 0, 1, 100, 0, 0]);
  const account = { daily, targets: dates.map(() => [1]), orders: [] };
  return { initial: 100, dates, symbols: {TQQQ: Array(5).fill(account), QQQ: Array(5).fill(account)},
           market: dates.map(() => [20, 20, "MISSING", 390, 390]) };
}

async function openPanel(t) {
  const page = await browser.newPage();
  t.after(() => page.close());
  await page.clock.install({ time: start });
  const demo = 'window.REPLAY = ' + JSON.stringify(fixture()) + ';';
  await page.route(origin + '/**', async route => {
    const filename = decodeURIComponent(new URL(route.request().url()).pathname.slice(1));
    if (filename === "回放数据.js") return route.fulfill({contentType: "text/javascript", body: demo});
    const file = filename || "交易面板.html";
    const type = {".html": "text/html", ".js": "text/javascript", ".css": "text/css"}[path.extname(file)];
    if (!type) return route.fulfill({status: 404});
    return route.fulfill({contentType: type + "; charset=utf-8", body: await readFile(path.join(panel, file))});
  });
  await page.addInitScript(() => {
    window.pendingSignals = [];
    window.fetch = (url, options) => new Promise(resolve => window.pendingSignals.push({
      url, respond: report => resolve({ok: true, json: async () => ({report})}),
      signal: options?.signal
    }));
  });
  await page.goto(origin);
  return page;
}

function report(ticker, reason) {
  return {version:"intraday-v1", ticker, reason, status:"READY", session:"REGULAR", action:"WATCH_RISK",
          generated_at:start.toISOString(), valid_until:new Date(start.getTime() + 90000).toISOString(),
          snapshots: (ticker === "TQQQ" ? ["TQQQ","QQQ"] : [ticker]).map(ticker => ({ticker, price: 99}))};
}

async function respond(page, index, data) {
  await page.evaluate(([i, value]) => window.pendingSignals[i].respond(value), [index, data]);
}

test("09:35 strategy shows forward results and hides expired account quantities", async t => {
  const page=await openPanel(t);
  await page.click('[data-view="signals"]');
  await page.selectOption("#signalStrategy","opening0935");
  assert.match(await page.evaluate(()=>pendingSignals[1].url),/strategy=opening0935/);
  const sample={...report("TQQQ","半仓"),strategy_id:"opening0935-v1",
    opening:{previous:1,target:.5,vxn:19,median:20,vxn_date:"2025-01-01",pre:"MISSING"},
    sizing:{status:"CHECKS_PASSED",held:65,available:60,validated_allocation:{delta_quantity:-30,target_quantity:35}},
    forward_events:[{signal:start.toISOString(),earliest:start.toISOString(),qty:35,fill:100,
      after15_result:{avoided:70,missed:0,fees:5,net:65}}]};
  await respond(page,1,sample);
  assert.match(await page.textContent("#sellNote"),/50.00%/);
  assert.match(await page.textContent("#sizingRows"),/-30/);
  assert.match(await page.textContent("#forwardRows"),/3,500.00/);
  assert.match(await page.textContent("#forwardRows"),/65.00/);
  await page.clock.runFor(91000);
  assert.doesNotMatch(await page.textContent("#sizingRows"),/-30/);
  assert.match(await page.textContent("#forwardRows"),/65.00/);
});

test("slow response survives a polling interval and expires on screen", async t => {
  const page = await openPanel(t);
  await page.click('[data-view="signals"]');
  await page.clock.runFor(11000);
  assert.equal(await page.evaluate(() => pendingSignals.length), 1);
  await respond(page, 0, report("TQQQ", "slow report"));
  assert.equal(await page.textContent("#signalReason"), "slow report");
  await page.clock.runFor(90000);
  assert.equal(await page.textContent("#signalStatus"), "已过期 · 等待新信号");
  assert.match(await page.textContent("#sellStatus"), /存档/);
});

test("a timed-out fetch releases its slot and the next poll recovers", async t => {
  const page = await openPanel(t);
  await page.evaluate(() => {
    window.fetch = (url, options) => new Promise((resolve, reject) => {
      options.signal.addEventListener("abort", () => reject(new Error("timeout")), {once:true});
      pendingSignals.push({url, respond: report => resolve({ok:true, json:async () => ({report})})});
    });
  });
  await page.click('[data-view="signals"]');
  await page.clock.runFor(41000);
  assert.equal(await page.evaluate(() => pendingSignals.length), 2);
  await respond(page, 1, report("TQQQ", "recovered"));
  assert.equal(await page.textContent("#signalReason"), "recovered");
});

test("switching ticker or importing a file supersedes an in-flight poll", async t => {
  const page = await openPanel(t);
  await page.click('[data-view="signals"]');
  await page.selectOption("#signalSymbol", "QQQ");
  await respond(page, 1, report("QQQ", "current"));
  await respond(page, 0, report("TQQQ", "stale"));
  assert.equal(await page.textContent("#signalReason"), "current");
  await page.click("#resume");
  await page.setInputFiles("#signalFile", {name:"latest.json", mimeType:"application/json", buffer:Buffer.from(JSON.stringify(report("QQQ","manual")))});
  await page.waitForFunction(() => document.querySelector("#signalReason").textContent === "manual");
  await respond(page, 2, report("QQQ", "superseded"));
  assert.equal(await page.textContent("#signalReason"), "manual");
  await page.click("#resume");
  await respond(page, 3, null);
  assert.match(await page.textContent("#signalSource"), /未找到/);
});

test("drawdown uses only past peaks, including peaks before the chart window", async t => {
  const page = await openPanel(t);
  assert.equal(await page.textContent("#maxdd"), "50.00%");
  assert.match(await page.textContent("#draw"), /25.00%/);
  await page.click('[data-value="dd"]');
  await page.click('[data-value="63"]');
  assert.match(await page.textContent("#chartReadout"), /-25.00%/);
  await page.fill("#date", "2025-01-01");
  await page.dispatchEvent("#date", "change");
  assert.equal(await page.textContent("#maxdd"), "0.00%");
  assert.match(await page.textContent("#chartReadout"), /策略 0.00%/);
});

test("chart pointer uses cached history and redraws once within the same session", async t => {
  const page = await openPanel(t);
  const result = await page.evaluate(() => {
    const canvas = document.getElementById("chart");
    let reads = 0, paints = 0;
    for (const account of [REPLAY.symbols.TQQQ[3], REPLAY.symbols.TQQQ[0]]) {
      for (const row of account.daily) {
        const value = row[0];
        Object.defineProperty(row, 0, { get() { reads++; return value; } });
      }
    }
    const ctx = canvas.getContext("2d"), clear = ctx.clearRect.bind(ctx);
    ctx.clearRect = (...args) => { paints++; clear(...args); };
    const clientX = canvas.getBoundingClientRect().left + Number(canvas.dataset.left) + 2;
    for (let i=0; i<20; i++) canvas.dispatchEvent(new PointerEvent("pointermove",{clientX}));
    return {reads, paints};
  });
  assert.deepEqual(result, {reads:0, paints:1});
});
