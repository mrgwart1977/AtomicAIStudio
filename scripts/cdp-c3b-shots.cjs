// C3-B evidence shots — Cloud Studio image panel via CDP on :9222.
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

  // ---- 1440x900: panel top — form + disabled providers + SIM banner ----
  await metrics(1440, 900);
  await send("Page.navigate", { url: "http://localhost:1420/#project/cloud" });
  await sleep(3500);
  await evalJs(`(()=>{const el=document.querySelector('[data-testid="cloud-image-panel"]');if(el)el.scrollIntoView({block:'start'});return !!el;})()`);
  await sleep(800);
  await shot("01-image-panel-form-1440");

  // ---- fill form + preview → approval card ----
  const setSel = (sel, v) => `(()=>{const el=document.querySelector('${sel}');if(!el)return 'missing';
    const st=Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value').set;
    st.call(el,'${v}');el.dispatchEvent(new Event('change',{bubbles:true}));return el.value;})()`;
  const setTa = (sel, v) => `(()=>{const el=document.querySelector('${sel}');if(!el)return 'missing';
    const st=Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set;
    st.call(el,${JSON.stringify(v)});el.dispatchEvent(new Event('input',{bubbles:true}));return el.value;})()`;

  console.log("provider:", await evalJs(setSel("#ci-provider", "openai-images")));
  await sleep(1200);
  console.log("model:", await evalJs(setSel("#ci-model", "gpt-image-1")));
  console.log("prompt:", await evalJs(setTa("#ci-prompt", "a neon-lit storyboard keyframe — rainy alley, cinematic")));
  await evalJs(setSel("#ci-size", "1536x1024"));
  await evalJs(setSel("#ci-quality", "medium"));
  await sleep(300);
  await evalJs(`(()=>{document.querySelector('[data-testid="ci-preview-btn"]').click();return true;})()`);
  await sleep(1500);
  await evalJs(`(()=>{const el=document.querySelector('[data-testid="ci-approval"]');if(el)el.scrollIntoView({block:'center'});return !!el;})()`);
  await sleep(600);
  await shot("02-approval-card-1440");

  // ---- scroll to job cards — succeeded job + thumbnail + fin row ----
  await evalJs(`(()=>{const el=[...document.querySelectorAll('h3')].find(x=>x.textContent.includes('Cloud image jobs'));if(el)el.scrollIntoView({block:'start'});return !!el;})()`);
  await sleep(800);
  await shot("03-job-cards-1440");

  // ---- min window 1100x700 ----
  await metrics(1100, 700);
  await sleep(900);
  await evalJs(`(()=>{const el=document.querySelector('[data-testid="cloud-image-panel"]');if(el)el.scrollIntoView({block:'start'});return !!el;})()`);
  await sleep(600);
  await shot("04-image-panel-1100");
  await evalJs(`(()=>{const el=[...document.querySelectorAll('h3')].find(x=>x.textContent.includes('Cloud image jobs'));if(el)el.scrollIntoView({block:'start'});return !!el;})()`);
  await sleep(600);
  await shot("05-job-cards-1100");

  process.exit(0);
}
main().catch((e) => { console.error(e); process.exit(1); });
