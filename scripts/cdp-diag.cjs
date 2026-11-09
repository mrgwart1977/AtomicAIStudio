// One-off CDP diagnostic: grab console errors + a screenshot of a page
// already open in a remote-debugging browser (:9222).
// Usage: node scripts/cdp-diag.cjs <url-substring> [shot.png]
const target = process.argv[2] || "localhost:1420";
const shot = process.argv[3];

async function main() {
  const list = await (await fetch("http://localhost:9222/json")).json();
  const page = list.find((t) => t.type === "page" && t.url.includes(target));
  if (!page) { console.error("no page for", target); process.exit(1); }
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  let id = 0;
  const pending = new Map();
  const send = (method, params = {}) =>
    new Promise((res) => { const i = ++id; pending.set(i, res); ws.send(JSON.stringify({ id: i, method, params })); });
  ws.onmessage = async (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m.result); pending.delete(m.id); }
    if (m.method === "Runtime.exceptionThrown")
      console.log("EXCEPTION:", JSON.stringify(m.params.exceptionDetails, null, 1).slice(0, 2000));
    if (m.method === "Runtime.consoleAPICalled" && ["error", "warning"].includes(m.params.type))
      console.log("CONSOLE", m.params.type, m.params.args.map((a) => a.value ?? a.description ?? "").join(" ").slice(0, 500));
    if (m.method === "Log.entryAdded" && ["error"].includes(m.params.entry.level))
      console.log("LOG", JSON.stringify(m.params.entry).slice(0, 500));
  };
  await new Promise((r) => (ws.onopen = r));
  await send("Runtime.enable");
  await send("Log.enable");
  await send("Page.enable");
  await send("Page.reload", { ignoreCache: true });
  await new Promise((r) => setTimeout(r, 6000));
  const html = await send("Runtime.evaluate", { expression: "document.getElementById('root')?.innerHTML.length ?? -1" });
  console.log("root innerHTML length:", html?.result?.value);
  if (shot) {
    const s = await send("Page.captureScreenshot", { format: "png" });
    if (s?.data) require("fs").writeFileSync(shot, Buffer.from(s.data, "base64"));
    console.log("shot:", shot);
  }
  process.exit(0);
}
main().catch((e) => { console.error(e); process.exit(1); });
