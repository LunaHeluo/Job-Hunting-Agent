import fs from "node:fs/promises";
import path from "node:path";

const outputDir = path.resolve("artifacts/unified-shell-qa");
const targets = await fetch("http://127.0.0.1:9225/json/list").then(response => response.json());
const target = targets.find(item => item.type === "page" && item.url.startsWith("http://127.0.0.1:8001/"));
if (!target) throw new Error("Starter Agent page target was not found");

const socket = new WebSocket(target.webSocketDebuggerUrl);
const pending = new Map();
let sequence = 0;

socket.addEventListener("message", event => {
  const message = JSON.parse(event.data);
  if (!message.id) return;
  const waiter = pending.get(message.id);
  if (!waiter) return;
  pending.delete(message.id);
  if (message.error) waiter.reject(new Error(JSON.stringify(message.error)));
  else waiter.resolve(message.result);
});

await new Promise((resolve, reject) => {
  socket.addEventListener("open", resolve, { once: true });
  socket.addEventListener("error", reject, { once: true });
});

function send(method, params = {}) {
  const id = ++sequence;
  socket.send(JSON.stringify({ id, method, params }));
  return new Promise((resolve, reject) => pending.set(id, { resolve, reject }));
}

async function evaluate(expression, awaitPromise = false) {
  const result = await send("Runtime.evaluate", {
    expression,
    awaitPromise,
    returnByValue: true,
    userGesture: true,
  });
  if (result.exceptionDetails) {
    throw new Error(result.exceptionDetails.exception?.description || result.exceptionDetails.text);
  }
  return result.result.value;
}

const delay = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));

async function waitFor(expression, timeout = 10000) {
  const started = Date.now();
  while (Date.now() - started < timeout) {
    if (await evaluate(expression)) return;
    await delay(75);
  }
  throw new Error(`Timed out waiting for: ${expression}`);
}

async function pressKey(key, { shift = false } = {}) {
  const modifiers = shift ? 8 : 0;
  await send("Input.dispatchKeyEvent", { type: "keyDown", key, code: key, modifiers });
  await send("Input.dispatchKeyEvent", { type: "keyUp", key, code: key, modifiers });
  await delay(100);
}

async function clickSelector(selector) {
  const point = await evaluate(`(() => {
    const rect = document.querySelector(${JSON.stringify(selector)}).getBoundingClientRect();
    return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
  })()`);
  await send("Input.dispatchMouseEvent", { type: "mouseMoved", ...point });
  await send("Input.dispatchMouseEvent", { type: "mousePressed", button: "left", clickCount: 1, ...point });
  await send("Input.dispatchMouseEvent", { type: "mouseReleased", button: "left", clickCount: 1, ...point });
  await delay(75);
}

async function replaceText(selector, value) {
  await clickSelector(selector);
  await send("Input.dispatchKeyEvent", { type: "keyDown", key: "a", code: "KeyA", modifiers: 2, windowsVirtualKeyCode: 65 });
  await send("Input.dispatchKeyEvent", { type: "keyUp", key: "a", code: "KeyA", modifiers: 2, windowsVirtualKeyCode: 65 });
  await send("Input.dispatchKeyEvent", { type: "keyDown", key: "Backspace", code: "Backspace", windowsVirtualKeyCode: 8 });
  await send("Input.dispatchKeyEvent", { type: "keyUp", key: "Backspace", code: "Backspace", windowsVirtualKeyCode: 8 });
  if (value) await send("Input.insertText", { text: value });
  await delay(75);
  return evaluate(`document.querySelector(${JSON.stringify(selector)}).value`);
}

await send("Page.enable");
await send("Runtime.enable");
await send("Network.enable");
await send("Network.setCacheDisabled", { cacheDisabled: true });
await send("Page.navigate", { url: `http://127.0.0.1:8001/?qa=${Date.now()}#/workbench` });
await waitFor('document.readyState === "complete" && !!document.querySelector(".workbench-layout")');
await delay(2000);

const viewports = [
  { width: 1280, height: 800 },
  { width: 1440, height: 900 },
  { width: 1920, height: 1080 },
];
const geometry = [];

for (const viewport of viewports) {
  await send("Emulation.setDeviceMetricsOverride", {
    width: viewport.width,
    height: viewport.height,
    deviceScaleFactor: 1,
    mobile: false,
    screenWidth: viewport.width,
    screenHeight: viewport.height,
  });
  await delay(350);
  const typedValue = viewport.width <= 1440
    ? await replaceText("#messageInput", `Edge ${viewport.width} composer input`)
    : "";
  const measured = await evaluate(`(() => {
    const rect = selector => {
      const value = document.querySelector(selector).getBoundingClientRect();
      return { top: Math.round(value.top), bottom: Math.round(value.bottom), height: Math.round(value.height) };
    };
    const left = rect(".workbench-left");
    const canvas = rect(".workbench-canvas");
    const context = rect(".workbench-context-card");
    const interactive = selector => {
      const element = document.querySelector(selector);
      const value = element.getBoundingClientRect();
      const centerX = value.left + value.width / 2;
      const centerY = value.top + value.height / 2;
      const hit = document.elementFromPoint(centerX, centerY);
      return {
        top: Math.round(value.top), bottom: Math.round(value.bottom), height: Math.round(value.height),
        width: Math.round(value.width), withinViewport: value.top >= 0 && value.bottom <= innerHeight,
        visible: value.width > 0 && value.height > 0 && getComputedStyle(element).visibility !== "hidden",
        hitTarget: hit === element || element.contains(hit),
      };
    };
    return {
      viewport: "${viewport.width}x${viewport.height}", left, canvas, context,
      maxTopDelta: Math.max(left.top, canvas.top, context.top) - Math.min(left.top, canvas.top, context.top),
      maxBottomDelta: Math.max(left.bottom, canvas.bottom, context.bottom) - Math.min(left.bottom, canvas.bottom, context.bottom),
      documentClientWidth: document.documentElement.clientWidth,
      documentScrollWidth: document.documentElement.scrollWidth,
      bodyScrollWidth: document.body.scrollWidth,
      horizontalOverflow: Math.max(document.documentElement.scrollWidth, document.body.scrollWidth) > document.documentElement.clientWidth,
      composer: interactive("#composer"),
      input: interactive("#messageInput"),
      send: interactive("#sendButton"),
      typedValue: ${JSON.stringify(typedValue)},
    };
  })()`);
  geometry.push(measured);
  if (viewport.width <= 1440) await replaceText("#messageInput", "");
  const screenshot = await send("Page.captureScreenshot", {
    format: "png",
    fromSurface: true,
    captureBeyondViewport: false,
  });
  await fs.writeFile(path.join(outputDir, `unified-shell-${viewport.width}x${viewport.height}.png`), Buffer.from(screenshot.data, "base64"));
}

await send("Emulation.setDeviceMetricsOverride", {
  width: 1024,
  height: 768,
  deviceScaleFactor: 1,
  mobile: false,
  screenWidth: 1024,
  screenHeight: 768,
});
await delay(250);
const safeFlow = await evaluate(`(() => {
  const layout = document.querySelector(".workbench-layout");
  const left = document.querySelector(".workbench-left").getBoundingClientRect();
  const canvas = document.querySelector(".workbench-canvas").getBoundingClientRect();
  const context = document.querySelector(".workbench-context-card").getBoundingClientRect();
  return {
    viewport: "1024x768",
    gridTemplateColumns: getComputedStyle(layout).gridTemplateColumns,
    bodyOverflow: getComputedStyle(document.body).overflow,
    columnTops: { left: Math.round(left.top), canvas: Math.round(canvas.top), context: Math.round(context.top) },
    sequentialTopOrder: left.top <= canvas.top && canvas.top <= context.top,
    maxColumnWidth: Math.round(Math.max(left.width, canvas.width, context.width)),
    documentClientWidth: document.documentElement.clientWidth,
    documentScrollWidth: document.documentElement.scrollWidth,
    horizontalOverflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
  };
})()`);

await send("Emulation.setDeviceMetricsOverride", {
  width: 1440,
  height: 900,
  deviceScaleFactor: 1,
  mobile: false,
  screenWidth: 1440,
  screenHeight: 900,
});
await delay(200);

const typedDraft = await replaceText("#messageInput", "Task 7 draft stays with the Agent");

const initialState = await evaluate(`(() => {
  const messages = document.querySelector("#messages");
  const center = document.querySelector(".workspace-scroll-region");
  const context = document.querySelector("#workbenchContextContent");
  messages.style.paddingBottom = "900px";
  center.style.paddingBottom = "1000px";
  context.style.paddingBottom = "1000px";
  messages.scrollTop = 120;
  center.scrollTop = 88;
  context.scrollTop = 64;
  return {
    input: document.querySelector("#messageInput").value,
    typedDraft: ${JSON.stringify(typedDraft)},
    agentScroll: messages.scrollTop,
    centerScroll: center.scrollTop,
    contextScroll: context.scrollTop,
    selectionContext: document.querySelector("#workbenchAgentContext").textContent,
    selectedItemPresent: !!document.querySelector('[aria-current="true"].is-selected, .is-selected[aria-current="true"]'),
  };
})()`);

await evaluate('document.querySelector("#versionMapPageTab").click()');
await waitFor('location.hash === "#/version-map" && document.querySelector("#versionMapPageTab").getAttribute("aria-current") === "page"');
const versionEmptyContext = await evaluate('document.querySelector("#workbenchContextTitle").textContent');
await evaluate(`(() => {
  document.querySelector(".workspace-scroll-region").scrollTop = 144;
  document.querySelector("#workbenchContextContent").scrollTop = 96;
})()`);
await evaluate('document.querySelector("#applicationsPageTab").click()');
await waitFor('location.hash === "#/applications" && document.querySelector("#applicationsPageTab").getAttribute("aria-current") === "page"');
const applicationEmptyContext = await evaluate('document.querySelector("#workbenchContextTitle").textContent');
await evaluate(`(() => {
  document.querySelector(".workspace-scroll-region").scrollTop = 176;
  document.querySelector("#workbenchContextContent").scrollTop = 112;
})()`);
await evaluate('document.querySelector("#workbenchPageTab").click()');
await waitFor('location.hash === "#/workbench" && document.querySelector("#workbenchPageTab").getAttribute("aria-current") === "page"');
await delay(250);

const restoredState = await evaluate(`(() => ({
  input: document.querySelector("#messageInput").value,
  agentScroll: document.querySelector("#messages").scrollTop,
  centerScroll: document.querySelector(".workspace-scroll-region").scrollTop,
  contextScroll: document.querySelector("#workbenchContextContent").scrollTop,
  selectionContext: document.querySelector("#workbenchAgentContext").textContent,
  route: location.hash,
}))()`);

await evaluate(`(() => {
  document.querySelector("#messages").style.paddingBottom = "";
  document.querySelector(".workspace-scroll-region").style.paddingBottom = "";
  document.querySelector("#workbenchContextContent").style.paddingBottom = "";
})()`);

const k1Results = [];
for (const item of [
  { type: "knowledge", trigger: "#knowledgeNavButton", title: "个人知识库" },
  { type: "capabilities", trigger: "#capabilitiesNavButton", title: "模型、Tool 与 MCP" },
  { type: "trust", trigger: "#trustNavButton", title: "信任中心" },
]) {
  const primaryHash = await evaluate("location.hash");
  await evaluate('document.querySelector("#workbenchSettingsButton").click()');
  await waitFor('document.querySelector("#settingsOverlay").hidden === false');
  const settingsFocus = await evaluate('document.activeElement?.id === "settingsCloseButton"');
  await evaluate(`document.querySelector(${JSON.stringify(item.trigger)}).click()`);
  await waitFor(`document.querySelector("#advancedOverlay").hidden === false && document.querySelector("#advancedTitle").textContent === ${JSON.stringify(item.title)}`);
  const opened = await evaluate(`(() => ({
    panelVisible: document.querySelector(${JSON.stringify(`#${item.type === "capabilities" ? "capabilitiesView" : `${item.type}View`}`)}).hidden === false,
    primaryHashUnchanged: location.hash === ${JSON.stringify(primaryHash)},
    activeInside: document.querySelector("#advancedDialog").contains(document.activeElement),
    activeId: document.activeElement?.id || "",
  }))()`);
  await pressKey("Escape");
  const closed = await evaluate(`(() => ({
    overlayHidden: document.querySelector("#advancedOverlay").hidden,
    focusReturned: document.activeElement?.id === "workbenchSettingsButton",
    primaryHashUnchanged: location.hash === ${JSON.stringify(primaryHash)},
  }))()`);
  k1Results.push({ ...item, settingsFocus, opened, closed });
}

await evaluate('document.querySelector("#workbenchSettingsButton").click()');
await waitFor('document.querySelector("#settingsOverlay").hidden === false');
await evaluate('document.querySelector("#capabilitiesNavButton").click()');
await waitFor('document.querySelector("#advancedOverlay").hidden === false && document.querySelector("#capabilitiesView").hidden === false');
await evaluate('document.querySelector("#workbenchPageTab").focus()');
await pressKey("Tab");
const trapOutside = await evaluate(`(() => ({
  activeInside: document.querySelector("#advancedDialog").contains(document.activeElement),
  activeId: document.activeElement?.id || "",
  focusVisible: document.activeElement?.matches(":focus-visible") || false,
}))()`);
await evaluate('document.querySelector("#advancedCloseButton").focus()');
await pressKey("Tab", { shift: true });
const trapWrap = await evaluate(`(() => ({
  activeInside: document.querySelector("#advancedDialog").contains(document.activeElement),
  activeId: document.activeElement?.id || "",
  didWrap: document.activeElement?.id !== "advancedCloseButton",
}))()`);
await pressKey("Escape");

await evaluate(`(() => {
  const api = document.querySelector("#apiBase");
  api.dataset.qaOriginal = api.value;
  api.value = "http://127.0.0.1:9";
  api.dispatchEvent(new Event("change", { bubbles: true }));
  document.querySelector("#workbenchSettingsButton").click();
})()`);
await waitFor('document.querySelector("#settingsOverlay").hidden === false');
await evaluate('document.querySelector("#knowledgeNavButton").click()');
await waitFor('document.querySelector("#advancedOverlay").hidden === false');
await waitFor('document.querySelector("#knowledgeStatus").textContent !== "正在加载知识库..." && document.querySelector("#knowledgeStatus").textContent.length > 0');
const isolatedError = await evaluate(`(() => ({
  message: document.querySelector("#knowledgeStatus").textContent,
  errorInsideK1: document.querySelector("#advancedDialog").contains(document.querySelector("#knowledgeStatus")),
  k1Visible: !document.querySelector("#advancedOverlay").hidden,
  persistentShellStillPresent: !!document.querySelector("#appShell") && !document.querySelector("#appShell").hidden,
  legacyShellAbsent: !document.querySelector("#chatView") && !document.querySelector(".sidebar"),
  bodyHasHorizontalOverflow: document.body.scrollWidth > document.documentElement.clientWidth,
}))()`);
await pressKey("Escape");
await evaluate(`(() => {
  const api = document.querySelector("#apiBase");
  api.value = api.dataset.qaOriginal;
  api.dispatchEvent(new Event("change", { bubbles: true }));
  delete api.dataset.qaOriginal;
})()`);

const result = {
  browser: await evaluate("navigator.userAgent"),
  geometry,
  safeFlow,
  interaction: {
    initialState,
    restoredState,
    agentInputPreserved: restoredState.input === initialState.input,
    agentScrollPreserved: restoredState.agentScroll === initialState.agentScroll,
    centerScrollRestored: restoredState.centerScroll === initialState.centerScroll,
    contextScrollRestored: restoredState.contextScroll === initialState.contextScroll,
    selectionContextStable: restoredState.selectionContext === initialState.selectionContext,
    routeEmptyContexts: { versionMap: versionEmptyContext, applications: applicationEmptyContext },
    k1Results,
    focusTrap: { outsideRecovery: trapOutside, wrapFromFirst: trapWrap },
    isolatedError,
  },
};

for (const item of geometry) {
  if (item.maxTopDelta > 1 || item.maxBottomDelta > 1) throw new Error(`${item.viewport}: desktop columns are not aligned`);
  if (item.horizontalOverflow) throw new Error(`${item.viewport}: horizontal overflow detected`);
  if (item.viewport !== "1920x1080") {
    for (const [name, control] of Object.entries({ composer: item.composer, input: item.input, send: item.send })) {
      if (!control.visible || !control.withinViewport || !control.hitTarget) throw new Error(`${item.viewport}: ${name} is not visibly operable`);
    }
    if (item.typedValue !== `Edge ${item.viewport.split("x")[0]} composer input`) throw new Error(`${item.viewport}: real input did not reach the Agent composer`);
  }
}
if (!safeFlow.sequentialTopOrder || safeFlow.horizontalOverflow) throw new Error("1024x768 safe flow failed");
if (!result.interaction.agentInputPreserved || !result.interaction.agentScrollPreserved) throw new Error("Agent state did not survive primary navigation");
if (!result.interaction.centerScrollRestored || !result.interaction.contextScrollRestored) throw new Error("route scroll state was not restored");
if (versionEmptyContext !== "版本详情" || applicationEmptyContext !== "投递详情") throw new Error("route-specific empty context was not rendered");
if (!k1Results.every(item => item.settingsFocus && item.opened.panelVisible && item.opened.primaryHashUnchanged && item.closed.overlayHidden && item.closed.focusReturned)) throw new Error("K1 open/close acceptance failed");
if (!trapOutside.activeInside || !trapOutside.focusVisible || !trapWrap.activeInside || !trapWrap.didWrap) throw new Error("K1 keyboard trap failed");
if (!isolatedError.errorInsideK1 || !isolatedError.k1Visible || !isolatedError.legacyShellAbsent) throw new Error("K1 error isolation failed");

await fs.writeFile(path.join(outputDir, "browser-acceptance.json"), `${JSON.stringify(result, null, 2)}\n`, "utf8");
console.log(JSON.stringify(result, null, 2));
socket.close();
