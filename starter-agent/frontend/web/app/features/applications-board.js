import { getWorkbenchContext } from "../workbench-context.js";

const LABELS = Object.freeze({
  to_decide: "待决定", to_apply: "待投递", applied: "已投递", assessment: "笔试",
  interview: "面试", offer: "Offer", rejected: "拒绝", withdrawn: "撤回", archived: "归档",
});
const NEXT = Object.freeze({
  to_decide: ["to_apply", "archived"], to_apply: ["applied", "withdrawn", "archived"],
  applied: ["assessment", "interview", "rejected", "withdrawn", "archived"],
  assessment: ["interview", "rejected", "withdrawn"], interview: ["offer", "rejected", "withdrawn"],
  offer: ["withdrawn", "archived"], rejected: ["archived"], withdrawn: ["archived"], archived: [],
});

function token(prefix) { return `${prefix}_${crypto.randomUUID().replaceAll("-", "")}`; }

export function createApplicationsBoard({ request, elements, onApplicationSelect = () => {} }) {
  let workspaceId = "";
  let activeCard = null;

  async function render(nextWorkspaceId, query = "", status = "", { isCurrent = () => true } = {}) {
    workspaceId = nextWorkspaceId;
    if (!isCurrent()) return;
    elements.main.textContent = "正在加载投递看板…";
    try {
      const params = new URLSearchParams({ workspace_id: workspaceId });
      if (query.trim()) params.set("query", query.trim());
      if (status) params.set("status", status);
      const [page, funnel, reminders] = await Promise.all([
        request(`/v1/workbench/applications?${params}`),
        request(`/v1/workbench/analytics/funnel?workspace_id=${encodeURIComponent(workspaceId)}`),
        request(`/v1/workbench/reminders?workspace_id=${encodeURIComponent(workspaceId)}`),
      ]);
      if (!isCurrent()) return;
      renderBoard(page.items || [], query, status, funnel, reminders);
    } catch (error) { if (isCurrent()) elements.main.textContent = `投递看板加载失败：${error.message}`; }
  }

  function renderBoard(items, query, status, funnel, reminders) {
    const shell = document.createElement("section"); shell.className = "applications-board";
    const toolbar = document.createElement("div"); toolbar.className = "applications-toolbar";
    const search = document.createElement("input"); search.type = "search"; search.placeholder = "搜索公司、岗位或下一步"; search.value = query; search.setAttribute("aria-label", "搜索投递记录");
    const filter = document.createElement("select"); filter.setAttribute("aria-label", "按投递状态筛选");
    filter.append(new Option("全部状态", ""), ...Object.entries(LABELS).map(([value, label]) => new Option(label, value, false, value === status)));
    const apply = document.createElement("button"); apply.type = "button"; apply.textContent = "筛选"; apply.addEventListener("click", () => render(workspaceId, search.value, filter.value));
    const create = document.createElement("button"); create.type = "button"; create.textContent = "从当前简历与岗位建立记录"; create.addEventListener("click", () => previewCreate(shell));
    toolbar.append(search, filter, apply, create); shell.append(toolbar);
    const insight = document.createElement("aside"); insight.className = "application-insights";
    const stages = (funnel.stages || []).map(item => `${LABELS[item.status] || item.status} ${item.reached}`).join(" · ");
    insight.textContent = `漏斗 ${funnel.definition_version}：${stages || "暂无事件"}；提醒 ${(reminders.items || []).filter(item => item.status === "due").length} 条到期。统计只来自投递事件，提醒不会发送外部消息。`;
    shell.append(insight);
    const columns = document.createElement("div"); columns.className = "application-columns";
    for (const [state, label] of Object.entries(LABELS)) {
      if (status && state !== status) continue;
      const column = document.createElement("section"); column.className = "application-column"; column.dataset.status = state;
      const heading = document.createElement("h2"); const values = items.filter(item => item.application.current_status === state);
      heading.textContent = `${label} · ${values.length}`; column.append(heading);
      for (const value of values) column.append(renderCard(value));
      if (!values.length) { const empty = document.createElement("p"); empty.className = "workbench-empty"; empty.textContent = "暂无记录"; column.append(empty); }
      columns.append(column);
    }
    shell.append(columns); elements.main.replaceChildren(shell);
  }

  function renderCard(value) {
    const application = value.application; const job = value.job_snapshot;
    const card = document.createElement("article"); card.className = "application-card";
    card.tabIndex = 0;
    const selectApplication = () => {
      activeCard?.classList.remove("is-active");
      activeCard = card;
      card.classList.add("is-active");
      onApplicationSelect(application);
    };
    card.addEventListener("click", selectApplication);
    card.addEventListener("keydown", event => {
      if (event.target === card && (event.key === "Enter" || event.key === " ")) {
        event.preventDefault();
        selectApplication();
      }
    });
    const title = document.createElement("h3"); title.textContent = `${job.company} · ${job.title}`;
    const meta = document.createElement("p"); meta.textContent = `优先级 ${application.priority} · 简历 ${application.resume_version_id}`;
    const next = document.createElement("p"); next.textContent = `下一步：${application.next_action || "未设置"}`;
    const remind = document.createElement("p"); remind.textContent = application.remind_at ? `提醒：${new Date(application.remind_at).toLocaleString()}` : "提醒：未设置";
    const timeline = document.createElement("details"); const summary = document.createElement("summary"); summary.textContent = `时间线 ${application.events.length} 条`; timeline.append(summary);
    const list = document.createElement("ol");
    for (const event of application.events) { const item = document.createElement("li"); item.textContent = `${LABELS[event.to_status]} · ${new Date(event.occurred_at).toLocaleString()}${event.note ? ` · ${event.note}` : ""}`; list.append(item); }
    timeline.append(list); card.append(title, meta, next, remind, timeline);
    const choices = NEXT[application.current_status] || [];
    if (choices.length) {
      const select = document.createElement("select"); select.setAttribute("aria-label", `${job.company} 下一状态`);
      for (const state of choices) select.add(new Option(LABELS[state], state));
      const preview = document.createElement("button"); preview.type = "button"; preview.textContent = "预览状态变更";
      preview.addEventListener("click", () => previewEvent(card, application, select.value)); card.append(select, preview);
    }
    const schedule = document.createElement("button"); schedule.type = "button"; schedule.textContent = "设置本地提醒";
    schedule.addEventListener("click", () => editReminder(card, application)); card.append(schedule);
    const review = document.createElement("button"); review.type = "button"; review.textContent = "面试复盘";
    review.addEventListener("click", () => openInterviewReview(card, application)); card.append(review);
    return card;
  }

  function editReminder(card, application) {
    card.querySelector(".reminder-editor")?.remove();
    const panel = document.createElement("div"); panel.className = "reminder-editor application-confirmation";
    const when = document.createElement("input"); when.type = "datetime-local"; when.setAttribute("aria-label", "提醒时间");
    if (application.remind_at) when.value = new Date(application.remind_at).toISOString().slice(0, 16);
    const action = document.createElement("input"); action.placeholder = "到时要做什么"; action.value = application.next_action || ""; action.setAttribute("aria-label", "提醒事项");
    const save = document.createElement("button"); save.type = "button"; save.textContent = "保存可见待办";
    const status = document.createElement("p"); status.textContent = "只保存本地可见待办，不发送邮件或消息。";
    save.addEventListener("click", async () => {
      save.disabled = true;
      try {
        await request(`/v1/workbench/applications/${encodeURIComponent(application.application_id)}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ expected_revision: application.revision, priority: application.priority, next_action: action.value.trim() || null, remind_at: when.value ? new Date(when.value).toISOString() : null }) });
        await render(workspaceId);
      } catch (error) { status.textContent = `提醒保存失败：${error.message}；投递状态未改变。`; save.disabled = false; }
    });
    panel.append(status, when, action, save); card.append(panel);
  }

  async function openInterviewReview(card, application) {
    card.querySelector(".interview-review")?.remove();
    const panel = document.createElement("section"); panel.className = "interview-review application-confirmation";
    panel.textContent = "正在加载复盘…"; card.append(panel);
    let review;
    try {
      review = await request(`/v1/workbench/applications/${encodeURIComponent(application.application_id)}/interview-review`);
    } catch (error) {
      if (error.status !== 404) { panel.textContent = `复盘加载失败：${error.message}`; return; }
      review = await request("/v1/workbench/interview-reviews", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ review_id: token("ir"), application_id: application.application_id }) });
    }
    renderInterviewPanel(panel, review, application);
  }

  function renderInterviewPanel(panel, review, application) {
    panel.replaceChildren();
    const heading = document.createElement("strong"); heading.textContent = `面试复盘 · ${review.rounds.length} 轮`;
    const privacy = document.createElement("p"); privacy.textContent = "只记录你主动输入的事实；不读取音频、日历或邮件，也不会修改简历。";
    const rounds = document.createElement("ol");
    for (const round of review.rounds) {
      const item = document.createElement("li"); item.textContent = `${round.round_type} · ${new Date(round.occurred_at).toLocaleString()} · ${round.result || "结果未填写"}`; rounds.append(item);
    }
    const type = document.createElement("input"); type.placeholder = "轮次类型，如：技术一面"; type.setAttribute("aria-label", "面试轮次类型");
    const questions = document.createElement("textarea"); questions.placeholder = "问题，每行一条"; questions.setAttribute("aria-label", "面试问题");
    const feedback = document.createElement("textarea"); feedback.placeholder = "反馈，每行一条"; feedback.setAttribute("aria-label", "面试反馈");
    const result = document.createElement("input"); result.placeholder = "结果（可选）"; result.setAttribute("aria-label", "面试结果");
    const improvements = document.createElement("textarea"); improvements.placeholder = "改进项，每行一条"; improvements.setAttribute("aria-label", "改进项");
    const save = document.createElement("button"); save.type = "button"; save.textContent = "确认保存本轮事实";
    save.addEventListener("click", async () => {
      if (!type.value.trim()) return;
      save.disabled = true;
      try {
        const updated = await request(`/v1/workbench/interview-reviews/${encodeURIComponent(review.review_id)}/rounds`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ expected_revision: review.revision, round_id: token("round"), round_type: type.value.trim(), occurred_at: new Date().toISOString(), questions: lines(questions.value), answers: [], feedback: lines(feedback.value), result: result.value.trim() || null, improvement_items: lines(improvements.value), user_confirmed: true }) });
        renderInterviewPanel(panel, updated, application);
      } catch (error) { privacy.textContent = `保存失败：${error.message}`; save.disabled = false; }
    });
    panel.append(heading, privacy, rounds, type, questions, feedback, result, improvements, save);
    if (review.rounds.length) {
      const propose = document.createElement("button"); propose.type = "button"; propose.textContent = "生成事实摘要候选";
      propose.addEventListener("click", async () => {
        propose.disabled = true;
        try { renderInterviewPanel(panel, await request(`/v1/workbench/interview-reviews/${encodeURIComponent(review.review_id)}/summary-candidates`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ expected_revision: review.revision, summary_id: token("is") }) }), application); }
        catch (error) { privacy.textContent = `摘要失败：${error.message}`; propose.disabled = false; }
      }); panel.append(propose);
    }
    for (const summary of review.summary_candidates) {
      const candidate = document.createElement("article"); candidate.className = "interview-summary-candidate";
      const text = document.createElement("pre"); text.textContent = summary.text;
      const source = document.createElement("small"); source.textContent = `候选 · 引用 ${summary.cited_round_ids.join("、")} · ${summary.status}`;
      candidate.append(text, source);
      if (summary.status === "pending") for (const decision of ["accepted", "rejected"]) {
        const button = document.createElement("button"); button.type = "button"; button.textContent = decision === "accepted" ? "确认采用" : "拒绝";
        button.addEventListener("click", async () => { button.disabled = true; try { renderInterviewPanel(panel, await request(`/v1/workbench/interview-reviews/${encodeURIComponent(review.review_id)}/summary-candidates/${encodeURIComponent(summary.summary_id)}/decision`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ expected_revision: review.revision, decision }) }), application); } catch (error) { privacy.textContent = `决策失败：${error.message}`; button.disabled = false; } });
        candidate.append(button);
      }
      panel.append(candidate);
    }
  }

  function lines(value) { return value.split(/\r?\n/).map(item => item.trim()).filter(Boolean); }

  function previewEvent(card, application, toStatus) {
    card.querySelector(".application-confirmation")?.remove();
    const panel = document.createElement("div"); panel.className = "application-confirmation";
    const copy = document.createElement("p"); copy.textContent = `将 ${LABELS[application.current_status]} 改为 ${LABELS[toStatus]}。只有点击确认才写入事件，不会访问招聘网站。`;
    const note = document.createElement("input"); note.placeholder = "备注（可选）"; note.setAttribute("aria-label", "状态变更备注");
    const next = document.createElement("input"); next.placeholder = "下一步（可选）"; next.setAttribute("aria-label", "下一步");
    const confirm = document.createElement("button"); confirm.type = "button"; confirm.textContent = "明确确认并记录";
    confirm.addEventListener("click", async () => {
      confirm.disabled = true; const id = token("app_event");
      try {
        await request(`/v1/workbench/applications/${encodeURIComponent(application.application_id)}/events`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ operation_id: token("op_application"), idempotency_key: id, event_id: token("ae"), workspace_id: workspaceId, expected_revision: application.revision, to_status: toStatus, note: note.value.trim() || null, next_action: next.value.trim() || null, remind_at: null, user_confirmed: true }) });
        await render(workspaceId);
      } catch (error) { copy.textContent = `记录失败：${error.message}`; confirm.disabled = false; }
    });
    panel.append(copy, note, next, confirm); card.append(panel);
  }

  function previewCreate(container) {
    container.querySelector(".application-create-confirmation")?.remove();
    const context = getWorkbenchContext(); const panel = document.createElement("div"); panel.className = "application-create-confirmation application-confirmation";
    if (!context?.resume_version_id || !context?.job_snapshot_id) { panel.textContent = "请先在工作台选择已确认简历版本和岗位快照。"; container.prepend(panel); return; }
    const copy = document.createElement("p"); copy.textContent = `候选动作：记录当前岗位已投递，绑定 ${context.resume_version_id} 与 ${context.job_snapshot_id}。尚未提交。`;
    const confirm = document.createElement("button"); confirm.type = "button"; confirm.textContent = "我确认已经投递";
    confirm.addEventListener("click", async () => {
      confirm.disabled = true; const key = token("application_create");
      try {
        await request("/v1/workbench/applications", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ operation_id: token("op_application"), idempotency_key: key, application_id: token("app"), event_id: token("ae"), workspace_id: workspaceId, job_snapshot_id: context.job_snapshot_id, resume_version_id: context.resume_version_id, initial_status: "applied", priority: 50, next_action: "等待后续通知", note: "用户明确确认已投递", user_confirmed: true }) });
        await render(workspaceId);
      } catch (error) { copy.textContent = `创建失败：${error.message}`; confirm.disabled = false; }
    });
    panel.append(copy, confirm); container.prepend(panel);
  }

  return Object.freeze({ render });
}
