export class GraphRenderer {
  render(container, map, onSelect, options = {}) {
    container.replaceChildren();
    const preference = options.preference || { node_positions: {}, viewport_zoom: 1 };
    const toolbar = document.createElement("div"); toolbar.className = "version-map-toolbar";
    const search = document.createElement("input"); search.type = "search"; search.placeholder = "搜索版本"; search.setAttribute("aria-label", "搜索版本节点");
    const type = document.createElement("select"); type.setAttribute("aria-label", "筛选节点类型");
    for (const [value, label] of [["", "全部类型"], ["base", "基础"], ["direction", "方向"], ["company", "公司"]]) {
      const option = document.createElement("option"); option.value = value; option.textContent = label; type.append(option);
    }
    const zoomOut = document.createElement("button"); zoomOut.type = "button"; zoomOut.textContent = "−"; zoomOut.setAttribute("aria-label", "缩小版本地图");
    const zoomIn = document.createElement("button"); zoomIn.type = "button"; zoomIn.textContent = "+"; zoomIn.setAttribute("aria-label", "放大版本地图");
    const mini = document.createElement("span"); mini.className = "version-map-minimap";
    toolbar.append(search, type, zoomOut, zoomIn, mini);
    const canvas = document.createElement("div");
    canvas.className = "version-map-canvas";
    canvas.setAttribute("role", "tree");
    let zoom = preference.viewport_zoom || 1;
    const pageSize = Math.max(20, Number(options.pageSize || 60));
    let renderLimit = pageSize;
    const loadMore = document.createElement("button");
    loadMore.type = "button";
    loadMore.textContent = "加载更多节点";
    loadMore.className = "version-map-load-more";
    let selectedVersionId = options.selectedVersionId || "";
    const selectedLineage = () => {
      const lineage = new Set();
      let cursor = selectedVersionId;
      while (cursor) {
        lineage.add(cursor);
        cursor = (map.nodes || []).find(item => item.version_id === cursor)?.parent_version_id;
      }
      return lineage;
    };
    const applySelection = () => {
      const lineage = selectedLineage();
      for (const item of canvas.querySelectorAll(".version-node")) {
        item.classList.toggle("is-lineage-focus", lineage.has(item.dataset.versionId));
        item.setAttribute("aria-current", item.dataset.versionId === selectedVersionId ? "true" : "false");
      }
    };
    const draw = () => {
      canvas.replaceChildren();
      const query = search.value.trim().toLocaleLowerCase();
      const filtered = (map.nodes || []).filter(node => (!query || `${node.label} ${node.version_id}`.toLocaleLowerCase().includes(query)) && (!type.value || node.node_type === type.value));
      const visible = filtered.slice(0, renderLimit);
      mini.textContent = `已渲染 ${visible.length}/${filtered.length} · 共 ${(map.nodes || []).length} 节点 · ${Math.round(zoom * 100)}%`;
      canvas.style.setProperty("--version-map-zoom", String(zoom));
      for (const node of visible) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = `version-node version-node-${node.node_type}`;
        button.dataset.versionId = node.version_id;
        button.setAttribute("role", "treeitem");
        button.draggable = true;
        const label = document.createElement("strong");
        label.textContent = node.label;
        const meta = document.createElement("span");
        meta.textContent = `${node.node_type} · ${node.status}`;
        button.append(label, meta);
        if (node.upstream_changes_available) {
          const notice = document.createElement("span");
          notice.className = "version-node-notice";
          notice.textContent = "有上游变化";
          button.append(notice);
        }
        button.addEventListener("click", event => {
          selectedVersionId = node.version_id;
          applySelection();
          onSelect(node, event);
        });
        button.addEventListener("dragend", event => {
          const positions = { ...(preference.node_positions || {}), [node.version_id]: [event.clientX, event.clientY] };
          options.onPreferenceChange?.({ ...preference, node_positions: positions, viewport_zoom: zoom });
        });
        button.addEventListener("keydown", event => {
          if (!["ArrowDown", "ArrowRight", "ArrowUp", "ArrowLeft", "Home", "End"].includes(event.key)) return;
          event.preventDefault();
          const items = [...canvas.querySelectorAll('[role="treeitem"]')];
          const index = items.indexOf(button);
          const target = event.key === "Home" ? items[0]
            : event.key === "End" ? items.at(-1)
            : ["ArrowDown", "ArrowRight"].includes(event.key) ? items[Math.min(items.length - 1, index + 1)]
            : items[Math.max(0, index - 1)];
          target?.focus();
        });
        canvas.append(button);
      }
      applySelection();
      loadMore.hidden = visible.length >= filtered.length;
    };
    search.addEventListener("input", () => { renderLimit = pageSize; draw(); });
    type.addEventListener("change", () => { renderLimit = pageSize; draw(); });
    loadMore.addEventListener("click", () => { renderLimit += pageSize; draw(); canvas.querySelectorAll('[role="treeitem"]')[renderLimit - pageSize]?.focus(); });
    const changeZoom = delta => { zoom = Math.min(2, Math.max(.5, Number((zoom + delta).toFixed(2)))); draw(); options.onPreferenceChange?.({ ...preference, viewport_zoom: zoom }); };
    zoomOut.addEventListener("click", () => changeZoom(-.1)); zoomIn.addEventListener("click", () => changeZoom(.1));
    draw();
    const relation = document.createElement("div");
    relation.className = "version-map-relations";
    relation.setAttribute("aria-label", "版本血缘关系");
    relation.textContent = (map.edges || []).length
      ? map.edges.map(edge => `${edge.parent_version_id} → ${edge.child_version_id}`).join(" · ")
      : "当前只有根版本";
    container.append(toolbar, canvas, loadMore, relation);
  }
}
