import { updateWorkbenchContext } from "../workbench-context.js";

const selected = { resumeVersionId: "", jobSnapshotId: "" };

const TAILORING_REJECTION_LABELS = Object.freeze({
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
  return analysis?.rule_version !== "match-rule.v2";
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

export function createJobMatching({ request, elements, reloadHome, activatePanel = () => {} }) {
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
    const research = button("自动岗位调研", () => renderResearchForm(workspaceId));
    research.classList.add("workbench-job-research");
    research.disabled = home.features?.delegated_research !== true;
    if (research.disabled) research.title = home.features?.unavailable_reasons?.delegated_research || "Release Gate 未通过";
    elements.jobs.append(research);
    if (research.disabled) {
      const reason = document.createElement("small"); reason.className = "release-gate-closed"; reason.textContent = `自动调研关闭：${research.title}。手工 JD 与单 URL 仍可使用。`; elements.jobs.append(reason);
    }
  }

  function renderResearchForm(workspaceId) {
    activatePanel();
    const panel = document.createElement("section"); panel.className = "research-panel";
    const title = document.createElement("h2"); title.textContent = "自动岗位调研";
    const query = document.createElement("textarea"); query.rows = 5; query.placeholder = "例如：上海 Python 后端，偏 AI 平台"; query.setAttribute("aria-label", "岗位调研条件");
    const status = document.createElement("div"); status.className = "operation-status";
    const start = button("创建调研任务", async () => {
      if (query.value.trim().length < 3) { status.textContent = "请填写明确的岗位方向。"; return; }
      start.disabled = true; status.textContent = "正在检查 Release Gate 并创建 Parent Run…";
      try {
        const run = await request("/v1/workbench/research-runs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ workspace_id: workspaceId, query: query.value.trim(), target_valid_jobs: 3, max_pages: 3 }) });
        status.textContent = `调研已创建：${run.parent_run_id}。结果只进入候选栏。`;
        pollResearchCandidates(workspaceId, run.parent_run_id, panel, 0);
      } catch (error) { status.textContent = `调研未启动：${error.message}`; start.disabled = false; }
    }, "primary-action");
    panel.append(title, query, start, status); elements.main.replaceChildren(panel);
  }

  async function pollResearchCandidates(workspaceId, parentRunId, panel, attempt) {
    try {
      const page = await request(`/v1/workbench/research-runs/${encodeURIComponent(parentRunId)}/candidates`);
      let list = panel.querySelector(".research-candidates");
      if (!list) { list = document.createElement("div"); list.className = "research-candidates"; panel.append(list); }
      list.replaceChildren();
      for (const candidate of page.items || []) {
        const card = document.createElement("article"); card.className = "research-candidate";
        const title = document.createElement("strong"); title.textContent = `${candidate.company} · ${candidate.title}`;
        const meta = document.createElement("p"); meta.textContent = `${candidate.location || "地点未知"} · ${candidate.evidence_level}`;
        const source = document.createElement("a"); source.href = candidate.final_url; source.target = "_blank"; source.rel = "noopener noreferrer"; source.textContent = "查看来源";
        const jd = document.createElement("details"); const jdTitle = document.createElement("summary"); jdTitle.textContent = "查看 JD"; const jdText = document.createElement("pre"); jdText.textContent = [...candidate.responsibilities, ...candidate.requirements].map(item => `- ${item}`).join("\n"); jd.append(jdTitle, jdText);
        const retain = button("评估并留存", async () => {
          const operationId = token("op_research_job"); retain.disabled = true;
          try {
            await request(`/v1/workbench/research-runs/${encodeURIComponent(parentRunId)}/retain`, { method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": operationId }, body: JSON.stringify({ workspace_id: workspaceId, candidate_id: candidate.candidate_id, operation_id: operationId, idempotency_key: operationId }) });
            meta.textContent = "已留存为 Job/JobSnapshot；候选本身未被当作投递记录。"; await reloadHome();
          } catch (error) { meta.textContent = `留存失败：${error.message}`; retain.disabled = false; }
        }, "primary-action");
        retain.disabled = candidate.evidence_level !== "complete";
        if (retain.disabled) retain.title = "证据不完整，不能留存";
        card.append(title, meta, source, jd, retain); list.append(card);
      }
      if ((page.items || []).length || attempt >= 30) return;
    } catch (error) {
      if (attempt >= 30) { const failed = document.createElement("p"); failed.textContent = `候选加载失败：${error.message}`; panel.append(failed); return; }
    }
    window.setTimeout(() => pollResearchCandidates(workspaceId, parentRunId, panel, attempt + 1), Math.min(10000, 1000 + attempt * 500));
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
    panel.append(heading, helper, role, employer, place, sections, sourceDetails, back, save, status);
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

  function renderJobForm(workspaceId) {
    const overlay = document.createElement("div"); overlay.className = "workbench-modal-overlay";
    const closeDialog = () => overlay.remove();
    const panel = document.createElement("section"); panel.className = "job-input-panel";
    panel.setAttribute("role", "dialog"); panel.setAttribute("aria-modal", "true"); panel.setAttribute("aria-label", "导入职位描述");
    const title = document.createElement("h2"); title.textContent = "评估岗位来源";
    const close = button("关闭", closeDialog, "secondary-action");
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
    panel.append(title, close, kind, role, company, location, content, file, authorized, submit, status);
    overlay.append(panel); overlay.addEventListener("click", event => { if (event.target === overlay) closeDialog(); }); document.body.append(overlay);
  }

  async function openJob(workspaceId, jobId) {
    activatePanel();
    elements.main.textContent = "正在加载岗位快照…";
    try {
      const [job, snapshots] = await Promise.all([request(`/v1/workbench/jobs/${encodeURIComponent(jobId)}`), request(`/v1/workbench/jobs/${encodeURIComponent(jobId)}/snapshots`)]);
      const snapshot = (snapshots.items || []).at(-1);
      if (!snapshot) throw new Error("岗位没有可用快照");
      selected.jobSnapshotId = snapshot.snapshot_id;
      updateWorkbenchContext({ workspace_id: workspaceId, job_snapshot_id: snapshot.snapshot_id });
      const source = await request(`/v1/workbench/job-snapshots/${encodeURIComponent(snapshot.snapshot_id)}/content?workspace_id=${encodeURIComponent(workspaceId)}`);
      const panel = document.createElement("section"); panel.className = "job-preview-panel";
      const title = document.createElement("h2"); title.textContent = `${job.company} · ${job.title}`;
      const meta = document.createElement("p"); meta.textContent = `快照 ${snapshot.snapshot_id} · ${snapshot.source_status}${snapshot.verified ? " · 已验证来源" : " · 手工来源"}`;
      const pre = document.createElement("pre"); pre.textContent = source.markdown;
      panel.append(title, meta, pre, button("使用此快照进行匹配", () => renderMatchChooser(workspaceId), "primary-action")); elements.main.replaceChildren(panel);
    } catch (error) { elements.main.textContent = `岗位加载失败：${error.message}`; }
  }

  async function renderMatchChooser(workspaceId) {
    activatePanel();
    elements.main.className = "";
    elements.main.textContent = "正在加载可评估对象…";
    try {
      const [home, jobs] = await Promise.all([request(`/v1/workbench/workspaces/${encodeURIComponent(workspaceId)}/home`), request(`/v1/workbench/jobs?workspace_id=${encodeURIComponent(workspaceId)}`)]);
      const panel = document.createElement("section"); panel.className = "match-chooser";
      const title = document.createElement("h2"); title.textContent = "匹配评估";
      const resume = document.createElement("select"); resume.setAttribute("aria-label", "已确认简历版本");
      for (const item of (home.recent_versions || []).filter(value => value.status === "confirmed")) { const option = document.createElement("option"); option.value = item.version_id; option.textContent = item.label; resume.append(option); }
      const snapshot = document.createElement("select"); snapshot.setAttribute("aria-label", "岗位快照");
      for (const job of jobs.items || []) {
        const page = await request(`/v1/workbench/jobs/${encodeURIComponent(job.job_id)}/snapshots`);
        for (const item of page.items || []) { const option = document.createElement("option"); option.value = item.snapshot_id; option.textContent = `${job.company} · ${job.title} · ${new Date(item.captured_at).toLocaleDateString()}`; snapshot.append(option); }
      }
      if (selected.jobSnapshotId) snapshot.value = selected.jobSnapshotId;
      const status = document.createElement("div"); status.className = "operation-status";
      const evaluate = button("开始证据匹配", async () => {
        if (!resume.value || !snapshot.value) { status.textContent = "需要已确认简历版本和岗位快照。"; return; }
        evaluate.disabled = true; status.textContent = "正在提取要求并验证简历证据…";
        try {
          const analysis = await evaluateMatch(workspaceId, resume.value, snapshot.value);
          selected.resumeVersionId = resume.value; selected.jobSnapshotId = snapshot.value;
          updateWorkbenchContext({ workspace_id: workspaceId, resume_version_id: resume.value, job_snapshot_id: snapshot.value, match_analysis_id: analysis.analysis_id });
          renderAnalysis(workspaceId, analysis);
        } catch (error) { status.textContent = `评估失败：${error.message}`; evaluate.disabled = false; }
      }, "primary-action");
      panel.append(title, label("简历版本", resume), label("岗位快照", snapshot), evaluate, status); elements.main.replaceChildren(panel);
    } catch (error) { elements.main.textContent = `评估对象加载失败：${error.message}`; }
  }

  function label(text, control) { const value = document.createElement("label"); value.append(text, control); return value; }

  async function renderAnalysis(workspaceId, analysis) {
    activatePanel();
    elements.main.className = "";
    updateWorkbenchContext({
      workspace_id: workspaceId,
      resume_version_id: analysis.resume_version_id,
      job_snapshot_id: analysis.job_snapshot_id,
      match_analysis_id: analysis.analysis_id,
    });
    let job = null;
    try { job = await request(`/v1/workbench/job-snapshots/${encodeURIComponent(analysis.job_snapshot_id)}`); } catch { /* Score data remains useful without snapshot metadata. */ }
    const panel = document.createElement("section"); panel.className = "match-analysis-panel";
    const total = Number(analysis.total_score || 0);
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
    const description = document.createElement("p"); description.textContent = recommendationCopy; jobBlock.append(jobTitle, description);
    const gradeBadge = document.createElement("span"); gradeBadge.className = `match-grade match-grade-${grade.toLowerCase()}`; gradeBadge.textContent = `${grade} · ${recommendation}`;
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
    const gapTitle = document.createElement("h3"); gapTitle.textContent = "短板"; gapsSection.append(gapTitle);
    const gapList = document.createElement("div"); gapList.className = "match-insight-list match-insight-gap";
    if (!gaps.length) { const empty = document.createElement("p"); empty.textContent = "当前分析未发现明确短板。"; gapList.append(empty); }
    for (const item of gaps) gapList.append(renderInsightCard(item, "gap"));
    gapsSection.append(gapList);
    const strategy = document.createElement("aside"); strategy.className = "match-strategy";
    const strategyTitle = document.createElement("strong"); strategyTitle.textContent = "下一步建议：";
    strategy.append(strategyTitle, ` ${gaps.length ? "优先围绕短板生成基于证据的修改建议；系统不会自动补写不存在的经历。" : "可生成基于当前已验证证据的定制建议。"}`);
    const requirements = document.createElement("details"); requirements.className = "match-requirement-details";
    const requirementSummary = document.createElement("summary"); requirementSummary.textContent = `查看全部 ${analysis.requirements?.length || 0} 条匹配依据`;
    const requirementList = document.createElement("div"); requirementList.className = "requirement-list";
    for (const item of analysis.requirements || []) {
      const detail = document.createElement("details"); detail.className = `requirement requirement-${item.verdict}`;
      const summary = document.createElement("summary"); summary.textContent = `${item.verdict} · ${item.original_text}`;
      const explanation = document.createElement("p"); explanation.textContent = item.explanation;
      detail.append(summary, explanation);
      for (const ref of item.evidence || []) { const quote = document.createElement("blockquote"); quote.textContent = ref.quote || "证据已验证；正文按需显示。"; detail.append(quote); }
      if (item.verdict === "missing" || item.verdict === "conflict") { const warning = document.createElement("p"); warning.className = "gap-warning"; warning.textContent = "能力缺口：不会自动写入简历。"; detail.append(warning); }
      requirementList.append(detail);
    }
    requirements.append(requirementSummary, requirementList);
    const actions = document.createElement("div"); actions.className = "draft-actions";
    actions.append(button("重新选择", () => renderMatchChooser(workspaceId)), button("生成证据建议", () => prepareSuggestions(workspaceId, analysis), "primary-action"));
    panel.append(heading, analysisMeta, dimensions, highlights, gapsSection, strategy, requirements, actions); elements.main.replaceChildren(panel);
  }

  function renderInsightCard(item, tone) {
    const card = document.createElement("article"); card.className = `match-insight-card match-insight-${tone}`;
    const title = document.createElement("p"); title.className = "match-insight-title"; title.textContent = item.original_text;
    const body = document.createElement("p"); body.className = "match-insight-body";
    const quote = item.evidence?.[0]?.quote;
    body.textContent = quote || item.explanation;
    card.append(title, body); return card;
  }

  async function prepareSuggestions(workspaceId, analysis) {
    activatePanel({ resetScroll: true });
    elements.main.className = "";
    elements.main.textContent = "正在创建可恢复 Draft 并生成候选…";
    try {
      const version = await request(`/v1/workbench/resume-versions/${encodeURIComponent(analysis.resume_version_id)}`);
      const draftId = token("rd_match");
      const draft = await request(`/v1/workbench/resume-versions/${encodeURIComponent(version.version_id)}/drafts`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ draft_id: draftId, workspace_id: workspaceId, branch_id: version.branch_id }),
      });
      await request(`/v1/workbench/match-analyses/${encodeURIComponent(analysis.analysis_id)}/suggestion-candidates`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ workspace_id: workspaceId, draft_id: draft.draft_id }),
      });
      return await renderSuggestions(workspaceId, analysis);
    } catch (error) {
      elements.main.textContent = `建议准备失败：${error.message}`;
      return false;
    }
  }

  async function prepareTailoredResume(workspaceId, analysisId) {
    activatePanel({ resetScroll: true });
    elements.main.className = "";
    elements.main.textContent = "正在读取匹配分析并准备 AI 定制简历…";
    try {
      const analysis = await request(`/v1/workbench/match-analyses/${encodeURIComponent(analysisId)}`);
      if (requiresEvidenceUpgrade(analysis)) {
        renderEvidenceUpgrade(workspaceId, analysis);
        return;
      }
      const page = await request(`/v1/workbench/match-analyses/${encodeURIComponent(analysisId)}/suggestions`);
      const existing = (page.items || []).filter(suggestion => suggestion.change_type === "ai_tailor_v1");
      if (existing.length) {
        const latest = [...existing].sort((left, right) => String(right.created_at).localeCompare(String(left.created_at)))[0];
        const reusable = existing.filter(suggestion =>
          suggestion.target_draft_id === latest.target_draft_id
          && suggestion.target_draft_revision === latest.target_draft_revision
        );
        renderTailoredSuggestions(workspaceId, analysis, reusable, { reused: true, attempts: 1, rejected_reasons: [] });
        return;
      }

      const version = await request(`/v1/workbench/resume-versions/${encodeURIComponent(analysis.resume_version_id)}`);
      const draft = await request(`/v1/workbench/resume-versions/${encodeURIComponent(version.version_id)}/drafts`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ draft_id: token("rd_tailored"), workspace_id: workspaceId, branch_id: version.branch_id }),
      });
      const generated = await request(`/v1/workbench/match-analyses/${encodeURIComponent(analysisId)}/tailored-resume-candidates`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ workspace_id: workspaceId, draft_id: draft.draft_id }),
      });
      renderTailoredSuggestions(workspaceId, analysis, generated.items || [], {
        reused: generated.reused === true,
        attempts: generated.attempts || 1,
        rejected_reasons: generated.rejected_reasons || [],
        reflection: generated.reflection,
      });
    } catch (error) {
      elements.main.textContent = `AI 定制简历准备失败：${error.message}`;
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
    panel.append(title, copy, actions, status); elements.main.replaceChildren(panel);
  }

  function renderTailoredSuggestions(workspaceId, analysis, suggestions, diagnostics = {}) {
    activatePanel({ resetScroll: true });
    elements.main.className = "";
    const panel = document.createElement("section"); panel.className = "suggestion-panel tailored-suggestion-panel";
    const header = document.createElement("header"); header.className = "tailored-suggestion-header";
    const heading = document.createElement("div");
    const title = document.createElement("h2"); title.textContent = "AI 定制简历建议";
    const helper = document.createElement("p"); helper.textContent = diagnostics.reused
      ? "已复用这次匹配已有的定制结果；模型没有重复运行。"
      : diagnostics.attempts === 2
        ? "已自动修复生成 2 次；所有保留改写均通过证据校验。"
        : "所有改写均绑定已验证证据。可编辑并选择后，一次写入 Draft。";
    heading.append(title, helper);
    const safety = document.createElement("span"); safety.className = "tailored-safety-badge"; safety.textContent = "正式版本未改变";
    header.append(heading, safety); panel.append(header);

    const requirementById = new Map((analysis.requirements || []).map(item => [item.requirement_id, item]));
    const cards = [];
    const values = suggestions || [];
    if (!values.length) {
      const empty = document.createElement("div"); empty.className = "workbench-empty";
      const emptyTitle = document.createElement("strong"); emptyTitle.textContent = "没有生成可安全使用的 AI 建议";
      const emptyCopy = document.createElement("p"); emptyCopy.textContent = "系统已保留正式版本，并拦截不符合证据约束的候选。";
      const reasons = document.createElement("ul"); reasons.className = "tailored-diagnostics";
      for (const reason of diagnostics.rejected_reasons || []) {
        const item = document.createElement("li"); item.textContent = TAILORING_REJECTION_LABELS[reason] || `安全校验未通过：${reason}`; reasons.append(item);
      }
      if (!reasons.children.length && diagnostics.reflection?.notes) {
        const item = document.createElement("li"); item.textContent = diagnostics.reflection.notes; reasons.append(item);
      }
      const retry = button("重新生成 AI 建议", () => prepareTailoredResume(workspaceId, analysis.analysis_id), "primary-action");
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
        reject.disabled = true;
        try {
          await request(`/v1/workbench/suggestions/${encodeURIComponent(suggestion.suggestion_id)}/decisions`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ decision: "reject" }) });
          checkbox.checked = false; checkbox.disabled = true; editor.disabled = true; state.textContent = "已拒绝"; status.textContent = "建议已拒绝；Draft 和正式版本均未改变。";
        } catch (error) { status.textContent = `拒绝失败：${error.message}`; reject.disabled = false; }
      });
      reject.disabled = !pending;
      const controls = document.createElement("div"); controls.className = "draft-actions"; controls.append(reject);
      card.append(cardHeader, trace, before, editorLabel, risk, controls, status); panel.append(card);
      cards.push({ suggestion, card, checkbox, editor, state, status, reject });
    }

    const batchStatus = document.createElement("div"); batchStatus.className = "operation-status"; batchStatus.setAttribute("aria-live", "polite");
    const acceptSelected = button("批量接受到 Draft", async () => {
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
        for (const item of selectedCards) {
          item.checkbox.disabled = true; item.editor.disabled = true; item.reject.disabled = true; item.state.textContent = "已采纳";
          item.status.textContent = "已应用到 Draft。";
        }
        batchStatus.textContent = `已批量应用 ${selectedCards.length} 条建议到 Draft；正式版本未改变。`;
      } catch (error) { batchStatus.textContent = `批量采纳失败：${error.message}`; acceptSelected.disabled = false; }
    }, "primary-action");
    acceptSelected.disabled = !cards.some(item => item.suggestion.status === "pending");
    const batchBar = document.createElement("div"); batchBar.className = "tailored-batch-bar";
    batchBar.append(button("返回分析", () => renderAnalysis(workspaceId, analysis)), batchStatus, acceptSelected);
    panel.append(batchBar); elements.main.replaceChildren(panel);
  }

  async function renderSuggestions(workspaceId, analysis) {
    activatePanel({ resetScroll: true });
    elements.main.className = "";
    elements.main.textContent = "正在加载建议…";
    try {
      const page = await request(`/v1/workbench/match-analyses/${encodeURIComponent(analysis.analysis_id)}/suggestions`);
      const panel = document.createElement("section"); panel.className = "suggestion-panel";
      const title = document.createElement("h2"); title.textContent = "建议审批"; panel.append(title);
      if (!(page.items || []).length) { const empty = document.createElement("div"); empty.className = "workbench-empty"; empty.textContent = "尚无经证据验证的修改建议。缺口不会被自动改写为经历。"; panel.append(empty); }
      for (const suggestion of page.items || []) {
        const row = document.createElement("article"); row.className = "suggestion-row";
        const reason = document.createElement("strong"); reason.textContent = suggestion.reason;
        const before = document.createElement("pre"); before.textContent = `原文\n${suggestion.original_text}`;
        const after = document.createElement("textarea"); after.value = suggestion.proposed_text; after.setAttribute("aria-label", "可编辑建议文本");
        const status = document.createElement("div"); status.className = "operation-status";
        const decide = async decision => { try { await request(`/v1/workbench/suggestions/${encodeURIComponent(suggestion.suggestion_id)}/decisions`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ decision, workspace_id: decision === "accept" ? workspaceId : null, edited_text: decision === "accept" ? after.value : null }) }); status.textContent = decision === "accept" ? "建议已应用到 Draft；正式版本未改变。" : "建议已拒绝。"; } catch (error) { status.textContent = `审批失败：${error.message}`; } };
        const controls = document.createElement("div"); controls.className = "draft-actions"; controls.append(button("拒绝", () => decide("reject")), button("接受到 Draft", () => decide("accept"), "primary-action"));
        row.append(reason, before, after, controls, status); panel.append(row);
      }
      panel.append(button("返回分析", () => renderAnalysis(workspaceId, analysis))); elements.main.replaceChildren(panel);
      return true;
    } catch (error) {
      elements.main.textContent = `建议加载失败：${error.message}`;
      return false;
    }
  }

  async function renderMain(home, workspaceId) {
    if (!(home.stats?.resume_count)) return;
    if (!(home.stats?.job_count)) return;
    elements.main.className = "workbench-match-restoring";
    elements.main.textContent = "正在恢复最近一次岗位匹配…";
    try {
      const page = await request(`/v1/workbench/match-analyses?workspace_id=${encodeURIComponent(workspaceId)}&limit=50`);
      const latest = selectRestorableAnalysis(page.items || []);
      if (latest) {
        await renderAnalysis(workspaceId, latest);
        return;
      }
      elements.main.className = "workbench-match-placeholder";
      elements.main.textContent = "尚无匹配结果。选择右侧岗位，或点击上方“开始匹配分析”。";
    } catch (error) {
      elements.main.className = "workbench-match-placeholder";
      elements.main.textContent = `最近一次匹配结果恢复失败：${error.message}`;
    }
  }

  return Object.freeze({ renderJobList, renderMain, renderJobForm, renderMatchChooser, renderAnalysis, prepareTailoredResume });
}
