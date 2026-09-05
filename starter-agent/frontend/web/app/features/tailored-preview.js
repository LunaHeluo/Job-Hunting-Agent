// Read back the persisted Draft: the preview must never imply unaccepted edits were saved.
export async function renderTailoredPreview({ request, container, workspaceId, analysis, draftId, isCurrent = () => true, onBack }) {
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
  panel.append(node("h2", "完整简历对照"), node("p", "右侧为已采纳到 Draft 的实际内容；高亮表示与另一侧不同的行。请核对事实后保存，原版本不会被覆盖。"));
  const columns = node("div", "", "tailored-preview-columns");
  for (const [title, markdown, other] of [["原始版本", original.markdown, content.markdown], ["当前 Draft", content.markdown, original.markdown]]) {
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
  const confirm = action("确认版本", async () => {
    if (!isCurrent() || !savedVersion) return;
    confirm.disabled = true;
    try {
      await request(`/v1/workbench/resume-versions/${encodeURIComponent(savedVersion.version_id)}/confirm`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ workspace_id: workspaceId, expected_revision: savedVersion.revision }),
      });
      status.textContent = "版本已确认，可在版本地图中查看并导出 PDF / Word。";
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
        const copy = await request(`/v1/workbench/resume-versions/${encodeURIComponent(analysis.resume_version_id)}/drafts`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ draft_id: `rd_${crypto.randomUUID().replaceAll("-", "")}`, workspace_id: workspaceId, branch_id: branch.branch_id }),
        });
        const patched = await request(`/v1/workbench/drafts/${encodeURIComponent(copy.draft_id)}`, {
          method: "PATCH", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ workspace_id: workspaceId, markdown: content.markdown, expected_revision: copy.revision, expected_content_sha256: copy.content.content_sha256 }),
        });
        saveDraftId = patched.draft_id; saveRevision = patched.revision; migrated = true;
      }
      savedVersion = await request(`/v1/workbench/drafts/${encodeURIComponent(saveDraftId)}/versions`, {
        method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": versionId },
        body: JSON.stringify({ workspace_id: workspaceId, version_id: versionId, label: name.value.trim(), expected_draft_revision: saveRevision }),
      });
      name.disabled = true; confirm.hidden = false;
      status.textContent = "已保存待确认版本；确认后可用于匹配和导出。";
    } catch (error) { status.textContent = `保存失败：${error.message}`; save.disabled = false; }
  });
  save.disabled = draft.status !== "active" || original.markdown === content.markdown;
  if (draft.status !== "active") status.textContent = "此 Draft 已保存，请到版本地图查看或确认对应版本。";
  else if (original.markdown === content.markdown) status.textContent = "尚无已采纳的修改，请先返回建议列表采纳。";
  const actions = node("div", "", "draft-actions"); actions.append(action("返回建议", onBack), save, confirm);
  panel.append(columns, label, actions, status); container.replaceChildren(panel);
  return true;
}
