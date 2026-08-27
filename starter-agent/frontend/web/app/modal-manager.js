export function createModalManager({ overlay, dialog, title, closeButton, panels, onBeforeClose = () => {} }) {
  let currentType = null;
  let returnFocus = null;
  const focusable = () => [...dialog.querySelectorAll(
    'button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [href], [tabindex]:not([tabindex="-1"])'
  )].filter(node => !node.hidden);

  function show(type, trigger) {
    currentType = type;
    returnFocus = trigger || document.activeElement;
    for (const [name, panel] of Object.entries(panels)) panel.hidden = name !== type;
    title.textContent = { knowledge: "个人知识库", capabilities: "模型、Tool 与 MCP", trust: "信任中心" }[type];
    overlay.hidden = false;
    document.body.classList.add("modal-open");
    closeButton.focus();
  }

  function close() {
    if (!currentType) return;
    onBeforeClose(currentType);
    overlay.hidden = true;
    document.body.classList.remove("modal-open");
    currentType = null;
    returnFocus?.focus();
  }

  function onKeydown(event) {
    if (!currentType) return;
    if (event.key === "Escape") return close();
    if (event.key !== "Tab") return;
    const nodes = focusable();
    if (!nodes.length) return event.preventDefault();
    const first = nodes[0];
    const last = nodes.at(-1);
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  }

  closeButton.addEventListener("click", close);
  overlay.addEventListener("click", event => { if (event.target === overlay) close(); });
  document.addEventListener("keydown", onKeydown);
  return Object.freeze({ open: show, replace: type => show(type, returnFocus), close, activeType: () => currentType });
}
