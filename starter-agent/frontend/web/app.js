import { createApiClient } from "./app/api-client.js";
import { createHashRouter } from "./app/router.js";
import { createStore } from "./app/store.js";
import { createWorkbenchShell, createRouteActivationCoordinator } from "./app/features/workbench-shell.js?v=20260823-workbench-polish";
import { getWorkbenchContext } from "./app/workbench-context.js";
import { createShellState, resolveShellRoute } from "./app/shell-state.js";
import { createModalManager } from "./app/modal-manager.js";
import { createTrustReadOwnership } from "./app/trust-request-state.js";
window.StarterAgentModules = Object.freeze({ createApiClient, createHashRouter, createStore });

    /* capability-ui-logic:start */
    const CapabilityUiLogic = Object.freeze({
      confirmationTargetKey(confirmation) {
        const summary = confirmation?.arguments_summary || {};
        const operation = String(summary.operation || confirmation?.tool_name || "");
        const kind = operation.split(".", 1)[0];
        const target = String(
          summary.target || confirmation?.destination || ""
        );
        return kind && target ? `${kind}:${target}` : "";
      },

      isTargetLocked(targetKey, lockedTargets) {
        return Boolean(targetKey) && lockedTargets.includes(targetKey);
      },

      confirmationDetails(confirmation) {
        const summary = confirmation?.arguments_summary || {};
        const diff = Object.entries(summary.diff || {}).map(([field, change]) => ({
          field,
          before: Array.isArray(change) ? change[0] : null,
          after: Array.isArray(change) ? change[1] : change
        }));
        return {
          operation: summary.operation || confirmation?.tool_name || "",
          destination: summary.target || confirmation?.destination || "",
          risk: summary.risk || confirmation?.risk || "",
          impact: Array.isArray(summary.impact) ? [...summary.impact] : [],
          data: summary.payload && typeof summary.payload === "object"
            ? {...summary.payload}
            : {},
          diff
        };
      },

      canLoadRawDefinition(role, expanded) {
        return role === "admin" && expanded === true;
      },

      isManagementConfirmation(confirmation) {
        return confirmation?.server_id === "management"
          && confirmation?.session_id === "management";
      },

      reconcileManagementConfirmations(authoritative, local, inFlightIds) {
        const retained = new Map();
        for (const confirmation of authoritative || []) {
          if (
            confirmation?.id
            && this.isManagementConfirmation(confirmation)
          ) {
            retained.set(confirmation.id, confirmation);
          }
        }
        const inFlight = new Set(inFlightIds || []);
        for (const confirmation of local || []) {
          if (
            confirmation?.id
            && inFlight.has(confirmation.id)
            && this.isManagementConfirmation(confirmation)
            && !retained.has(confirmation.id)
          ) {
            retained.set(confirmation.id, confirmation);
          }
        }
        return [...retained.values()];
      },

      isAuthorityRequestCurrent(token, current) {
        return token.apiBase === current.apiBase
          && token.identityRevision === current.identityRevision;
      },

      isRequestCurrent(token, current) {
        return token.epoch === current.epoch
          && token.route === current.route
          && token.selection === current.selection
          && token.apiBase === current.apiBase;
      }
    });
    /* capability-ui-logic:end */

    const TOOL_GOVERNANCE_STORAGE_KEY = "starter-agent.tool-governance-enabled";
    const CHAT_SESSION_STORAGE_KEY = "starter-agent.current-chat-session";
    const state = {
      sessionId: sessionStorage.getItem(CHAT_SESSION_STORAGE_KEY),
      isSending: false,
      chatPending: false,
      sessions: [],
      sessionOffset: 0,
      sessionLimit: 30,
      sessionsHasMore: false,
      sessionsLoading: false,
      activeAssistantBubble: null,
      systemBubble: null,
      tools: [],
      visibleTools: [],
      activeToolIndex: 0,
      toolStatusRows: new Map(),
      sessionUsage: { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 },
      maxTotalTokens: 128000,
      toolGovernanceEnabled: localStorage.getItem(TOOL_GOVERNANCE_STORAGE_KEY) !== "false",
      memories: [],
      editingMemoryId: null,
      pendingEmailDrafts: [],
      chatConfirmations: new Map(),
      confirmationDecisionLocks: new Set(),
      confirmationIdempotencyKeys: new Map(),
      confirmationExpiryTimer: null,
      delegationRuns: new Map(),
      delegationEventSequences: new Map(),
      delegationStreams: new Map(),
      delegationReconnectTimers: new Map(),
      activeWorkbenchEpoch: null,
      workbenchContextChanged: false,
      skipKnowledgeForNextMessage: false
    };

    const capabilityState = {
      route: "mcp-servers",
      servers: [],
      serverDetails: new Map(),
      tools: [],
      toolDetails: new Map(),
      skills: [],
      skillDetails: new Map(),
      skillRegistryRevision: 0,
      skillsStale: false,
      skillsLastError: null,
      selectedServerId: null,
      selectedToolName: null,
      selectedSkillName: null,
      pendingOperations: new Set(),
      confirmations: new Map(),
      confirmationDecisionLocks: new Set(),
      confirmationIdempotencyKeys: new Map(),
      confirmationProposalIds: new Set(),
      confirmationExpiryTimer: null,
      requestEpoch: 0,
      requestController: new AbortController(),
      identityRevision: 0,
      rawState: {
        skillName: null,
        expanded: false,
        status: "idle",
        definition: null,
        lease: 0
      },
      lastRefreshAt: null
    };

    const trustState = {
      route: "evals",
      suites: [],
      cases: [],
      runs: [],
      selectedRunId: null,
      caseResults: [],
      metrics: [],
      failureClusters: [],
      traces: [],
      safety: null,
      loading: false,
      error: null,
      requestEpoch: 0,
      requestController: new AbortController()
    };
    const trustReadOwnership = createTrustReadOwnership();

    const apiBaseInput = document.querySelector("#apiBase");
    const providerSelect = document.querySelector("#providerSelect");
    const modelSelect = document.querySelector("#modelSelect");
    const chatKnowledgeMode = document.querySelector("#chatKnowledgeMode");
    const statusEl = document.querySelector("#status");
    const tokenBudgetEl = document.querySelector("#tokenBudget");
    const messagesEl = document.querySelector("#messages");
    const chatConfirmationCards = document.querySelector("#chatConfirmationCards");
    const delegationTaskCards = document.querySelector("#delegationTaskCards");
    const delegationRunDetail = document.querySelector("#delegationRunDetail");
    const messageInput = document.querySelector("#messageInput");
    const sendButton = document.querySelector("#sendButton");
    const clearButton = document.querySelector("#clearButton");
    const newSessionButton = document.querySelector("#newSessionButton");
    const sessionListEl = document.querySelector("#sessionList");
    const clearAllSessionsButton = document.querySelector("#clearAllSessionsButton");
    const composer = document.querySelector("#composer");
    const toolMenu = document.querySelector("#toolMenu");
    const settingsButton = document.querySelector("#settingsButton");
    const workbenchSettingsButton = document.querySelector("#workbenchSettingsButton");
    const settingsOverlay = document.querySelector("#settingsOverlay");
    const settingsCloseButton = document.querySelector("#settingsCloseButton");
    const toolGovernanceToggle = document.querySelector("#toolGovernanceToggle");
    const memoryForm = document.querySelector("#memoryForm");
    const memoryKey = document.querySelector("#memoryKey");
    const memoryValue = document.querySelector("#memoryValue");
    const memoryCategory = document.querySelector("#memoryCategory");
    const memoryExpiresAt = document.querySelector("#memoryExpiresAt");
    const memorySensitivity = document.querySelector("#memorySensitivity");
    const memorySaveButton = document.querySelector("#memorySaveButton");
    const memoryCancelButton = document.querySelector("#memoryCancelButton");
    const memoryList = document.querySelector("#memoryList");
    const knowledgeView = document.querySelector("#knowledgeView");
    const capabilitiesView = document.querySelector("#capabilitiesView");
    const trustView = document.querySelector("#trustView");
    const knowledgeNavButton = document.querySelector("#knowledgeNavButton");
    const capabilitiesNavButton = document.querySelector("#capabilitiesNavButton");
    const trustNavButton = document.querySelector("#trustNavButton");
    const workbenchView = document.querySelector("#workbenchView");
    const workbenchPageTab = document.querySelector("#workbenchPageTab");
    const versionMapPageTab = document.querySelector("#versionMapPageTab");
    const applicationsPageTab = document.querySelector("#applicationsPageTab");
    const capabilityServersTab = document.querySelector("#capabilityServersTab");
    const capabilitySkillsTab = document.querySelector("#capabilitySkillsTab");
    const capabilityRefreshButton = document.querySelector("#capabilityRefreshButton");
    const capabilityRefreshTime = document.querySelector("#capabilityRefreshTime");
    const capabilityGlobalError = document.querySelector("#capabilityGlobalError");
    const capabilityConfirmation = document.querySelector("#capabilityConfirmation");
    const capabilityList = document.querySelector("#capabilityList");
    const capabilityDetail = document.querySelector("#capabilityDetail");
    const trustStatus = document.querySelector("#trustStatus");
    const trustRefreshButton = document.querySelector("#trustRefreshButton");
    const trustError = document.querySelector("#trustError");
    const trustEvalsTab = document.querySelector("#trustEvalsTab");
    const trustTracesTab = document.querySelector("#trustTracesTab");
    const trustSafetyTab = document.querySelector("#trustSafetyTab");
    const trustEvalsPanel = document.querySelector("#trustEvalsPanel");
    const trustTracesPanel = document.querySelector("#trustTracesPanel");
    const trustSafetyPanel = document.querySelector("#trustSafetyPanel");
    const trustStartRunButton = document.querySelector("#trustStartRunButton");
    const trustSuiteSelect = document.querySelector("#trustSuiteSelect");
    const trustCompareBaseRun = document.querySelector("#trustCompareBaseRun");
    const trustCompareCandidateRun = document.querySelector("#trustCompareCandidateRun");
    const trustEvalRuns = document.querySelector("#trustEvalRuns");
    const trustEvalCases = document.querySelector("#trustEvalCases");
    const trustFailureClusters = document.querySelector("#trustFailureClusters");
    const trustTraceSearchButton = document.querySelector("#trustTraceSearchButton");
    const trustTraceRunFilter = document.querySelector("#trustTraceRunFilter");
    const trustTraceCaseFilter = document.querySelector("#trustTraceCaseFilter");
    const trustTraceSessionFilter = document.querySelector("#trustTraceSessionFilter");
    const trustTraceTurnFilter = document.querySelector("#trustTraceTurnFilter");
    const trustTraceToolFilter = document.querySelector("#trustTraceToolFilter");
    const trustTraceEvents = document.querySelector("#trustTraceEvents");
    const trustSafetyGate = document.querySelector("#trustSafetyGate");
    const trustSafetyPolicy = document.querySelector("#trustSafetyPolicy");
    const trustSafetyEvidence = document.querySelector("#trustSafetyEvidence");
    const knowledgeUploadForm = document.querySelector("#knowledgeUploadForm");
    const knowledgeFile = document.querySelector("#knowledgeFile");
    const knowledgeDocumentType = document.querySelector("#knowledgeDocumentType");
    const knowledgeAuthorized = document.querySelector("#knowledgeAuthorized");
    const knowledgeUploadButton = document.querySelector("#knowledgeUploadButton");
    const knowledgeStatus = document.querySelector("#knowledgeStatus");
    const knowledgeDocumentCount = document.querySelector("#knowledgeDocumentCount");
    const knowledgeSelectAllDocuments = document.querySelector("#knowledgeSelectAllDocuments");
    const knowledgeDeleteSelectedButton = document.querySelector("#knowledgeDeleteSelectedButton");
    const knowledgeDocumentList = document.querySelector("#knowledgeDocumentList");
    const knowledgeChunkPreview = document.querySelector("#knowledgeChunkPreview");
    const knowledgeChunkTitle = document.querySelector("#knowledgeChunkTitle");
    const advancedOverlay = document.querySelector("#advancedOverlay");
    const advancedDialog = document.querySelector("#advancedDialog");
    const advancedTitle = document.querySelector("#advancedTitle");
    const advancedCloseButton = document.querySelector("#advancedCloseButton");
    const requestedRoute = new URLSearchParams(window.location.search).get("route") || "";
    const shellState = createShellState(
      resolveShellRoute(window.location.hash, requestedRoute)
    );
    const advancedWindow = createModalManager({
      overlay: advancedOverlay,
      dialog: advancedDialog,
      title: advancedTitle,
      closeButton: advancedCloseButton,
      panels: { knowledge: knowledgeView, capabilities: capabilitiesView, trust: trustView },
      onBeforeClose(type) {
        shellState.closeOverlay();
        if (type === "capabilities") advanceCapabilityRequestEpoch();
        if (type === "trust") advanceTrustRequestEpoch();
      },
    });
    const workbenchShell = createWorkbenchShell({
      getApiBase: apiBase,
      elements: {
        status: document.querySelector("#workbenchStatus"),
        workspace: document.querySelector("#workbenchWorkspaceSelect"),
        jobCount: document.querySelector("#workbenchJobCount"),
        jobList: document.querySelector("#workbenchJobList"),
        agentContext: document.querySelector("#workbenchAgentContext"),
        operationCards: document.querySelector("#workbenchOperationCards"),
        mode: document.querySelector("#workbenchModeLabel"),
        title: document.querySelector("#workbenchTitle"),
        main: document.querySelector("#workbenchMainContent"),
        match: document.querySelector("#workbenchMatchContent"),
        contextTitle: document.querySelector("#workbenchContextTitle"),
        contextMeta: document.querySelector("#workbenchContextMeta"),
        contextDescription: document.querySelector("#workbenchContextDescription"),
        contextContent: document.querySelector("#workbenchContextContent"),
        actionBar: document.querySelector("#workbenchActionBar"),
        actionStatus: document.querySelector("#workbenchActionStatus"),
        archiveTab: document.querySelector("#workbenchArchiveTab"),
        matchTab: document.querySelector("#workbenchMatchTab"),
        view: document.querySelector("#workbenchView"),
        candidateRail: document.querySelector("#workbenchCandidateRail"),
        stageResume: document.querySelector("#workbenchStageResume"),
        stageJob: document.querySelector("#workbenchStageJob"),
        stageAnalysis: document.querySelector("#workbenchStageAnalysis"),
        stageEyebrow: document.querySelector("#workbenchStageEyebrow"),
        stageTitle: document.querySelector("#workbenchStageTitle"),
        stageDescription: document.querySelector("#workbenchStageDescription"),
        stagePrimary: document.querySelector("#workbenchStagePrimary"),
        stageSecondary: document.querySelector("#workbenchStageSecondary"),
        agentActions: document.querySelector("#workbenchAgentActions"),
        taskCenter: document.querySelector(".workbench-task-center"),
        tailorResumeButton: document.querySelector("#workbenchTailorResumeButton"),
      },
    });
    let activeKnowledgeBaseId = null;
    let knowledgeDocuments = [];
    let selectedKnowledgeDocumentId = null;
    let selectedKnowledgeDocumentIds = new Set();

    function apiBase() {
      return apiBaseInput.value.replace(/\/+$/, "");
    }

    function currentCapabilitySelection() {
      if (capabilityState.route === "skills") {
        return capabilityState.selectedSkillName || "";
      }
      return [
        capabilityState.selectedServerId || "",
        capabilityState.selectedToolName || ""
      ].join("|");
    }

    function currentCapabilityRequestState() {
      return {
        epoch: capabilityState.requestEpoch,
        route: capabilityState.route,
        selection: currentCapabilitySelection(),
        apiBase: apiBase()
      };
    }

    function captureCapabilityRequest() {
      return {
        ...currentCapabilityRequestState(),
        overlayToken: shellState.captureOverlayRequest()
      };
    }

    function isCapabilityRequestCurrent(token) {
      return shellState.isOverlayRequestCurrent(token.overlayToken)
        && CapabilityUiLogic.isRequestCurrent(
        token,
        currentCapabilityRequestState()
      );
    }

    function captureCapabilityAuthorityRequest() {
      return {
        apiBase: apiBase(),
        identityRevision: capabilityState.identityRevision
      };
    }

    function isCapabilityAuthorityRequestCurrent(token) {
      return CapabilityUiLogic.isAuthorityRequestCurrent(token, {
        apiBase: apiBase(),
        identityRevision: capabilityState.identityRevision
      });
    }

    function advanceCapabilityRequestEpoch() {
      capabilityState.requestEpoch += 1;
      capabilityState.requestController.abort();
      capabilityState.requestController = new AbortController();
      clearCapabilityRawState();
    }

    function isCapabilityAbort(error) {
      return error?.name === "AbortError";
    }

    function setStatus(text) {
      statusEl.textContent = text;
    }

    let settingsReturnFocus = settingsButton;

    async function openSettings(trigger = settingsButton) {
      settingsReturnFocus = trigger;
      toolGovernanceToggle.checked = state.toolGovernanceEnabled;
      settingsOverlay.hidden = false;
      settingsCloseButton.focus();
      await loadMemories();
    }

    function closeSettings({ restoreFocus = true } = {}) {
      settingsOverlay.hidden = true;
      if (restoreFocus) settingsReturnFocus?.focus();
    }

    function memoryCategoryLabel(category) {
      return ({
        profile: "个人资料",
        preference: "偏好",
        constraint: "约束",
        verified_skill: "已核验技能",
        application_state: "投递状态"
      })[category] || category;
    }

    function memorySourceLabel(sourceType) {
      return ({
        user_confirmed: "用户确认",
        local_file: "本地文件核验",
        conversation_inferred: "后台自动提取"
      })[sourceType] || sourceType;
    }

    function memoryExpiryValue(value) {
      return value ? String(value).slice(0, 10) : "";
    }

    function resetMemoryForm() {
      state.editingMemoryId = null;
      memoryForm.reset();
      memoryCategory.value = "preference";
      memorySensitivity.value = "personal";
      memorySaveButton.textContent = "新增记忆";
      memoryCancelButton.hidden = true;
    }

    function renderMemories() {
      memoryList.replaceChildren();
      if (!state.memories.length) {
        const empty = document.createElement("div");
        empty.className = "session-empty";
        empty.textContent = "暂无长期记忆";
        memoryList.appendChild(empty);
        return;
      }
      for (const memory of state.memories) {
        const item = document.createElement("article");
        item.className = `memory-item ${memory.status}`;
        const title = document.createElement("div");
        title.className = "memory-item-title";
        title.textContent = memory.key;
        const value = document.createElement("div");
        value.className = "memory-item-value";
        value.textContent = memory.value;
        const meta = document.createElement("div");
        meta.className = "memory-item-meta";
        meta.textContent = `${memoryCategoryLabel(memory.category)} · ${memory.status} · 来源=${memorySourceLabel(memory.source_type)} · 置信度=${memory.confidence} · 过期=${memoryExpiryValue(memory.expires_at) || "无"}`;
        const actions = document.createElement("div");
        actions.className = "memory-item-actions";
        const edit = document.createElement("button");
        edit.type = "button";
        edit.textContent = "修改";
        edit.addEventListener("click", () => editMemory(memory));
        const toggle = document.createElement("button");
        toggle.type = "button";
        toggle.textContent = memory.status === "active" ? "停用" : "启用";
        toggle.disabled = memory.status === "expired";
        toggle.addEventListener("click", () => toggleMemory(memory));
        const remove = document.createElement("button");
        remove.type = "button";
        remove.textContent = "删除";
        remove.addEventListener("click", () => deleteMemory(memory));
        actions.append(edit, toggle, remove);
        item.append(title, value, meta, actions);
        memoryList.appendChild(item);
      }
    }

    async function loadMemories() {
      memoryList.innerHTML = '<div class="session-empty">正在加载长期记忆...</div>';
      try {
        const response = await fetch(`${apiBase()}/v1/memories`);
        if (!response.ok) throw new Error(`${response.status} ${await response.text()}`);
        const data = await response.json();
        state.memories = data.memories || [];
        renderMemories();
      } catch (error) {
        memoryList.innerHTML = '<div class="session-empty">长期记忆加载失败</div>';
        setStatus(`长期记忆加载失败：${error.message}`);
      }
    }

    function editMemory(memory) {
      state.editingMemoryId = memory.id;
      memoryKey.value = memory.key;
      memoryValue.value = memory.value;
      memoryCategory.value = memory.category;
      memoryExpiresAt.value = memoryExpiryValue(memory.expires_at);
      memorySensitivity.value = memory.sensitivity;
      memorySaveButton.textContent = "保存修改";
      memoryCancelButton.hidden = false;
      memoryKey.focus();
    }

    function memoryPayload(status = "active") {
      const expiry = memoryExpiresAt.value
        ? new Date(`${memoryExpiresAt.value}T23:59:59`).toISOString()
        : null;
      return {
        key: memoryKey.value.trim(),
        value: memoryValue.value.trim(),
        category: memoryCategory.value,
        expires_at: expiry,
        sensitivity: memorySensitivity.value,
        status,
        source_type: "user_confirmed",
        confirmed: true
      };
    }

    async function saveMemory(event) {
      event.preventDefault();
      const current = state.memories.find(item => item.id === state.editingMemoryId);
      const payload = memoryPayload(current?.status === "disabled" ? "disabled" : "active");
      if (!payload.key || !payload.value) return;
      if (!window.confirm("确认将这条信息作为跨会话长期记忆保存吗？")) return;
      const editing = Boolean(state.editingMemoryId);
      const url = editing
        ? `${apiBase()}/v1/memories/${state.editingMemoryId}`
        : `${apiBase()}/v1/memories`;
      if (!editing) delete payload.status;
      try {
        const response = await fetch(url, {
          method: editing ? "PUT" : "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload)
        });
        if (!response.ok) throw new Error(`${response.status} ${await response.text()}`);
        resetMemoryForm();
        await loadMemories();
        setStatus(editing ? "长期记忆已修改" : "长期记忆已创建");
      } catch (error) {
        setStatus(`长期记忆保存失败：${error.message}`);
      }
    }

    async function toggleMemory(memory) {
      const payload = {
        key: memory.key,
        value: memory.value,
        category: memory.category,
        expires_at: memory.expires_at,
        sensitivity: memory.sensitivity,
        status: memory.status === "active" ? "disabled" : "active",
        confirmed: true
      };
      const response = await fetch(`${apiBase()}/v1/memories/${memory.id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      if (!response.ok) {
        setStatus(`更新记忆状态失败：${response.status}`);
        return;
      }
      await loadMemories();
    }

    async function deleteMemory(memory) {
      if (!window.confirm(`确认删除长期记忆“${memory.key}”吗？删除后不会继续注入 Context。`)) return;
      const response = await fetch(`${apiBase()}/v1/memories/${memory.id}`, { method: "DELETE" });
      if (!response.ok) {
        setStatus(`删除长期记忆失败：${response.status}`);
        return;
      }
      if (state.editingMemoryId === memory.id) resetMemoryForm();
      await loadMemories();
      setStatus("长期记忆已删除");
    }

    function formatTokenCount(value) {
      return new Intl.NumberFormat("zh-CN").format(Number(value) || 0);
    }

    function updateTokenBudget(usage = {}, maximum = 128000, budgetStatus = "normal") {
      state.sessionUsage = usage;
      state.maxTotalTokens = maximum || 128000;
      const total = Number(usage.total_tokens) || 0;
      const label = budgetStatus === "exceeded"
        ? " · 已超出预算"
        : budgetStatus === "warning" ? " · 接近预算" : "";
      tokenBudgetEl.textContent = `本会话 tokens: ${formatTokenCount(total)} / ${formatTokenCount(state.maxTotalTokens)}${label}`;
      tokenBudgetEl.className = `token-budget ${budgetStatus === "normal" ? "" : budgetStatus}`.trim();
    }

    function updateComposerAvailability() {
      const blocked = state.isSending || state.chatPending;
      messageInput.disabled = blocked;
      sendButton.disabled = blocked;
      clearButton.disabled = blocked;
      newSessionButton.disabled = blocked;
      clearAllSessionsButton.disabled = blocked;
      sendButton.textContent = state.chatPending
        ? "等待确认"
        : state.isSending ? "发送中" : "发送";
    }

    function setSending(value) {
      state.isSending = value;
      updateComposerAvailability();
    }

    function setChatPending(value) {
      state.chatPending = Boolean(value);
      updateComposerAvailability();
    }

    function appendToolConfirmationField(container, label, value) {
      const term = document.createElement("dt");
      term.textContent = label;
      const description = document.createElement("dd");
      description.textContent = value === null || value === undefined || value === ""
        ? "—"
        : String(value);
      container.append(term, description);
    }

    function toolConfirmationStatusText(confirmation) {
      const status = confirmation.status || "pending";
      const reason = confirmation.reason_code || confirmation.gate_reason_code || "";
      if (status === "pending") return "等待当前会话用户决定；Tool 尚未调用。";
      if (status === "approved") return "已批准，服务端正在重新校验 Gate。";
      if (status === "consumed") return "确认已消费，Tool 最多执行一次。";
      if (status === "cancelled") return "已取消；Tool 未调用。";
      if (status === "expired") return "确认已超时；Tool 未调用。";
      if (status === "invalidated") {
        return `确认已失效；Tool 未调用。${reason ? ` 原因：${reason}` : ""}`;
      }
      return `${status}${reason ? ` · ${reason}` : ""}`;
    }

    function isTerminalToolConfirmation(confirmation) {
      return ["consumed", "cancelled", "expired", "invalidated"].includes(
        confirmation?.status || ""
      );
    }

    function removeChatConfirmation(id) {
      if (!id) return;
      state.chatConfirmations.delete(id);
      state.confirmationDecisionLocks.delete(id);
      for (const key of [...state.confirmationIdempotencyKeys.keys()]) {
        if (String(key).startsWith(`${id}:`)) {
          state.confirmationIdempotencyKeys.delete(key);
        }
      }
    }

    function pruneTerminalChatConfirmations() {
      for (const [id, confirmation] of state.chatConfirmations.entries()) {
        if (isTerminalToolConfirmation(confirmation)) {
          removeChatConfirmation(id);
        }
      }
    }

    function renderToolConfirmation(confirmation) {
      const card = document.createElement("section");
      card.className = "chat-confirmation-card";
      card.dataset.confirmationId = confirmation.id;
      card.dataset.status = confirmation.status || "pending";

      const title = document.createElement("h3");
      title.textContent = "需要确认 Tool 调用";

      const summaryRow = document.createElement("div");
      summaryRow.className = "chat-confirmation-summary";
      const toolName = document.createElement("strong");
      toolName.textContent = confirmation.tool_name || "未知 Tool";
      const riskPill = document.createElement("span");
      riskPill.className = "chat-confirmation-pill";
      riskPill.textContent = confirmation.risk || "unknown";
      summaryRow.append(toolName, riskPill);

      const fields = document.createElement("dl");
      fields.className = "chat-confirmation-fields";
      const summary = confirmation.arguments_summary || {};
      appendToolConfirmationField(fields, "Server", confirmation.server_id);
      appendToolConfirmationField(fields, "Tool", confirmation.tool_name);
      appendToolConfirmationField(fields, "风险", confirmation.risk);
      appendToolConfirmationField(
        fields,
        "目标 / 数据去向",
        confirmation.destination
      );

      const details = document.createElement("details");
      details.className = "chat-confirmation-details";
      const detailsSummary = document.createElement("summary");
      detailsSummary.textContent = "查看技术详情";
      const detailFields = document.createElement("dl");
      detailFields.className = "chat-confirmation-fields";
      appendToolConfirmationField(
        detailFields,
        "参数安全摘要",
        JSON.stringify(summary, null, 2)
      );
      appendToolConfirmationField(
        detailFields,
        "数据类别",
        (confirmation.data_classes || []).join(", ")
      );
      appendToolConfirmationField(detailFields, "过期时间", confirmation.expires_at);
      appendToolConfirmationField(detailFields, "Audit", confirmation.audit_ref);
      appendToolConfirmationField(detailFields, "Trace", confirmation.trace_ref);
      details.append(detailsSummary, detailFields);
      card.append(title, summaryRow, fields, details);

      if ((confirmation.status || "pending") === "pending") {
        const actions = document.createElement("div");
        actions.className = "chat-confirmation-actions";
        const locked = state.confirmationDecisionLocks.has(confirmation.id);
        const once = document.createElement("button");
        once.type = "button";
        once.textContent = "仅本次允许";
        once.disabled = locked;
        once.addEventListener(
          "click",
          () => decideToolConfirmation(confirmation, "once")
        );
        const allowlist = document.createElement("button");
        allowlist.type = "button";
        allowlist.textContent = "加入 Allowlist";
        allowlist.disabled = locked || confirmation.allowlist_allowed === false;
        if (confirmation.allowlist_allowed === false) {
          allowlist.title = confirmation.allowlist_reason
            || "服务端 always_confirm 策略禁止加入 Allowlist";
        }
        allowlist.addEventListener(
          "click",
          () => decideToolConfirmation(confirmation, "allowlist")
        );
        const cancel = document.createElement("button");
        cancel.type = "button";
        cancel.textContent = "取消";
        cancel.disabled = locked;
        cancel.addEventListener(
          "click",
          () => decideToolConfirmation(confirmation, "cancel")
        );
        actions.append(once, allowlist, cancel);
        card.appendChild(actions);
        if (confirmation.allowlist_allowed === false) {
          const policyReason = document.createElement("div");
          policyReason.className = "chat-confirmation-status error";
          policyReason.textContent = confirmation.allowlist_reason
            || "服务端 always_confirm 策略要求每次单独确认。";
          card.appendChild(policyReason);
        }
      }

      const status = document.createElement("div");
      status.className = `chat-confirmation-status${
        ["expired", "invalidated"].includes(confirmation.status) ? " error" : ""
      }`;
      status.textContent = toolConfirmationStatusText(confirmation);
      card.appendChild(status);
      chatConfirmationCards.appendChild(card);
    }

    function renderChatConfirmations() {
      pruneTerminalChatConfirmations();
      chatConfirmationCards.replaceChildren();
      for (const confirmation of state.chatConfirmations.values()) {
        renderToolConfirmation(confirmation);
      }
      setChatPending(
        [...state.chatConfirmations.values()].some(
          confirmation => (confirmation.status || "pending") === "pending"
        )
      );
      scheduleChatConfirmationExpiryRefresh();
    }

    function normalizeToolConfirmation(value) {
      const confirmation = {
        ...value,
        id: value.id || value.confirmation_id
      };
      confirmation.allowlist_allowed = value.allowlist_allowed
        ?? value.gate_reason_code !== "always_confirm";
      confirmation.allowlist_reason = value.allowlist_reason || (
        confirmation.allowlist_allowed
          ? null
          : "服务端 always_confirm 策略要求每次单独确认。"
      );
      confirmation.trace_ref = value.trace_ref || (
        value.session_id && value.turn_id && value.call_id
          ? `trace:${value.session_id}:${value.turn_id}:${value.call_id}`
          : null
      );
      return confirmation;
    }

    function reconcileChatConfirmations(authoritative) {
      const confirmations = [];
      for (const item of authoritative || []) {
        const normalized = normalizeToolConfirmation(item || {});
        if (
          normalized.id
          && normalized.server_id !== "management"
          && !isTerminalToolConfirmation(normalized)
        ) {
          confirmations.push([normalized.id, normalized]);
        }
      }
      state.chatConfirmations = new Map(confirmations);
      renderChatConfirmations();
    }

    async function loadChatConfirmations() {
      if (!state.sessionId) {
        reconcileChatConfirmations([]);
        return;
      }
      try {
        const response = await fetch(
          `${apiBase()}/v1/capabilities/confirmations/pending?session_id=${
            encodeURIComponent(state.sessionId)
          }`
        );
        if (!response.ok) throw new Error(`${response.status}`);
        const payload = await response.json();
        reconcileChatConfirmations(payload.confirmations);
      } catch (error) {
        appendError(`恢复 Tool 确认失败：${error.message}`);
      }
    }

    async function refreshChatConfirmationAuthority(confirmation) {
      if (!state.sessionId || !confirmation?.id) return;
      try {
        const response = await fetch(
          `${apiBase()}/v1/capabilities/confirmations/${
            encodeURIComponent(confirmation.id)
          }?session_id=${encodeURIComponent(state.sessionId)}`
        );
        if (!response.ok) {
          if (response.status === 404) state.chatConfirmations.delete(confirmation.id);
          renderChatConfirmations();
          return;
        }
        const payload = await response.json();
        const normalized = normalizeToolConfirmation(payload.confirmation || {});
        if (isTerminalToolConfirmation(normalized)) {
          removeChatConfirmation(normalized.id);
        } else {
          state.chatConfirmations.set(normalized.id, normalized);
        }
        renderChatConfirmations();
      } catch (error) {
        appendError(`读取确认权威状态失败：${error.message}`);
      }
    }

    function scheduleChatConfirmationExpiryRefresh() {
      if (state.confirmationExpiryTimer) {
        clearTimeout(state.confirmationExpiryTimer);
        state.confirmationExpiryTimer = null;
      }
      const pending = [...state.chatConfirmations.values()].filter(
        item => (item.status || "pending") === "pending"
      );
      if (!pending.length) return;
      const nextExpiry = Math.min(
        ...pending.map(item => Date.parse(item.expires_at || "")).filter(Number.isFinite)
      );
      if (!Number.isFinite(nextExpiry)) return;
      state.confirmationExpiryTimer = setTimeout(async () => {
        for (const confirmation of pending) {
          await refreshChatConfirmationAuthority(confirmation);
        }
      }, Math.max(0, nextExpiry - Date.now()) + 50);
    }

    async function decideToolConfirmation(confirmation, decision) {
      if (
        !state.sessionId
        || state.confirmationDecisionLocks.has(confirmation.id)
        || (confirmation.status || "pending") !== "pending"
      ) return;
      state.confirmationDecisionLocks.add(confirmation.id);
      const slot = `${confirmation.id}:${decision}`;
      let idempotencyKey = state.confirmationIdempotencyKeys.get(slot);
      if (!idempotencyKey) {
        idempotencyKey = `chat-ui-${crypto.randomUUID()}`;
        state.confirmationIdempotencyKeys.set(slot, idempotencyKey);
      }
      renderChatConfirmations();
      try {
        const response = await fetch(
          `${apiBase()}/v1/capabilities/confirmations/${
            encodeURIComponent(confirmation.id)
          }/decisions`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              expected_revision: confirmation.revision,
              decision,
              idempotency_key: idempotencyKey,
              session_id: state.sessionId
            })
          }
        );
        const payload = await response.json();
        if (!response.ok) {
          const authority = payload?.detail?.authoritative_state;
          if (authority?.id) {
            state.chatConfirmations.set(
              authority.id,
              normalizeToolConfirmation(authority)
            );
          } else {
            await refreshChatConfirmationAuthority(confirmation);
          }
          throw new Error(
            payload?.detail?.message || payload?.detail?.code || `${response.status}`
          );
        }
        const decided = payload.confirmation || payload.state;
        if (decided?.id) {
          const normalized = normalizeToolConfirmation(decided);
          if (
            isTerminalToolConfirmation(normalized)
            || normalized.status === "approved"
          ) {
            removeChatConfirmation(decided.id);
          } else {
            state.chatConfirmations.set(decided.id, normalized);
          }
        }
      } catch (error) {
        appendError(`Tool 确认决定失败：${error.message}`);
      } finally {
        state.confirmationDecisionLocks.delete(confirmation.id);
        renderChatConfirmations();
      }
    }

    function scrollMessages() {
      messagesEl.scrollTop = messagesEl.scrollHeight;
    }

    function appendMessage(role, content, meta = "") {
      const row = document.createElement("div");
      row.className = `message-row ${role}`;
      const bubble = document.createElement("div");
      bubble.className = "bubble";
      const contentEl = document.createElement("div");
      contentEl.textContent = content;
      bubble.appendChild(contentEl);
      if (meta) {
        const metaEl = document.createElement("div");
        metaEl.className = "meta";
        metaEl.textContent = meta;
        bubble.appendChild(metaEl);
      }
      row.appendChild(bubble);
      messagesEl.appendChild(row);
      scrollMessages();
      return { row, bubble, contentEl };
    }

    function appendError(message) {
      const target = state.activeAssistantBubble || state.systemBubble;
      if (!target) {
        appendMessage("error", message);
        return;
      }
      const error = document.createElement("div");
      error.className = "assistant-inline-error";
      error.textContent = message;
      target.bubble.appendChild(error);
      scrollMessages();
    }

    function toolActivityContainer() {
      if (!state.activeAssistantBubble) return null;
      let container = state.activeAssistantBubble.bubble.querySelector(".tool-activities");
      if (!container) {
        container = document.createElement("div");
        container.className = "tool-activities";
        state.activeAssistantBubble.bubble.insertBefore(
          container,
          state.activeAssistantBubble.contentEl
        );
      }
      return container;
    }

    function showToolStarted(event) {
      const row = document.createElement("div");
      row.className = "tool-status running";
      const icon = document.createElement("span");
      icon.className = "tool-spinner";
      const text = document.createElement("span");
      text.textContent = event.display || (
        event.name === "summarize_context"
          ? "上下文摘要正在执行"
          : `${event.name || "未知工具"} 工具正在执行`
      );
      row.append(icon, text);
      const container = toolActivityContainer();
      if (container) container.appendChild(row);
      else messagesEl.appendChild(row);
      state.toolStatusRows.set(event.call_id, { row, icon, text });
      scrollMessages();
    }

    function toolGovernanceMetrics(event) {
      const raw = Number(event.raw_result_tokens);
      const context = Number(event.context_result_tokens);
      if (!Number.isFinite(raw) || !Number.isFinite(context)) return null;
      const rawText = raw.toLocaleString();
      const contextText = context.toLocaleString();
      if (event.tool_governance_enabled === false) {
        return `工具治理已关闭 · ${rawText} tokens 原样进入上下文`;
      }
      if (!event.is_truncated) {
        return `工具治理已开启 · ${rawText} tokens · 未超过阈值，未裁剪`;
      }
      const reduction = raw > 0
        ? Math.max(0, (1 - context / raw) * 100)
        : 0;
      return `工具治理已开启 · ${rawText} → ${contextText} tokens · 减少 ${reduction.toFixed(1)}% · 已裁剪`;
    }

    function showToolCompleted(event) {
      let item = state.toolStatusRows.get(event.call_id);
      if (!item) {
        showToolStarted(event);
        item = state.toolStatusRows.get(event.call_id);
      }
      item.row.className = `tool-status ${event.ok ? "success" : "error"}`;
      item.icon.textContent = event.ok ? "✓" : "×";
      item.text.textContent = event.display || (event.ok
        ? `已执行 ${event.name || "未知工具"} 工具`
        : `${event.name || "未知工具"} 工具执行失败${event.error_code ? `（${event.error_code}）` : ""}`);
      if (event.summary_id) {
        item.row.title = `summary_id=${event.summary_id}`;
        item.row.dataset.summaryId = event.summary_id;
      }
      const metricsText = toolGovernanceMetrics(event);
      if (metricsText) {
        const metrics = document.createElement("span");
        metrics.className = "tool-governance-metrics";
        metrics.textContent = metricsText;
        if (event.raw_source_ref) {
          metrics.title = `raw_source_ref=${event.raw_source_ref}`;
        }
        item.row.appendChild(metrics);
      }
      scrollMessages();
    }

    function appendSummaryTrace(target, trace) {
      if (!target || !trace) return;
      const row = document.createElement("div");
      row.className = "tool-status success";
      row.title = `summary_id=${trace.summary_id}`;
      const icon = document.createElement("span");
      icon.textContent = "✓";
      const text = document.createElement("span");
      text.textContent = `已执行上下文摘要 · summary前 tokens=${trace.before_tokens} · summary后 tokens=${trace.after_tokens}`;
      row.append(icon, text);
      target.bubble.insertBefore(row, target.contentEl);
    }

    function appendHistoricalTool(target, message) {
      let result = {};
      try {
        result = JSON.parse(message.content || "{}");
      } catch (_error) {
        result = {};
      }
      let container = target.bubble.querySelector(".tool-activities");
      if (!container) {
        container = document.createElement("div");
        container.className = "tool-activities";
        target.bubble.insertBefore(container, target.contentEl);
      }
      const row = document.createElement("div");
      const ok = result.ok !== false;
      row.className = `tool-status ${ok ? "success" : "error"}`;
      const icon = document.createElement("span");
      icon.textContent = ok ? "✓" : "×";
      const text = document.createElement("span");
      text.textContent = result.display || (ok
        ? `已执行 ${message.name || "未知工具"} 工具`
        : `${message.name || "未知工具"} 工具执行失败${result.error_code ? `（${result.error_code}）` : ""}`);
      row.append(icon, text);
      container.appendChild(row);
    }

    function clearConversation() {
      state.sessionId = null;
      sessionStorage.removeItem(CHAT_SESSION_STORAGE_KEY);
      state.activeAssistantBubble = null;
      state.systemBubble = null;
      state.toolStatusRows.clear();
      state.chatConfirmations.clear();
      state.confirmationDecisionLocks.clear();
      state.confirmationIdempotencyKeys.clear();
      renderChatConfirmations();
      messagesEl.replaceChildren();
      updateTokenBudget();
      setStatus("新会话");
      renderSessions();
      messageInput.focus();
    }

    function payloadFor(message) {
      const command = parseSlashCommand(message);
      const payload = { message: command ? command.message : message };
      if (command) payload.tool = command.tool;
      if (state.sessionId) payload.session_id = state.sessionId;
      if (providerSelect.value) payload.provider = providerSelect.value;
      if (modelSelect.value) payload.model = modelSelect.value;
      payload.tool_governance_enabled = state.toolGovernanceEnabled;
      const workbenchContext = getWorkbenchContext();
      if (workbenchContext) payload.workbench_context = workbenchContext;
      const skipKnowledge = state.skipKnowledgeForNextMessage;
      state.skipKnowledgeForNextMessage = false;
      if (!skipKnowledge && chatKnowledgeMode.value === "auto" && activeKnowledgeBaseId) {
        payload.knowledge_mode = "required";
        payload.knowledge_base_id = activeKnowledgeBaseId;
        delete payload.tool;
      } else {
        payload.knowledge_mode = "off";
      }
      return payload;
    }

    function parseSlashCommand(message) {
      const match = message.match(/^\/([A-Za-z0-9_-]+)(?:\s+([\s\S]*))?$/);
      if (!match) return null;
      const tool = state.tools.find((item) => item.name === match[1]);
      if (!tool) return null;
      return { tool: tool.name, message: (match[2] || "").trim() };
    }

    function selectTool(tool) {
      messageInput.value = `/${tool.name} `;
      hideToolMenu();
      messageInput.focus();
    }

    function hideToolMenu() {
      toolMenu.hidden = true;
      state.visibleTools = [];
      state.activeToolIndex = 0;
    }

    function renderToolMenu() {
      const match = messageInput.value.match(/^\/([^\s]*)$/);
      if (!match) {
        hideToolMenu();
        return;
      }
      const keyword = match[1].toLowerCase();
      state.visibleTools = state.tools.filter((tool) =>
        tool.name.toLowerCase().includes(keyword)
      );
      state.activeToolIndex = Math.min(
        state.activeToolIndex,
        Math.max(0, state.visibleTools.length - 1)
      );
      toolMenu.replaceChildren();
      for (const [index, tool] of state.visibleTools.entries()) {
        const option = document.createElement("button");
        option.type = "button";
        option.className = `tool-option${index === state.activeToolIndex ? " active" : ""}`;
        const name = document.createElement("span");
        name.className = "tool-name";
        name.textContent = `/${tool.name}`;
        const description = document.createElement("span");
        description.className = "tool-description";
        description.textContent = tool.description;
        const risk = document.createElement("span");
        risk.className = "tool-risk";
        risk.textContent = tool.risk_level;
        option.append(name, description, risk);
        option.addEventListener("click", () => selectTool(tool));
        toolMenu.appendChild(option);
      }
      toolMenu.hidden = state.visibleTools.length === 0;
    }

    async function loadTools() {
      try {
        const response = await fetch(`${apiBase()}/v1/tools`);
        if (!response.ok) throw new Error(`${response.status} ${await response.text()}`);
        const data = await response.json();
        state.tools = data.tools || [];
      } catch (error) {
        state.tools = [];
        hideToolMenu();
        appendError(`读取工具列表失败：${error.message}`);
      }
    }

    function renderModels(providers, defaultProvider, defaultModel) {
      const selectedProvider = providerSelect.value || defaultProvider;
      const provider = providers.find((item) => item.name === selectedProvider);
      const models = provider?.models || [];
      modelSelect.replaceChildren();
      if (models.length === 0) {
        const option = document.createElement("option");
        option.value = "";
        option.textContent = "该 Provider 暂未配置可用模型";
        modelSelect.appendChild(option);
      }
      for (const model of models) {
        const option = document.createElement("option");
        option.value = model;
        option.textContent = model === defaultModel ? `${model}（默认）` : model;
        modelSelect.appendChild(option);
      }
      modelSelect.disabled = models.length === 0;
      if (models.includes(defaultModel)) modelSelect.value = defaultModel;
    }

    async function loadProviders() {
      try {
        const response = await fetch(`${apiBase()}/v1/providers`);
        if (!response.ok) throw new Error(`${response.status} ${await response.text()}`);
        const data = await response.json();
        providerSelect.replaceChildren();
        const defaultOption = document.createElement("option");
        defaultOption.value = "";
        defaultOption.textContent = `使用后端默认 (${data.default_provider})`;
        providerSelect.appendChild(defaultOption);
        for (const provider of data.providers) {
          const option = document.createElement("option");
          option.value = provider.name;
          option.textContent = provider.has_api_key
            ? `${provider.name} · ${provider.type}`
            : `${provider.name} · 缺少 Key`;
          providerSelect.appendChild(option);
        }
        renderModels(data.providers, data.default_provider, data.default_model);
        providerSelect.onchange = () => {
          renderModels(data.providers, data.default_provider, data.default_model);
        };
        setStatus("已连接本地 API");
      } catch (error) {
        setStatus("无法读取 provider");
        appendError(`读取 provider 失败：${error.message}`);
      }
    }

    function sessionLabel(session) {
      return session.title || session.last_message || "未命名会话";
    }

    function createSessionItem(session) {
        const item = document.createElement("div");
        item.className = `session-item${session.id === state.sessionId ? " active" : ""}`;
        item.setAttribute("role", "button");
        item.tabIndex = 0;

        const text = document.createElement("div");
        const title = document.createElement("div");
        title.className = "session-title";
        title.textContent = sessionLabel(session);
        const last = document.createElement("div");
        last.className = "session-last";
        last.textContent = `${session.message_count} 条 · ${session.last_message || "无摘要"}`;
        text.append(title, last);

        const del = document.createElement("button");
        del.type = "button";
        del.className = "delete-session";
        del.title = "删除会话";
        del.textContent = "×";
        del.addEventListener("click", async (event) => {
          event.stopPropagation();
          await deleteSession(session.id);
        });

        item.append(text, del);
        item.addEventListener("click", () => switchSession(session.id));
        item.addEventListener("keydown", (event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            switchSession(session.id);
          }
        });
        return item;
    }

    function renderSessions() {
      sessionListEl.replaceChildren();
      if (!state.sessions.length) {
        const empty = document.createElement("div");
        empty.className = "session-empty";
        empty.textContent = "暂无历史会话";
        sessionListEl.appendChild(empty);
        return;
      }

      for (const session of state.sessions) {
        sessionListEl.appendChild(createSessionItem(session));
      }
    }

    async function loadSessions(reset = true) {
      if (state.sessionsLoading) return;
      if (!reset && !state.sessionsHasMore) return;
      state.sessionsLoading = true;
      const offset = reset ? 0 : state.sessionOffset;
      try {
        const response = await fetch(
          `${apiBase()}/v1/sessions?limit=${state.sessionLimit}&offset=${offset}`
        );
        if (!response.ok) throw new Error(`${response.status} ${await response.text()}`);
        const data = await response.json();
        const incoming = data.sessions || [];
        if (reset) {
          state.sessions = incoming;
          renderSessions();
        } else {
          const existing = new Set(state.sessions.map(item => item.id));
          const additions = incoming.filter(item => !existing.has(item.id));
          state.sessions.push(...additions);
          for (const session of additions) {
            sessionListEl.appendChild(createSessionItem(session));
          }
        }
        state.sessionOffset = offset + incoming.length;
        state.sessionsHasMore = Boolean(data.has_more);
      } catch (error) {
        if (reset) {
          sessionListEl.innerHTML = "";
          const note = document.createElement("div");
          note.className = "session-empty";
          note.textContent = "会话加载失败";
          sessionListEl.appendChild(note);
        }
        setStatus(`会话加载失败：${error.message}`);
      } finally {
        state.sessionsLoading = false;
      }
    }

    async function clearAllSessions() {
      if (state.isSending) return;
      const ok = window.confirm(
        "确认清除全部对话吗？这会删除所有会话、消息、摘要和会话 Token 记录，但不会删除长期记忆和简历文件。"
      );
      if (!ok) return;
      clearAllSessionsButton.disabled = true;
      try {
        const response = await fetch(`${apiBase()}/v1/sessions`, {
          method: "DELETE"
        });
        if (!response.ok) throw new Error(`${response.status} ${await response.text()}`);
        const data = await response.json();
        state.sessions = [];
        state.sessionOffset = 0;
        state.sessionsHasMore = false;
        state.sessionId = null;
        sessionStorage.removeItem(CHAT_SESSION_STORAGE_KEY);
        state.systemBubble = null;
        state.activeAssistantBubble = null;
        reconcileChatConfirmations([]);
        messagesEl.replaceChildren();
        updateTokenBudget();
        renderSessions();
        state.systemBubble = appendMessage(
          "assistant",
          "全部对话已清除。长期记忆和简历文件仍然保留。"
        );
        setStatus(`已清除 ${data.deleted_sessions || 0} 个会话`);
      } catch (error) {
        appendError(`清除全部对话失败：${error.message}`);
        setStatus("清除全部对话失败");
      } finally {
        clearAllSessionsButton.disabled = false;
      }
    }

    async function switchSession(id) {
      if (state.isSending) return;
      try {
        setStatus("正在加载历史会话");
        const response = await fetch(`${apiBase()}/v1/sessions/${id}/messages`);
        if (!response.ok) throw new Error(`${response.status} ${await response.text()}`);
        const data = await response.json();
        state.sessionId = data.session_id;
        sessionStorage.setItem(CHAT_SESSION_STORAGE_KEY, state.sessionId);
        updateTokenBudget(
          data.session_usage,
          data.max_total_tokens,
          data.token_budget_status
        );
        state.systemBubble = null;
        for (const stream of state.delegationStreams.values()) stream.close();
        for (const timer of state.delegationReconnectTimers.values()) clearTimeout(timer);
        state.delegationStreams.clear(); state.delegationReconnectTimers.clear();
        state.delegationRuns.clear(); state.delegationEventSequences.clear();
        delegationTaskCards.replaceChildren(); delegationRunDetail.hidden = true;
        messagesEl.replaceChildren();
        let lastAssistant = null;
        const turnAssistants = new Map();
        for (const message of data.messages) {
          const parentRunId = message.parent_run_id || message.metadata?.parent_run_id;
          if (parentRunId) state.delegationRuns.set(parentRunId, { parent: { id: parentRunId }, child_runs: [], merge_reports: [] });
          if (message.role === "user") {
            if (message.content) appendMessage("user", message.content);
            continue;
          }
          if (message.role === "assistant") {
            if (!message.content) continue;
            let rendered = turnAssistants.get(message.turn_id);
            if (rendered && !rendered.contentEl.textContent) {
              rendered.contentEl.textContent = message.content;
            } else {
              rendered = appendMessage("assistant", message.content);
              turnAssistants.set(message.turn_id, rendered);
            }
            lastAssistant = rendered;
            continue;
          }
          if (message.role === "tool") {
            let rendered = turnAssistants.get(message.turn_id);
            if (!rendered) {
              rendered = appendMessage("assistant", "");
              turnAssistants.set(message.turn_id, rendered);
            }
            appendHistoricalTool(rendered, message);
            lastAssistant = rendered;
          }
        }
        appendSummaryTrace(lastAssistant, data.latest_summary_trace);
        await loadChatConfirmations();
        await loadDelegationRunForSession();
        setStatus("已切换历史会话");
        renderSessions();
      } catch (error) {
        appendError(`加载历史会话失败：${error.message}`);
      }
    }

    async function deleteSession(id) {
      if (state.isSending) return;
      const ok = window.confirm("确认删除这个会话及其历史消息吗？");
      if (!ok) return;
      try {
        const response = await fetch(`${apiBase()}/v1/sessions/${id}`, {
          method: "DELETE"
        });
        if (!response.ok) throw new Error(`${response.status} ${await response.text()}`);
        if (state.sessionId === id) {
          messagesEl.replaceChildren();
          state.sessionId = null;
          sessionStorage.removeItem(CHAT_SESSION_STORAGE_KEY);
          state.systemBubble = null;
          reconcileChatConfirmations([]);
          updateTokenBudget();
        }
        await loadSessions();
        setStatus("会话已删除");
      } catch (error) {
        appendError(`删除会话失败：${error.message}`);
      }
    }

    function updateAssistantMeta(result) {
      if (!state.activeAssistantBubble) return;
      const meta = document.createElement("div");
      meta.className = "meta";
      const usage = result.usage || {};
      const prompt = usage.prompt_tokens ?? usage.input_tokens;
      const completion = usage.completion_tokens ?? usage.output_tokens;
      const total = usage.total_tokens ?? (
        Number.isFinite(prompt) && Number.isFinite(completion)
          ? prompt + completion
          : undefined
      );
      let tokenText = "tokens=unknown";
      if (result.provider === "mock" && Object.keys(usage).length === 0) {
        tokenText = "tokens=mock";
      } else if ([prompt, completion, total].every(Number.isFinite)) {
        tokenText = `tokens=${prompt}/${completion}/${total}`;
      }
      const governanceText = result.tool_governance_enabled === false
        ? "governance=off"
        : "governance=on";
      meta.textContent = `${result.provider} / ${result.model} · tools=${result.tool_calls} · ${tokenText} · ${governanceText}`;
      state.activeAssistantBubble.bubble.appendChild(meta);
      if (result.summary_trace) {
        const traceMeta = document.createElement("div");
        traceMeta.className = "meta";
        traceMeta.textContent = `context summary=${result.summary_trace.summary_id} · ${result.summary_trace.before_tokens}→${result.summary_trace.after_tokens} tokens`;
        state.activeAssistantBubble.bubble.appendChild(traceMeta);
      }
    }

    function appendContinuationAction(result) {
      const continuation = result.continuation;
      if (!state.activeAssistantBubble || !continuation) return;
      const button = document.createElement("button");
      button.type = "button";
      button.className = "continuation-action";
      button.textContent = `继续生成（已完成 ${continuation.model_calls} 次模型调用 / ${continuation.tool_calls} 次工具调用）`;
      button.addEventListener("click", async () => {
        if (state.isSending) return;
        button.disabled = true;
        button.textContent = "正在继续...";
        await sendMessage(continuation.next_message);
      });
      state.activeAssistantBubble.bubble.appendChild(button);
    }

    function emailApiError(payload, status) {
      const detail = payload?.detail || payload || {};
      return detail.display
        || detail.message
        || detail.error_code
        || detail.code
        || `请求失败：${status}`;
    }

    async function emailApiRequest(url, options = {}) {
      const response = await fetch(url, options);
      let payload = {};
      try {
        payload = await response.json();
      } catch (_error) {
        payload = {};
      }
      if (!response.ok) {
        throw new Error(emailApiError(payload, response.status));
      }
      return payload;
    }

    function addEmailPreviewField(grid, label, value) {
      if (!value) return;
      const labelEl = document.createElement("div");
      labelEl.className = "email-preview-label";
      labelEl.textContent = label;
      const valueEl = document.createElement("div");
      valueEl.textContent = Array.isArray(value) ? value.join(", ") : String(value);
      grid.append(labelEl, valueEl);
    }

    function queueEmailApproval(event) {
      const draftId = event.metadata?.draft_id;
      if (
        event.name !== "email_create_draft"
        || !event.ok
        || !draftId
        || !state.activeAssistantBubble
      ) return;
      state.pendingEmailDrafts.push({
        draftId,
        target: state.activeAssistantBubble
      });
    }

    async function cancelEmailApproval(approval, card, buttons, resultEl) {
      buttons.forEach(button => { button.disabled = true; });
      try {
        await emailApiRequest(
          `${apiBase()}/v1/email/approvals/${approval.approval_id}/revoke`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              session_id: state.sessionId,
              confirmed: false
            })
          }
        );
        resultEl.className = "email-send-result";
        resultEl.textContent = "已取消发送，邮件仍保留为本地草稿。";
        card.dataset.status = "cancelled";
      } catch (error) {
        resultEl.className = "email-send-result error";
        resultEl.textContent = `取消失败：${error.message}`;
        buttons.forEach(button => { button.disabled = false; });
      }
    }

    async function confirmAndSendEmail(approval, card, buttons, resultEl) {
      const confirmed = window.confirm(
        "请再次核对收件人、主题、正文和附件。确认通过 SMTP 发送这封邮件吗？"
      );
      if (!confirmed) return;
      buttons.forEach(button => { button.disabled = true; });
      resultEl.className = "email-send-result";
      resultEl.textContent = "正在确认审批并调用 email_send…";
      try {
        await emailApiRequest(
          `${apiBase()}/v1/email/approval-challenges/${approval.approval_id}/confirm`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              session_id: state.sessionId,
              confirmed: true
            })
          }
        );
        const result = await emailApiRequest(
          `${apiBase()}/v1/email/approvals/${approval.approval_id}/send`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              session_id: state.sessionId,
              idempotency_key: `email-ui-send-${crypto.randomUUID()}`
            })
          }
        );
        const receipt = result.data || {};
        if (receipt.status === "sent" && receipt.external_delivery === true) {
          resultEl.className = "email-send-result success";
          resultEl.textContent = [
            "✓ 已执行 email_send 工具",
            "邮件已成功发送",
            receipt.sent_at ? `发送时间：${receipt.sent_at}` : "",
            receipt.message_ref ? `邮件 ID：${receipt.message_ref}` : ""
          ].filter(Boolean).join("\n");
          card.dataset.status = "sent";
        } else if (receipt.status === "unknown") {
          resultEl.className = "email-send-result unknown";
          resultEl.textContent = [
            "已执行 email_send 工具",
            "发送结果待核验，请勿重复发送"
          ].join("\n");
          card.dataset.status = "unknown";
        } else {
          resultEl.className = "email-send-result";
          resultEl.textContent = result.display || "发送流程已完成，但未产生真实 SMTP 成功回执。";
          card.dataset.status = receipt.status || "completed";
        }
      } catch (error) {
        resultEl.className = "email-send-result error";
        resultEl.textContent = `email_send 执行失败：${error.message}`;
        card.dataset.status = "error";
      }
    }

    function renderEmailApprovalCard(approval, target) {
      const card = document.createElement("section");
      card.className = "email-approval-card";
      card.dataset.approvalId = approval.approval_id;
      card.dataset.status = approval.status;

      const title = document.createElement("h3");
      title.textContent = "邮件发送前确认";
      const grid = document.createElement("div");
      grid.className = "email-preview-grid";
      addEmailPreviewField(grid, "收件人", approval.to);
      addEmailPreviewField(grid, "抄送", approval.cc);
      addEmailPreviewField(grid, "密送", approval.bcc);
      addEmailPreviewField(grid, "主题", approval.subject);
      addEmailPreviewField(
        grid,
        "附件指纹",
        approval.attachment_sha256s?.length
          ? approval.attachment_sha256s
          : "无"
      );
      addEmailPreviewField(grid, "确认有效期", approval.expires_at);

      const body = document.createElement("div");
      body.className = "email-preview-body";
      body.textContent = approval.body_text;

      const actions = document.createElement("div");
      actions.className = "email-approval-actions";
      const confirmButton = document.createElement("button");
      confirmButton.type = "button";
      confirmButton.className = "email-confirm-send";
      confirmButton.textContent = "确认并发送";
      const cancelButton = document.createElement("button");
      cancelButton.type = "button";
      cancelButton.textContent = "取消";
      const buttons = [confirmButton, cancelButton];

      const resultEl = document.createElement("div");
      resultEl.className = "email-send-result";
      resultEl.textContent = "邮件尚未发送。确认操作只对当前预览内容有效。";

      confirmButton.addEventListener("click", () => {
        confirmAndSendEmail(approval, card, buttons, resultEl);
      });
      cancelButton.addEventListener("click", () => {
        cancelEmailApproval(approval, card, buttons, resultEl);
      });
      actions.append(confirmButton, cancelButton);
      card.append(title, grid, body, actions, resultEl);
      target.bubble.appendChild(card);
      scrollMessages();
    }

    async function flushEmailApprovalQueue() {
      if (!state.sessionId || state.pendingEmailDrafts.length === 0) return;
      const pending = state.pendingEmailDrafts.splice(0);
      for (const item of pending) {
        try {
          const approval = await emailApiRequest(
            `${apiBase()}/v1/email/drafts/${item.draftId}/approval-challenges`,
            {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                session_id: state.sessionId
              })
            }
          );
          renderEmailApprovalCard(approval, item.target);
        } catch (error) {
          const resultEl = document.createElement("div");
          resultEl.className = "email-send-result error";
          resultEl.textContent = `无法创建发送确认：${error.message}`;
          item.target.bubble.appendChild(resultEl);
        }
      }
      scrollMessages();
    }

    function applyStreamEvent(event) {
      if (event.type === "confirmation_required") {
        const confirmation = normalizeToolConfirmation(event);
        if (confirmation.session_id) {
          state.sessionId = confirmation.session_id;
          sessionStorage.setItem(CHAT_SESSION_STORAGE_KEY, state.sessionId);
        }
        state.chatConfirmations.set(confirmation.id, confirmation);
        renderChatConfirmations();
        setStatus("Tool 等待当前会话确认");
        return;
      }
      if (event.type === "confirmation_resolved") {
        const current = state.chatConfirmations.get(event.confirmation_id) || {};
        const resolved = normalizeToolConfirmation({
          ...current,
          ...event,
          id: event.confirmation_id
        });
        if (isTerminalToolConfirmation(resolved)) {
          state.chatConfirmations.delete(resolved.id);
          removeChatConfirmation(resolved.id);
        } else {
          state.chatConfirmations.set(resolved.id, resolved);
        }
        renderChatConfirmations();
        setStatus(
          event.gate_revalidated
            ? "确认已通过，Gate 已重新校验"
            : toolConfirmationStatusText(resolved)
        );
        return;
      }
      if (event.type === "tool_started") {
        if (event.confirmation_id) {
          const current = state.chatConfirmations.get(event.confirmation_id);
          if (current) {
            state.chatConfirmations.set(event.confirmation_id, {
              ...current,
              audit_ref: event.audit_ref || current.audit_ref,
              trace_ref: event.trace_ref || current.trace_ref,
              status: "approved",
              gate_revalidated: true
            });
            renderChatConfirmations();
          }
        }
        showToolStarted(event);
        return;
      }
      if (event.type === "tool_completed") {
        if (event.confirmation_id) {
          const current = state.chatConfirmations.get(event.confirmation_id);
          if (current) {
            state.chatConfirmations.set(event.confirmation_id, {
              ...current,
              audit_ref: event.audit_ref || current.audit_ref,
              trace_ref: event.trace_ref || current.trace_ref,
              status: event.tool_invoked ? "consumed" : "invalidated",
              reason_code: event.error_code || event.status
            });
            renderChatConfirmations();
          }
          removeChatConfirmation(event.confirmation_id);
          renderChatConfirmations();
        }
        showToolCompleted(event);
        queueEmailApproval(event);
        return;
      }
      if (event.type === "delta") {
        if (state.activeAssistantBubble.contentEl.dataset.thinking === "true") {
          state.activeAssistantBubble.contentEl.textContent = "";
          delete state.activeAssistantBubble.contentEl.dataset.thinking;
          state.activeAssistantBubble.bubble.classList.remove("is-thinking");
        }
        state.activeAssistantBubble.contentEl.textContent += event.content || "";
        scrollMessages();
        return;
      }
      if (event.type === "done") {
        state.sessionId = event.result.session_id;
        sessionStorage.setItem(CHAT_SESSION_STORAGE_KEY, state.sessionId);
        void flushEmailApprovalQueue();
        if (
          event.result.content &&
          (!state.activeAssistantBubble.contentEl.textContent.trim()
            || state.activeAssistantBubble.contentEl.dataset.thinking === "true")
        ) {
          state.activeAssistantBubble.contentEl.textContent = event.result.content;
          delete state.activeAssistantBubble.contentEl.dataset.thinking;
          state.activeAssistantBubble.bubble.classList.remove("is-thinking");
        }
        updateAssistantMeta(event.result);
        if (event.result.parent_run_id) {
          const parentRunId = event.result.parent_run_id;
          state.delegationRuns.set(parentRunId, {
            parent: { id: parentRunId, status: event.result.run_status, ...(event.result.task_card || {}) },
            child_runs: [], merge_reports: []
          });
          renderDelegationTaskCard(parentRunId);
          void loadDelegationRun(parentRunId).catch(error => appendError(`加载任务状态失败：${error.message}`));
          void startDelegationEventStream(parentRunId);
        }
        if (event.result.finish_reason === "continuation_required") {
          appendContinuationAction(event.result);
        }
        updateTokenBudget(
          event.result.session_usage,
          event.result.max_total_tokens,
          event.result.token_budget_status
        );
        setStatus(
          event.result.finish_reason === "continuation_required"
            ? "本轮达到模型调用上限，可继续生成"
            : "回复完成"
        );
        loadSessions();
        return;
      }
      if (event.type === "error") {
        const error = event.error || {};
        const message = error.message || "请求处理失败";
        const suggestion = error.suggestion ? `\n${error.suggestion}` : "";
        appendError(`${message}${suggestion}`);
        setStatus("后端返回错误");
      }
    }

    async function readStream(response) {
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const parts = buffer.split("\n\n");
        buffer = parts.pop() || "";
        for (const part of parts) {
          const lines = part.split("\n");
          for (const line of lines) {
            if (!line.startsWith("data:")) continue;
            const raw = line.slice(5).trim();
            if (!raw) continue;
            applyStreamEvent(JSON.parse(raw));
          }
        }
      }
    }

    async function sendMessage(message) {
      appendMessage("user", message);
      state.activeAssistantBubble = appendMessage("assistant", "正在思考…");
      state.activeAssistantBubble.contentEl.dataset.thinking = "true";
      state.activeAssistantBubble.bubble.classList.add("is-thinking");
      setSending(true);
      setStatus("正在生成回复");
      const requestContext = getWorkbenchContext();
      state.activeWorkbenchEpoch = requestContext?.context_epoch || null;
      state.workbenchContextChanged = false;
      try {
        const response = await fetch(`${apiBase()}/v1/chat/stream`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payloadFor(message))
        });
        if (!response.ok) throw new Error(`${response.status} ${await response.text()}`);
        if (!response.body) throw new Error("浏览器未返回响应流");
        await readStream(response);
      } catch (error) {
        appendError(`请求失败：${error.message}。请检查 uv run agent serve 是否已启动。`);
        setStatus("请求失败");
      } finally {
        if (state.workbenchContextChanged) {
          setStatus("工作台上下文已切换；旧流仅保留为普通回答，未执行任何业务回填。");
        }
        setSending(false);
        state.activeAssistantBubble = null;
        messageInput.focus();
      }
    }

    composer.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (state.isSending) return;
      const message = messageInput.value.trim();
      if (!message) return;
      const command = parseSlashCommand(message);
      if (command && !command.message) {
        appendError("选择工具后，请继续输入要交给工具处理的内容");
        return;
      }
      messageInput.value = "";
      hideToolMenu();
      await sendMessage(message);
    });

    messageInput.addEventListener("input", renderToolMenu);
    messageInput.addEventListener("keydown", (event) => {
      if (!toolMenu.hidden && state.visibleTools.length) {
        if (event.key === "ArrowDown" || event.key === "ArrowUp") {
          event.preventDefault();
          const direction = event.key === "ArrowDown" ? 1 : -1;
          state.activeToolIndex = (
            state.activeToolIndex + direction + state.visibleTools.length
          ) % state.visibleTools.length;
          renderToolMenu();
          return;
        }
        if (event.key === "Enter") {
          event.preventDefault();
          selectTool(state.visibleTools[state.activeToolIndex]);
          return;
        }
        if (event.key === "Escape") {
          event.preventDefault();
          hideToolMenu();
          return;
        }
      }
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        composer.requestSubmit();
      }
    });

    clearButton.addEventListener("click", clearConversation);
    newSessionButton.addEventListener("click", clearConversation);
    clearAllSessionsButton.addEventListener("click", clearAllSessions);
    sessionListEl.addEventListener("scroll", () => {
      const remaining = sessionListEl.scrollHeight
        - sessionListEl.scrollTop
        - sessionListEl.clientHeight;
      if (remaining < 120) loadSessions(false);
    });
    settingsButton.addEventListener("click", () => openSettings(settingsButton));
    workbenchSettingsButton.addEventListener("click", () => openSettings(workbenchSettingsButton));
    settingsCloseButton.addEventListener("click", closeSettings);
    settingsOverlay.addEventListener("click", (event) => {
      if (event.target === settingsOverlay) closeSettings();
    });
    toolGovernanceToggle.addEventListener("change", () => {
      state.toolGovernanceEnabled = toolGovernanceToggle.checked;
      localStorage.setItem(
        TOOL_GOVERNANCE_STORAGE_KEY,
        String(state.toolGovernanceEnabled)
      );
      setStatus(state.toolGovernanceEnabled ? "工具治理已开启" : "工具治理已关闭");
    });
    memoryForm.addEventListener("submit", saveMemory);
    memoryCancelButton.addEventListener("click", resetMemoryForm);
    document.addEventListener("keydown", (event) => {
      if (settingsOverlay.hidden) return;
      if (event.key === "Escape") { closeSettings(); return; }
      if (event.key !== "Tab") return;
      const focusable = [...settingsOverlay.querySelectorAll('button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [href], [tabindex]:not([tabindex="-1"])')].filter(item => !item.hidden);
      if (!focusable.length) return;
      const first = focusable[0]; const last = focusable.at(-1);
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    });
    apiBaseInput.addEventListener("change", async () => {
      for (const stream of state.delegationStreams.values()) stream.close();
      state.delegationStreams.clear();
      clearCapabilityConfirmations();
      advanceCapabilityRequestEpoch();
      clearConversation();
      await loadProviders();
      await loadTools();
      await loadSessions();
      if (advancedWindow.activeType() === "capabilities") await refreshCapabilityRoute();
    });

    function capabilityElement(tagName, className = "", text = null) {
      const element = document.createElement(tagName);
      if (className) element.className = className;
      if (text !== null && text !== undefined) element.textContent = String(text);
      return element;
    }

    function delegationField(label, value) {
      const row = document.createElement("div");
      const strong = document.createElement("strong");
      strong.textContent = `${label}: `;
      const text = document.createElement("span");
      text.textContent = value === undefined || value === null || value === "" ? "—" : String(value);
      row.append(strong, text);
      return row;
    }

    function safeBudgetText(budget) {
      const value = budget || {};
      return ["steps", "tokens", "cost_microunits", "wall_clock_ms", "model_calls", "tool_calls"]
        .map(key => `${key}=${value[key] ?? 0}`).join(" · ");
    }

    function orchestrationDetailsSection(title, rows) {
      const section = document.createElement("details");
      section.className = "delegation-task-card orchestration-debug-section";
      const summary = document.createElement("summary");
      summary.textContent = title;
      section.appendChild(summary);
      for (const [label, value] of rows) section.appendChild(delegationField(label, value));
      return section;
    }

    function renderOrchestrationDebug(run) {
      const orchestration = run.orchestration;
      if (!orchestration) return;
      if (orchestration.route) {
        delegationRunDetail.appendChild(orchestrationDetailsSection("编排路由", [
          ["Route", orchestration.route.route],
          ["Confidence", orchestration.route.confidence],
          ["Reason", orchestration.route.reason_summary],
          ["Risk", orchestration.route.risk_level],
          ["Fallback", orchestration.route.fallback?.route],
        ]));
      }
      if (orchestration.plan) {
        const dependencies = (orchestration.plan.steps || []).map(step =>
          `${step.step_id} ← ${(step.depends_on || []).join(", ") || "root"} · ${step.status} · ${step.parallel_decision_reason || (step.parallel_candidate ? "parallel candidate" : "serial")}`
        ).join("\n");
        delegationRunDetail.appendChild(orchestrationDetailsSection("Plan 依赖 DAG", [
          ["Plan", `${orchestration.plan.plan_id} / ${orchestration.plan.status}`],
          ["Join Policy", orchestration.plan.join_policy],
          ["Dependencies", dependencies],
        ]));
      }
      if (orchestration.background_task) {
        delegationRunDetail.appendChild(orchestrationDetailsSection("后台任务", [
          ["Task ID", orchestration.background_task.task_id],
          ["状态", orchestration.background_task.status],
          ["阶段", orchestration.background_task.phase],
          ["停止原因", orchestration.background_task.reason_code],
        ]));
      }
      if (orchestration.join_decision) {
        const join = orchestration.join_decision;
        delegationRunDetail.appendChild(orchestrationDetailsSection("Join Decision", [
          ["Policy", join.policy], ["Outcome", join.outcome], ["Reason", join.reason_code],
          ["Included", [...(join.accepted || []), ...(join.partial || [])].join(", ")],
          ["Failed / Missing", [...(join.failed || []), ...(join.timed_out || []), ...(join.cancelled || []), ...(join.missing || [])].join(", ")],
        ]));
      }
      if (orchestration.verify_result) {
        const verification = orchestration.verify_result;
        const failures = (verification.failures || []).map(item => `${item.rule_id}@${item.path}`).join("\n");
        delegationRunDetail.appendChild(orchestrationDetailsSection("Runtime Verify", [
          ["Decision", verification.decision], ["Passed", verification.passed],
          ["失败项", failures], ["修复次数", orchestration.revision_count?.recovery ?? 0],
        ]));
      }
      if (orchestration.budget) {
        delegationRunDetail.appendChild(orchestrationDetailsSection("预算进度", [
          ["已消耗", safeBudgetText(orchestration.budget.consumed)],
          ["上限", safeBudgetText(orchestration.budget.limit)],
          ["剩余", safeBudgetText(orchestration.budget.remaining)],
          ["停止维度", orchestration.budget.stop_dimension],
        ]));
      }
      if ((orchestration.model_decisions || []).length) {
        delegationRunDetail.appendChild(orchestrationDetailsSection("Model Router", orchestration.model_decisions.map(item => [
          item.purpose, `${item.selected_provider || "—"}/${item.selected_model || "—"} · ${item.reason_summary}`
        ])));
      }
      if (orchestration.stop_reason) {
        delegationRunDetail.appendChild(orchestrationDetailsSection("停止原因", [["Reason", orchestration.stop_reason]]));
      }
    }

    function renderDelegationTaskCard(parentRunId) {
      const run = state.delegationRuns.get(parentRunId);
      if (!run) return;
      let card = delegationTaskCards.querySelector(`[data-parent-run-id="${CSS.escape(parentRunId)}"]`);
      if (!card) {
        card = document.createElement("article");
        card.className = "delegation-task-card";
        card.dataset.parentRunId = parentRunId;
        delegationTaskCards.appendChild(card);
      }
      card.replaceChildren();
      const parent = run.parent || {};
      const children = run.child_runs || [];
      const completed = parent.children_completed ?? children.filter(child => ["succeeded", "partial", "failed", "timed_out", "budget_exhausted", "cancelled"].includes(child.status)).length;
      const title = document.createElement("h3");
      title.textContent = "后台调研任务";
      card.append(
        title,
        delegationField("Parent Run", parent.id || parentRunId),
        delegationField("状态 / 阶段", `${parent.status || "queued"} / ${parent.phase || "—"}`),
        delegationField("Child 进度", `${completed} / ${children.length}`),
        delegationField("五维预算", safeBudgetText(parent.budget_consumed)),
        delegationField("开始时间", parent.started_at || parent.created_at),
      );
      const actions = document.createElement("div");
      actions.className = "delegation-run-actions";
      const detail = document.createElement("button"); detail.type = "button"; detail.textContent = "查看详情";
      detail.addEventListener("click", () => renderDelegationRunDetail(parentRunId));
      actions.appendChild(detail);
      if (["created", "queued", "running", "waiting_children", "waiting_for_user"].includes(parent.status)) {
        const cancel = document.createElement("button"); cancel.type = "button"; cancel.textContent = "取消";
        cancel.addEventListener("click", () => cancelDelegationRun(parentRunId)); actions.appendChild(cancel);
      }
      if (parent.status === "waiting_for_user") {
        const resume = document.createElement("button"); resume.type = "button"; resume.textContent = "安全继续";
        resume.addEventListener("click", () => resumeDelegationRun(parentRunId)); actions.appendChild(resume);
      }
      card.appendChild(actions);
    }

    function renderDelegationRunDetail(parentRunId) {
      const run = state.delegationRuns.get(parentRunId);
      if (!run) return;
      delegationRunDetail.hidden = false;
      delegationRunDetail.replaceChildren();
      const parent = run.parent || {};
      delegationRunDetail.append(
        Object.assign(document.createElement("h3"), { textContent: `运行详情 · ${parent.id || parentRunId}` }),
        delegationField("Parent 状态 / phase", `${parent.status || "—"} / ${parent.phase || "—"}`),
        delegationField("预算", safeBudgetText(parent.budget_consumed)),
        delegationField("Deadline", parent.deadline_at),
      );
      for (const child of run.child_runs || []) {
        const childCard = document.createElement("article"); childCard.className = "delegation-task-card";
        childCard.append(
          Object.assign(document.createElement("h3"), { textContent: child.specialist_id || child.child_task_id || child.id }),
          delegationField("状态 / phase", `${child.status || "—"} / ${child.phase || "—"}`),
          delegationField("Attempt", child.attempt), delegationField("Deadline", child.deadline_at),
          delegationField("失败原因", child.error_code), delegationField("租约", child.lease_expires_at),
        );
        delegationRunDetail.appendChild(childCard);
      }
      for (const report of run.merge_reports || []) {
        delegationRunDetail.append(
          delegationField("Merge evidence", report.id),
          delegationField("Missing", (report.missing || []).join(", ")),
          delegationField("Conflicts", (report.conflicts || []).length),
          delegationField("Source validation", (report.source_validation || []).length),
          delegationField("Evidence validation", (report.evidence_validation || []).length),
        );
      }
      renderOrchestrationDebug(run);
      // Artifact bodies are intentionally absent: the API supplies metadata only.
    }

    async function loadDelegationRun(parentRunId) {
      const response = await fetch(`${apiBase()}/v1/runs/${encodeURIComponent(parentRunId)}`);
      if (!response.ok) throw new Error(`${response.status} ${await response.text()}`);
      const run = await response.json();
      state.delegationRuns.set(parentRunId, run);
      renderDelegationTaskCard(parentRunId);
      const detailHeading = delegationRunDetail.querySelector("h3");
      if (
        !delegationRunDetail.hidden &&
        detailHeading?.textContent?.includes(parentRunId)
      ) {
        renderDelegationRunDetail(parentRunId);
      }
      if (["succeeded", "partial", "failed", "timed_out", "budget_exhausted", "cancelled"].includes(run.parent?.status)) {
        state.delegationStreams.get(parentRunId)?.close();
        state.delegationStreams.delete(parentRunId);
        const timer = state.delegationReconnectTimers.get(parentRunId);
        if (timer) clearTimeout(timer);
        state.delegationReconnectTimers.delete(parentRunId);
      }
      return run;
    }

    async function loadDelegationArtifactMetadata(parentRunId) {
      const response = await fetch(`${apiBase()}/v1/runs/${encodeURIComponent(parentRunId)}/artifacts`);
      if (!response.ok) throw new Error(`${response.status} ${await response.text()}`);
      return response.json();
    }

    async function loadDelegationRunForSession() {
      for (const parentRunId of state.delegationRuns.keys()) {
        const run = await loadDelegationRun(parentRunId);
        if (!delegationRunIsTerminal(run.parent?.status)) {
          void startDelegationEventStream(parentRunId);
        }
      }
    }

    function applyDelegationRunEvent(parentRunId, event) {
      const sequence = event?.event_seq;
      const current = state.delegationEventSequences.get(parentRunId) || 0;
      if (!Number.isInteger(sequence) || sequence <= current) return;
      state.delegationEventSequences.set(parentRunId, sequence);
      void loadDelegationRun(parentRunId).catch(() => {});
    }

    async function startDelegationEventStream(parentRunId) {
      state.delegationStreams.get(parentRunId)?.close();
      let cursor = state.delegationEventSequences.get(parentRunId) || 0;
      try {
        const response = await fetch(`${apiBase()}/v1/runs/${encodeURIComponent(parentRunId)}/events?after_seq=${cursor}`);
        if (response.ok) {
          const page = await response.json();
          for (const event of page.events || []) applyDelegationRunEvent(parentRunId, event);
          cursor = Number(page.next_cursor) || state.delegationEventSequences.get(parentRunId) || cursor;
        }
      } catch (_error) {
        // REST catch-up is retried via SSE reconnect; it never mutates a Run.
      }
      const stream = new EventSource(`${apiBase()}/v1/runs/${encodeURIComponent(parentRunId)}/events/stream?after_seq=${cursor}`);
      stream.onmessage = event => {
        try { const data = JSON.parse(event.data); if (data.type === "run_event") applyDelegationRunEvent(parentRunId, data.event); } catch (_error) {}
      };
      stream.onerror = () => {
        stream.close();
        if (state.delegationReconnectTimers.has(parentRunId)) return;
        const timer = setTimeout(async () => {
          state.delegationReconnectTimers.delete(parentRunId);
          try {
            const run = await loadDelegationRun(parentRunId);
            if (!["succeeded", "partial", "failed", "timed_out", "budget_exhausted", "cancelled"].includes(run.parent?.status)) void startDelegationEventStream(parentRunId);
          } catch (_error) { void startDelegationEventStream(parentRunId); }
        }, 1500);
        state.delegationReconnectTimers.set(parentRunId, timer);
      };
      state.delegationStreams.set(parentRunId, stream);
    }

    async function mutateDelegationRun(parentRunId, action) {
      const parent = state.delegationRuns.get(parentRunId)?.parent;
      if (!parent) return;
      const body = { expected_version: parent.version, idempotency_key: `delegation-ui:${action}:${parentRunId}:${parent.version}` };
      if (action === "cancel") body.reason = "user_requested";
      const response = await fetch(`${apiBase()}/v1/runs/${encodeURIComponent(parentRunId)}/${action}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      if (!response.ok) throw new Error(`${response.status} ${await response.text()}`);
      await loadDelegationRun(parentRunId);
    }

    async function cancelDelegationRun(parentRunId) { try { await mutateDelegationRun(parentRunId, "cancel"); } catch (error) { appendError(`取消任务失败：${error.message}`); } }
    async function resumeDelegationRun(parentRunId) { try { await mutateDelegationRun(parentRunId, "resume"); } catch (error) { appendError(`继续任务失败：${error.message}`); } }

    function capabilityButton(label, onClick, options = {}) {
      const button = capabilityElement("button", options.className || "", label);
      button.type = "button";
      button.disabled = Boolean(options.disabled);
      if (options.ariaLabel) button.setAttribute("aria-label", options.ariaLabel);
      button.addEventListener("click", onClick);
      return button;
    }

    function appendCapabilityField(container, label, value) {
      const term = capabilityElement("dt", "", label);
      const description = capabilityElement(
        "dd",
        "",
        value === null || value === undefined || value === "" ? "—" : value
      );
      container.append(term, description);
    }

    function renderCapabilityError(error) {
      const message = error?.message || "能力管理请求失败";
      capabilityGlobalError.textContent = message;
      capabilityGlobalError.hidden = false;
    }

    function clearCapabilityError() {
      capabilityGlobalError.textContent = "";
      capabilityGlobalError.hidden = true;
    }

    function updateCapabilityRefreshTime() {
      capabilityState.lastRefreshAt = new Date();
      capabilityRefreshTime.textContent =
        `最后刷新：${capabilityState.lastRefreshAt.toLocaleString()}`;
    }

    function renderCapabilitySkeleton(target) {
      target.replaceChildren();
      const skeleton = capabilityElement("div", "capability-skeleton");
      skeleton.setAttribute("aria-label", "正在加载");
      for (let index = 0; index < 4; index += 1) {
        skeleton.appendChild(capabilityElement("span"));
      }
      target.appendChild(skeleton);
    }

    function renderCapabilityEmpty(target, message) {
      target.replaceChildren(
        capabilityElement("div", "capability-empty", message)
      );
    }

    function renderCapabilityStale(container, message) {
      container.appendChild(
        capabilityElement(
          "div",
          "capability-stale",
          message || "当前显示的是上次成功加载的权威状态。"
        )
      );
    }

    async function capabilityRequest(path, options = {}) {
      const response = await fetch(`${apiBase()}${path}`, options);
      let payload = null;
      try {
        payload = await response.json();
      } catch (_error) {
        payload = null;
      }
      if (!response.ok) {
        const detail = payload?.detail;
        const message = detail?.message || payload?.message ||
          `能力管理请求失败（HTTP ${response.status}）`;
        const error = new Error(message);
        error.code = detail?.code || `http_${response.status}`;
        error.authoritativeState = detail?.authoritative_state || null;
        throw error;
      }
      return payload || {};
    }

    async function capabilityMutation(path, revision, extra = {}, method = "POST") {
      return capabilityRequest(path, {
        method,
        headers: {
          "Content-Type": "application/json",
          "If-Match": String(revision)
        },
        body: JSON.stringify({ expected_revision: revision, ...extra })
      });
    }

    async function trustRequest(path, options = {}) {
      const response = await fetch(`${apiBase()}${path}`, options);
      let payload = null;
      try {
        payload = await response.json();
      } catch (_error) {
        payload = null;
      }
      if (!response.ok) {
        const detail = payload?.detail;
        const message = detail?.message || payload?.message ||
          `Trust Center 请求失败（HTTP ${response.status}）`;
        const error = new Error(message);
        error.code = detail?.code || `http_${response.status}`;
        throw error;
      }
      return payload || {};
    }

    function advanceTrustRequestEpoch() {
      trustState.requestEpoch += 1;
      trustState.requestController.abort();
      trustState.requestController = new AbortController();
    }

    function captureTrustRequest() {
      return {
        epoch: trustState.requestEpoch,
        route: trustState.route,
        apiBase: apiBase(),
        overlayToken: shellState.captureOverlayRequest()
      };
    }

    function isTrustRequestCurrent(token) {
      return shellState.isOverlayRequestCurrent(token.overlayToken)
        && token.epoch === trustState.requestEpoch
        && token.route === trustState.route
        && token.apiBase === apiBase();
    }

    function captureTrustRead(lane, selection) {
      return trustReadOwnership.capture({
        ...shellState.captureOverlayRequest(),
        epoch: trustState.requestEpoch,
        route: trustState.route,
        apiBase: apiBase(),
        lane,
        selection
      });
    }

    function isTrustReadCurrent(token, selection) {
      return trustReadOwnership.isCurrent(token, {
        ...shellState.captureOverlayRequest(),
        epoch: trustState.requestEpoch,
        route: trustState.route,
        apiBase: apiBase(),
        lane: token.lane,
        selection
      });
    }

    function setTrustStatus(message, isError = false) {
      trustStatus.textContent = message;
      trustStatus.style.color = isError ? "var(--warn)" : "var(--muted)";
    }

    function renderTrustError(error) {
      trustError.textContent = error?.message || "Trust Center 请求失败";
      trustError.hidden = false;
      setTrustStatus(trustError.textContent, true);
    }

    function clearTrustError() {
      trustError.textContent = "";
      trustError.hidden = true;
    }

    function trustField(label, value) {
      const row = capabilityElement("div", "capability-meta-row");
      row.append(
        capabilityElement("strong", "", label),
        capabilityElement("span", "", value === null || value === undefined || value === "" ? "—" : value)
      );
      return row;
    }

    function renderTrustEmpty(target, message) {
      target.replaceChildren(capabilityElement("div", "capability-empty", message));
    }

    function renderTrustOptions(select, values, placeholder) {
      select.replaceChildren();
      const empty = capabilityElement("option", "", placeholder);
      empty.value = "";
      select.appendChild(empty);
      for (const value of values) {
        const option = capabilityElement("option", "", value.label);
        option.value = value.id;
        select.appendChild(option);
      }
    }

    function updateTrustTabs() {
      const route = trustState.route;
      trustEvalsTab.setAttribute("aria-selected", route === "evals" ? "true" : "false");
      trustTracesTab.setAttribute("aria-selected", route === "traces" ? "true" : "false");
      trustSafetyTab.setAttribute("aria-selected", route === "safety" ? "true" : "false");
      trustEvalsPanel.hidden = route !== "evals";
      trustTracesPanel.hidden = route !== "traces";
      trustSafetyPanel.hidden = route !== "safety";
    }

    function setTrustRoute(route) {
      if (!['evals', 'traces', 'safety'].includes(route)) return;
      const routeChanged = trustState.route !== route;
      if (routeChanged) advanceTrustRequestEpoch();
      trustState.route = route;
      updateTrustTabs();
      if (advancedWindow.activeType() === "trust") void refreshTrustRoute();
    }

    function renderTrustEvalRuns() {
      trustEvalRuns.replaceChildren();
      if (!trustState.runs.length) {
        renderTrustEmpty(trustEvalRuns, "暂无 Eval Run。运行固定评测会先创建真实后端 Run。");
        return;
      }
      for (const run of trustState.runs) {
        const card = capabilityElement("article", "capability-card");
        const title = capabilityElement("h3", "", `${run.id} · ${run.status}`);
        const actions = capabilityElement("div", "capability-actions");
        actions.append(
          capabilityButton("查看证据", () => {
            trustState.selectedRunId = run.id;
            void loadTrustRunEvidence(run.id);
          }),
          capabilityElement("button", "", "取消运行（待后端支持）")
        );
        actions.lastChild.type = "button";
        actions.lastChild.disabled = true;
        card.append(
          title,
          trustField("类型", run.run_type),
          trustField("Suite", run.suite_id),
          trustField("代码版本", run.code_version),
          trustField("Policy", run.policy_version),
          trustField("开始时间", run.started_at),
          actions
        );
        trustEvalRuns.appendChild(card);
      }
    }

    function renderTrustEvalCases() {
      trustEvalCases.replaceChildren();
      if (trustState.metrics.length) {
        const metricTitle = capabilityElement("h3", "", "指标");
        trustEvalCases.appendChild(metricTitle);
        for (const metric of trustState.metrics) {
          trustEvalCases.appendChild(
            trustField(metric.name, metric.value ?? metric.missing_reason)
          );
        }
      }
      if (trustState.caseResults.length) {
        trustEvalCases.appendChild(capabilityElement("h3", "", "Case Results"));
        for (const result of trustState.caseResults) {
          trustEvalCases.appendChild(
            trustField(result.case_id, `${result.status} · ${result.error_code || "no_error"}`)
          );
        }
      }
      if (!trustState.metrics.length && !trustState.caseResults.length) {
        renderTrustEmpty(trustEvalCases, "选择 Run 后显示 Case、Assertion、Metric 与 Gate 证据。");
      }
    }

    function renderTrustFailureClusters(gate = null) {
      trustFailureClusters.replaceChildren();
      if (gate) {
        trustFailureClusters.append(
          capabilityElement("h3", "", "Release Gate"),
          trustField("状态", gate.status),
          trustField("安全阻塞", gate.safety_blocking ? "是" : "否"),
          trustField("阻塞原因", (gate.blocking_reasons || []).join("；"))
        );
      }
      if (!trustState.failureClusters.length) {
        trustFailureClusters.appendChild(
          capabilityElement("div", "capability-empty", "暂无失败簇。")
        );
        return;
      }
      for (const cluster of trustState.failureClusters) {
        const card = capabilityElement("article", "capability-card");
        card.append(
          capabilityElement("h3", "", cluster.id),
          trustField("Root Cause", cluster.root_cause),
          trustField("严重级别", cluster.severity),
          trustField("案例", (cluster.case_ids || []).join(", "))
        );
        trustFailureClusters.appendChild(card);
      }
    }

    function renderTrustEvals() {
      renderTrustOptions(
        trustSuiteSelect,
        trustState.suites.map(suite => ({ id: suite.id, label: `${suite.id} · ${suite.version}` })),
        "选择 Suite"
      );
      renderTrustOptions(
        trustCompareBaseRun,
        trustState.runs.map(run => ({ id: run.id, label: `${run.id} · ${run.status}` })),
        "基线 Run"
      );
      renderTrustOptions(
        trustCompareCandidateRun,
        trustState.runs.map(run => ({ id: run.id, label: `${run.id} · ${run.status}` })),
        "对比 Run"
      );
      renderTrustEvalRuns();
      renderTrustEvalCases();
      renderTrustFailureClusters();
      if (trustState.suites.length || trustState.runs.length || trustState.cases.length) {
        setTrustStatus(
          `Suites ${trustState.suites.length} · Runs ${trustState.runs.length} · Cases ${trustState.cases.length}`
        );
      } else {
        setTrustStatus("真实后端暂无 Trust Eval 数据");
      }
    }

    async function loadTrustRunEvidence(runId) {
      const request = captureTrustRequest();
      const trustRead = captureTrustRead("run-evidence", runId);
      if (!isTrustRequestCurrent(request) || !isTrustReadCurrent(trustRead, runId)) return;
      clearTrustError();
      setTrustStatus(`正在加载 ${runId} 的报告证据...`);
      try {
        const [caseResults, metrics, clusters, gateResult] = await Promise.allSettled([
          trustRequest(`/v1/trust/runs/${encodeURIComponent(runId)}/case-results`, { signal: trustState.requestController.signal }),
          trustRequest(`/v1/trust/runs/${encodeURIComponent(runId)}/metrics`, { signal: trustState.requestController.signal }),
          trustRequest(`/v1/trust/runs/${encodeURIComponent(runId)}/failure-clusters`, { signal: trustState.requestController.signal }),
          trustRequest(`/v1/trust/runs/${encodeURIComponent(runId)}/gate`, { signal: trustState.requestController.signal })
        ]);
        if (!isTrustRequestCurrent(request) || !isTrustReadCurrent(trustRead, runId)) return;
        trustState.caseResults = caseResults.status === "fulfilled"
          ? caseResults.value.case_results || []
          : [];
        trustState.metrics = metrics.status === "fulfilled"
          ? metrics.value.metrics || []
          : [];
        trustState.failureClusters = clusters.status === "fulfilled"
          ? clusters.value.failure_clusters || []
          : [];
        const gate = gateResult.status === "fulfilled" ? gateResult.value.gate : null;
        renderTrustEvalCases();
        renderTrustFailureClusters(gate);
        setTrustStatus(`已加载 ${runId} 的证据；Gate 缺失会按后端原样显示为空。`);
      } catch (error) {
        if (error.name !== "AbortError" && isTrustRequestCurrent(request) && isTrustReadCurrent(trustRead, runId)) renderTrustError(error);
      }
    }

    async function loadTrustEvals(overlayToken = shellState.captureOverlayRequest()) {
      if (!shellState.isOverlayRequestCurrent(overlayToken)) return;
      const request = captureTrustRequest();
      const trustRead = captureTrustRead("evals", "fixture");
      if (!isTrustRequestCurrent(request) || !isTrustReadCurrent(trustRead, "fixture")) return;
      clearTrustError();
      setTrustStatus("正在加载固定评测状态...");
      renderCapabilitySkeleton(trustEvalRuns);
      try {
        const [suites, cases, runs] = await Promise.all([
          trustRequest("/v1/trust/suites", { signal: trustState.requestController.signal }),
          trustRequest("/v1/trust/cases", { signal: trustState.requestController.signal }),
          trustRequest("/v1/trust/runs?run_type=fixture", { signal: trustState.requestController.signal })
        ]);
        if (!isTrustRequestCurrent(request) || !isTrustReadCurrent(trustRead, "fixture")) return;
        trustState.suites = suites.suites || [];
        trustState.cases = cases.cases || [];
        trustState.runs = runs.runs || [];
        renderTrustEvals();
      } catch (error) {
        if (error.name !== "AbortError" && isTrustRequestCurrent(request) && isTrustReadCurrent(trustRead, "fixture")) renderTrustError(error);
      }
    }

    async function startTrustEvalRun() {
      const suiteId = trustSuiteSelect.value || trustState.suites[0]?.id;
      if (!suiteId) {
        renderTrustError(new Error("没有可运行的 Suite；请先通过后端写入 Eval Suite。"));
        return;
      }
      trustStartRunButton.disabled = true;
      clearTrustError();
      setTrustStatus("正在创建真实后端 Eval Run...");
      const runId = `ui-fixture-${Date.now()}`;
      try {
        await trustRequest("/v1/trust/runs", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            id: runId,
            suite_id: suiteId,
            run_type: "fixture",
            code_version: "ui-requested",
            code_dirty: true,
            prompt_version: "ui-requested",
            skill_version: "ui-requested",
            tool_schema_version: "ui-requested",
            policy_version: "ui-requested",
            fixture_manifest_hash: null
          })
        });
        await loadTrustEvals();
        trustState.selectedRunId = runId;
        setTrustStatus(`已创建 Run ${runId}；执行进度以真实后端状态为准。`);
      } catch (error) {
        renderTrustError(error);
      } finally {
        trustStartRunButton.disabled = false;
      }
    }

    function renderTrustTraces() {
      trustTraceEvents.replaceChildren();
      if (!trustState.traces.length) {
        renderTrustEmpty(trustTraceEvents, "暂无 Trace；可按 Run / Case / Session / Turn / Tool 过滤。");
        return;
      }
      for (const event of trustState.traces) {
        const card = capabilityElement("article", "capability-card");
        card.append(
          capabilityElement("h3", "", `${event.event_type} · ${event.id}`),
          trustField("Run / Case", `${event.eval_run_id || "—"} / ${event.case_id || "—"}`),
          trustField("Session / Turn", `${event.session_id || "—"} / ${event.turn_id || "—"}`),
          trustField("Model / Tool", `${event.model_request_id || "—"} / ${event.tool_call_id || "—"}`),
          trustField("Policy / Approval", `${event.policy_decision_id || "—"} / ${event.approval_id || "—"}`),
          trustField("状态", event.status),
          trustField("摘要", event.summary)
        );
        trustTraceEvents.appendChild(card);
      }
      setTrustStatus(`已加载 ${trustState.traces.length} 条 Trace 事件`);
    }

    async function loadTrustTraces(overlayToken = shellState.captureOverlayRequest()) {
      if (!shellState.isOverlayRequestCurrent(overlayToken)) return;
      const params = new URLSearchParams();
      const filters = [
        ["eval_run_id", trustTraceRunFilter.value],
        ["case_id", trustTraceCaseFilter.value],
        ["session_id", trustTraceSessionFilter.value],
        ["turn_id", trustTraceTurnFilter.value],
        ["tool_call_id", trustTraceToolFilter.value]
      ];
      for (const [key, value] of filters) {
        if (value.trim()) params.set(key, value.trim());
      }
      const suffix = params.toString() ? `?${params.toString()}` : "";
      const request = captureTrustRequest();
      const trustRead = captureTrustRead("traces", suffix);
      if (!isTrustRequestCurrent(request) || !isTrustReadCurrent(trustRead, suffix)) return;
      clearTrustError();
      setTrustStatus("正在加载 Trace...");
      renderCapabilitySkeleton(trustTraceEvents);
      try {
        const payload = await trustRequest(`/v1/trust/traces${suffix}`, {
          signal: trustState.requestController.signal
        });
        if (!isTrustRequestCurrent(request) || !isTrustReadCurrent(trustRead, suffix)) return;
        trustState.traces = payload.traces || [];
        renderTrustTraces();
      } catch (error) {
        if (error.name !== "AbortError" && isTrustRequestCurrent(request) && isTrustReadCurrent(trustRead, suffix)) renderTrustError(error);
      }
    }

    function renderTrustSafety() {
      trustSafetyPolicy.replaceChildren();
      trustSafetyEvidence.replaceChildren();
      const safety = trustState.safety || {};
      trustSafetyGate.textContent = safety.gate_status || "unknown";
      trustSafetyPolicy.append(
        trustField("策略版本", safety.policy_version),
        trustField("Gate 状态", safety.gate_status),
        trustField("BLOCKED 原因", (safety.blocking_reasons || []).join("；"))
      );
      const evidence = safety.evidence || [];
      if (!evidence.length) {
        renderTrustEmpty(trustSafetyEvidence, "暂无 Safety Gate 阻塞证据。unknown 不是 PASS。");
        return;
      }
      for (const item of evidence) {
        const card = capabilityElement("article", "capability-card");
        card.append(
          capabilityElement("h3", "", item.run_id || item.id || "Gate Evidence"),
          trustField("状态", item.status),
          trustField("安全阻塞", item.safety_blocking ? "是" : "否"),
          trustField("阻塞原因", (item.blocking_reasons || []).join("；"))
        );
        trustSafetyEvidence.appendChild(card);
      }
      setTrustStatus(`Safety Gate: ${safety.gate_status || "unknown"}`);
    }

    async function loadTrustSafety(overlayToken = shellState.captureOverlayRequest()) {
      if (!shellState.isOverlayRequestCurrent(overlayToken)) return;
      const request = captureTrustRequest();
      const trustRead = captureTrustRead("safety", "safety");
      if (!isTrustRequestCurrent(request) || !isTrustReadCurrent(trustRead, "safety")) return;
      clearTrustError();
      setTrustStatus("正在加载 Safety 状态...");
      renderCapabilitySkeleton(trustSafetyEvidence);
      try {
        const payload = await trustRequest("/v1/trust/safety", {
          signal: trustState.requestController.signal
        });
        if (!isTrustRequestCurrent(request) || !isTrustReadCurrent(trustRead, "safety")) return;
        trustState.safety = payload;
        renderTrustSafety();
      } catch (error) {
        if (error.name !== "AbortError" && isTrustRequestCurrent(request) && isTrustReadCurrent(trustRead, "safety")) renderTrustError(error);
      }
    }

    async function refreshTrustRoute() {
      const overlayToken = shellState.captureOverlayRequest();
      if (!shellState.isOverlayRequestCurrent(overlayToken)) return;
      updateTrustTabs();
      if (trustState.route === "traces") {
        await loadTrustTraces(overlayToken);
      } else if (trustState.route === "safety") {
        await loadTrustSafety(overlayToken);
      } else {
        await loadTrustEvals(overlayToken);
      }
    }

    function capabilityServerActionPath(serverId, action) {
      const encoded = encodeURIComponent(serverId);
      const paths = {
        connect: `/v1/capabilities/servers/${encoded}/connect`,
        disconnect: `/v1/capabilities/servers/${encoded}/disconnect`,
        enable: `/v1/capabilities/servers/${encoded}/enable`,
        disable: `/v1/capabilities/servers/${encoded}/disable`,
        "health-check": `/v1/capabilities/servers/${encoded}/health-check`
      };
      return paths[action];
    }

    function capabilityToolActionPath(toolName, action) {
      const encoded = encodeURIComponent(toolName);
      const paths = {
        enable: `/v1/capabilities/tools/${encoded}/enable`,
        disable: `/v1/capabilities/tools/${encoded}/disable`
      };
      return paths[action];
    }

    function capabilitySkillActionPath(skillName, action) {
      const encoded = encodeURIComponent(skillName);
      const paths = {
        enable: `/v1/capabilities/skills/${encoded}/enable`,
        disable: `/v1/capabilities/skills/${encoded}/disable`
      };
      return paths[action];
    }

    function isCapabilityOperationPending(key) {
      return capabilityState.pendingOperations.has(key);
    }

    async function withCapabilityOperation(key, operation) {
      if (isCapabilityOperationPending(key)) return;
      const request = captureCapabilityRequest();
      capabilityState.pendingOperations.add(key);
      renderCurrentCapabilityState();
      try {
        return await operation();
      } finally {
        capabilityState.pendingOperations.delete(key);
        if (isCapabilityRequestCurrent(request)) renderCurrentCapabilityState();
      }
    }

    function renderCurrentCapabilityState() {
      if (capabilityState.route === "skills") {
        renderCapabilitySkills();
        const detail = capabilityState.skillDetails.get(
          capabilityState.selectedSkillName
        );
        if (detail) renderCapabilitySkillDetail(detail);
      } else {
        renderCapabilityServers();
        const detail = capabilityState.serverDetails.get(
          capabilityState.selectedServerId
        );
        if (detail) renderCapabilityServerDetail(detail);
      }
    }

    function renderCapabilityServers() {
      capabilityList.replaceChildren();
      if (!capabilityState.servers.length) {
        renderCapabilityEmpty(capabilityList, "暂无 MCP Server");
        return;
      }
      for (const server of capabilityState.servers) {
        const selected = server.id === capabilityState.selectedServerId;
        const item = capabilityButton(
          server.name || server.id,
          async () => {
            advanceCapabilityRequestEpoch();
            capabilityState.selectedServerId = server.id;
            capabilityState.selectedToolName = null;
            renderCapabilityServers();
            await loadCapabilityServer(server.id, true);
          },
          {
            className: "capability-list-item",
            ariaLabel: `查看 MCP Server ${server.name || server.id}`
          }
        );
        item.setAttribute("aria-current", selected ? "true" : "false");
        item.appendChild(
          capabilityElement(
            "span",
            "capability-meta",
            `${server.connection_state} · ${server.health_state} · revision ${server.revision}`
          )
        );
        const badge = capabilityElement(
          "span",
          "capability-badge",
          server.enabled ? "已启用" : "已停用"
        );
        item.appendChild(badge);
        capabilityList.appendChild(item);
      }
    }

    function renderCapabilityServerDetail(payload) {
      const server = payload.server;
      const snapshot = payload.snapshot;
      const tools = payload.tools || [];
      capabilityDetail.replaceChildren();
      capabilityDetail.appendChild(
        capabilityElement("h2", "", server.name || server.id)
      );
      if (snapshot?.stale || snapshot?.error) {
        renderCapabilityStale(
          capabilityDetail,
          snapshot.error || "此 Server 的能力快照已过期。"
        );
      }

      const pendingPrefix = `server:${server.id}:`;
      const serverBusy = isCapabilityTargetLocked(`server:${server.id}`)
        || [...capabilityState.pendingOperations].some(
          key => key.startsWith(pendingPrefix)
        );
      const actions = capabilityElement("div", "capability-actions");
      const connected = server.connection_state === "ready";
      actions.append(
        capabilityButton(
          connected ? "Disconnect" : "Connect",
          () => mutateCapabilityServer(
            server.id,
            connected ? "disconnect" : "connect"
          ),
          { disabled: serverBusy }
        ),
        capabilityButton(
          server.enabled ? "Disable" : "Enable",
          () => mutateCapabilityServer(
            server.id,
            server.enabled ? "disable" : "enable"
          ),
          { disabled: serverBusy }
        ),
        capabilityButton(
          "Health check",
          () => mutateCapabilityServer(server.id, "health-check"),
          { disabled: serverBusy }
        ),
        capabilityButton(
          "Refresh",
          () => refreshCapabilityServer(server.id),
          { disabled: serverBusy }
        )
      );
      capabilityDetail.appendChild(actions);

      const statusGrid = capabilityElement("dl", "capability-status-grid");
      appendCapabilityField(statusGrid, "Connection", server.connection_state);
      appendCapabilityField(statusGrid, "Health", server.health_state);
      appendCapabilityField(statusGrid, "Operation", server.operation_state);
      appendCapabilityField(statusGrid, "Revision", server.revision);
      appendCapabilityField(statusGrid, "Transport", server.transport);
      appendCapabilityField(statusGrid, "Runtime", [
        server.runtime_name,
        server.runtime_version
      ].filter(Boolean).join(" "));
      appendCapabilityField(statusGrid, "Last checked", server.last_checked_at);
      appendCapabilityField(
        statusGrid,
        "Last error",
        server.last_error || server.error_code
      );
      capabilityDetail.appendChild(statusGrid);

      const toolsSection = capabilityElement(
        "section",
        "capability-subsection"
      );
      toolsSection.appendChild(capabilityElement("h3", "", `Tools (${tools.length})`));
      const toolList = capabilityElement("div", "capability-tools-list");
      toolList.setAttribute("aria-label", "Server tools");
      toolsSection.appendChild(toolList);
      if (!tools.length) {
        renderCapabilityEmpty(toolList, "此快照没有可见 Tool。");
      }
      for (const tool of tools) {
        const toolButton = capabilityButton(
          tool.name,
          async () => {
            advanceCapabilityRequestEpoch();
            capabilityState.selectedToolName = tool.name;
            await loadCapabilityTool(tool.name, true);
          },
          { className: "capability-list-item" }
        );
        toolButton.appendChild(
          capabilityElement(
            "span",
            "capability-meta",
            `${tool.review || "unreviewed"} · ${tool.enabled ? "enabled" : "disabled"}`
          )
        );
        toolList.appendChild(toolButton);
      }
      capabilityDetail.appendChild(toolsSection);

      const resources = capabilityElement("section", "capability-subsection");
      resources.append(
        capabilityElement(
          "h3",
          "",
          `Resources (${snapshot?.resource_count || 0})`
        ),
        capabilityElement(
          "p",
          "capability-note",
          "Task11 API 当前仅提供 Resource 数量，不提供资源明细。"
        )
      );
      capabilityDetail.appendChild(resources);

      const prompts = capabilityElement("section", "capability-subsection");
      prompts.append(
        capabilityElement(
          "h3",
          "",
          `Prompts (${snapshot?.prompt_count || 0})`
        ),
        capabilityElement(
          "p",
          "capability-note",
          "Task11 API 当前仅提供 Prompt 数量，不提供提示词明细。"
        )
      );
      capabilityDetail.appendChild(prompts);

      const selectedTool = capabilityState.toolDetails.get(
        capabilityState.selectedToolName
      );
      if (selectedTool?.tool?.server_id === server.id) {
        capabilityDetail.appendChild(renderCapabilityToolDetail(selectedTool, true));
      }
    }

    function renderCapabilityToolDetail(payload, returnNode = false) {
      const tool = payload.tool;
      const root = capabilityElement("section", "capability-subsection");
      root.appendChild(capabilityElement("h3", "", `Tool · ${tool.name}`));
      const operationKey = `tool:${tool.name}`;
      const toolBusy = isCapabilityOperationPending(operationKey)
        || isCapabilityTargetLocked(operationKey);
      const actions = capabilityElement("div", "capability-actions");
      actions.append(
        capabilityButton(
          tool.enabled ? "Disable Tool" : "Enable Tool",
          () => mutateCapabilityTool(
            tool.name,
            tool.enabled ? "disable" : "enable"
          ),
          { disabled: toolBusy }
        ),
        capabilityButton(
          "Approve review",
          () => reviewCapabilityTool(tool.name, "approved"),
          { disabled: toolBusy }
        ),
        capabilityButton(
          "Require review",
          () => reviewCapabilityTool(tool.name, "review_required"),
          { disabled: toolBusy }
        )
      );
      root.appendChild(actions);

      const fields = capabilityElement("dl", "capability-status-grid");
      appendCapabilityField(fields, "Risk", tool.risk_level);
      appendCapabilityField(fields, "Review", tool.review_state);
      appendCapabilityField(fields, "Revision", tool.revision);
      appendCapabilityField(fields, "Schema hash", tool.schema_hash);
      appendCapabilityField(
        fields,
        "Context exposure",
        tool.enabled && tool.connected && tool.review_state === "approved"
          ? "当前可进入模型工具上下文"
          : "当前不会进入模型工具上下文"
      );
      root.appendChild(fields);

      root.appendChild(capabilityElement("h3", "", "Policy scope"));
      if (!(payload.policies || []).length) {
        root.appendChild(
          capabilityElement("p", "capability-note", "没有持久化 Policy rule。")
        );
      }
      for (const policy of payload.policies || []) {
        const policyBlock = capabilityElement("div", "capability-code");
        policyBlock.textContent = JSON.stringify({
          id: policy.id,
          effect: policy.effect,
          schemes: policy.schemes,
          domains: policy.domains,
          actions: policy.actions,
          data_classes: policy.data_classes,
          roles: policy.roles,
          enabled: policy.enabled,
          revision: policy.revision
        }, null, 2);
        root.appendChild(policyBlock);
      }

      root.appendChild(capabilityElement("h3", "", "Tool Schema"));
      const schema = capabilityElement("pre", "capability-code");
      schema.textContent = JSON.stringify(payload.schema?.schema || tool.schema || {}, null, 2);
      root.appendChild(schema);
      if (returnNode) return root;
      capabilityDetail.replaceChildren(root);
      return root;
    }

    function renderCapabilitySkills() {
      capabilityList.replaceChildren();
      if (capabilityState.skillsStale) {
        renderCapabilityStale(
          capabilityList,
          capabilityState.skillsLastError ||
            "Skill registry 刷新失败，保留上次成功定义。"
        );
      }
      if (!capabilityState.skills.length) {
        capabilityList.appendChild(
          capabilityElement("div", "capability-empty", "暂无 Skill")
        );
        return;
      }
      for (const skill of capabilityState.skills) {
        const selected = skill.name === capabilityState.selectedSkillName;
        const item = capabilityButton(
          skill.name,
          async () => {
            advanceCapabilityRequestEpoch();
            capabilityState.selectedSkillName = skill.name;
            renderCapabilitySkills();
            await loadCapabilitySkill(skill.name, true);
          },
          {
            className: "capability-list-item",
            ariaLabel: `查看 Skill ${skill.name}`
          }
        );
        item.setAttribute("aria-current", selected ? "true" : "false");
        item.append(
          capabilityElement("span", "capability-meta", skill.description),
          capabilityElement(
            "span",
            "capability-badge",
            skill.enabled ? "已启用" : "已停用"
          )
        );
        capabilityList.appendChild(item);
      }
    }

    function appendCapabilityStringList(container, title, values, fallback) {
      const section = capabilityElement("section", "capability-subsection");
      section.appendChild(capabilityElement("h3", "", title));
      if (!Array.isArray(values) || !values.length) {
        section.appendChild(capabilityElement("p", "capability-note", fallback));
      } else {
        const list = capabilityElement("ul");
        for (const value of values) {
          list.appendChild(capabilityElement("li", "", value));
        }
        section.appendChild(list);
      }
      container.appendChild(section);
    }

    function clearCapabilityRawState() {
      const nextLease = (capabilityState.rawState?.lease || 0) + 1;
      capabilityState.rawState = {
        skillName: null,
        expanded: false,
        status: "idle",
        definition: null,
        lease: nextLease
      };
      renderCurrentCapabilityState();
    }

    function renderCapabilityRawDefinition(container, skillName) {
      const section = capabilityElement("section", "capability-subsection");
      section.appendChild(
        capabilityElement("h3", "", "管理员 Raw definition")
      );
      const rawState = capabilityState.rawState;
      const active = rawState.skillName === skillName && rawState.expanded;
      if (!active) {
        section.append(
          capabilityElement(
            "p",
            "capability-note",
            "仅管理员可按需加载完整定义；普通详情不会读取或缓存 Raw definition。"
          ),
          capabilityButton(
            "管理员：展开完整定义",
            () => loadCapabilityRawDefinition(skillName)
          )
        );
      } else if (rawState.status === "loading") {
        section.appendChild(
          capabilityElement("p", "capability-note", "正在加载完整定义…")
        );
      } else if (rawState.status === "ready" && rawState.definition !== null) {
        section.appendChild(
          capabilityElement(
            "p",
            "capability-note",
            "以下是管理员端点返回的完整定义，包含示例、验证规则与失败策略。"
          )
        );
        const definition = capabilityElement("pre", "capability-code");
        definition.textContent = rawState.definition;
        section.appendChild(definition);
        section.appendChild(
          capabilityButton(
            "收起并清除",
            () => {
              clearCapabilityRawState();
              const detail = capabilityState.skillDetails.get(skillName);
              if (detail) renderCapabilitySkillDetail(detail);
            }
          )
        );
      }
      container.appendChild(section);
    }

    async function loadCapabilityRawDefinition(skillName) {
      clearCapabilityRawState();
      const lease = capabilityState.rawState.lease;
      capabilityState.rawState = {
        skillName,
        expanded: true,
        status: "loading",
        definition: null,
        lease
      };
      const detail = capabilityState.skillDetails.get(skillName);
      if (detail) renderCapabilitySkillDetail(detail);
      const request = captureCapabilityRequest();
      try {
        const raw = await capabilityRequest(
          `/v1/capabilities/skills/${encodeURIComponent(skillName)}/raw`,
          { signal: capabilityState.requestController.signal }
        );
        if (
          !isCapabilityRequestCurrent(request)
          || capabilityState.rawState.lease !== lease
          || capabilityState.rawState.skillName !== skillName
        ) return;
        capabilityState.rawState = {
          skillName,
          expanded: true,
          status: "ready",
          definition: raw.definition,
          lease
        };
        const latest = capabilityState.skillDetails.get(skillName);
        if (latest) renderCapabilitySkillDetail(latest);
      } catch (error) {
        const current = isCapabilityRequestCurrent(request)
          && capabilityState.rawState.lease === lease;
        clearCapabilityRawState();
        const latest = capabilityState.skillDetails.get(skillName);
        if (current && !isCapabilityAbort(error)) {
          renderCapabilityError(error);
          if (latest) renderCapabilitySkillDetail(latest);
        }
      }
    }

    function renderCapabilitySkillDetail(payload) {
      const skill = payload.skill;
      const record = payload.record;
      const health = payload.health || {};
      capabilityDetail.replaceChildren();
      capabilityDetail.appendChild(capabilityElement("h2", "", skill.name));
      if (
        capabilityState.skillsStale ||
        record?.load_state === "stale" ||
        health.last_error
      ) {
        renderCapabilityStale(
          capabilityDetail,
          health.last_error || capabilityState.skillsLastError ||
            "Skill 定义已过期，当前保留上次成功加载的定义。"
        );
      }

      const revision = record?.revision ?? capabilityState.skillRegistryRevision;
      const operationKey = `skill:${skill.name}`;
      const skillBusy = isCapabilityOperationPending(operationKey)
        || isCapabilityTargetLocked(operationKey);
      const actions = capabilityElement("div", "capability-actions");
      actions.append(
        capabilityButton(
          skill.enabled ? "Disable Skill" : "Enable Skill",
          () => mutateCapabilitySkill(
            skill.name,
            skill.enabled ? "disable" : "enable",
            revision
          ),
          { disabled: skillBusy }
        ),
        capabilityButton(
          "Reload Skill",
          () => reloadCapabilitySkill(skill.name),
          {
            className: "capability-danger",
            disabled: skillBusy
          }
        )
      );
      capabilityDetail.appendChild(actions);

      const fields = capabilityElement("dl", "capability-status-grid");
      appendCapabilityField(fields, "Description", skill.description);
      appendCapabilityField(fields, "Version", skill.version);
      appendCapabilityField(fields, "Source", skill.source);
      appendCapabilityField(fields, "Dependency state", skill.dependency_state);
      appendCapabilityField(fields, "Load state", record?.load_state);
      appendCapabilityField(fields, "Revision", revision);
      appendCapabilityField(fields, "Snapshot hash", skill.snapshot_hash);
      appendCapabilityField(fields, "Last error", health.last_error);
      capabilityDetail.appendChild(fields);

      appendCapabilityStringList(
        capabilityDetail,
        "Dependencies",
        (skill.dependencies || []).map(
          dependency => `${dependency.kind}:${dependency.name}` +
            (dependency.required ? "（必需）" : "（可选）")
        ),
        "没有声明依赖。"
      );
      appendCapabilityStringList(
        capabilityDetail,
        "Missing dependencies",
        skill.missing_dependencies,
        "没有缺失依赖。"
      );
      renderCapabilityRawDefinition(capabilityDetail, skill.name);
    }

    async function loadCapabilityServers(overlayToken = shellState.captureOverlayRequest()) {
      if (!shellState.isOverlayRequestCurrent(overlayToken)) return;
      const request = captureCapabilityRequest();
      const requestApplies = capabilityState.route === "mcp-servers";
      if (!capabilityState.servers.length) {
        renderCapabilitySkeleton(capabilityList);
        renderCapabilitySkeleton(capabilityDetail);
      }
      clearCapabilityError();
      try {
        const payload = await capabilityRequest(
          "/v1/capabilities/servers",
          { signal: capabilityState.requestController.signal }
        );
        if (!requestApplies || !isCapabilityRequestCurrent(request)) return;
        capabilityState.servers = payload.servers || [];
        if (!capabilityState.servers.some(
          server => server.id === capabilityState.selectedServerId
        )) {
          capabilityState.selectedServerId = capabilityState.servers[0]?.id || null;
          capabilityState.selectedToolName = null;
        }
        renderCapabilityServers();
        if (capabilityState.selectedServerId) {
          await loadCapabilityServer(capabilityState.selectedServerId);
        } else {
          renderCapabilityEmpty(capabilityDetail, "选择一个 MCP Server 查看详情");
        }
      } catch (error) {
        if (
          isCapabilityAbort(error)
          || !requestApplies
          || !isCapabilityRequestCurrent(request)
        ) return;
        renderCapabilityError(error);
        renderCapabilityServers();
        if (capabilityState.servers.length) {
          renderCapabilityStale(capabilityList, error.message);
        }
      } finally {
        if (requestApplies && isCapabilityRequestCurrent(request)) {
          updateCapabilityRefreshTime();
        }
      }
    }

    async function loadCapabilityServer(serverId, focusDetail = false) {
      const request = captureCapabilityRequest();
      const requestApplies = capabilityState.route === "mcp-servers"
        && capabilityState.selectedServerId === serverId;
      try {
        const [payload, catalog] = await Promise.all([
          capabilityRequest(
            `/v1/capabilities/servers/${encodeURIComponent(serverId)}`,
            { signal: capabilityState.requestController.signal }
          ),
          capabilityRequest(
            "/v1/capabilities/tools",
            { signal: capabilityState.requestController.signal }
          )
        ]);
        if (!requestApplies || !isCapabilityRequestCurrent(request)) return;
        capabilityState.tools = catalog.capabilities || [];
        payload.tools = capabilityState.tools.filter(
          tool => tool.server === serverId
        );
        capabilityState.serverDetails.set(serverId, payload);
        const serverIndex = capabilityState.servers.findIndex(
          server => server.id === serverId
        );
        if (serverIndex >= 0) {
          capabilityState.servers.splice(serverIndex, 1, payload.server);
        }
        renderCapabilityServers();
        if (capabilityState.selectedServerId === serverId) {
          renderCapabilityServerDetail(payload);
          if (focusDetail) capabilityDetail.focus();
        }
      } catch (error) {
        if (
          isCapabilityAbort(error)
          || !requestApplies
          || !isCapabilityRequestCurrent(request)
        ) return;
        renderCapabilityError(error);
        const cached = capabilityState.serverDetails.get(serverId);
        if (cached) {
          renderCapabilityServerDetail(cached);
          renderCapabilityStale(capabilityDetail, error.message);
        }
      } finally {
        if (requestApplies && isCapabilityRequestCurrent(request)) {
          updateCapabilityRefreshTime();
        }
      }
    }

    async function loadCapabilityTool(toolName, focusDetail = false) {
      const request = captureCapabilityRequest();
      const requestApplies = capabilityState.route === "mcp-servers"
        && capabilityState.selectedToolName === toolName;
      try {
        const [detail, schema, policies] = await Promise.all([
          capabilityRequest(
            `/v1/capabilities/tools/${encodeURIComponent(toolName)}`,
            { signal: capabilityState.requestController.signal }
          ),
          capabilityRequest(
            `/v1/capabilities/tools/${encodeURIComponent(toolName)}/schema`,
            { signal: capabilityState.requestController.signal }
          ),
          capabilityRequest(
            `/v1/capabilities/tools/${encodeURIComponent(toolName)}/policies`,
            { signal: capabilityState.requestController.signal }
          )
        ]);
        if (!requestApplies || !isCapabilityRequestCurrent(request)) return;
        const payload = {
          tool: detail.tool,
          schema,
          policies: policies.policies || []
        };
        capabilityState.toolDetails.set(toolName, payload);
        const serverPayload = capabilityState.serverDetails.get(
          detail.tool.server_id
        );
        if (serverPayload && capabilityState.selectedServerId === detail.tool.server_id) {
          renderCapabilityServerDetail(serverPayload);
        } else {
          renderCapabilityToolDetail(payload);
        }
        if (focusDetail) capabilityDetail.focus();
      } catch (error) {
        if (
          isCapabilityAbort(error)
          || !requestApplies
          || !isCapabilityRequestCurrent(request)
        ) return;
        renderCapabilityError(error);
        const cached = capabilityState.toolDetails.get(toolName);
        if (cached) renderCapabilityToolDetail(cached);
      } finally {
        if (requestApplies && isCapabilityRequestCurrent(request)) {
          updateCapabilityRefreshTime();
        }
      }
    }

    async function loadCapabilitySkills(overlayToken = shellState.captureOverlayRequest()) {
      if (!shellState.isOverlayRequestCurrent(overlayToken)) return;
      const request = captureCapabilityRequest();
      const requestApplies = capabilityState.route === "skills";
      if (!capabilityState.skills.length) {
        renderCapabilitySkeleton(capabilityList);
        renderCapabilitySkeleton(capabilityDetail);
      }
      clearCapabilityError();
      try {
        const payload = await capabilityRequest(
          "/v1/capabilities/skills",
          { signal: capabilityState.requestController.signal }
        );
        if (!requestApplies || !isCapabilityRequestCurrent(request)) return;
        capabilityState.skillRegistryRevision = payload.revision || 0;
        capabilityState.skills = payload.skills || [];
        capabilityState.skillsStale = Boolean(payload.stale);
        capabilityState.skillsLastError = payload.last_error || null;
        if (!capabilityState.skills.some(
          skill => skill.name === capabilityState.selectedSkillName
        )) {
          capabilityState.selectedSkillName = capabilityState.skills[0]?.name || null;
        }
        renderCapabilitySkills();
        if (capabilityState.selectedSkillName) {
          await loadCapabilitySkill(capabilityState.selectedSkillName);
        } else {
          renderCapabilityEmpty(capabilityDetail, "选择一个 Skill 查看详情");
        }
      } catch (error) {
        if (
          isCapabilityAbort(error)
          || !requestApplies
          || !isCapabilityRequestCurrent(request)
        ) return;
        capabilityState.skillsStale = true;
        capabilityState.skillsLastError = error.message;
        renderCapabilityError(error);
        renderCapabilitySkills();
      } finally {
        if (requestApplies && isCapabilityRequestCurrent(request)) {
          updateCapabilityRefreshTime();
        }
      }
    }

    async function loadCapabilitySkill(skillName, focusDetail = false) {
      const request = captureCapabilityRequest();
      const requestApplies = capabilityState.route === "skills"
        && capabilityState.selectedSkillName === skillName;
      try {
        const [detail, health] = await Promise.all([
          capabilityRequest(
            `/v1/capabilities/skills/${encodeURIComponent(skillName)}`,
            { signal: capabilityState.requestController.signal }
          ),
          capabilityRequest(
            `/v1/capabilities/skills/${encodeURIComponent(skillName)}/health`,
            { signal: capabilityState.requestController.signal }
          )
        ]);
        if (!requestApplies || !isCapabilityRequestCurrent(request)) return;
        const payload = {
          skill: detail.skill,
          record: detail.record,
          health
        };
        capabilityState.skillDetails.set(skillName, payload);
        if (capabilityState.selectedSkillName === skillName) {
          renderCapabilitySkillDetail(payload);
          if (focusDetail) capabilityDetail.focus();
        }
      } catch (error) {
        if (
          isCapabilityAbort(error)
          || !requestApplies
          || !isCapabilityRequestCurrent(request)
        ) return;
        renderCapabilityError(error);
        const cached = capabilityState.skillDetails.get(skillName);
        if (cached) {
          renderCapabilitySkillDetail(cached);
          renderCapabilityStale(capabilityDetail, error.message);
        }
      } finally {
        if (requestApplies && isCapabilityRequestCurrent(request)) {
          updateCapabilityRefreshTime();
        }
      }
    }

    async function mutateCapabilityServer(serverId, action) {
      if (isCapabilityTargetLocked(`server:${serverId}`)) return;
      const request = captureCapabilityRequest();
      const key = `server:${serverId}:${action}`;
      await withCapabilityOperation(key, async () => {
        const cached = capabilityState.serverDetails.get(serverId);
        const revision = cached?.server?.revision;
        if (revision === undefined) return;
        try {
          const result = await capabilityMutation(
            capabilityServerActionPath(serverId, action),
            revision
          );
          if (isCapabilityRequestCurrent(request)) {
            if (result.confirmation) renderCapabilityConfirmation(result.confirmation);
            clearCapabilityError();
          }
        } catch (error) {
          if (isCapabilityRequestCurrent(request)) renderCapabilityError(error);
        } finally {
          await loadCapabilityServer(serverId);
        }
      });
    }

    async function refreshCapabilityServer(serverId) {
      if (isCapabilityTargetLocked(`server:${serverId}`)) return;
      const request = captureCapabilityRequest();
      const key = `server:${serverId}:refresh`;
      await withCapabilityOperation(key, async () => {
        const cached = capabilityState.serverDetails.get(serverId);
        const revision = cached?.server?.revision;
        if (revision === undefined) return;
        try {
          const result = await capabilityMutation(
            `/v1/capabilities/servers/${encodeURIComponent(serverId)}/refresh`,
            revision
          );
          if (isCapabilityRequestCurrent(request)) {
            if (result.confirmation) renderCapabilityConfirmation(result.confirmation);
            clearCapabilityError();
          }
        } catch (error) {
          if (isCapabilityRequestCurrent(request)) renderCapabilityError(error);
        } finally {
          await loadCapabilityServer(serverId);
        }
      });
    }

    async function mutateCapabilityTool(toolName, action) {
      const key = `tool:${toolName}`;
      if (isCapabilityTargetLocked(key)) return;
      const request = captureCapabilityRequest();
      await withCapabilityOperation(key, async () => {
        const cached = capabilityState.toolDetails.get(toolName);
        const revision = cached?.tool?.revision;
        if (revision === undefined) return;
        try {
          const result = await capabilityMutation(
            capabilityToolActionPath(toolName, action),
            revision
          );
          if (isCapabilityRequestCurrent(request)) {
            if (result.confirmation) renderCapabilityConfirmation(result.confirmation);
            clearCapabilityError();
          }
        } catch (error) {
          if (isCapabilityRequestCurrent(request)) renderCapabilityError(error);
        } finally {
          await loadCapabilityTool(toolName);
        }
      });
    }

    async function reviewCapabilityTool(toolName, reviewState) {
      const key = `tool:${toolName}`;
      if (isCapabilityTargetLocked(key)) return;
      const request = captureCapabilityRequest();
      await withCapabilityOperation(key, async () => {
        const revision = capabilityState.toolDetails.get(toolName)?.tool?.revision;
        if (revision === undefined) return;
        try {
          await capabilityMutation(
            `/v1/capabilities/tools/${encodeURIComponent(toolName)}/review`,
            revision,
            { review_state: reviewState }
          );
          if (isCapabilityRequestCurrent(request)) clearCapabilityError();
        } catch (error) {
          if (isCapabilityRequestCurrent(request)) renderCapabilityError(error);
        } finally {
          await loadCapabilityTool(toolName);
        }
      });
    }

    async function mutateCapabilitySkill(skillName, action, revision) {
      const key = `skill:${skillName}`;
      if (isCapabilityTargetLocked(key)) return;
      const request = captureCapabilityRequest();
      await withCapabilityOperation(key, async () => {
        try {
          await capabilityMutation(
            capabilitySkillActionPath(skillName, action),
            revision
          );
          if (isCapabilityRequestCurrent(request)) clearCapabilityError();
        } catch (error) {
          if (isCapabilityRequestCurrent(request)) renderCapabilityError(error);
        } finally {
          await loadCapabilitySkills();
          await loadCapabilitySkill(skillName);
        }
      });
    }

    async function reloadCapabilitySkill(skillName) {
      const key = `skill:${skillName}`;
      if (isCapabilityTargetLocked(key)) return;
      const request = captureCapabilityRequest();
      await withCapabilityOperation(key, async () => {
        try {
          const result = await capabilityMutation(
            `/v1/capabilities/skills/${encodeURIComponent(skillName)}/reload`,
            capabilityState.skillRegistryRevision
          );
          if (isCapabilityRequestCurrent(request)) {
            if (result.confirmation) renderCapabilityConfirmation(result.confirmation);
            clearCapabilityError();
          }
        } catch (error) {
          if (isCapabilityRequestCurrent(request)) renderCapabilityError(error);
        } finally {
          await loadCapabilitySkills();
          await loadCapabilitySkill(skillName);
        }
      });
    }

    function capabilityDisplayValue(value) {
      if (value === null || value === undefined || value === "") return "—";
      if (typeof value === "string") return value;
      try {
        return JSON.stringify(value);
      } catch (_error) {
        return String(value);
      }
    }

    function clearCapabilityConfirmations() {
      capabilityState.confirmations.clear();
      capabilityState.confirmationDecisionLocks.clear();
      capabilityState.confirmationIdempotencyKeys.clear();
      capabilityState.confirmationProposalIds.clear();
      if (capabilityState.confirmationExpiryTimer) {
        clearTimeout(capabilityState.confirmationExpiryTimer);
        capabilityState.confirmationExpiryTimer = null;
      }
      renderCapabilityConfirmations();
      renderCurrentCapabilityState();
    }

    function pruneExpiredCapabilityConfirmations() {
      const now = Date.now();
      for (const [id, confirmation] of capabilityState.confirmations) {
        const expiresAt = Date.parse(confirmation.expires_at || "");
        if (Number.isFinite(expiresAt) && expiresAt <= now) {
          capabilityState.confirmations.delete(id);
          capabilityState.confirmationDecisionLocks.delete(id);
          capabilityState.confirmationIdempotencyKeys.delete(id);
          capabilityState.confirmationProposalIds.delete(id);
        }
      }
    }

    function scheduleCapabilityConfirmationExpiry() {
      if (capabilityState.confirmationExpiryTimer) {
        clearTimeout(capabilityState.confirmationExpiryTimer);
        capabilityState.confirmationExpiryTimer = null;
      }
      const expiries = [...capabilityState.confirmations.values()]
        .map(item => Date.parse(item.expires_at || ""))
        .filter(value => Number.isFinite(value) && value > Date.now());
      if (!expiries.length) return;
      const delay = Math.min(Math.min(...expiries) - Date.now() + 20, 2_147_000_000);
      capabilityState.confirmationExpiryTimer = setTimeout(() => {
        const expired = [...capabilityState.confirmations.values()].filter(
          item => {
            const expiresAt = Date.parse(item.expires_at || "");
            return Number.isFinite(expiresAt) && expiresAt <= Date.now();
          }
        );
        pruneExpiredCapabilityConfirmations();
        renderCapabilityConfirmations();
        renderCurrentCapabilityState();
        void (async () => {
          await loadCapabilityConfirmations();
          for (const confirmation of expired) {
            await refreshCapabilityAuthorityForConfirmation(confirmation);
          }
        })();
      }, Math.max(20, delay));
    }

    function isCapabilityTargetLocked(targetKey) {
      pruneExpiredCapabilityConfirmations();
      const locked = [...capabilityState.confirmations.values()]
        .map(item => CapabilityUiLogic.confirmationTargetKey(item));
      return CapabilityUiLogic.isTargetLocked(targetKey, locked);
    }

    function addCapabilityConfirmation(confirmation) {
      if (
        !confirmation?.id
        || !CapabilityUiLogic.isManagementConfirmation(confirmation)
      ) return;
      capabilityState.confirmations.set(confirmation.id, confirmation);
      capabilityState.confirmationProposalIds.add(confirmation.id);
      pruneExpiredCapabilityConfirmations();
      renderCapabilityConfirmations();
      renderCurrentCapabilityState();
      scheduleCapabilityConfirmationExpiry();
    }

    function renderCapabilityConfirmation(confirmation) {
      addCapabilityConfirmation(confirmation);
    }

    function renderCapabilityConfirmations() {
      pruneExpiredCapabilityConfirmations();
      capabilityConfirmation.replaceChildren();
      const confirmations = [...capabilityState.confirmations.values()];
      capabilityConfirmation.hidden = confirmations.length === 0;
      for (const confirmation of confirmations) {
        const model = CapabilityUiLogic.confirmationDetails(confirmation);
        const card = capabilityElement("section", "capability-subsection");
        card.appendChild(
          capabilityElement("h3", "", "需要管理员确认")
        );
        const fields = capabilityElement("dl", "capability-status-grid");
        appendCapabilityField(fields, "Operation", model.operation);
        appendCapabilityField(fields, "Data destination", model.destination);
        appendCapabilityField(fields, "Risk", model.risk);
        appendCapabilityField(
          fields,
          "Impact",
          model.impact.length ? model.impact.join(" · ") : "—"
        );
        card.appendChild(fields);

        const diffTitle = capabilityElement("strong", "", "Changes");
        card.appendChild(diffTitle);
        if (!model.diff.length) {
          card.appendChild(
            capabilityElement("p", "capability-note", "没有字段差异。")
          );
        }
        for (const change of model.diff) {
          const row = capabilityElement("div", "capability-code");
          const field = capabilityElement("strong", "", change.field);
          const values = capabilityElement(
            "div",
            "",
            `${capabilityDisplayValue(change.before)} → ` +
              capabilityDisplayValue(change.after)
          );
          row.append(field, values);
          card.appendChild(row);
        }

        const dataTitle = capabilityElement("strong", "", "Data");
        card.appendChild(dataTitle);
        const data = capabilityElement("pre", "capability-code");
        data.textContent = JSON.stringify(model.data, null, 2);
        card.appendChild(data);

        const deciding = capabilityState.confirmationDecisionLocks.has(
          confirmation.id
        );
        const actions = capabilityElement("div", "capability-actions");
        actions.append(
          capabilityButton(
            "确认并执行",
            () => decideCapabilityConfirmation(confirmation, "once"),
            { disabled: deciding }
          ),
          capabilityButton(
            "取消",
            () => decideCapabilityConfirmation(confirmation, "cancel"),
            { disabled: deciding }
          )
        );
        card.appendChild(actions);
        capabilityConfirmation.appendChild(card);
      }
    }

    async function loadCapabilityConfirmations() {
      const request = captureCapabilityRequest();
      const proposalIdsAtRequest = new Set(
        capabilityState.confirmationProposalIds
      );
      try {
        const payload = await capabilityRequest(
          "/v1/capabilities/confirmations/pending?session_id=management",
          { signal: capabilityState.requestController.signal }
        );
        if (!isCapabilityRequestCurrent(request)) return;
        const arrivedAfterRequest = [...capabilityState.confirmationProposalIds]
          .filter(id => !proposalIdsAtRequest.has(id));
        const reconciled = CapabilityUiLogic.reconcileManagementConfirmations(
          payload.confirmations,
          [...capabilityState.confirmations.values()],
          arrivedAfterRequest
        );
        capabilityState.confirmations = new Map(
          reconciled.map(item => [item.id, item])
        );
        capabilityState.confirmationProposalIds = new Set(arrivedAfterRequest);
        for (const confirmation of payload.confirmations || []) {
          capabilityState.confirmationProposalIds.delete(confirmation?.id);
        }
        renderCapabilityConfirmations();
        renderCurrentCapabilityState();
        scheduleCapabilityConfirmationExpiry();
      } catch (error) {
        if (
          !isCapabilityAbort(error)
          && isCapabilityRequestCurrent(request)
        ) {
          renderCapabilityError(error);
        }
      }
    }

    async function refreshCapabilityAuthorityForConfirmation(confirmation) {
      const summary = confirmation.arguments_summary || {};
      const operation = summary.operation || "";
      const target = summary.target;
      if (!target) return;
      const request = captureCapabilityAuthorityRequest();
      try {
        if (operation.startsWith("server.")) {
          const payload = await capabilityRequest(
            `/v1/capabilities/servers/${encodeURIComponent(target)}`
          );
          if (!isCapabilityAuthorityRequestCurrent(request)) return;
          const cached = capabilityState.serverDetails.get(target);
          capabilityState.serverDetails.set(target, {
            ...(cached || {}),
            ...payload
          });
          const index = capabilityState.servers.findIndex(
            server => server.id === target
          );
          if (index >= 0 && payload.server) {
            capabilityState.servers.splice(index, 1, payload.server);
          }
        } else if (operation.startsWith("tool.")) {
          const [detail, schema, policies] = await Promise.all([
            capabilityRequest(
              `/v1/capabilities/tools/${encodeURIComponent(target)}`
            ),
            capabilityRequest(
              `/v1/capabilities/tools/${encodeURIComponent(target)}/schema`
            ),
            capabilityRequest(
              `/v1/capabilities/tools/${encodeURIComponent(target)}/policies`
            )
          ]);
          if (!isCapabilityAuthorityRequestCurrent(request)) return;
          capabilityState.toolDetails.set(target, {
            tool: detail.tool,
            schema,
            policies: policies.policies || []
          });
        } else if (operation.startsWith("skill.")) {
          const [detail, health] = await Promise.all([
            capabilityRequest(
              `/v1/capabilities/skills/${encodeURIComponent(target)}`
            ),
            capabilityRequest(
              `/v1/capabilities/skills/${encodeURIComponent(target)}/health`
            )
          ]);
          if (!isCapabilityAuthorityRequestCurrent(request)) return;
          capabilityState.skillDetails.set(target, {
            skill: detail.skill,
            record: detail.record,
            health
          });
          const index = capabilityState.skills.findIndex(
            skill => skill.name === target
          );
          if (index >= 0 && detail.skill) {
            capabilityState.skills.splice(index, 1, detail.skill);
          }
        }
      } catch (_error) {
        // A later foreground refresh reports errors; background reconciliation
        // must never overwrite or redraw the current route.
      }
    }

    async function decideCapabilityConfirmation(confirmation, decision) {
      if (!CapabilityUiLogic.isManagementConfirmation(confirmation)) return;
      if (capabilityState.confirmationDecisionLocks.has(confirmation.id)) return;
      const request = captureCapabilityRequest();
      capabilityState.confirmationDecisionLocks.add(confirmation.id);
      renderCapabilityConfirmations();
      const idempotencySlot = `${confirmation.id}:${decision}`;
      let idempotencyKey = capabilityState.confirmationIdempotencyKeys.get(
        idempotencySlot
      );
      if (!idempotencyKey) {
        idempotencyKey = globalThis.crypto?.randomUUID?.() ||
          `capability-${Date.now()}-${Math.random()}`;
        capabilityState.confirmationIdempotencyKeys.set(
          idempotencySlot,
          idempotencyKey
        );
      }
      try {
        const decisionPayload = decision === "once"
          ? { decision: "once" }
          : { decision: "cancel" };
        await capabilityMutation(
          `/v1/capabilities/confirmations/${encodeURIComponent(confirmation.id)}/decisions`,
          confirmation.revision,
          {
            ...decisionPayload,
            idempotency_key: idempotencyKey
          }
        );
        if (isCapabilityRequestCurrent(request)) {
          capabilityState.confirmations.delete(confirmation.id);
          capabilityState.confirmationIdempotencyKeys.delete(idempotencySlot);
          clearCapabilityError();
        }
      } catch (error) {
        if (isCapabilityRequestCurrent(request)) renderCapabilityError(error);
      } finally {
        capabilityState.confirmationDecisionLocks.delete(confirmation.id);
        await loadCapabilityConfirmations();
        await refreshCapabilityAuthorityForConfirmation(confirmation);
        if (isCapabilityRequestCurrent(request)) {
          renderCapabilityConfirmations();
          renderCurrentCapabilityState();
        }
      }
    }

    async function refreshCapabilityRoute() {
      const overlayToken = shellState.captureOverlayRequest();
      if (!shellState.isOverlayRequestCurrent(overlayToken)) return;
      capabilityRefreshButton.disabled = true;
      try {
        if (capabilityState.route === "skills") {
          await loadCapabilitySkills(overlayToken);
        } else {
          await loadCapabilityServers(overlayToken);
        }
      } finally {
        if (shellState.isOverlayRequestCurrent(overlayToken)) {
          capabilityRefreshButton.disabled = false;
        }
      }
    }

    function updateCapabilityTabs() {
      capabilityServersTab.setAttribute(
        "aria-selected",
        capabilityState.route === "mcp-servers" ? "true" : "false"
      );
      capabilitySkillsTab.setAttribute(
        "aria-selected",
        capabilityState.route === "skills" ? "true" : "false"
      );
    }

    function navigatePrimaryHash(hash) {
      if (window.location.hash === hash) {
        void applyPrimaryHashRoute();
      } else {
        window.location.hash = hash;
      }
    }

    const workbenchCenterScroll = document.querySelector(".workspace-scroll-region");
    const workbenchContextScroll = document.querySelector("#workbenchContextContent");
    let renderedPrimaryRoute = shellState.snapshot().primaryRoute;

    function rememberRouteScroll(route) {
      shellState.rememberScroll(`${route}:center`, workbenchCenterScroll.scrollTop);
      shellState.rememberScroll(`${route}:context`, workbenchContextScroll.scrollTop);
    }

    function restoreRouteScroll(route) {
      workbenchCenterScroll.scrollTop = shellState.scrollFor(`${route}:center`);
      workbenchContextScroll.scrollTop = shellState.scrollFor(`${route}:context`);
    }

    const routeActivation = createRouteActivationCoordinator({
      onStart(route) {
        rememberRouteScroll(renderedPrimaryRoute);
        shellState.navigate(route);
        workbenchPageTab.setAttribute("aria-current", route === "workbench" ? "page" : "false");
        versionMapPageTab.setAttribute("aria-current", route === "version-map" ? "page" : "false");
        applicationsPageTab.setAttribute("aria-current", route === "applications" ? "page" : "false");
      },
      activate: route => workbenchShell.activate(route),
      onActivated(route) {
        renderedPrimaryRoute = route;
      },
      onCurrent(route) {
        restoreRouteScroll(route);
      },
    });

    function setCapabilityRoute(route) {
      if (!["mcp-servers", "skills"].includes(route)) return;
      const routeChanged = capabilityState.route !== route;
      if (routeChanged) advanceCapabilityRequestEpoch();
      capabilityState.route = route;
      updateCapabilityTabs();
      if (advancedWindow.activeType() === "capabilities") {
        void refreshCapabilityRoute();
        void loadCapabilityConfirmations();
      }
    }

    async function applyPrimaryHashRoute() {
      const route = resolveShellRoute(window.location.hash, requestedRoute);
      await routeActivation.apply(route);
    }

    async function openAdvancedWindow(type, trigger) {
      const replacingType = advancedWindow.activeType();
      if (replacingType && replacingType !== type) {
        advancedWindow.replace(type, nextType => shellState.openOverlay(nextType));
      } else {
        shellState.openOverlay(type);
        advancedWindow.open(type, trigger);
      }
      if (type === "knowledge") await loadKnowledgeBase();
      if (type === "capabilities") {
        await refreshCapabilityRoute();
        await loadCapabilityConfirmations();
      }
      if (type === "trust") await refreshTrustRoute();
    }

    function setKnowledgeStatus(message, isError = false) {
      knowledgeStatus.textContent = message;
      knowledgeStatus.style.color = isError ? "var(--warn)" : "var(--muted)";
    }

    async function knowledgeError(response, fallback) {
      let detail = null;
      try { detail = await response.json(); } catch (_error) { /* no body */ }
      return detail?.detail?.message || detail?.message || fallback;
    }

    async function loadKnowledgeBase(overlayToken = shellState.captureOverlayRequest()) {
      if (!shellState.isOverlayRequestCurrent(overlayToken)) return;
      setKnowledgeStatus("正在加载知识库...");
      knowledgeDocumentList.replaceChildren();
      try {
        const basesResponse = await fetch(`${apiBase()}/v1/knowledge-bases`);
        if (!shellState.isOverlayRequestCurrent(overlayToken)) return;
        if (!basesResponse.ok) throw new Error(await knowledgeError(basesResponse, "知识库加载失败"));
        const bases = await basesResponse.json();
        activeKnowledgeBaseId = bases.knowledge_bases[0]?.id || null;
        if (!activeKnowledgeBaseId) throw new Error("没有可用知识库");
        const response = await fetch(`${apiBase()}/v1/knowledge-bases/${activeKnowledgeBaseId}/documents`);
        if (!shellState.isOverlayRequestCurrent(overlayToken)) return;
        if (!response.ok) throw new Error(await knowledgeError(response, "文档列表加载失败"));
        const payload = await response.json();
        if (!shellState.isOverlayRequestCurrent(overlayToken)) return;
        knowledgeDocuments = payload.documents;
        const existingDocumentIds = new Set(knowledgeDocuments.map(item => item.id));
        selectedKnowledgeDocumentIds = new Set(
          [...selectedKnowledgeDocumentIds].filter(id => existingDocumentIds.has(id))
        );
        if (
          selectedKnowledgeDocumentId
          && !knowledgeDocuments.some(item => item.id === selectedKnowledgeDocumentId)
        ) {
          selectedKnowledgeDocumentId = null;
          resetKnowledgeChunkPreview();
        }
        renderKnowledgeDocuments(knowledgeDocuments);
        setKnowledgeStatus(`已加载 ${knowledgeDocuments.length} 份文档`);
      } catch (error) {
        if (shellState.isOverlayRequestCurrent(overlayToken)) {
          setKnowledgeStatus(error.message || "知识库加载失败", true);
        }
      }
    }

    function renderKnowledgeDocuments(documents) {
      knowledgeDocumentList.replaceChildren();
      knowledgeDocumentCount.textContent = documents.length
        ? `${documents.length} 份文档`
        : "";
      if (!documents.length) {
        const empty = document.createElement("p");
        empty.textContent = "暂无文档";
        knowledgeDocumentList.appendChild(empty);
        updateKnowledgeBulkActions();
        return;
      }
      for (const item of documents) {
        const row = document.createElement("article");
        row.className = "knowledge-document";
        row.classList.toggle("is-selected", item.id === selectedKnowledgeDocumentId);
        row.setAttribute("aria-current", item.id === selectedKnowledgeDocumentId ? "true" : "false");
        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.checked = selectedKnowledgeDocumentIds.has(item.id);
        checkbox.setAttribute("aria-label", `选择 ${item.filename}`);
        checkbox.addEventListener("change", () => toggleKnowledgeDocumentSelection(item.id, checkbox.checked));
        const body = document.createElement("div");
        body.className = "knowledge-document-body";
        const title = document.createElement("strong");
        title.className = "knowledge-document-title";
        title.textContent = item.filename;
        const meta = document.createElement("div");
        meta.className = "knowledge-meta";
        meta.textContent = `版本 ${item.version || "-"} · ${item.status} · Chunk ${item.chunk_count} · ${item.created_at}`;
        const actions = document.createElement("div");
        actions.className = "knowledge-actions";
        const preview = document.createElement("button");
        preview.type = "button";
        preview.textContent = "查看 Chunk";
        preview.addEventListener("click", () => loadKnowledgeChunks(item));
        const update = document.createElement("button");
        update.type = "button";
        update.textContent = "更新";
        update.addEventListener("click", () => chooseKnowledgeUpdate(item));
        const remove = document.createElement("button");
        remove.type = "button";
        remove.textContent = "删除";
        remove.addEventListener("click", () => deleteKnowledgeDocument(item));
        actions.append(preview, update, remove);
        body.append(title, meta, actions);
        row.append(checkbox, body);
        knowledgeDocumentList.appendChild(row);
      }
      updateKnowledgeBulkActions();
    }

    function updateKnowledgeBulkActions() {
      const total = knowledgeDocuments.length;
      const selectedCount = selectedKnowledgeDocumentIds.size;
      knowledgeDeleteSelectedButton.disabled = selectedKnowledgeDocumentIds.size === 0;
      knowledgeDeleteSelectedButton.textContent = selectedCount
        ? `删除选中 (${selectedCount})`
        : "删除选中";
      knowledgeSelectAllDocuments.disabled = total === 0;
      knowledgeSelectAllDocuments.checked = total > 0 && selectedCount === total;
      knowledgeSelectAllDocuments.indeterminate = selectedCount > 0 && selectedCount < total;
    }

    function toggleKnowledgeDocumentSelection(documentId, selected) {
      if (selected) selectedKnowledgeDocumentIds.add(documentId);
      else selectedKnowledgeDocumentIds.delete(documentId);
      updateKnowledgeBulkActions();
    }

    function resetKnowledgeChunkPreview(message = "选择文档查看 Chunk") {
      knowledgeChunkTitle.textContent = "Chunk 预览";
      knowledgeChunkPreview.replaceChildren();
      knowledgeChunkPreview.className = "knowledge-chunk-empty";
      knowledgeChunkPreview.textContent = message;
    }

    function showKnowledgeChunkMessage(item, message, isError = false) {
      knowledgeChunkTitle.textContent = `${item.filename} · Chunk`;
      knowledgeChunkPreview.replaceChildren();
      knowledgeChunkPreview.className = "knowledge-chunk-empty";
      knowledgeChunkPreview.textContent = message;
      if (isError) knowledgeChunkPreview.style.color = "var(--warn)";
      else knowledgeChunkPreview.style.removeProperty("color");
    }

    async function loadKnowledgeChunks(item) {
      selectedKnowledgeDocumentId = item.id;
      renderKnowledgeDocuments(knowledgeDocuments);
      showKnowledgeChunkMessage(item, "正在加载 Chunk...");
      setKnowledgeStatus(`正在加载 ${item.filename} 的 Chunk...`);
      try {
        const response = await fetch(`${apiBase()}/v1/knowledge-bases/${activeKnowledgeBaseId}/documents/${item.id}/chunks`);
        if (!response.ok) {
          throw new Error(await knowledgeError(response, "Chunk 预览失败"));
        }
        const payload = await response.json();
        knowledgeChunkPreview.replaceChildren();
        knowledgeChunkPreview.className = "";
        knowledgeChunkPreview.style.removeProperty("color");
        if (!payload.chunks.length) {
          showKnowledgeChunkMessage(item, "该文档暂无 Chunk");
        } else {
          for (const chunk of payload.chunks) {
            const block = document.createElement("section");
            block.className = "knowledge-chunk";
            const meta = document.createElement("strong");
            meta.textContent = `${chunk.id} · ${(chunk.section_path || []).join(" / ")} · L${chunk.start_line}-L${chunk.end_line}`;
            const text = document.createElement("div");
            text.textContent = chunk.preview;
            block.append(meta, text);
            knowledgeChunkPreview.appendChild(block);
          }
        }
        setKnowledgeStatus(`已显示 ${payload.chunks.length} 个 Chunk`);
      } catch (error) {
        const message = error.message || "Chunk 预览失败";
        showKnowledgeChunkMessage(item, message, true);
        setKnowledgeStatus(message, true);
      }
    }

    async function submitKnowledgeFile(file, item = null) {
      const data = new FormData();
      data.append("file", file);
      data.append("confirmed_authorized", "true");
      if (!item) data.append("document_type", knowledgeDocumentType.value);
      const url = item
        ? `${apiBase()}/v1/knowledge-bases/${activeKnowledgeBaseId}/documents/${item.id}/content`
        : `${apiBase()}/v1/knowledge-bases/${activeKnowledgeBaseId}/documents`;
      const options = { method: item ? "PUT" : "POST", body: data, headers: {} };
      if (item) options.headers["If-Match"] = item.content_sha256;
      const response = await fetch(url, options);
      if (!response.ok) {
        const label = item ? "更新失败" : "上传失败";
        throw new Error(await knowledgeError(response, label));
      }
      const result = await response.json();
      const jobResponse = await fetch(`${apiBase()}/v1/knowledge-bases/${activeKnowledgeBaseId}/ingestion-jobs/${result.job_id}`);
      const job = jobResponse.ok ? await jobResponse.json() : null;
      if (job?.status === "failed") throw new Error(`解析或索引失败：${job.error_code || job.stage}`);
    }

    async function chooseKnowledgeUpdate(item) {
      const picker = document.createElement("input");
      picker.type = "file";
      picker.accept = ".md,.markdown";
      picker.addEventListener("change", async () => {
        if (!picker.files[0]) return;
        try {
          setKnowledgeStatus(`正在更新 ${item.filename}...`);
          await submitKnowledgeFile(picker.files[0], item);
          await loadKnowledgeBase();
        } catch (error) {
          setKnowledgeStatus(error.message || "更新失败", true);
        }
      });
      picker.click();
    }

    async function deleteKnowledgeDocumentRequest(item) {
      const response = await fetch(
        `${apiBase()}/v1/knowledge-bases/${activeKnowledgeBaseId}/documents/${item.id}`,
        { method: "DELETE" }
      );
      if (!response.ok) {
        throw new Error(await knowledgeError(response, "删除失败"));
      }
      if (selectedKnowledgeDocumentId === item.id) {
        selectedKnowledgeDocumentId = null;
        resetKnowledgeChunkPreview();
      }
      selectedKnowledgeDocumentIds.delete(item.id);
    }

    async function deleteKnowledgeDocument(item) {
      if (!window.confirm(`确认删除“${item.filename}”版本 ${item.version}？删除后旧内容不可检索。`)) return;
      try {
        await deleteKnowledgeDocumentRequest(item);
      } catch (error) {
        setKnowledgeStatus(error.message || "删除失败", true);
        return;
      }
      await loadKnowledgeBase();
    }

    async function deleteSelectedKnowledgeDocuments() {
      const selectedItems = knowledgeDocuments.filter(
        item => selectedKnowledgeDocumentIds.has(item.id)
      );
      if (!selectedItems.length) return;
      if (!window.confirm(`确认删除选中的 ${selectedItems.length} 份文档？删除后旧内容不可检索。`)) return;
      knowledgeDeleteSelectedButton.disabled = true;
      setKnowledgeStatus(`正在删除 ${selectedItems.length} 份文档...`);
      const failures = [];
      for (const item of selectedItems) {
        try {
          await deleteKnowledgeDocumentRequest(item);
        } catch (error) {
          failures.push(`${item.filename}: ${error.message || "删除失败"}`);
        }
      }
      selectedKnowledgeDocumentIds.clear();
      await loadKnowledgeBase();
      if (failures.length) {
        setKnowledgeStatus(`有 ${failures.length} 份文档删除失败：${failures.join("；")}`, true);
      } else {
        setKnowledgeStatus(`已删除 ${selectedItems.length} 份文档`);
      }
    }

    knowledgeUploadForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!activeKnowledgeBaseId || !knowledgeFile.files[0] || !knowledgeAuthorized.checked) return;
      knowledgeUploadButton.disabled = true;
      try {
        setKnowledgeStatus("正在上传并建立索引...");
        await submitKnowledgeFile(knowledgeFile.files[0]);
        knowledgeUploadForm.reset();
        await loadKnowledgeBase();
      } catch (error) {
        setKnowledgeStatus(error.message || "上传失败", true);
      } finally {
        knowledgeUploadButton.disabled = false;
      }
    });
    knowledgeSelectAllDocuments.addEventListener("change", () => {
      if (knowledgeSelectAllDocuments.checked) {
        selectedKnowledgeDocumentIds = new Set(knowledgeDocuments.map(item => item.id));
      } else {
        selectedKnowledgeDocumentIds.clear();
      }
      renderKnowledgeDocuments(knowledgeDocuments);
    });
    knowledgeDeleteSelectedButton.addEventListener("click", deleteSelectedKnowledgeDocuments);
    workbenchPageTab.addEventListener(
      "click",
      () => navigatePrimaryHash("#/workbench")
    );
    versionMapPageTab.addEventListener(
      "click",
      () => navigatePrimaryHash("#/version-map")
    );
    applicationsPageTab.addEventListener(
      "click",
      () => navigatePrimaryHash("#/applications")
    );
    window.addEventListener("workbench-context-change", event => {
      const epoch = event.detail?.context_epoch;
      if (state.isSending && state.activeWorkbenchEpoch && epoch !== state.activeWorkbenchEpoch) {
        state.workbenchContextChanged = true;
      }
      const context = event.detail || {};
      const parts = [
        context.workspace_id && "当前求职档案",
        context.resume_version_id && "已载入简历",
        context.job_snapshot_id && "已选择 JD",
        context.match_analysis_id && "已有匹配分析",
      ].filter(Boolean);
      document.querySelector("#workbenchAgentContext").textContent = parts.length
        ? `当前上下文：${parts.join(" · ")}`
        : "导入简历后即可直接在这里提问；添加 JD 后可进行匹配分析。";
    });
    document.querySelector("#workbenchAgentActions").addEventListener("click", async event => {
      const actionButton = event.target.closest("[data-agent-action]");
      const action = actionButton?.dataset.agentAction;
      if (!action) return;
      const context = getWorkbenchContext();
      const card = document.querySelector("#workbenchAgentActionCard");
      card.replaceChildren();
      if (!context?.workspace_id && action !== "prepare_resume") { card.textContent = "请先建立简历档案。"; return; }
      const chatPrompts = {
        prepare_resume: "我还没有建立简历档案。请给我一份精简的准备清单，告诉我上传简历前应该整理哪些教育、实习、项目、技能和可量化成果。",
        ai_edit_resume: "请基于当前工作台中已载入的简历，先指出最值得修改的三处，再为每一处给出可核验的改写候选。保持事实边界，不要编造经历、技能或数据，也不要自动保存版本。",
        rewrite_section: "请基于当前工作台上下文，指出最值得改写的一段，并给出可核验的改写候选。不要编造经历，也不要自动保存版本。",
        compare_versions: "请比较当前工作台中选定的简历版本，说明主要差异、可能影响和建议保留的内容。只做分析，不要修改版本。",
        review_merge: "请审查当前工作台中的合并方案，说明冲突点、风险和推荐的人工决策。不要自动提交合并。",
        confirm_version: "请检查当前待确认的简历版本是否适合确认，列出确认前需要人工核对的事项。不要替我确认版本。",
        mark_applied: "请根据当前工作台中的岗位和简历版本，帮我核对投递前的最后检查项，并给出投递后的跟进建议。不要替我记录投递。",
      };
      if (action === "tailor_resume") {
        if (!context.match_analysis_id) {
          card.textContent = "请先选择岗位并完成一次匹配分析，再生成 AI 定制简历。";
          return;
        }
        card.textContent = "正在基于当前匹配分析创建可恢复 Draft…";
        try {
          await workbenchShell.tailorResume(context);
          card.textContent = "定制建议已生成。请在中间区域逐条核对、编辑并决定是否接受。";
        } catch (error) {
          card.textContent = `AI 定制简历未生成：${error.message}`;
        }
        return;
      }
      if (chatPrompts[action]) {
        if (state.isSending) { card.textContent = "Agent 正在回复上一条消息，请稍后再试。"; return; }
        messageInput.value = chatPrompts[action];
        state.skipKnowledgeForNextMessage = true;
        hideToolMenu();
        composer.requestSubmit();
        return;
      }
      const title = document.createElement("strong"); title.textContent = `Candidate Action · ${action}`;
      const detail = document.createElement("p");
      card.append(title, detail);
      if (action === "explain_score") {
        if (state.isSending) { detail.textContent = "Agent 正在回复上一条消息，请稍后再解释分数。"; return; }
        if (!context.match_analysis_id) { detail.textContent = "请先完成一次匹配评估。"; return; }
        try {
          const response = await fetch(`${apiBase()}/v1/workbench/match-analyses/${encodeURIComponent(context.match_analysis_id)}`);
          if (!response.ok) throw new Error(`HTTP ${response.status}`);
          const analysis = await response.json();
          const score = analysis.total_score ?? "—";
          const requirementCount = analysis.requirements?.length || 0;
          const dimensions = (analysis.dimensions || []).map(item => `${item.name} ${Number(item.score).toFixed(1)}分`).join("；");
          const requirements = (analysis.requirements || []).map(item =>
            `- ${item.verdict}｜${item.original_text.slice(0, 180)}｜${item.explanation.slice(0, 180)}`
          ).join("\n");
          card.replaceChildren();
          messageInput.value = `请解释当前匹配分数（${score}/100，共 ${requirementCount} 个要求）。\n评分维度：${dimensions || "未提供"}\n要求分析：\n${requirements || "未提供"}\n\n请说明得分的关键原因、已匹配证据、主要缺口，以及最优先的改进建议。只基于以上经过验证的工作台分析回答，不要编造经历或自动修改简历。`;
          state.skipKnowledgeForNextMessage = true;
          hideToolMenu();
          composer.requestSubmit();
        } catch (error) { detail.textContent = `解释加载失败：${error.message}`; }
        return;
      }
      if (action === "rewrite_section") {
        detail.textContent = "将把候选请求填入左侧对话框；发送消息不构成修改确认。";
        const proceed = document.createElement("button"); proceed.type = "button"; proceed.textContent = "填入对话框";
        proceed.addEventListener("click", () => { messageInput.value = "请基于当前工作台引用提出这一段的改写候选，不要提交版本。"; messageInput.focus(); });
        card.append(proceed); return;
      }
      if (action === "compare_versions" || action === "review_merge") {
        detail.textContent = action === "compare_versions" ? "进入版本地图选择两个节点；比较是只读操作。" : "进入版本地图审查 Merge Proposal；Agent 不会提交或静默解决冲突。";
        const proceed = document.createElement("button"); proceed.type = "button"; proceed.textContent = "打开版本地图"; proceed.addEventListener("click", () => navigatePrimaryHash("#/version-map")); card.append(proceed); return;
      }
      if (action === "confirm_version") {
        if (!context.resume_version_id) { detail.textContent = "请先选择待确认版本。"; return; }
        try {
          const response = await fetch(`${apiBase()}/v1/workbench/resume-versions/${encodeURIComponent(context.resume_version_id)}`);
          if (!response.ok) throw new Error(`HTTP ${response.status}`);
          const version = await response.json();
          if (version.status !== "pending_confirmation") { detail.textContent = `当前版本状态为 ${version.status}，无需确认。`; return; }
          detail.textContent = `将确认 ${version.label}（revision ${version.revision}）。只有点击下方按钮才会提交。`;
          const confirm = document.createElement("button"); confirm.type = "button"; confirm.textContent = "明确确认此版本";
          confirm.addEventListener("click", async () => {
            confirm.disabled = true;
            const result = await fetch(`${apiBase()}/v1/workbench/resume-versions/${encodeURIComponent(version.version_id)}/confirm`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ workspace_id: context.workspace_id, expected_revision: version.revision }) });
            detail.textContent = result.ok ? "版本已由用户明确确认。" : `确认失败：HTTP ${result.status}`;
          });
          card.append(confirm);
        } catch (error) { detail.textContent = `候选动作加载失败：${error.message}`; }
        return;
      }
      if (action === "mark_applied") {
        if (!context.resume_version_id || !context.job_snapshot_id) { detail.textContent = "请先选择已确认简历版本和岗位快照。"; return; }
        detail.textContent = `候选动作：将记录“已投递”，固定绑定 ${context.resume_version_id} 与 ${context.job_snapshot_id}。尚未写入，也不会访问招聘网站。`;
        const confirm = document.createElement("button"); confirm.type = "button"; confirm.textContent = "我明确确认已经投递";
        confirm.addEventListener("click", async () => {
          confirm.disabled = true; const suffix = crypto.randomUUID().replaceAll("-", "");
          const response = await fetch(`${apiBase()}/v1/workbench/applications`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ operation_id: `op_application_${suffix}`, idempotency_key: `application-create-${suffix}`, application_id: `app_${suffix}`, event_id: `ae_${suffix}`, workspace_id: context.workspace_id, job_snapshot_id: context.job_snapshot_id, resume_version_id: context.resume_version_id, initial_status: "applied", priority: 50, next_action: "等待后续通知", note: "用户从 Agent 候选动作明确确认已投递", user_confirmed: true }) });
          detail.textContent = response.ok ? "投递事件已记录；未执行任何外部投递。" : `记录失败：HTTP ${response.status}`;
        });
        card.append(confirm);
      }
    });
    knowledgeNavButton.addEventListener(
      "click",
      () => { closeSettings({ restoreFocus: false }); void openAdvancedWindow("knowledge", settingsReturnFocus); }
    );
    capabilitiesNavButton.addEventListener(
      "click",
      () => { closeSettings({ restoreFocus: false }); void openAdvancedWindow("capabilities", settingsReturnFocus); }
    );
    trustNavButton.addEventListener(
      "click",
      () => { closeSettings({ restoreFocus: false }); void openAdvancedWindow("trust", settingsReturnFocus); }
    );
    capabilityServersTab.addEventListener(
      "click",
      () => setCapabilityRoute("mcp-servers")
    );
    capabilitySkillsTab.addEventListener(
      "click",
      () => setCapabilityRoute("skills")
    );
    capabilityRefreshButton.addEventListener("click", refreshCapabilityRoute);
    trustEvalsTab.addEventListener("click", () => setTrustRoute("evals"));
    trustTracesTab.addEventListener("click", () => setTrustRoute("traces"));
    trustSafetyTab.addEventListener("click", () => setTrustRoute("safety"));
    trustRefreshButton.addEventListener("click", refreshTrustRoute);
    trustStartRunButton.addEventListener("click", startTrustEvalRun);
    trustTraceSearchButton.addEventListener("click", loadTrustTraces);
    trustCompareBaseRun.addEventListener("change", () => {
      if (trustCompareBaseRun.value) void loadTrustRunEvidence(trustCompareBaseRun.value);
    });
    trustCompareCandidateRun.addEventListener("change", () => {
      if (trustCompareCandidateRun.value) void loadTrustRunEvidence(trustCompareCandidateRun.value);
    });
    for (const tab of [capabilityServersTab, capabilitySkillsTab]) {
      tab.addEventListener("keydown", (event) => {
        if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
        event.preventDefault();
        const next = tab === capabilityServersTab
          ? capabilitySkillsTab
          : capabilityServersTab;
        next.focus();
        next.click();
      });
    }
    for (const tab of [trustEvalsTab, trustTracesTab, trustSafetyTab]) {
      tab.addEventListener("keydown", (event) => {
        if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
        event.preventDefault();
        const tabs = [trustEvalsTab, trustTracesTab, trustSafetyTab];
        const current = tabs.indexOf(tab);
        const delta = event.key === "ArrowRight" ? 1 : -1;
        const next = tabs[(current + delta + tabs.length) % tabs.length];
        next.focus();
        next.click();
      });
    }
    window.addEventListener("hashchange", applyPrimaryHashRoute);
    window.addEventListener("starter-agent:identity-changed", () => {
      capabilityState.identityRevision += 1;
      clearCapabilityConfirmations();
      advanceCapabilityRequestEpoch();
      const detail = capabilityState.skillDetails.get(
        capabilityState.selectedSkillName
      );
      if (advancedWindow.activeType() === "capabilities" && detail) {
        renderCapabilitySkillDetail(detail);
      }
    });
    chatKnowledgeMode.addEventListener("change", async () => {
      if (chatKnowledgeMode.value === "auto" && !activeKnowledgeBaseId) {
        await loadKnowledgeBase();
      }
      setStatus(
        chatKnowledgeMode.value === "auto"
          ? "知识库问答为自动；有可用知识库时回答将严格依据已入库资料"
          : "知识库问答已关闭"
      );
    });

    async function boot() {
      messagesEl.replaceChildren();
      toolGovernanceToggle.checked = state.toolGovernanceEnabled;
      state.systemBubble = appendMessage("assistant", "你好，我是求职 Agent。你可以发来 JD 和简历目标，我会先做匹配分析和修改建议。");
      // Apply deep links before optional API bootstrapping so Workbench/Trust/
      // Capability routes remain visible during backend outages.
      await applyPrimaryHashRoute();
      await loadProviders();
      await loadTools();
      await loadSessions();
      const storedSession = state.sessionId;
      if (
        storedSession
        && state.sessions.some(session => session.id === storedSession)
      ) {
        await switchSession(storedSession);
      } else {
        state.sessionId = null;
        sessionStorage.removeItem(CHAT_SESSION_STORAGE_KEY);
        reconcileChatConfirmations([]);
      }
      await loadDelegationRunForSession();
      if (chatKnowledgeMode.value === "auto") await loadKnowledgeBase();
      await applyPrimaryHashRoute();
    }

    boot();
