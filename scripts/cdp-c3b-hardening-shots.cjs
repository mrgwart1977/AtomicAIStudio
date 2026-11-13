// C3-B hardening evidence — nav overflow fix + authoritative cost +
// authenticated thumbnails via CDP on :9222.
const fs = require("fs");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function main() {
  const list = await (await fetch("http://localhost:9222/json")).json();
  const page = list.find((t) => t.type === "page" && t.url.includes("1420"));
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  let id = 0; const p = new Map();
  const send = (m, pr = {}) => new Promise((r) => {
    const i = ++id; p.set(i, r);
    ws.send(JSON.stringify({ id: i, method: m, params: pr }));
  });
  ws.onmessage = (e) => {
    const m = JSON.parse(e.data);
    if (m.id && p.has(m.id)) { p.get(m.id)(m.result); p.delete(m.id); }
  };
  await new Promise((r) => (ws.onopen = r));
  await send("Page.enable");
  await send("Runtime.enable");
  const evalJs = async (expr) =>
    (await send("Runtime.evaluate", { expression: expr, awaitPromise: true })).result?.value;

  fs.mkdirSync("docs/c3b-evidence", { recursive: true });
  const shot = async (name) => {
    const s = await send("Page.captureScreenshot", { format: "png" });
    fs.writeFileSync(`docs/c3b-evidence/${name}.png`, Buffer.from(s.data, "base64"));
    console.log("saved", name);
  };
  const metrics = (w, h) => send("Emulation.setDeviceMetricsOverride",
    { width: w, height: h, deviceScaleFactor: 1, mobile: false });
  const setSel = (sel, v) => `(()=>{const el=document.querySelector('${sel}');if(!el)return 'missing';
    const st=Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value').set;
    st.call(el,'${v}');el.dispatchEvent(new Event('change',{bubbles:true}));return el.value;})()`;
  const setTa = (sel, v) => `(()=>{const el=document.querySelector('${sel}');if(!el)return 'missing';
    const st=Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set;
    st.call(el,${JSON.stringify(v)});el.dispatchEvent(new Event('input',{bubbles:true}));return el.value;})()`;

  // ---- 1440: approval card showing AUTHORITATIVE backend estimate ----
  await metrics(1440, 900);
  await send("Page.navigate", { url: "http://localhost:1420/#project/cloud" });
  await sleep(3500);
  console.log("provider:", await evalJs(setSel("#ci-provider", "openai-images")));
  await sleep(1200);
  await evalJs(setTa("#ci-prompt", "a neon-lit keyframe — authoritative cost check"));
  await evalJs(setSel("#ci-quality", "medium"));
  await sleep(300);
  await evalJs(`(()=>{document.querySelector('[data-testid="ci-preview-btn"]').click();return true;})()`);
  await sleep(1500);
  await evalJs(`(()=>{const el=document.querySelector('[data-testid="ci-approval"]');if(el)el.scrollIntoView({block:'center'});return !!el;})()`);
  await sleep(600);
  await shot("06-approval-authoritative-1440");

  // ---- 1440: job card with authenticated blob thumbnail ----
  await evalJs(`(()=>{const el=[...document.querySelectorAll('h3')].find(x=>x.textContent.includes('Cloud image jobs'));if(el)el.scrollIntoView({block:'start'});return !!el;})()`);
  await sleep(900);
  // confirm the DOM holds an object URL, not a token URL
  console.log("thumb src:", await evalJs(`(()=>{const i=document.querySelector('[data-testid^="ci-thumb-"]');return i?i.src.slice(0,40):'none';})()`));
  console.log("token in DOM:", await evalJs(`(()=>document.body.innerHTML.includes('token='))()`));
  await shot("07-job-cards-blob-thumb-1440");

  // ---- 1100: nav scroll — last tab reachable & scrolled into view ----
  await metrics(1100, 700);
  await sleep(900);
  await evalJs(`(()=>{const b=[...document.querySelectorAll('nav[aria-label="Workspace"] button')].find(x=>x.textContent.trim()==='Settings');if(b)b.scrollIntoView({inline:'end'});return !!b;})()`);
  await sleep(600);
  await shot("08-nav-scroll-1100");
  // prove it via metrics
  const nav = await evalJs(`(()=>{const n=document.querySelector('nav[aria-label="Workspace"]');return {sw:n.scrollWidth,cw:n.clientWidth,scrolls:n.scrollWidth>n.clientWidth}})()`);
  console.log("nav@1100:", JSON.stringify(nav));
  await evalJs(`(()=>{const el=document.querySelector('[data-testid="cloud-image-panel"]');if(el)el.scrollIntoView({block:'start'});return !!el;})()`);
  await sleep(600);
  await shot("09-panel-1100-fixed");

  // ---- 1920: everything visible, no scroll ----
  await metrics(1920, 1000);
  await sleep(900);
  await evalJs(`(()=>{const el=document.querySelector('nav[aria-label="Workspace"]');if(el)el.scrollIntoView({block:'start'});return !!el;})()`);
  await sleep(500);
  await shot("10-nav-full-1920");

  process.exit(0);
}
main().catch((e) => { console.error(e); process.exit(1); });
