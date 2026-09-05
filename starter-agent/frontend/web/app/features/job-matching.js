import { updateWorkbenchContext } from "../workbench-context.js";
import { renderTailoredPreview } from "./tailored-preview.js?v=compact-ui";

const selected = { resumeVersionId: "", jobSnapshotId: "" };

const TAILORING_REJECTION_LABELS = Object.freeze({
  tailoring_output_invalid: "模型返回格式不符合要求",
  tailoring_no_verified_evidence: "没有可用于此段改写的已验证证据",
  empty_model_output: "模型没有返回候选改写",
  unknown_block: "模型引用了不存在的简历区块",
  duplicate_block: "同一简历区块被重复改写",
  unknown_requirement: "模型引用了不存在或未匹配的岗位要求",
  unknown_evidence: "模型引用了不存在的证据",
  evidence_block_mismatch: "证据与简历区块不对应",
  evidence_requirement_mismatch: "证据与岗位要求不对应",
  new_numeric_claim: "新增了原文证据中没有的数字",
  unchanged_text: "候选内容与原文没有变化",
});

function token(prefix) {
  return `${prefix}_${crypto.randomUUID().replaceAll("-", "")}`;
}

export function analysisEvidenceCount(analysis) {
  return (analysis?.requirements || []).reduce(
    (total, requirement) => total + (requirement.evidence || []).length,
    0,
  );
}

export function requiresEvidenceUpgrade(analysis) {
  return analysis?.rule_version !== "match-rule.v2.3";
}

export function selectRestorableAnalysis(analyses) {
  const ordered = [...(analyses || [])].sort((left, right) =>
    String(right.created_at).localeCompare(String(left.created_at))
    || String(right.analysis_id).localeCompare(String(left.analysis_id))
  );
  const ready = ordered.filter(item => ["validated", "partial"].includes(item.status));
  const latest = ready[0] || ordered[0];
  if (!latest || !requiresEvidenceUpgrade(latest)) return latest;
  return ready.find(item =>
    !requiresEvidenceUpgrade(item)
    && item.resume_content_sha256 === latest.resume_content_sha256
    && item.job_content_sha256 === latest.job_content_sha256
  ) || latest;
}

export function buildMatchEvaluationPayload(
  workspaceId,
  resumeVersionId,
  jobSnapshotId,
  { analysisId, operationId },
) {
  return {
    analysis_id: analysisId,
    operation_id: operationId,
    idempotency_key: operationId,
    workspace_id: workspaceId,
    resume_version_id: resumeVersionId,
    job_snapshot_id: jobSnapshotId,
  };
}

export function createJobMatching({ request, apiBase = () => "", elements, reloadHome, activatePanel = () => {} }) {
  const tailoringDrafts = new Map();
  const tailoringMain = elements.tailoringMain || elements.main;
  let tailoringRequest = 0;
  let matchDialog = null;
  let lifecycleGuard = () => true;
  const isLifecycleCurrent = () => lifecycleGuard();
  const setLifecycleGuard = guard => { matchDialog?.close(); lifecycleGuard = typeof guard === "function" ? guard : () => true; };
  function button(label, action, className = "") {
    const value = document.createElement("button"); value.type = "button"; value.textContent = label; value.className = className; value.addEventListener("click", action); return value;
  }

  async function evaluateMatch(workspaceId, resumeVersionId, jobSnapshotId) {
    const analysisId = token("ma");
    const operationId = token("op_match");
    return request("/v1/workbench/match-analyses/evaluate", {
      method: "POST",
      headers: { "Content-Type": "application/json", "Idempotency-Key": operationId },
      body: JSON.stringify(buildMatchEvaluationPayload(
        workspaceId,
        resumeVersionId,
        jobSnapshotId,
        { analysisId, operationId },
      )),
    });
  }

  function renderJobList(home, workspaceId) {
    elements.jobs.className = "workbench-job-rail";
    elements.jobs.replaceChildren();
    const jobs = home.priority_jobs || [];
    const list = document.createElement("div");
    list.className = "workbench-job-rail-list";
    for (const job of jobs) {
      const item = button("", () => openJob(workspaceId, job.job_id), "job-list-item workbench-job-rail-item");
      const heading = document.createElement("strong"); heading.textContent = job.title;
      const company = document.createElement("span"); company.textContent = job.company || "未填写公司";
      const state = document.createElement("small"); state.className = "workbench-job-state"; state.textContent = job.user_status || "已确认";
      item.append(heading, company, state); list.append(item);
    }
    if (!jobs.length) {
      const empty = document.createElement("div"); empty.className = "workbench-empty workbench-job-rail-empty"; empty.textContent = "导入 JD 后，这里会显示岗位标签与匹配摘要。"; list.append(empty);
    }
    elements.jobs.append(list);
    const importButton = button("导入 JD（文本、文件或链接）", () => renderJobForm(workspaceId), "primary-action workbench-job-import");
    elements.jobs.append(importButton);
    const summary = document.createElement("section"); summary.className = "workbench-job-match-summary";
    const summaryTitle = document.createElement("strong"); summaryTitle.textContent = "匹配摘要";
    const summaryText = document.createElement("p"); summaryText.textContent = jobs.length
      ? "选择岗位可在中间区域查看完整分数、证据和改进建议。"
      : "确认岗位后，将自动在这里汇总匹配亮点与待提升项。";
    summary.append(summaryTitle, summaryText); elements.jobs.append(summary);
  }

  function renderJobAnalysisEditor({ workspaceId, panel, closeDialog, title, company, location, source, analysis, extractionMethod }) {
    panel.replaceChildren();
    const heading = document.createElement("h2"); heading.textContent = "检查并编辑 JD";
    const helper = document.createElement("p"); helper.className = "workbench-helper"; helper.textContent = `已通过 ${extractionMethod} 提取。标签可直接删除或补充；确认后才会创建岗位快照并用于匹配。`;
    const role = document.createElement("input"); role.value = title; role.setAttribute("aria-label", "岗位名称");
    const employer = document.createElement("input"); employer.value = company; employer.setAttribute("aria-label", "公司");
    const place = document.createElement("input"); place.value = location || ""; place.placeholder = "地点（可选）"; place.setAttribute("aria-label", "地点");
    const sections = document.createElement("div"); sections.className = "jd-analysis-sections";
    const editors = [
      createTagEditor("岗位职责", analysis.responsibilities || []),
      createTagEditor("必需要求", analysis.required_skills || []),
      createTagEditor("加分项", analysis.preferred_skills || []),
    ];
    editors.forEach(editor => sections.append(editor.element));
    const sourceDetails = document.createElement("details"); sourceDetails.className = "jd-original-source";
    const sourceSummary = document.createElement("summary"); sourceSummary.textContent = "查看解析后的原始 JD";
    const sourceText = document.createElement("pre"); sourceText.textContent = source; sourceDetails.append(sourceSummary, sourceText);
    const status = document.createElement("div"); status.className = "operation-status"; status.setAttribute("aria-live", "polite");
    const back = button("返回来源", () => { closeDialog(); renderJobForm(workspaceId); }, "secondary-action");
    const save = button("确认并留存岗位", async () => {
      const values = editors.map(editor => editor.values());
      if (!role.value.trim() || !employer.value.trim()) { status.textContent = "请补充岗位名称和公司。"; return; }
      if (!values.some(items => items.length)) { status.textContent = "请至少保留一条岗位职责、必需要求或加分项。"; return; }
      save.disabled = true; status.textContent = "正在创建岗位和不可变 JD 快照…";
      const candidateId = token("jc"); const operationId = token("op_job");
      const markdown = buildStructuredJobMarkdown(role.value.trim(), employer.value.trim(), place.value.trim(), values);
      try {
        await request("/v1/workbench/job-candidates", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ candidate_id: candidateId, workspace_id: workspaceId, source_kind: "text", title: role.value.trim(), company: employer.value.trim(), location: place.value.trim() || null, filename: "structured-job.md", content: markdown, confirmed_authorized: true }) });
        const promotion = await request("/v1/workbench/job-candidates/retain", { method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": operationId }, body: JSON.stringify({ candidate_id: candidateId, workspace_id: workspaceId, operation_id: operationId, idempotency_key: operationId }) });
        selected.jobSnapshotId = promotion.snapshot_id; closeDialog(); activatePanel(); await reloadHome();
      } catch (error) { status.textContent = `留存失败：${error.message}`; save.disabled = false; }
    }, "primary-action");
    const windowTitle = panel.closest("dialog")?.querySelector(".workbench-dialog-header h2");
    if (windowTitle) windowTitle.textContent = heading.textContent;
    panel.append(helper, role, employer, place, sections, sourceDetails, back, save, status);
  }

  function createTagEditor(labelText, initialValues) {
    const element = document.createElement("section"); element.className = "jd-tag-editor";
    const title = document.createElement("h3"); title.textContent = labelText;
    const tags = document.createElement("div"); tags.className = "jd-tag-list";
    const input = document.createElement("input"); input.placeholder = "输入一项后按 Enter"; input.setAttribute("aria-label", `${labelText}新增标签`);
    const values = [...new Set(initialValues.map(value => String(value).trim()).filter(Boolean))];
    const render = () => {
      tags.replaceChildren();
      for (const value of values) {
        const tag = button(`${value} ×`, () => { values.splice(values.indexOf(value), 1); render(); }, "jd-tag");
        tags.append(tag);
      }
    };
    const add = () => { const value = input.value.trim(); if (value && !values.includes(value)) { values.push(value); render(); } input.value = ""; };
    input.addEventListener("keydown", event => { if (event.key === "Enter") { event.preventDefault(); add(); } });
    input.addEventListener("blur", add); render(); element.append(title, tags, input);
    return { element, values: () => [...values] };
  }

  function buildStructuredJobMarkdown(title, company, location, [responsibilities, required, preferred]) {
    const section = (heading, values) => values.length ? `\n## ${heading}\n${values.map(value => `- ${value}`).join("\n")}` : "";
    return `# ${title}\n\n公司：${company}${location ? `\n地点：${location}` : ""}${section("岗位职责", responsibilities)}${section("必需要求", required)}${section("加分项", preferred)}\n`;
  }

  function openWorkbenchDialog(titleText, bodyClass = "") {
    matchDialog?.close();
    const dialog = document.createElement("dialog"); dialog.className = "workbench-dialog";
    dialog.setAttribute("aria-labelledby", "workbenchDialogTitle");
    const header = document.createElement("header"); header.className = "workbench-dialog-header";
    const title = document.createElement("h2"); title.id = "workbenchDialogTitle"; title.textContent = titleText;
    const close = button("×", () => dialog.close(), "workbench-dialog-close"); close.setAttribute("aria-label", "关闭窗口");
    header.append(title, close);
    const panel = document.createElement("section"); panel.className = `workbench-dialog-body ${bodyClass}`;
    dialog.append(header, panel); document.body.append(dialog); matchDialog = dialog;
    dialog.addEventListener("close", () => { if (matchDialog === dialog) matchDialog = null; dialog.remove(); });
    dialog.addEventListener("click", event => {
      const rect = dialog.getBoundingClientRect();
      if (event.target === dialog && (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom)) dialog.close();
    });
    dialog.showModal();
    return { dialog, panel, title, closeDialog: () => dialog.close() };
  }

  function renderJobForm(workspaceId) {
    const { panel, closeDialog } = openWorkbenchDialog("评估岗位来源", "job-input-panel");
    const kind = document.createElement("select"); kind.setAttribute("aria-label", "JD 来源类型"); kind.innerHTML = '<option value="text">粘贴 JD</option><option value="file">上传 JD 文件/截图</option><option value="stable_url">稳定 URL</option>';
    const role = document.createElement("input"); role.placeholder = "岗位名称"; role.setAttribute("aria-label", "岗位名称");
    const company = document.createElement("input"); company.placeholder = "公司"; company.setAttribute("aria-label", "公司");
    const location = document.createElement("input"); location.placeholder = "地点（可选）"; location.setAttribute("aria-label", "地点");
    const content = document.createElement("textarea"); content.rows = 16; content.placeholder = "粘贴完整 JD；或选择稳定 URL 后输入 https://…"; content.setAttribute("aria-label", "JD 正文或 URL");
    const file = document.createElement("input"); file.type = "file"; file.accept = ".txt,.md,.markdown,.docx,.pdf,.png,.jpg,.jpeg,.webp"; file.hidden = true; file.setAttribute("aria-label", "JD 文件或截图");
    const updateSourceInput = () => {
      const uploading = kind.value === "file";
      content.hidden = uploading; file.hidden = !uploading;
      content.placeholder = kind.value === "stable_url" ? "输入稳定 URL，例如 https://careers.example.com/job/123" : "粘贴完整 JD";
    };
    kind.addEventListener("change", updateSourceInput); updateSourceInput();
    const authorized = document.createElement("label"); const check = document.createElement("input"); check.type = "checkbox"; authorized.append(check, " 我确认有权留存此岗位描述");
    const status = document.createElement("div"); status.className = "operation-status"; status.setAttribute("aria-live", "polite");
    const submit = button("评估并留存", async () => {
      if (((kind.value === "file" && !file.files?.[0]) || (kind.value !== "file" && !content.value.trim())) || (kind.value !== "stable_url" && (!role.value.trim() || !company.value.trim() || !check.checked))) { status.textContent = "请填写来源信息、选择文件（如适用）并确认留存授权。"; return; }
      submit.disabled = true; status.textContent = "正在创建候选；尚未写入正式岗位…";
      const candidateId = token("jc"); const operationId = token("op_job");
      try {
        if (kind.value === "file") {
          const data = new FormData(); data.set("file", file.files[0]);
          const payload = await request("/v1/workbench/job-documents/analyze/upload", { method: "POST", body: data });
          renderJobAnalysisEditor({ workspaceId, panel, closeDialog, title: role.value.trim(), company: company.value.trim(), location: location.value.trim(), source: payload.markdown, analysis: payload.analysis, extractionMethod: payload.extraction_method });
          return;
        } else if (kind.value === "text") {
          const payload = await request("/v1/workbench/job-documents/analyze", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ filename: "job.md", content: content.value }) });
          renderJobAnalysisEditor({ workspaceId, panel, closeDialog, title: role.value.trim(), company: company.value.trim(), location: location.value.trim(), source: payload.markdown, analysis: payload.analysis, extractionMethod: payload.extraction_method });
          return;
        } else {
          await request("/v1/workbench/job-candidates", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ candidate_id: candidateId, workspace_id: workspaceId, source_kind: "stable_url", url: content.value.trim() }),
          });
        }
        const promotion = await request("/v1/workbench/job-candidates/retain", {
          method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": operationId },
          body: JSON.stringify({ candidate_id: candidateId, workspace_id: workspaceId, operation_id: operationId, idempotency_key: operationId }),
        });
        status.textContent = "岗位及不可变 JD 快照已确认。"; selected.jobSnapshotId = promotion.snapshot_id; closeDialog(); activatePanel(); await reloadHome();
      } catch (error) { status.textContent = `留存失败：${error.message}`; }
      finally { submit.disabled = false; }
    }, "primary-action");
    panel.append(kind, role, company, location, content, file, authorized, submit, status);

  }

  async function openJob(workspaceId, jobId) {
    if (!isLifecycleCurrent()) return false;
    const ownerGuard = lifecycleGuard;
    const { dialog, panel, title } = openWorkbenchDialog("岗位详情", "job-preview-panel");
    const current = () => dialog.open && lifecycleGuard === ownerGuard && ownerGuard();
    panel.textContent = "正在加载岗位快照…";
    try {
      const [job, snapshots] = await Promise.all([request(`/v1/workbench/jobs/${encodeURIComponent(jobId)}`), request(`/v1/workbench/jobs/${encodeURIComponent(jobId)}/snapshots`)]);
      if (!current()) return false;
      const snapshot = (snapshots.items || []).at(-1);
      if (!snapshot) throw new Error("岗位没有可用快照");
      const source = await request(`/v1/workbench/job-snapshots/${encodeURIComponent(snapshot.snapshot_id)}/content?workspace_id=${encodeURIComponent(workspaceId)}`);
      if (!current()) return false;
      title.textContent = `${job.company} · ${job.title}`;
      const meta = document.createElement("p"); meta.textContent = snapshot.verified ? "已验证来源" : "手工来源";
      const pre = document.createElement("pre"); pre.textContent = source.markdown;
      const use = button("使用此快照进行匹配", () => {
        selected.jobSnapshotId = snapshot.snapshot_id;
        dialog.close(); renderMatchChooser(workspaceId);
      }, "primary-action");
      panel.replaceChildren(meta, pre, use);
    } catch (error) { if (current()) panel.textContent = `岗位加载失败：${error.message}`; return false; }
  }

  async function renderMatchChooser(workspaceId, { tailor = false } = {}) {
    if (!isLifecycleCurrent()) return false;
    const ownerGuard = lifecycleGuard;
    const { dialog, panel } = openWorkbenchDialog("匹配评估", "match-chooser");
    const status = document.createElement("div"); status.className = "operation-status";
    status.setAttribute("role", "status"); status.textContent = "正在加载可评估对象…";
    panel.append(status);
    const current = () => dialog.open && lifecycleGuard === ownerGuard && ownerGuard();
    try {
      const [home, jobs] = await Promise.all([request(`/v1/workbench/workspaces/${encodeURIComponent(workspaceId)}/home`), request(`/v1/workbench/jobs?workspace_id=${encodeURIComponent(workspaceId)}`)]);
      if (!current()) return false;
      const resume = document.createElement("select"); resume.setAttribute("aria-label", "已确认简历版本");
      for (const item of (home.recent_versions || []).filter(value => value.status === "confirmed")) { const option = document.createElement("option"); option.value = item.version_id; option.textContent = item.label; resume.append(option); }
      const snapshot = document.createElement("select"); snapshot.setAttribute("aria-label", "岗位快照");
      for (const job of jobs.items || []) {
        const page = await request(`/v1/workbench/jobs/${encodeURIComponent(job.job_id)}/snapshots`);
        if (!current()) return false;
        for (const item of page.items || []) { const option = document.createElement("option"); option.value = item.snapshot_id; option.textContent = `${job.company} · ${job.title} · ${new Date(item.captured_at).toLocaleDateString()}`; snapshot.append(option); }
      }
      if ([...snapshot.options].some(item => item.value === selected.jobSnapshotId)) snapshot.value = selected.jobSnapshotId;
      if ([...resume.options].some(item => item.value === selected.resumeVersionId)) resume.value = selected.resumeVersionId;
      status.textContent = "";
      const evaluate = button("开始证据匹配", async () => {
        if (!resume.value || !snapshot.value) { status.textContent = "需要已确认简历版本和岗位快照。"; return; }
        evaluate.disabled = true; resume.disabled = true; snapshot.disabled = true;
        status.textContent = "正在提取要求并验证简历证据…";
        try {
          const analysis = await evaluateMatch(workspaceId, resume.value, snapshot.value);
          if (!current()) return;
          selected.resumeVersionId = resume.value; selected.jobSnapshotId = snapshot.value;
          updateWorkbenchContext({ workspace_id: workspaceId, resume_version_id: resume.value, job_snapshot_id: snapshot.value, match_analysis_id: analysis.analysis_id });
          dialog.close();
          if (tailor) await prepareTailoredResume(workspaceId, analysis.analysis_id);
          else await renderAnalysis(workspaceId, analysis);
        } catch (error) { if (current()) { status.textContent = `评估失败：${error.message}`; evaluate.disabled = false; resume.disabled = false; snapshot.disabled = false; } }
      }, "primary-action");
      panel.replaceChildren(label("简历版本", resume), label("岗位快照", snapshot), evaluate, status);
      resume.focus();
    } catch (error) { if (current()) status.textContent = `评估对象加载失败：${error.message}`; return false; }
  }

  function label(text, control) { const value = document.createElement("label"); value.append(text, control); return value; }

  async function renderAnalysis(workspaceId, analysis, options = {}) {
    const localGuard = typeof options.isCurrent === "function" ? options.isCurrent : () => true;
    const isCurrent = () => localGuard() && isLifecycleCurrent();
    if (!isCurrent()) return false;
    if (activatePanel() === false) return false;
    elements.main.className = "";
    updateWorkbenchContext({
      workspace_id: workspaceId,
      resume_version_id: analysis.resume_version_id,
      job_snapshot_id: analysis.job_snapshot_id,
      match_analysis_id: analysis.analysis_id,
    });
    let job = null;
    try { job = await request(`/v1/workbench/job-snapshots/${encodeURIComponent(analysis.job_snapshot_id)}`); } catch { /* Score data remains useful without snapshot metadata. */ }
    if (!isCurrent()) return false;
    const panel = document.createElement("section"); panel.className = "match-analysis-panel";
    const total = Number(analysis.total_score || 0);
    const noEvidence = analysis.total_score === 0 && analysisEvidenceCount(analysis) === 0
      && (analysis.requirements || []).length > 0
      && analysis.requirements.every(item => item.verdict === "missing");
    const matched = (analysis.requirements || []).filter(item => item.verdict === "matched" || item.verdict === "partial");
    const gaps = (analysis.requirements || []).filter(item => item.verdict === "missing" || item.verdict === "conflict");
    const grade = total >= 75 ? "A" : total >= 55 ? "B" : "C";
    const recommendation = total >= 75 ? "建议投递" : total >= 55 ? "优化后投递" : "优先补齐短板";
    const recommendationCopy = total >= 75 ? "核心要求匹配良好，可结合亮点直接投递。" : total >= 55 ? "已有可验证基础，建议先强化短板再投递。" : "当前证据覆盖有限，建议先补齐关键要求。";
    const heading = document.createElement("header"); heading.className = "match-summary-header";
    const scoreBlock = document.createElement("div"); scoreBlock.className = "match-score-number";
    const score = document.createElement("strong"); score.textContent = analysis.total_score == null ? "—" : `${total.toFixed(total % 1 ? 2 : 0)}`;
    const denominator = document.createElement("span"); denominator.textContent = "/100"; scoreBlock.append(score, denominator);
    const jobBlock = document.createElement("div"); jobBlock.className = "match-job-summary";
    const jobTitle = document.createElement("h2"); jobTitle.textContent = job ? `${job.company} · ${job.title}` : "当前岗位匹配分析";
    const description = document.createElement("p"); description.textContent = noEvidence
      ? "当前规则未找到匹配证据，0 分不代表简历质量为零。请先核对解析原文与岗位要求；字面关键词匹配可能漏掉中文改写或同义表达。"
      : recommendationCopy; jobBlock.append(jobTitle, description);
    const gradeBadge = document.createElement("span"); gradeBadge.className = `match-grade match-grade-${grade.toLowerCase()}`; gradeBadge.textContent = `${grade} · ${recommendation}`;
    if (noEvidence) { gradeBadge.textContent = "匹配证据待核对"; gradeBadge.className = "match-grade"; }
    heading.append(scoreBlock, jobBlock, gradeBadge);
    const analysisMeta = document.createElement("div"); analysisMeta.className = "match-analysis-meta";
    const state = document.createElement("span"); state.textContent = `分析状态：${analysis.status}`;
    const rule = document.createElement("span"); rule.textContent = `规则：${analysis.rule_version} · 已验证证据 ${analysisEvidenceCount(analysis)} 条`;
    const coverage = document.createElement("span"); coverage.textContent = `已覆盖 ${matched.length} 项 · 待补齐 ${gaps.length} 项`;
    analysisMeta.append(state, rule, coverage);
    const dimensions = document.createElement("div"); dimensions.className = "score-dimensions";
    for (const item of analysis.dimensions || []) { const row = document.createElement("span"); row.textContent = `${item.name.replaceAll("_", " ")} ${item.score.toFixed(1)}（权重 ${Math.round(item.weight * 100)}%）`; dimensions.append(row); }
    const highlights = document.createElement("section"); highlights.className = "match-insights";
    const highlightTitle = document.createElement("h3"); highlightTitle.textContent = "匹配亮点"; highlights.append(highlightTitle);
    const highlightList = document.createElement("div"); highlightList.className = "match-insight-list match-insight-positive";
    if (!matched.length) { const empty = document.createElement("p"); empty.textContent = "尚未找到可验证的匹配证据。"; highlightList.append(empty); }
    for (const item of matched) highlightList.append(renderInsightCard(item, "positive"));
    highlights.append(highlightList);
    const gapsSection = document.createElement("section"); gapsSection.className = "match-insights";
    const gapTitle = document.createElement("h3"); gapTitle.textContent = noEvidence ? "尚未找到证据的要求" : "短板"; gapsSection.append(gapTitle);
    const gapList = document.createElement("div"); gapList.className = "match-insight-list match-insight-gap";
    if (!gaps.length) { const empty = document.createElement("p"); empty.textContent = "当前分析未发现明确短板。"; gapList.append(empty); }
    for (const item of gaps) gapList.append(renderInsightCard(item, "gap"));
    gapsSection.append(gapList);
    const strategy = document.createElement("aside"); strategy.className = "match-strategy";
    const strategyTitle = document.createElement("strong"); strategyTitle.textContent = "下一步建议：";
    strategy.append(strategyTitle, ` ${gaps.length ? "优先围绕短板生成基于证据的修改建议；系统不会自动补写不存在的经历。" : "可生成基于当前已验证证据的定制建议。"}`);
    if (noEvidence) strategy.textContent = "下一步：核对下方简历原文。若内容缺失，重新上传；若原文完整，请对照岗位逐条核实证据。补充真实素材后重新评估。";
    const requirements = document.createElement("details"); requirements.className = "match-requirement-details";
    const requirementSummary = document.createElement("summary"); requirementSummary.textContent = `查看全部 ${analysis.requirements?.length || 0} 条匹配依据`;
    const requirementList = document.createElement("div"); requirementList.className = "requirement-list";
    for (const item of analysis.requirements || []) {
      const detail = document.createElement("details"); detail.className = `requirement requirement-${item.verdict}`;
      const summary = document.createElement("summary"); summary.textContent = `${item.verdict} · ${item.original_text}`;
      const explanation = document.createElement("p"); explanation.textContent = item.explanation;
      detail.append(summary, explanation);
      for (const ref of item.evidence || []) { const quote = document.createElement("blockquote"); quote.textContent = ref.quote || "证据已验证；正文按需显示。"; detail.append(quote); }
      if (item.verdict === "missing" || item.verdict === "conflict") { const warning = document.createElement("p"); warning.className = "gap-warning"; warning.textContent = "当前证据不足或冲突：不会自动补写经历，请核对原文。"; detail.append(warning); }
      requirementList.append(detail);
    }
    requirements.append(requirementSummary, requirementList);
    const actions = document.createElement("div"); actions.className = "draft-actions";
    actions.append(button("重新选择", () => renderMatchChooser(workspaceId)), button("AI 定制简历", () => prepareTailoredResume(workspaceId, analysis.analysis_id).catch(() => {}), "primary-action"));
    panel.append(heading, analysisMeta, dimensions, highlights, gapsSection, strategy, requirements, actions); elements.main.replaceChildren(panel);
    return true;
  }

  function renderInsightCard(item, tone) {
    const card = document.createElement("article"); card.className = `match-insight-card match-insight-${tone}`;
    const title = document.createElement("p"); title.className = "match-insight-title"; title.textContent = item.original_text;
    const body = document.createElement("p"); body.className = "match-insight-body";
    const quote = item.evidence?.[0]?.quote;
    body.textContent = quote || item.explanation;
    card.append(title, body); return card;
  }

  async function prepareTailoredResume(workspaceId, analysisId, { regenerate = false, blockIds = [], expectedDraftRevision } = {}) {
    if (!isLifecycleCurrent()) return false;
    const ownerGuard = lifecycleGuard;
    const requestNumber = ++tailoringRequest;
    const current = () => ownerGuard() && ownerGuard === lifecycleGuard && requestNumber === tailoringRequest;
    if (activatePanel({ panel: "tailor", resetScroll: true }) === false) return false;
    tailoringMain.className = "";
    tailoringMain.textContent = "正在读取匹配分析并准备 AI 定制简历…";
    try {
      const analysis = await request(`/v1/workbench/match-analyses/${encodeURIComponent(analysisId)}`);
      if (!current()) return false;
      if (requiresEvidenceUpgrade(analysis)) {
        renderEvidenceUpgrade(workspaceId, analysis);
        return;
      }
      const page = await request(`/v1/workbench/match-analyses/${encodeURIComponent(analysisId)}/suggestions`);
      if (!current()) return false;
      const existing = (page.items || []).filter(suggestion => suggestion.change_type === "ai_tailor_v1");
      const key = `tailoring-draft:${workspaceId}:${analysisId}`;
      let remembered = tailoringDrafts.get(key);
      try { remembered ||= JSON.parse(sessionStorage.getItem(key) || "null"); } catch {}
      const draftIds = [...new Set([remembered?.draft_id, ...[...existing].sort((a, b) => String(b.created_at).localeCompare(String(a.created_at))).map(item => item.target_draft_id)].filter(Boolean))];
      let draft;
      for (const draftId of draftIds) {
        const candidate = await request(`/v1/workbench/drafts/${encodeURIComponent(draftId)}`);
        if (!current()) return false;
        if (candidate.status === "active" && candidate.base_version_id === analysis.resume_version_id) {
          draft = candidate; break;
        }
      }
      const remember = value => {
        tailoringDrafts.set(key, value);
        try { sessionStorage.setItem(key, JSON.stringify(value)); } catch {}
      };
      if (expectedDraftRevision !== undefined && (!draft || draft.revision !== expectedDraftRevision)) {
        throw new Error("Draft 已更新，请重新打开当前建议后再生成。");
      }
      if (!draft) {
        const version = await request(`/v1/workbench/resume-versions/${encodeURIComponent(analysis.resume_version_id)}`);
        if (!current()) return false;
        const job = await request(`/v1/workbench/job-snapshots/${encodeURIComponent(analysis.job_snapshot_id)}`);
        if (!current()) return false;
        const branch = remembered?.branch_id && !remembered.draft_id ? remembered : await request(`/v1/workbench/resume-versions/${encodeURIComponent(version.version_id)}/branches`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ branch_id: token("rb_tailored"), resume_id: version.resume_id,
            name: `${job.company || "目标公司"} · ${job.title || "目标岗位"}`.slice(0, 160), branch_type: "company", job_snapshot_id: analysis.job_snapshot_id }),
        });
        remember({branch_id: branch.branch_id});
        if (!current()) return false;
        draft = await request(`/v1/workbench/resume-versions/${encodeURIComponent(version.version_id)}/drafts`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ draft_id: token("rd_tailored"), workspace_id: workspaceId, branch_id: branch.branch_id }),
        });
      }
      remember({draft_id: draft.draft_id, branch_id: draft.branch_id});
      if (!current()) return false;
      const reusable = existing.filter(suggestion => suggestion.status === "pending"
        && suggestion.target_draft_id === draft.draft_id && suggestion.target_draft_revision === draft.revision);
      const history = await request(`/v1/workbench/drafts/${encodeURIComponent(draft.draft_id)}/tailoring-generations?analysis_id=${encodeURIComponent(analysisId)}`);
      if (!current()) return false;
      const latest = history.items?.[0];
      const previousFailures = {};
      for (const result of [...(history.items || [])].reverse()) {
        Object.assign(previousFailures, result.block_failures || {});
        for (const item of result.items || []) delete previousFailures[item.block_id];
        for (const id of result.unchanged_block_ids || []) delete previousFailures[id];
        if (result.outcome === "no_change") for (const id of result.block_ids || []) delete previousFailures[id];
      }
      if (!regenerate && (reusable.length || (latest && !(latest.items || []).length))) {
        renderTailoredSuggestions(workspaceId, analysis, reusable, {
          ...latest, reused: true, outcome: reusable.length ? "ready" : latest.outcome,
          draft_id: draft.draft_id, draft_revision: draft.revision, block_failures: previousFailures,
        });
        return;
      }
      const generated = await request(`/v1/workbench/match-analyses/${encodeURIComponent(analysisId)}/tailored-resume-candidates`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ workspace_id: workspaceId, draft_id: draft.draft_id, ...(regenerate ? { generation_id: token("tg") } : {}), ...(blockIds.length ? {block_ids: blockIds} : {}) }),
      });
      if (!current()) return false;
      const replacedBlockIds = new Set((generated.items || []).map(item => item.block_id));
      const failures = {...previousFailures, ...(generated.block_failures || {})};
      for (const id of replacedBlockIds) delete failures[id];
      for (const id of generated.unchanged_block_ids || []) delete failures[id];
      if (generated.outcome === "no_change") for (const id of blockIds) delete failures[id];
      const candidates = blockIds.length
        ? [...reusable.filter(item => !replacedBlockIds.has(item.block_id)), ...(generated.items || [])]
        : generated.items || [];
      renderTailoredSuggestions(workspaceId, analysis, candidates, {
        ...generated,
        block_failures: failures,
        empty_scope_result: blockIds.length > 0 && !(generated.items || []).length,
        draft_id: generated.draft_id || draft.draft_id,
        reused: generated.reused === true,
        attempts: generated.attempts ?? 1,
        rejected_reasons: generated.rejected_reasons || [],
        reflection: generated.reflection,
      });
    } catch (error) {
      if (!current()) return false;
      tailoringMain.textContent = `AI 定制简历准备失败：${error.message}`;
      throw error;
    }
  }

  function renderEvidenceUpgrade(workspaceId, analysis) {
    const panel = document.createElement("section"); panel.className = "suggestion-panel tailored-upgrade-panel";
    const title = document.createElement("h2"); title.textContent = "证据策略需要升级";
    const copy = document.createElement("p"); copy.textContent = "这份历史分析仍可查看，但 AI 定制简历需要按当前简历重新选择一次可信证据。相同内容只升级一次，之后刷新会直接复用。";
    const status = document.createElement("div"); status.className = "operation-status"; status.setAttribute("aria-live", "polite");
    const upgrade = button("一次性升级分析", async () => {
      upgrade.disabled = true; status.textContent = "正在按当前简历范围重新验证证据…";
      try {
        const upgraded = await evaluateMatch(
          workspaceId,
          analysis.resume_version_id,
          analysis.job_snapshot_id,
        );
        updateWorkbenchContext({
          workspace_id: workspaceId,
          resume_version_id: upgraded.resume_version_id,
          job_snapshot_id: upgraded.job_snapshot_id,
          match_analysis_id: upgraded.analysis_id,
        });
        await prepareTailoredResume(workspaceId, upgraded.analysis_id);
      } catch (error) {
        status.textContent = `升级失败：${error.message}`; upgrade.disabled = false;
      }
    }, "primary-action");
    const actions = document.createElement("div"); actions.className = "draft-actions";
    actions.append(button("返回分析", () => renderAnalysis(workspaceId, analysis)), upgrade);
    panel.append(title, copy, actions, status); tailoringMain.replaceChildren(panel);
  }

  function renderTailoredSuggestions(workspaceId, analysis, suggestions, diagnostics = {}) {
    if (!elements.tailoringMain && activatePanel({ panel: "tailor", resetScroll: true }) === false) return false;
    const ownerGuard = lifecycleGuard;
    const ownsPanel = () => ownerGuard() && lifecycleGuard === ownerGuard;
    tailoringMain.className = "";
    const panel = document.createElement("section"); panel.className = "suggestion-panel tailored-suggestion-panel";
    const header = document.createElement("header"); header.className = "tailored-suggestion-header";
    const heading = document.createElement("div");
    const title = document.createElement("h2"); title.textContent = "AI 定制简历建议";
    const helper = document.createElement("p"); helper.textContent = diagnostics.reused
      ? "已复用这次匹配已有的定制结果；模型没有重复运行。"
      : !suggestions.length ? `生成已结束，共执行 ${diagnostics.attempts || 0} 轮。请查看下方结果说明。`
      : diagnostics.attempts === 2
        ? "已自动修复生成 2 次；所有保留改写均通过证据校验。"
        : "所有改写均绑定已验证证据。可编辑并选择后，一次写入 Draft。";
    heading.append(title, helper);
    const safety = document.createElement("span"); safety.className = "tailored-safety-badge"; safety.textContent = "正式版本未改变";
    header.append(heading, safety); panel.append(header);
    if (diagnostics.block_ids?.length) {
      const scope = document.createElement("p"); scope.textContent = "本次仅重新生成选定段落；其他候选保留，Draft 尚未改变。"; panel.append(scope);
    }

    if (diagnostics.reflection?.notes) {
      const plan = document.createElement("details");
      const title = document.createElement("summary"); title.textContent = "全文定制思路";
      const notes = document.createElement("p"); notes.textContent = diagnostics.reflection.notes;
      plan.append(title, notes); panel.append(plan);
    }
    const failedBlocks = Object.keys(diagnostics.block_failures || {});
    if (failedBlocks.length) {
      const partial = document.createElement("p");
      partial.textContent = `${failedBlocks.length} 个段落未生成有效候选，原文和其他段落的候选已保留。`;
      panel.append(partial, button("重试未完成段落", () => prepareTailoredResume(workspaceId, analysis.analysis_id, {
        regenerate: true, blockIds: failedBlocks, expectedDraftRevision: diagnostics.draft_revision,
      })));
    }

    const requirementById = new Map((analysis.requirements || []).map(item => [item.requirement_id, item]));
    const cards = [];
    const values = suggestions || [];
    if (!values.length || diagnostics.empty_scope_result) {
      const empty = document.createElement("div"); empty.className = "workbench-empty";
      const messages = {
        no_change: ["本次无需修改", "候选与原文一致。可查看完整原文，无需保存重复版本。"],
        insufficient_evidence: ["可用证据不足", "请返回分析检查未匹配要求，补充真实项目或职责资料后重新分析。"],
        generation_failed: ["模型返回格式或生成过程失败", "本次未取得有效候选。可以重新生成；这不表示你的经历不符合岗位。"],
        validation_failed: ["候选未通过证据校验", "请根据以下原因核对引用或补充事实资料，再重新生成。"],
      };
      const [emptyHeading, emptyDescription] = messages[diagnostics.outcome] || ["本次没有可用候选", "暂未获得明确的生成诊断，可查看原文或重新生成。"];
      const emptyTitle = document.createElement("strong"); emptyTitle.textContent = emptyHeading;
      const emptyCopy = document.createElement("p"); emptyCopy.textContent = emptyDescription;
      const reasons = document.createElement("ul"); reasons.className = "tailored-diagnostics";
      for (const reason of diagnostics.rejected_reasons || []) {
        const item = document.createElement("li"); item.textContent = TAILORING_REJECTION_LABELS[reason] || `安全校验未通过：${reason}`; reasons.append(item);
      }
      for (const attempt of diagnostics.diagnostics || []) {
        const item = document.createElement("li"); item.textContent = `第 ${attempt.attempt} 轮：${attempt.candidate_count} 条候选，${attempt.accepted_count} 条通过`; reasons.append(item);
      }
      const retry = button("重新生成 AI 建议", () => prepareTailoredResume(workspaceId, analysis.analysis_id, { regenerate: true, blockIds: diagnostics.empty_scope_result ? diagnostics.block_ids || [] : [] }), "primary-action");
      empty.append(emptyTitle, emptyCopy, reasons, retry);
      panel.append(empty);
    }
    for (const suggestion of values) {
      const card = document.createElement("article"); card.className = "tailored-suggestion-card";
      const pending = suggestion.status === "pending";
      const cardHeader = document.createElement("header");
      const selector = document.createElement("label"); selector.className = "tailored-selector";
      const checkbox = document.createElement("input"); checkbox.type = "checkbox"; checkbox.checked = pending; checkbox.disabled = !pending;
      const reason = document.createElement("strong"); reason.textContent = suggestion.reason;
      selector.append(checkbox, reason);
      const state = document.createElement("span"); state.className = `tailored-state tailored-state-${suggestion.status}`; state.textContent = suggestion.status === "accepted" ? "已采纳" : suggestion.status === "rejected" ? "已拒绝" : suggestion.status === "invalidated" ? "已失效" : "待确认";
      cardHeader.append(selector, state);

      const trace = document.createElement("div"); trace.className = "tailored-trace";
      const requirementSection = document.createElement("section");
      const requirementTitle = document.createElement("h3"); requirementTitle.textContent = "关联岗位要求"; requirementSection.append(requirementTitle);
      for (const requirementId of suggestion.requirement_ids || []) {
        const requirement = document.createElement("p"); requirement.textContent = requirementById.get(requirementId)?.original_text || requirementId; requirementSection.append(requirement);
      }
      const evidenceSection = document.createElement("section"); evidenceSection.className = "tailored-evidence";
      const evidenceTitle = document.createElement("h3"); evidenceTitle.textContent = "证据摘录"; evidenceSection.append(evidenceTitle);
      for (const evidence of suggestion.resume_evidence || []) {
        const quote = document.createElement("blockquote"); quote.textContent = evidence.quote || "证据已验证"; evidenceSection.append(quote);
      }
      trace.append(requirementSection, evidenceSection);

      const before = document.createElement("details"); before.className = "tailored-original";
      const beforeTitle = document.createElement("summary"); beforeTitle.textContent = "查看原文";
      const beforeText = document.createElement("pre"); beforeText.textContent = suggestion.original_text; before.append(beforeTitle, beforeText);
      const editorLabel = document.createElement("label"); editorLabel.className = "tailored-editor-label"; editorLabel.textContent = "建议改写（可编辑）";
      const editor = document.createElement("textarea"); editor.value = suggestion.proposed_text; editor.rows = Math.max(3, String(suggestion.proposed_text).split("\n").length + 1); editor.disabled = !pending; editor.setAttribute("aria-label", "AI 定制简历建议文本"); editorLabel.append(editor);
      const risk = document.createElement("p"); risk.className = "tailored-risk"; risk.textContent = `风险提示：${suggestion.risk || "采纳前请确认措辞仍符合原始职责边界。"}`;
      const status = document.createElement("div"); status.className = "operation-status"; status.setAttribute("aria-live", "polite");
      const reject = button("拒绝", async () => {
        if (!ownsPanel()) return;
        reject.disabled = true;
        try {
          await request(`/v1/workbench/suggestions/${encodeURIComponent(suggestion.suggestion_id)}/decisions`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ decision: "reject" }) });
          if (!ownsPanel()) return;
          suggestion.status = "rejected";
          regenerateSection.disabled = true;
          checkbox.checked = false; checkbox.disabled = true; editor.disabled = true; state.textContent = "已拒绝"; status.textContent = "建议已拒绝；Draft 和正式版本均未改变。";
        } catch (error) { status.textContent = `拒绝失败：${error.message}`; reject.disabled = false; }
      });
      reject.disabled = !pending;
      const regenerateSection = button("重新生成此段", async () => {
        if (!ownsPanel() || regenerateSection.disabled || suggestion.status !== "pending") return;
        regenerateSection.disabled = true;
        try {
          await prepareTailoredResume(workspaceId, analysis.analysis_id, {regenerate: true, blockIds: [suggestion.block_id], expectedDraftRevision: suggestion.target_draft_revision});
        } catch (error) { if (ownsPanel()) { status.textContent = `此段生成失败：${error.message}`; regenerateSection.disabled = false; } }
      });
      regenerateSection.disabled = !pending || !suggestion.block_id;
      const controls = document.createElement("div"); controls.className = "draft-actions"; controls.append(reject, regenerateSection);
      card.append(cardHeader, trace, before, editorLabel, risk, controls, status); panel.append(card);
      cards.push({ suggestion, card, checkbox, editor, state, status, reject, regenerateSection });
    }

    const batchStatus = document.createElement("div"); batchStatus.className = "operation-status"; batchStatus.setAttribute("aria-live", "polite");
    const acceptSelected = button("批量接受到 Draft", async () => {
      if (!ownsPanel()) return;
      const selectedCards = cards.filter(item => item.checkbox.checked && item.suggestion.status === "pending");
      if (!selectedCards.length) { batchStatus.textContent = "请至少选择一条待确认建议。"; return; }
      acceptSelected.disabled = true; batchStatus.textContent = "正在一次性写入 Draft…";
      try {
        const acceptIds = selectedCards.map(item => item.suggestion.suggestion_id);
        const edited = Object.fromEntries(selectedCards.map(item => [item.suggestion.suggestion_id, item.editor.value]));
        await request("/v1/workbench/suggestions/batch-decisions", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ workspace_id: workspaceId, accept_ids: acceptIds, reject_ids: [], edited_text_by_id: edited }),
        });
        if (!ownsPanel()) return;
        for (const item of selectedCards) {
          item.suggestion.status = "accepted";
          item.checkbox.disabled = true; item.editor.disabled = true; item.reject.disabled = true; item.state.textContent = "已采纳";
          item.status.textContent = "已应用到 Draft。";
        }
        batchStatus.textContent = `已批量应用 ${selectedCards.length} 条建议到 Draft；正式版本未改变。`;
        // A batch advances the Draft revision; remaining suggestions must be refreshed before reuse.
        for (const item of cards) { item.checkbox.disabled = true; item.editor.disabled = true; item.reject.disabled = true; item.regenerateSection.disabled = true; }
      } catch (error) { batchStatus.textContent = `批量采纳失败：${error.message}`; acceptSelected.disabled = false; }
    }, "primary-action");
    acceptSelected.disabled = !cards.some(item => item.suggestion.status === "pending");
    const batchBar = document.createElement("div"); batchBar.className = "tailored-batch-bar";
    const preview = button("查看完整 Draft / 保存版本", async () => {
      preview.disabled = true;
      try {
        await renderTailoredPreview({ request, apiBase, container: tailoringMain, workspaceId, analysis,
          suggestions: cards.filter(item => item.checkbox.checked).map(item => ({...item.suggestion, proposed_text: item.editor.value})),
          draftId: diagnostics.draft_id || values[0]?.target_draft_id, isCurrent: () => ownerGuard() && lifecycleGuard === ownerGuard,
          onBack: () => prepareTailoredResume(workspaceId, analysis.analysis_id) });
      } catch (error) { batchStatus.textContent = `预览加载失败：${error.message}`; }
      finally { preview.disabled = false; }
    });
    preview.disabled = !(diagnostics.draft_id || values[0]?.target_draft_id);
    batchBar.append(button("返回分析", () => renderAnalysis(workspaceId, analysis)), batchStatus, acceptSelected, preview);
    panel.append(batchBar); tailoringMain.replaceChildren(panel);
  }

  async function renderMain(home, workspaceId, options = {}) {
    const localGuard = typeof options.isCurrent === "function" ? options.isCurrent : () => true;
    const isCurrent = () => localGuard() && isLifecycleCurrent();
    if (!isCurrent()) return false;
    if (!(home.stats?.resume_count) || !(home.stats?.job_count)) {
      elements.main.className = "workbench-match-placeholder";
      elements.main.textContent = "当前求职目标还不能进行岗位匹配。";
      return true;
    }
    const preserveContent = options.preserveContent === true;
    if (!preserveContent) {
      elements.main.className = "workbench-match-restoring";
      elements.main.textContent = "正在恢复最近一次岗位匹配…";
    }
    try {
      const page = await request(`/v1/workbench/match-analyses?workspace_id=${encodeURIComponent(workspaceId)}&limit=50`);
      if (!isCurrent()) return false;
      const latest = selectRestorableAnalysis((page.items || []).filter(item => item.status !== "stale"));
      if (latest) {
        return await renderAnalysis(workspaceId, latest, { isCurrent });
      }
      elements.main.className = "workbench-match-placeholder";
      elements.main.textContent = "尚无匹配结果。选择右侧岗位，或点击上方“开始匹配分析”。";
      return true;
    } catch (error) {
      if (!isCurrent()) return false;
      if (preserveContent) elements.status.textContent = `岗位匹配刷新失败：${error.message}`;
      else {
        elements.main.className = "workbench-match-placeholder";
        elements.main.textContent = `最近一次匹配结果恢复失败：${error.message}`;
      }
      return false;
    }
  }

  return Object.freeze({ renderJobList, renderMain, renderJobForm, renderMatchChooser, renderAnalysis, prepareTailoredResume, setLifecycleGuard });
}
