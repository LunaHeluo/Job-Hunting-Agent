// Read back the persisted Draft: the preview must never imply unaccepted edits were saved.
export async function renderTailoredPreview({ request, apiBase = () => "", container, workspaceId, analysis, draftId, suggestions = [], isCurrent = () => true, onBack }) {
  const query = `?workspace_id=${encodeURIComponent(workspaceId)}`;
  const [draft, content, original, job] = await Promise.all([
    request(`/v1/workbench/drafts/${encodeURIComponent(draftId)}`),
    request(`/v1/workbench/drafts/${encodeURIComponent(draftId)}/content${query}`),
    request(`/v1/workbench/resume-versions/${encodeURIComponent(analysis.resume_version_id)}/content${query}`),
    request(`/v1/workbench/job-snapshots/${encodeURIComponent(analysis.job_snapshot_id)}`),
  ]);
  if (!isCurrent()) return false;
  const node = (tag, text, className = "") => {
    const value = document.createElement(tag); value.textContent = text; value.className = className; return value;
  };
  const action = (text, callback) => {
    const value = node("button", text); value.type = "button"; value.addEventListener("click", callback); return value;
  };
  const panel = node("section", "", "tailored-preview");
  panel.append(node("h2", "完整简历对照"), node("p", "请按标题区分原版本、已采纳到 Draft 的内容和尚未采纳的候选预览；高亮表示差异。核对事实并采纳后再保存，原版本不会被覆盖。"));
  let candidateMarkdown = content.markdown;
  const replacements = [];
  for (const suggestion of suggestions) {
    if (draft.status !== "active" || suggestion.status !== "pending" || suggestion.target_draft_id !== draftId || suggestion.target_draft_revision !== content.revision) continue;
    const source = suggestion.original_text;
    const start = source ? content.markdown.indexOf(source) : -1;
    if (start < 0 || content.markdown.indexOf(source, start + source.length) !== -1) continue;
    const end = start + source.length;
    if (replacements.some(item => start < item.end && end > item.start)) continue;
    replacements.push({start, end, text: suggestion.proposed_text});
  }
  for (const item of replacements.sort((a, b) => b.start - a.start)) candidateMarkdown = candidateMarkdown.slice(0, item.start) + item.text + candidateMarkdown.slice(item.end);
  const columns = node("div", "", "tailored-preview-columns");
  const documents = [["原始版本", original.markdown, content.markdown], ["当前 Draft", content.markdown, original.markdown]];
  if (replacements.length) documents.push(["候选预览（尚未采纳或保存）", candidateMarkdown, content.markdown]);
  for (const [title, markdown, other] of documents) {
    const section = node("section", ""); section.append(node("h3", title));
    const documentBody = node("pre", "", "tailored-preview-document");
    const otherLines = new Set(other.split("\n"));
    for (const line of markdown.split("\n")) documentBody.append(node("span", `${line}\n`, otherLines.has(line) ? "" : "tailored-changed-line"));
    section.append(documentBody); columns.append(section);
  }
  const label = node("label", "新版本名称");
  const name = node("input", ""); name.value = `${job.company || "目标公司"} · ${job.title || "目标岗位"} 定制版`.slice(0, 120); name.maxLength = 120;
  name.setAttribute("aria-label", "定制版本名称"); label.append(name);
  const status = node("div", "", "operation-status"); status.setAttribute("aria-live", "polite");
  let savedVersion = null;
  const versionId = `rv_${crypto.randomUUID().replaceAll("-", "")}`;
  let saveDraftId = draftId;
  let saveRevision = content.revision;
  let migrated = false;
  let confirmed = false;
  const downloads = node("div", "", "draft-actions");
  const exportButtons = ["pdf", "docx"].map(format => {
    const label = format === "pdf" ? "PDF" : "Word";
    const exportButton = action(`导出 ${label}`, async () => {
      if (!isCurrent() || !confirmed || !savedVersion) return;
      exportButton.disabled = true;
      const exportToken = crypto.randomUUID().replaceAll("-", "");
      const key = `tailoring-export-${savedVersion.version_id}-${format}`;
      try {
        const result = await request("/v1/workbench/exports", {
          method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": key },
          body: JSON.stringify({operation_id: `op_export_${exportToken}`, export_id: `exp_${exportToken}`, idempotency_key: key,
            workspace_id: workspaceId, resume_version_id: savedVersion.version_id, format, settings: {title: name.value.trim()}}),
        });
        if (!isCurrent()) return;
        if (result.operation?.status !== "committed" || result.export?.status !== "available") {
          status.textContent = "导出尚未完成，请稍后重试。"; exportButton.disabled = false; return;
        }
        const link = node("a", `下载 ${label}`);
        link.href = `${apiBase()}/v1/workbench/exports/${encodeURIComponent(result.export.export_id)}/download`;
        link.download = `${name.value.trim()}.${format}`;
        downloads.append(link); status.textContent = `已生成已确认版本的 ${label} 文件。`;
      } catch (error) { if (isCurrent()) { status.textContent = `导出失败：${error.message}`; exportButton.disabled = false; } }
    });
    exportButton.hidden = true;
    return exportButton;
  });
  const confirm = action("确认版本", async () => {
    if (!isCurrent() || !savedVersion) return;
    confirm.disabled = true;
    try {
      await request(`/v1/workbench/resume-versions/${encodeURIComponent(savedVersion.version_id)}/confirm`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ workspace_id: workspaceId, expected_revision: savedVersion.revision }),
      });
      if (!isCurrent()) return;
      confirmed = true;
      for (const button of exportButtons) button.hidden = false;
      status.textContent = "版本已确认，可直接导出 PDF / Word。";
    } catch (error) { status.textContent = `确认失败：${error.message}`; confirm.disabled = false; }
  });
  confirm.hidden = true;
  const save = action("保存为岗位定制版本", async () => {
    if (!isCurrent()) return;
    if (!name.value.trim()) { status.textContent = "请输入版本名称。"; return; }
    save.disabled = true;
    try {
      // Older tailoring used the source branch. Copy its accepted text into a JD-bound
      // Draft without modifying the original Draft or any of its suggestion references.
      if (!String(draft.branch_id).startsWith("rb_tailored_") && !migrated) {
        const branch = await request(`/v1/workbench/resume-versions/${encodeURIComponent(analysis.resume_version_id)}/branches`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ branch_id: `rb_tailored_${crypto.randomUUID().replaceAll("-", "")}`, resume_id: draft.resume_id,
            name: name.value.trim().slice(0, 160), branch_type: "company", job_snapshot_id: analysis.job_snapshot_id }),
        });
        if (!isCurrent()) return;
        const copy = await request(`/v1/workbench/resume-versions/${encodeURIComponent(analysis.resume_version_id)}/drafts`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ draft_id: `rd_${crypto.randomUUID().replaceAll("-", "")}`, workspace_id: workspaceId, branch_id: branch.branch_id }),
        });
        if (!isCurrent()) return;
        const patched = await request(`/v1/workbench/drafts/${encodeURIComponent(copy.draft_id)}`, {
          method: "PATCH", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ workspace_id: workspaceId, markdown: content.markdown, expected_revision: copy.revision, expected_content_sha256: copy.content.content_sha256 }),
        });
        if (!isCurrent()) return;
        saveDraftId = patched.draft_id; saveRevision = patched.revision; migrated = true;
      }
      savedVersion = await request(`/v1/workbench/drafts/${encodeURIComponent(saveDraftId)}/versions`, {
        method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": versionId },
        body: JSON.stringify({ workspace_id: workspaceId, version_id: versionId, label: name.value.trim(), expected_draft_revision: saveRevision }),
      });
      if (!isCurrent()) return;
      name.disabled = true; confirm.hidden = false;
      status.textContent = "已保存待确认版本；确认后可用于匹配和导出。";
    } catch (error) { status.textContent = `保存失败：${error.message}`; save.disabled = false; }
  });
  save.disabled = draft.status !== "active" || original.markdown === content.markdown;
  if (draft.status !== "active") status.textContent = "此 Draft 已保存，请到版本地图查看或确认对应版本。";
  else if (original.markdown === content.markdown) status.textContent = "尚无已采纳的修改，请先返回建议列表采纳。";
  const actions = node("div", "", "draft-actions"); actions.append(action("返回建议", onBack), save, confirm, ...exportButtons);
  panel.append(columns, label, actions, status, downloads); container.replaceChildren(panel);
  return true;
}
