const WORKSPACE_KEY = "resume-agent.current-workspace";
import { createResumeWorkspace } from "./resume-workspace.js?v=compact-ui";
import { createJobMatching } from "./job-matching.js?v=compact-ui";
import { updateWorkbenchContext } from "../workbench-context.js";
import { createOperationMonitor } from "./operation-monitor.js";
import { createApplicationsBoard } from "./applications-board.js";

export function createRouteActivationCoordinator({ activate, onStart, onActivated = () => {}, onCurrent, schedule = callback => requestAnimationFrame(callback) }) {
  let epoch = 0;
  return Object.freeze({
    async apply(route) {
      const token = ++epoch;
      onStart(route);
      await activate(route);
      if (token !== epoch) return false;
      onActivated(route);
      schedule(() => {
        if (token === epoch) onCurrent(route);
      });
      return true;
    },
  });
}

export function contentPanelPresentation(panel, hasResume = false) {
  const archive = panel !== "match";
  return Object.freeze({
    archive,
    headingHidden: false,
    mode: archive ? "档案" : "匹配分数",
    title: archive
      ? (hasResume ? "当前简历档案" : "建立你的第一份简历档案")
      : "准备匹配评估",
  });
}

export function createContentPanelCoordinator({ scrollRegion, render }) {
  let activePanel = "archive";
  let attached = true;
  const scrollPositions = new Map();
  return Object.freeze({
    show(panel, { resetScroll = false } = {}) {
      if (attached) {
        scrollPositions.set(activePanel, Math.max(0, Number(scrollRegion.scrollTop) || 0));
      }
      activePanel = panel;
      render(panel);
      scrollRegion.scrollTop = resetScroll ? 0 : (scrollPositions.get(panel) || 0);
      attached = true;
    },
    suspend() {
      if (!attached) return;
      scrollPositions.set(activePanel, Math.max(0, Number(scrollRegion.scrollTop) || 0));
      attached = false;
    },
  });
}

export function createWorkbenchShell({ getApiBase, elements }) {
  let workspaces = [];
  let activeWorkspaceId = localStorage.getItem(WORKSPACE_KEY) || "";
  let loading = false;
  let activeRoute = "workbench";
  let contextWorkspaceId = "";
  let currentHome = null;
  let loadEpoch = 0;
  let activationEpoch = 0;

  function isCurrentActivation(route, token) {
    return activeRoute === route && activationEpoch === token;
  }

  function renderContext({ title, meta = "", description = "", content }) {
    elements.contextTitle.textContent = title;
    elements.contextMeta.textContent = meta;
    elements.contextDescription.textContent = description;
    elements.contextContent.replaceChildren();
    if (content) elements.contextContent.append(content);
  }

  function renderWorkbenchContext() {
    renderContext({
      title: "岗位候选",
      meta: elements.jobCount.textContent,
      description: "确认后的 JD 可在这里快速查看，并进入完整匹配分析。",
      content: elements.jobList,
    });
  }

  function renderVersionContext(node, { inspectorMount = elements.jobList } = {}) {
    if (!node) {
      const empty = document.createElement("section");
      empty.className = "context-detail-card";
      empty.textContent = "选择版本节点后显示版本详情与可用操作。";
      renderContext({ title: "版本详情", description: "当前版本地图尚未选择节点", content: empty });
      return;
    }
    renderContext({ title: "版本详情", meta: `r${node.revision}`, description: "当前选中的简历版本", content: inspectorMount });
  }

  function renderApplicationContext(application) {
    if (!application) {
      const empty = document.createElement("section");
      empty.className = "context-detail-card";
      empty.textContent = "选择投递卡片后显示状态、下一步与相关操作。";
      renderContext({ title: "投递详情", description: "当前投递看板尚未选择记录", content: empty });
      return;
    }
    const panel = document.createElement("section");
    panel.className = "context-detail-card";
    const heading = document.createElement("strong");
    heading.textContent = application.title || application.application_id;
    const detail = document.createElement("p");
    detail.textContent = `状态：${application.status || application.current_status} · 下一步：${application.next_action || "未设置"}`;
    panel.append(heading, detail);
    renderContext({ title: "投递详情", description: "当前选中的投递记录", content: panel });
  }

  const contentScrollRegion = elements.scrollRegion || { scrollTop: 0, dataset: {} };

  function applyContentPanelPresentation(panel) {
    const hasResume = Boolean(currentHome?.recent_versions?.[0]);
    const presentation = contentPanelPresentation(panel, hasResume);
    elements.main.hidden = !presentation.archive;
    elements.match.hidden = presentation.archive;
    elements.archiveTab.setAttribute("aria-current", presentation.archive ? "page" : "false");
    elements.matchTab.setAttribute("aria-current", presentation.archive ? "false" : "page");
    elements.mode.textContent = presentation.mode;
    elements.title.closest(".workbench-section-heading").hidden = presentation.headingHidden;
    elements.title.textContent = presentation.title;
    contentScrollRegion.dataset.contentPanel = panel;
    contentScrollRegion.dataset.hasResume = String(hasResume);
  }

  const contentPanelCoordinator = createContentPanelCoordinator({
    scrollRegion: contentScrollRegion,
    render: applyContentPanelPresentation,
  });

  function showContentPanel(panel, options = {}) {
    contentPanelCoordinator.show(panel, options);
  }

  function setStatus(message, error = false) {
    elements.status.textContent = message;
    elements.status.style.color = error ? "var(--wb-danger)" : "var(--wb-muted)";
  }

  async function request(path, options = {}) {
    const response = await fetch(`${getApiBase()}${path}`, options);
    let payload = null;
    try { payload = await response.json(); } catch (_error) { /* empty body */ }
    if (!response.ok) {
      const validationDetail = Array.isArray(payload?.detail)
        ? payload.detail.map(item => `${(item.loc || []).slice(1).join(".") || "请求"}：${item.msg || "无效"}`).join("；")
        : null;
      const error = new Error(payload?.error?.message || payload?.detail?.message || validationDetail || `HTTP ${response.status}`);
      error.status = response.status;
      error.code = payload?.error?.code || payload?.detail?.code || (validationDetail ? "request_validation_failed" : null);
      error.retryable = Boolean(payload?.error?.retryable);
      throw error;
    }
    return payload;
  }

  const resumeWorkspace = createResumeWorkspace({
    request,
    apiBase: getApiBase,
    elements: {
      main: elements.main,
      resumes: elements.resumeList,
      jobs: elements.jobList,
      status: elements.status,
    },
    reloadHome: () => load(true),
    onVersionSelect: renderVersionContext,
  });
  const jobMatching = createJobMatching({
    request,
    elements: { main: elements.match, jobs: elements.jobList, status: elements.status },
    activatePanel: options => showContentPanel("match", options),
    reloadHome: () => load(true),
  });
  const operationMonitor = createOperationMonitor({ request, apiBase: getApiBase, container: elements.operationCards });
  const applicationsBoard = createApplicationsBoard({
    request,
    elements: { main: elements.main },
    onApplicationSelect: renderApplicationContext,
  });

  const STAGES = Object.freeze({
    A: {
      key: "profile",
      eyebrow: "当前阶段 · 未建档",
      title: "先建立可信简历档案",
      description: "上传 DOCX 或 PDF，系统只会使用解析并确认后的内容。",
      primary: "上传简历",
    },
    B: {
      key: "job",
      eyebrow: "当前阶段 · 待投递",
      title: "添加一个目标岗位",
      description: "简历已经就绪。粘贴 JD、上传文件或使用稳定链接，确认后再开始匹配。",
      primary: "导入岗位 JD",
      secondary: "查看当前档案",
    },
    C: {
      key: "analysis",
      eyebrow: "当前阶段 · 分析中",
      title: "验证岗位要求与简历证据",
      description: "选择已确认的简历版本和岗位快照，查看匹配依据、短板和下一步建议。",
      primary: "开始匹配分析",
      secondary: "查看当前档案",
    },
  });

  function replaceAgentActions(stage) {
    const actions = {
      A: [
        ["prepare_resume", "如何准备简历"],
      ],
      B: [
        ["ai_edit_resume", "AI 修改简历"],
        ["rewrite_section", "哪块最应该改"],
        ["compare_versions", "比较简历版本"],
      ],
      C: [
        ["ai_edit_resume", "AI 修改简历"],
        ["tailor_resume", "AI 定制简历"],
        ["explain_score", "解释匹配分数"],
        ["rewrite_section", "短板怎么补"],
      ],
    }[stage];
    elements.agentActions.replaceChildren(...actions.map(([action, label]) => {
      const item = document.createElement("button");
      item.type = "button";
      item.dataset.agentAction = action;
      item.textContent = label;
      return item;
    }));
  }

  function renderStage(stage, stats) {
    const config = STAGES[stage];
    elements.view.dataset.stage = config.key;
    elements.stageResume.dataset.state = stage === "A" ? "current" : "done";
    elements.stageJob.dataset.state = stage === "A" ? "pending" : stage === "B" ? "current" : "done";
    elements.stageAnalysis.dataset.state = stage === "C" ? "current" : "pending";
    elements.stageEyebrow.textContent = config.eyebrow;
    elements.stageTitle.textContent = stats.active_operation_count > 0 && stage === "C"
      ? "分析任务正在执行"
      : config.title;
    elements.stageDescription.textContent = stats.active_operation_count > 0 && stage === "C"
      ? `当前有 ${stats.active_operation_count} 个任务在执行；可以在左侧查看实时进度。`
      : config.description;
    elements.stagePrimary.textContent = stats.active_operation_count > 0 && stage === "C"
      ? "分析任务进行中"
      : config.primary;
    elements.stagePrimary.disabled = stats.active_operation_count > 0 && stage === "C";
    elements.stageSecondary.hidden = !config.secondary;
    elements.stageSecondary.textContent = config.secondary || "";
    elements.candidateRail.hidden = false;
    elements.matchTab.disabled = stage === "A";
    elements.matchTab.title = stage === "A" ? "先上传简历建档" : "";
    replaceAgentActions(stage);
    elements.tailorResumeButton.hidden = stage !== "C";
    elements.tailorResumeButton.onclick = () => {
      elements.agentActions.querySelector('[data-agent-action="tailor_resume"]')?.click();
    };

    elements.stagePrimary.onclick = () => {
      if (stage === "A") return resumeWorkspace.renderImport(activeWorkspaceId || null);
      if (stage === "B") return jobMatching.renderJobForm(activeWorkspaceId);
      showContentPanel("match");
      jobMatching.renderMatchChooser(activeWorkspaceId);
    };
    elements.stageSecondary.onclick = () => {
      showContentPanel("archive");
      const version = currentHome?.recent_versions?.[0];
      if (version) void resumeWorkspace.renderResumePreview(activeWorkspaceId, version.version_id, version.label);
    };
  }

  function setStepStates(stats, mode) {
    const states = {
      resume: stats.resume_count > 0 ? "done" : "current",
      job: stats.resume_count > 0 && stats.job_count > 0 ? "done" : stats.resume_count > 0 ? "current" : "pending",
      analysis: mode === "C" || mode === "D" ? "done" : stats.job_count > 0 ? "current" : "pending",
      revision: mode === "D" ? "current" : "pending",
      export: "pending",
    };
    // 流程条已移除；保留状态计算，供后续状态提示或埋点复用。
    return states;
  }

  async function renderHome(home, route, isCurrent) {
    if (!isCurrent()) return;
    currentHome = home;
    const stats = home.stats || {};
    // 阶段只由后端 home 统计推导，不会显示虚假统计或模拟成功状态。
    const mode = !stats.resume_count ? "A" : !stats.job_count ? "B" : "C";
    const copy = {
      A: ["建立你的第一份简历档案", "导入 DOCX 或 PDF；首次导入会自动创建本地求职档案。"],
      B: ["选择目标岗位", "简历已准备好。粘贴 JD 或输入稳定链接开始匹配。"],
      C: ["准备匹配评估", "选择已确认的简历版本和岗位快照；分析结果将展示要求项与证据。"],
      D: ["确认本次修改", "逐条审批建议后，再保存并确认新版本。"],
    }[mode];
    applyContentPanelPresentation(elements.match.hidden ? "archive" : "match");
    elements.main.textContent = copy[1];
    elements.jobCount.textContent = `${stats.job_count || 0} 个`;
    elements.jobList.textContent = stats.job_count
      ? `已确认岗位 ${stats.job_count} 个；候选来源需逐项确认。`
      : "暂无已确认岗位。";
    if (route === "workbench") renderWorkbenchContext();
    else if (route === "version-map") renderVersionContext(null);
    else if (route === "applications") renderApplicationContext(null);
    elements.agentContext.textContent = stats.resume_count
      ? `当前上下文：${home.workspace?.name || "求职目标"}；Agent 不会自动提交修改。`
      : "建立档案后，Agent 才会获得显式 ResumeVersion 上下文。";
    renderStage(mode, stats);
    setStepStates(stats, mode);
    setStatus(`已加载 ${home.workspace?.name || "工作台"} · revision ${home.workspace?.revision || 0}`);
    if (home.workspace?.workspace_id) {
      const changed = contextWorkspaceId && contextWorkspaceId !== home.workspace.workspace_id;
      contextWorkspaceId = home.workspace.workspace_id;
      updateWorkbenchContext(changed
        ? { workspace_id: contextWorkspaceId, resume_version_id: null, job_snapshot_id: null, match_analysis_id: null, resume_branch_id: null, lineage_focus_version_id: null, merge_proposal_id: null }
        : { workspace_id: contextWorkspaceId });
    }
    const resumeRender = resumeWorkspace.renderResumeList(
      home,
      route,
      home.workspace?.workspace_id || activeWorkspaceId,
      { isCurrent },
    );
    if (!isCurrent()) return;
    if (route === "version-map") {
      await resumeRender;
      return;
    }
    if (route === "workbench") {
      jobMatching.renderJobList(home, activeWorkspaceId);
      if (mode === "C") {
        showContentPanel("match");
        void jobMatching.renderMain(home, activeWorkspaceId);
      } else if (home.recent_versions?.[0]) {
        showContentPanel("archive");
        void resumeWorkspace.renderResumePreview(
          activeWorkspaceId,
          home.recent_versions[0].version_id,
          home.recent_versions[0].label,
        );
      } else {
        showContentPanel("archive");
        elements.main.className = "workbench-empty workbench-stage-empty";
        elements.main.textContent = "上传现有简历后，这里会展示结构化档案预览。";
      }
      operationMonitor.load(activeWorkspaceId);
    } else if (route === "applications") {
      elements.jobList.textContent = "投递记录只绑定已确认的岗位快照与简历版本。";
      await applicationsBoard.render(activeWorkspaceId, "", "", { isCurrent });
    }
  }

  function renderWorkspaceOptions() {
    elements.workspace.replaceChildren();
    if (!workspaces.length) {
      const option = document.createElement("option");
      option.value = "";
      option.textContent = "尚未创建求职目标";
      elements.workspace.append(option);
      elements.workspace.disabled = true;
      return;
    }
    elements.workspace.disabled = false;
    for (const workspace of workspaces) {
      const option = document.createElement("option");
      option.value = workspace.workspace_id;
      option.textContent = workspace.name;
      option.selected = workspace.workspace_id === activeWorkspaceId;
      elements.workspace.append(option);
    }
  }

  async function load(force = false, route = activeRoute, activationToken = activationEpoch) {
    const token = ++loadEpoch;
    const isCurrent = () => token === loadEpoch && isCurrentActivation(route, activationToken);
    if (!isCurrent()) return;
    loading = true;
    setStatus("正在加载权威工作台状态…");
    try {
      const page = await request("/v1/workbench/workspaces?limit=50");
      if (!isCurrent()) return;
      workspaces = page.items || [];
      if (!workspaces.some(item => item.workspace_id === activeWorkspaceId)) {
        activeWorkspaceId = workspaces[0]?.workspace_id || "";
      }
      renderWorkspaceOptions();
      if (!activeWorkspaceId) {
        await renderHome({ stats: {}, recent_versions: [], workspace: null }, route, isCurrent);
        if (!isCurrent()) return;
        setStatus("尚未创建求职目标；导入首份简历时会自动创建。", false);
        return;
      }
      localStorage.setItem(WORKSPACE_KEY, activeWorkspaceId);
      const home = await request(`/v1/workbench/workspaces/${encodeURIComponent(activeWorkspaceId)}/home`);
      if (!isCurrent()) return;
      await renderHome(home, route, isCurrent);
    } catch (error) {
      if (isCurrent()) {
        setStatus(`工作台加载失败：${error.message}`, true);
        elements.main.textContent = "数据未加载成功。已保留当前页面，可稍后重试。";
      }
    } finally {
      if (isCurrent()) loading = false;
    }
  }

  elements.workspace.addEventListener("change", () => {
    activeWorkspaceId = elements.workspace.value;
    localStorage.setItem(WORKSPACE_KEY, activeWorkspaceId);
    load();
  });
  elements.archiveTab.addEventListener("click", () => {
    showContentPanel("archive");
    const version = currentHome?.recent_versions?.[0];
    if (version) void resumeWorkspace.renderResumePreview(activeWorkspaceId, version.version_id, version.label);
  });
  elements.matchTab.addEventListener("click", () => {
    showContentPanel("match");
    if (currentHome) jobMatching.renderMain(currentHome, activeWorkspaceId);
  });
  async function activate(route) {
    const token = ++activationEpoch;
    activeRoute = route;
    elements.title.textContent = route === "version-map" ? "版本地图" : route === "applications" ? "投递看板" : elements.title.textContent;
    elements.actionStatus.textContent = route === "version-map"
      ? "选择版本后显示可用操作"
      : route === "applications"
        ? "选择投递记录后显示可用操作"
        : "选择当前任务后显示可用操作";
    await load(true, route, token);
  }
  async function tailorResume(context) {
    const workspaceId = context?.workspace_id || activeWorkspaceId;
    const analysisId = context?.match_analysis_id;
    if (!workspaceId || !analysisId) throw new Error("需要当前求职目标和匹配分析");
    showContentPanel("match");
    await jobMatching.prepareTailoredResume(workspaceId, analysisId);
  }
  return Object.freeze({
    activate,
    load,
    tailorResume,
    suspendContentScroll: () => contentPanelCoordinator.suspend(),
  });
}
