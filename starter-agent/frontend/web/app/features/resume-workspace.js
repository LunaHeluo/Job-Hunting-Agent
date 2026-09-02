import { GraphRenderer } from "./version-map.js";
import { updateWorkbenchContext } from "../workbench-context.js";

function id(prefix) {
  return `${prefix}_${crypto.randomUUID().replaceAll("-", "")}`;
}

const IMPORT_FAILURES = Object.freeze({
  resume_file_type_unsupported: "仅支持 DOCX 或 PDF 文件。",
  resume_pdf_encrypted: "PDF 受密码保护，无法读取。请上传未加密版本。",
  resume_pdf_parse_failed: "PDF 文件无法读取，请确认文件未损坏。",
  resume_pdf_text_unavailable: "该 PDF 未包含可提取文字，可能是扫描件；请配置 MinerU OCR 后重试。",
  jd_file_too_large: "文件超过 8 MB 限制。",
  jd_file_empty: "文件为空。",
  jd_docx_parse_failed: "DOCX 文件无法读取，请确认文件未损坏或受密码保护。",
  jd_ocr_not_configured: "PDF 解析需要配置 MINERU_API_TOKEN，并重启后端。",
  jd_ocr_upload_url_failed: "MinerU 未接受解析请求：请检查 Token 是否有效、账户是否有可用额度。",
  jd_ocr_upload_failed: "文件上传到 MinerU 失败，请稍后重试。",
  jd_ocr_status_failed: "MinerU 查询解析状态失败，请稍后重试。",
  jd_ocr_parse_failed: "MinerU 未能在限定时间内完成 PDF 解析。",
  jd_ocr_result_missing: "MinerU 没有返回解析结果。",
  jd_ocr_result_download_failed: "无法下载 MinerU 的解析结果。",
  jd_ocr_result_invalid: "MinerU 返回的解析文件无效。",
  jd_ocr_markdown_missing: "MinerU 结果中缺少可导入的文本内容。",
  jd_content_empty_after_parse: "文件未提取到可用的简历文本。",
});

export function createResumeWorkspace({ request, apiBase, elements, reloadHome }) {
  const graph = new GraphRenderer();
  let selectedNode = null;
  let activeResumeId = null;
  let activeMap = null;

  function button(label, onClick, className = "") {
    const item = document.createElement("button");
    item.type = "button";
    item.textContent = label;
    item.className = className;
    item.addEventListener("click", onClick);
    return item;
  }

  function setProfileMetric(id, value) {
    const element = document.querySelector(id);
    if (element) element.textContent = value == null ? "—" : String(value);
  }

  function updateProfileMetrics(markdown, profile = null) {
    const structuredMetrics = profile?.metrics;
    if (structuredMetrics) {
      setProfileMetric("#workbenchEducationCount", structuredMetrics.education);
      setProfileMetric("#workbenchExperienceCount", structuredMetrics.experience);
      setProfileMetric("#workbenchProjectCount", structuredMetrics.projects);
      setProfileMetric("#workbenchSkillCount", structuredMetrics.skills);
      return;
    }
    const lines = String(markdown || "").split("\n");
    const countSectionItems = terms => {
      const start = lines.findIndex(line => /^#{1,4}\s/.test(line) && terms.some(term => line.toLowerCase().includes(term)));
      if (start < 0) return "—";
      const section = [];
      for (const line of lines.slice(start + 1)) {
        if (/^#{1,4}\s/.test(line)) break;
        section.push(line);
      }
      const items = section.filter(line => /^\s*[-*+]\s+/.test(line)).length;
      return items || 1;
    };
    setProfileMetric("#workbenchEducationCount", countSectionItems(["教育", "education"]));
    setProfileMetric("#workbenchExperienceCount", countSectionItems(["实习", "经历", "experience"]));
    setProfileMetric("#workbenchProjectCount", countSectionItems(["项目", "project"]));
    setProfileMetric("#workbenchSkillCount", countSectionItems(["技能", "skill"]));
  }

  async function ensureImportWorkspace(workspaceId) {
    if (workspaceId) return workspaceId;
    const token = crypto.randomUUID().replaceAll("-", "");
    const workspace = await request("/v1/workbench/workspaces", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        workspace_id: `ws_${token}`,
        name: "我的求职档案",
      }),
    });
    return workspace.workspace_id;
  }

  function renderImport(workspaceId) {
    const file = document.createElement("input");
    file.type = "file";
    file.accept = ".docx,.pdf";
    file.setAttribute("aria-label", "DOCX 或 PDF 简历文件");
    file.addEventListener("change", async () => {
      if (!file.files?.[0]) return;
      try {
        const targetWorkspaceId = await ensureImportWorkspace(workspaceId);
        await uploadResumeFile(targetWorkspaceId, file.files[0]);
      } catch (error) {
        elements.main.textContent = `无法初始化简历档案：${error.message}`;
      }
    }, { once: true });
    file.click();
  }

  async function uploadResumeFile(workspaceId, file) {
    elements.main.textContent = "正在解析并上传简历…";
    const token = crypto.randomUUID().replaceAll("-", "");
    const data = new FormData();
    const fallbackName = file.name.replace(/\.[^.]+$/, "") || "我的档案";
    data.set("operation_id", `op_import_${token}`); data.set("idempotency_key", `import-${token}`);
    data.set("workspace_id", workspaceId); data.set("resume_id", `res_${token}`);
    data.set("branch_id", `rb_${token}`); data.set("version_id", `rv_${token}`);
    data.set("resume_name", fallbackName); data.set("confirmed_authorized", "true");
    data.set("file", file);
    try {
      const imported = await request("/v1/workbench/resumes/imports/upload", { method: "POST", body: data });
      await request(`/v1/workbench/workspaces/${encodeURIComponent(workspaceId)}/active-resume/${encodeURIComponent(imported.result.resume_id)}`, { method: "POST" });
      await reloadHome();
      await renderResumePreview(workspaceId, imported.result.version_id, imported.resume_name || fallbackName, imported.source_filename);
    } catch (error) {
      const detail = IMPORT_FAILURES[error.code] || error.message;
      elements.main.textContent = `导入失败：${detail}${error.code ? `（${error.code}）` : ""}`;
    }
  }

  async function renderResumePreview(workspaceId, versionId, label, sourceFilename = "") {
    elements.main.className = "";
    elements.main.textContent = "正在加载简历预览…";
    try {
      const content = await request(`/v1/workbench/resume-versions/${encodeURIComponent(versionId)}/content?workspace_id=${encodeURIComponent(workspaceId)}`);
      updateWorkbenchContext({ workspace_id: workspaceId, resume_version_id: versionId });
      updateProfileMetrics(content.markdown, content.profile);
      const panel = document.createElement("section"); panel.className = "resume-document-preview";
      const header = document.createElement("header");
      const title = document.createElement("h2"); title.textContent = "档案预览";
      const meta = document.createElement("p"); meta.textContent = sourceFilename ? `当前简历档案：${label} · 来源：${sourceFilename}` : `当前简历档案：${label} · 可在左侧直接向 Agent 提问`;
      header.append(title, meta);
      const documentBody = document.createElement("article"); documentBody.className = "resume-document-body";
      renderResumeDocument(documentBody, content.markdown, content.profile, label);
      panel.append(header, documentBody); elements.main.replaceChildren(panel);
    } catch (error) {
      elements.main.textContent = `简历已导入，但预览加载失败：${error.message}`;
    }
  }

  function renderResumeDocument(container, markdown, profile, fallbackName) {
    const { intro, sections } = splitResumeSections(markdown);
    const identity = document.createElement("header"); identity.className = "resume-paper-identity";
    const name = document.createElement("h1"); name.textContent = profile?.name || fallbackName;
    const contact = document.createElement("p");
    const contactItems = [profile?.contact?.phone, profile?.contact?.email].filter(Boolean);
    contact.textContent = contactItems.join("  ·  ") || intro.slice(0, 2).join("  ·  ");
    identity.append(name); if (contact.textContent) identity.append(contact); container.append(identity);
    for (const line of intro.slice(2)) appendResumeLine(container, line);
    for (const section of sections) renderResumeSection(container, section);
  }

  function splitResumeSections(markdown) {
    const intro = []; const sections = []; let current = null;
    for (const rawLine of markdown.split("\n")) {
      const line = rawLine.trim(); if (!line) continue;
      const title = resumeSectionHeading(line);
      if (title) { current = { title, lines: [] }; sections.push(current); continue; }
      if (current) current.lines.push(line); else intro.push(cleanResumeLine(line));
    }
    return { intro, sections };
  }

  function resumeSectionHeading(line) {
    const markdownHeading = line.match(/^#{1,6}\s+(.+)$/);
    const value = (markdownHeading ? markdownHeading[1] : line).replace(/[*_`]/g, "").trim();
    const normalized = value.toLowerCase().replace(/[\s:：()（）【】\[\]._-]+/g, "");
    const names = ["教育", "education", "教育经历", "研究兴趣", "researchinterests", "研究经历", "research experience", "工作经历", "professionalexperience", "实习经历", "项目经历", "selectedprojects", "projects", "技能", "skills", "荣誉", "honors"];
    if (markdownHeading && value.length <= 80) return value;
    if (names.some(name => normalized === name.replace(/[\s_-]+/g, "").toLowerCase())) return value;
    if (/^[A-Z][A-Z\s&/\-]{2,79}$/.test(value)) return value;
    return null;
  }

  function renderResumeSection(container, section) {
    const block = document.createElement("section"); block.className = "resume-paper-section";
    const title = document.createElement("h2"); title.textContent = section.title; block.append(title);
    const dated = section.lines.some(line => /(?:19|20)\d{2}|至今|present/i.test(line));
    if (!dated) { section.lines.forEach(line => appendResumeLine(block, line)); container.append(block); return; }
    let entry = null;
    for (const rawLine of section.lines) {
      const line = cleanResumeLine(rawLine);
      if (/(?:19|20)\d{2}|至今|present/i.test(line) && !rawLine.match(/^\s*[-*+•]/)) {
        entry = document.createElement("article"); entry.className = "resume-paper-entry";
        const heading = document.createElement("div"); heading.className = "resume-paper-entry-heading";
        const parts = splitEntryDate(line); const name = document.createElement("strong"); name.textContent = parts.name;
        heading.append(name); if (parts.date) { const date = document.createElement("em"); date.textContent = parts.date; heading.append(date); }
        entry.append(heading); block.append(entry); continue;
      }
      appendResumeLine(entry || block, rawLine);
    }
    container.append(block);
  }

  function splitEntryDate(line) {
    const match = line.match(/^(.*?)(\s+(?:(?:19|20)\d{2}[^\n]*|至今|present))$/i);
    return match ? { name: match[1].trim(), date: match[2].trim() } : { name: line, date: "" };
  }

  function appendResumeLine(container, rawLine) {
    const bullet = /^\s*[-*+•]\s+/.test(rawLine); const line = cleanResumeLine(rawLine); if (!line) return;
    if (bullet) {
      let list = container.lastElementChild; if (!list || list.tagName !== "UL") { list = document.createElement("ul"); list.className = "resume-paper-list"; container.append(list); }
      const item = document.createElement("li"); item.textContent = line; list.append(item);
    } else { const paragraph = document.createElement("p"); paragraph.textContent = line; container.append(paragraph); }
  }

  function cleanResumeLine(line) { return line.replace(/^\s*(?:#{1,6}\s*|[-*+•]\s*)/, "").replace(/[*_`]/g, "").trim(); }

  async function renderVersionMap(workspaceId, resumeId) {
    activeResumeId = resumeId;
    elements.main.textContent = "正在加载版本血缘…";
    try {
      const [map, savedPreference] = await Promise.all([
        request(`/v1/workbench/resumes/${encodeURIComponent(resumeId)}/version-map`),
        request(`/v1/workbench/resumes/${encodeURIComponent(resumeId)}/view-preference`).catch(error => error.status === 404 ? null : Promise.reject(error)),
      ]);
      activeMap = map;
      let preference = savedPreference || { node_positions: {}, collapsed_branch_ids: [], viewport_x: 0, viewport_y: 0, viewport_zoom: 1, revision: null };
      let preferenceTimer = null;
      const savePreference = next => {
        preference = next;
        window.clearTimeout(preferenceTimer);
        preferenceTimer = window.setTimeout(async () => {
          try {
            const saved = await request(`/v1/workbench/resumes/${encodeURIComponent(resumeId)}/view-preference`, {
              method: "PUT", headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ node_positions: preference.node_positions || {}, collapsed_branch_ids: preference.collapsed_branch_ids || [], viewport_x: preference.viewport_x || 0, viewport_y: preference.viewport_y || 0, viewport_zoom: preference.viewport_zoom || 1, expected_revision: preference.revision || null }),
            });
            preference = saved;
          } catch (error) { elements.status.textContent = `视图偏好保存失败（业务血缘未改变）：${error.message}`; }
        }, 250);
      };
      graph.render(elements.main, map, async (node, event) => {
        if (event.shiftKey && selectedNode && selectedNode.version_id !== node.version_id) {
          await renderDiff(workspaceId, selectedNode, node);
          return;
        }
        selectedNode = node;
        updateWorkbenchContext({ workspace_id: workspaceId, resume_version_id: node.version_id, resume_branch_id: node.branch_id, lineage_focus_version_id: node.version_id });
        renderInspector(workspaceId, node);
      }, { preference, onPreferenceChange: savePreference });
    } catch (error) { elements.main.textContent = `版本地图加载失败：${error.message}`; }
  }

  function renderInspector(workspaceId, node) {
    elements.jobs.replaceChildren();
    const panel = document.createElement("section"); panel.className = "version-inspector";
    const title = document.createElement("h3"); title.textContent = node.label;
    const meta = document.createElement("p"); meta.textContent = `${node.node_type} · ${node.status}`;
    const open = button("在工作台打开", () => openDraft(workspaceId, node));
    const compare = button("Shift 选择第二个版本比较", () => { elements.status.textContent = "按住 Shift 选择另一节点；比较只读取后端 Diff。"; });
    const branch = button("从此版本创建方向分支", () => createDirectionBranch(workspaceId, node));
    const merge = button("创建三方合并方案", () => createMergeProposal(workspaceId, node));
    const exportStatus = document.createElement("div"); exportStatus.className = "operation-status"; exportStatus.setAttribute("aria-live", "polite");
    const template = document.createElement("select"); template.setAttribute("aria-label", "导出模板");
    template.append(new Option("ATS 清爽", "ats-clean@1.0.0"), new Option("ATS 紧凑", "ats-compact@1.0.0"));
    void request("/v1/workbench/export-templates").then(payload => {
      template.replaceChildren(...payload.items.map(item => new Option(item.label, `${item.template_id}@${item.template_version}`)));
    }).catch(() => { /* Built-in list remains usable when discovery is temporarily unavailable. */ });
    const exportPdf = button("导出 PDF", () => createExport(workspaceId, node, "pdf", exportStatus, template.value));
    const exportWord = button("导出 Word", () => createExport(workspaceId, node, "docx", exportStatus, template.value));
    const confirmed = node.status === "confirmed";
    for (const item of [exportPdf, exportWord]) {
      item.disabled = !confirmed;
      if (!confirmed) item.title = "请先确认版本；待确认版本不可导出。";
    }
    if (!confirmed) exportStatus.textContent = "请先确认版本，之后才能导出 PDF 或 Word。";
    panel.append(title, meta, open, compare, branch, merge, template, exportPdf, exportWord, exportStatus); elements.jobs.append(panel);
  }

  async function createExport(workspaceId, node, format, status, templateKey) {
    const token = crypto.randomUUID().replaceAll("-", "");
    const [templateId, templateVersion] = templateKey.split("@");
    status.textContent = `正在生成 ${format.toUpperCase()}…`;
    try {
      const result = await request("/v1/workbench/exports", {
        method: "POST",
        headers: { "Content-Type": "application/json", "Idempotency-Key": `export-${node.version_id}-${format}-${templateId}-${templateVersion}` },
        body: JSON.stringify({ operation_id: `op_export_${token}`, idempotency_key: `export-${node.version_id}-${format}-${templateId}-${templateVersion}`, export_id: `exp_${token}`, workspace_id: workspaceId, resume_version_id: node.version_id, format, template_id: templateId, template_version: templateVersion, settings: { title: node.label } }),
      });
      if (!result.export) { status.textContent = "导出尚未完成，可在任务卡查看进度。"; return; }
      const response = await fetch(`${apiBase()}/v1/workbench/exports/${encodeURIComponent(result.export.export_id)}/download`);
      if (!response.ok) throw new Error(`下载失败（HTTP ${response.status}）`);
      const blob = await response.blob(); const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a"); anchor.href = url; anchor.download = `${node.label}.${format}`; anchor.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
      status.textContent = `已导出固定版本 ${node.label}；后续编辑不会改变该文件。`;
      await reloadHome();
    } catch (error) { status.textContent = `导出失败：${error.message}`; }
  }

  async function renderDiff(workspaceId, left, right) {
    elements.main.textContent = "正在计算共同祖先与差异…";
    try {
      const diff = await request(`/v1/workbench/resume-versions/${encodeURIComponent(left.version_id)}/compare/${encodeURIComponent(right.version_id)}?workspace_id=${encodeURIComponent(workspaceId)}`);
      const panel = document.createElement("section"); panel.className = "version-diff-panel";
      const title = document.createElement("h2"); title.textContent = `${left.label} ↔ ${right.label}`;
      const ancestor = document.createElement("p"); ancestor.textContent = `共同祖先：${diff.common_ancestor_version_id || "无"}`;
      const pre = document.createElement("pre"); pre.textContent = (diff.unified || []).join("\n") || "正文无差异";
      panel.append(title, ancestor, pre, button("返回版本地图", () => renderVersionMap(workspaceId, activeResumeId)));
      elements.main.replaceChildren(panel);
    } catch (error) { elements.main.textContent = `比较失败：${error.message}`; }
  }

  async function createDirectionBranch(workspaceId, node) {
    const name = window.prompt("方向分支名称");
    if (!name?.trim()) return;
    try {
      const token = crypto.randomUUID().replaceAll("-", "");
      await request(`/v1/workbench/resume-versions/${encodeURIComponent(node.version_id)}/branches`, {
        method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": `branch-${token}` },
        body: JSON.stringify({ branch_id: `rb_${token}`, resume_id: activeResumeId, name: name.trim(), branch_type: "direction" }),
      });
      elements.status.textContent = "方向分支已创建；原版本正文未改变。";
      await reloadHome();
    } catch (error) { elements.status.textContent = `创建分支失败：${error.message}`; }
  }

  async function openDraft(workspaceId, node) {
    const token = crypto.randomUUID().replaceAll("-", "");
    elements.main.textContent = "正在建立可恢复 Draft…";
    try {
      const draft = await request(`/v1/workbench/resume-versions/${encodeURIComponent(node.version_id)}/drafts`, {
        method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": `draft-${token}` },
        body: JSON.stringify({ draft_id: `rd_${token}`, workspace_id: workspaceId, branch_id: node.branch_id }),
      });
      updateWorkbenchContext({ workspace_id: workspaceId, resume_version_id: node.version_id, resume_branch_id: node.branch_id });
      const content = await request(`/v1/workbench/drafts/${encodeURIComponent(draft.draft_id)}/content?workspace_id=${encodeURIComponent(workspaceId)}`);
      renderDraftEditor(workspaceId, draft, content);
    } catch (error) { elements.main.textContent = `Draft 创建失败：${error.message}`; }
  }

  function renderDraftEditor(workspaceId, initialDraft, initialContent) {
    let draft = initialDraft; let contentHash = initialContent.content_sha256;
    const panel = document.createElement("section"); panel.className = "draft-editor";
    const heading = document.createElement("div"); heading.className = "workbench-section-heading";
    const title = document.createElement("h2"); title.textContent = "简历 Draft";
    const state = document.createElement("span"); state.className = "status-label"; state.textContent = `revision ${draft.revision}`;
    heading.append(title, state);
    const editor = document.createElement("textarea"); editor.value = initialContent.markdown; editor.rows = 28; editor.setAttribute("aria-label", "简历 Draft Markdown");
    const status = document.createElement("div"); status.className = "operation-status";
    let dirty = false;
    const save = button("保存 Draft", async () => {
      save.disabled = true; status.textContent = "正在保存…";
      try {
        draft = await request(`/v1/workbench/drafts/${encodeURIComponent(draft.draft_id)}`, { method: "PATCH", headers: { "Content-Type": "application/json", "If-Match": String(draft.revision) }, body: JSON.stringify({ workspace_id: workspaceId, expected_revision: draft.revision, expected_content_sha256: contentHash, markdown: editor.value }) });
        contentHash = draft.content.content_sha256; dirty = false; pending.disabled = false; state.textContent = `revision ${draft.revision}`; status.textContent = "已保存到 Draft；尚未形成正式版本。";
      } catch (error) { status.textContent = error.status === 409 ? "保存冲突：请重新加载并比较服务器版本。" : `保存失败：${error.message}`; }
      finally { save.disabled = false; }
    });
    const pending = button("保存为待确认版本", async () => {
      const versionToken = crypto.randomUUID().replaceAll("-", "");
      try {
        const version = await request(`/v1/workbench/drafts/${encodeURIComponent(draft.draft_id)}/versions`, { method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": `version-${versionToken}` }, body: JSON.stringify({ workspace_id: workspaceId, version_id: `rv_${versionToken}`, label: `定制版本 ${new Date().toLocaleDateString()}`, expected_draft_revision: draft.revision }) });
        status.textContent = "待确认版本已生成；确认前不可导出或投递。";
        confirm.hidden = false; confirm.dataset.versionId = version.version_id; confirm.dataset.revision = version.revision;
      } catch (error) { status.textContent = `生成版本失败：${error.message}`; }
    }, "primary-action");
    editor.addEventListener("input", () => {
      dirty = true; pending.disabled = true; status.textContent = "有尚未保存的修改。";
    });
    const confirm = button("确认版本", async () => {
      try {
        await request(`/v1/workbench/resume-versions/${encodeURIComponent(confirm.dataset.versionId)}/confirm`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ workspace_id: workspaceId, expected_revision: Number(confirm.dataset.revision) }) });
        status.textContent = "版本已确认，可用于后续分析。"; confirm.disabled = true; await reloadHome();
      } catch (error) { status.textContent = `确认失败：${error.message}`; }
    }); confirm.hidden = true;
    const actions = document.createElement("div"); actions.className = "draft-actions"; actions.append(save, pending, confirm);
    panel.append(heading, editor, actions, status); elements.main.replaceChildren(panel);
  }

  async function createMergeProposal(workspaceId, targetNode) {
    const baseDefault = targetNode.parent_version_id || "";
    const baseVersionId = window.prompt("共同祖先版本 ID", baseDefault);
    if (!baseVersionId?.trim()) return;
    const upstreamDefault = (activeMap?.nodes || []).find(item => item.version_id !== targetNode.version_id && item.version_id !== baseVersionId)?.version_id || "";
    const upstreamVersionId = window.prompt("上游版本 ID", upstreamDefault);
    if (!upstreamVersionId?.trim()) return;
    const token = crypto.randomUUID().replaceAll("-", "");
    try {
      const proposal = await request("/v1/workbench/merge-proposals", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ proposal_id: `mp_${token}`, workspace_id: workspaceId, target_branch_id: targetNode.branch_id, base_version_id: baseVersionId.trim(), upstream_version_id: upstreamVersionId.trim(), target_version_id: targetNode.version_id }),
      });
      renderMergeProposal(workspaceId, proposal);
    } catch (error) { elements.status.textContent = `合并方案创建失败：${error.message}`; }
  }

  function renderMergeProposal(workspaceId, initialProposal) {
    let proposal = initialProposal;
    const panel = document.createElement("section"); panel.className = "merge-proposal-panel";
    const title = document.createElement("h2"); title.textContent = "三方合并方案";
    const summary = document.createElement("p");
    const decisions = document.createElement("div"); decisions.className = "merge-decisions";
    const status = document.createElement("div"); status.className = "operation-status"; status.setAttribute("aria-live", "polite");
    const commit = button("确认并提交合并", async () => {
      const token = crypto.randomUUID().replaceAll("-", "");
      commit.disabled = true; status.textContent = "正在验证输入并提交新版本…";
      try {
        const operation = await request(`/v1/workbench/merge-proposals/${encodeURIComponent(proposal.proposal_id)}/commit`, {
          method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": `merge-${token}` },
          body: JSON.stringify({ operation_id: `op_merge_${token}`, idempotency_key: `merge-${token}`, workspace_id: workspaceId }),
        });
        if (operation.status !== "committed") throw new Error(operation.error_code || `operation ${operation.status}`);
        status.textContent = "合并已提交：只在目标分支新增一个已确认版本。";
        await renderVersionMap(workspaceId, activeResumeId);
      } catch (error) { status.textContent = `合并提交失败：${error.message}`; commit.disabled = false; }
    }, "primary-action");

    const refresh = () => {
      summary.textContent = `状态：${proposal.status} · revision ${proposal.revision}`;
      decisions.replaceChildren();
      for (const item of proposal.decisions || []) {
        const row = document.createElement("article"); row.className = "merge-decision-row";
        const label = document.createElement("strong"); label.textContent = item.block_id;
        const current = document.createElement("span"); current.textContent = `当前决策：${item.decision}`;
        const decide = async (decision, manualContent = null) => {
          try {
            proposal = await request(`/v1/workbench/merge-proposals/${encodeURIComponent(proposal.proposal_id)}`, {
              method: "PATCH", headers: { "Content-Type": "application/json", "If-Match": String(proposal.revision) },
              body: JSON.stringify({ block_id: item.block_id, decision, expected_revision: proposal.revision, manual_content: manualContent }),
            });
            status.textContent = "决策已保存，尚未改变目标分支。"; refresh();
          } catch (error) { status.textContent = error.status === 409 ? "方案已变化，请重新载入后再决定。" : `决策失败：${error.message}`; }
        };
        const actions = document.createElement("div"); actions.className = "merge-decision-actions";
        actions.append(
          button("保留目标", () => decide("keep_target")),
          button("采用上游", () => decide("accept_upstream")),
          button("手工编辑", () => { const value = window.prompt("输入此区块的最终内容", item.manual_content || ""); if (value !== null) decide("manual", value); }),
        );
        row.append(label, current, actions); decisions.append(row);
      }
      commit.disabled = proposal.status !== "ready";
    };
    panel.append(title, summary, decisions, commit, status, button("返回版本地图", () => renderVersionMap(workspaceId, activeResumeId)));
    elements.main.replaceChildren(panel); refresh();
  }

  function renderResumeList(home, route, workspaceId) {
    const profileName = document.querySelector("#workbenchProfileName");
    const profileCaption = document.querySelector("#workbenchProfileCaption");
    const reupload = document.querySelector("#workbenchResumeReupload");
    if (reupload) reupload.onclick = () => renderImport(workspaceId || null);
    if (!workspaceId) {
      if (profileName) profileName.textContent = "我的档案";
      if (profileCaption) profileCaption.textContent = "导入简历后生成摘要";
      updateProfileMetrics("");
      return;
    }
    if (!(home.recent_versions || []).length) {
      if (profileName) profileName.textContent = home.workspace?.name || "我的档案";
      if (profileCaption) profileCaption.textContent = "尚未导入简历";
      updateProfileMetrics("");
      return;
    }
    if (profileName) profileName.textContent = home.recent_versions[0].label;
    if (profileCaption) profileCaption.textContent = `${home.recent_versions.length} 个档案版本`;
    void request(`/v1/workbench/resume-versions/${encodeURIComponent(home.recent_versions[0].version_id)}/content?workspace_id=${encodeURIComponent(workspaceId)}`)
      .then(content => updateProfileMetrics(content.markdown, content.profile))
      .catch(() => updateProfileMetrics(""));
    if (route === "version-map") renderVersionMap(workspaceId, home.recent_versions[0].resume_id);
  }

  return Object.freeze({ renderImport, renderResumeList, renderResumePreview, renderVersionMap });
}
