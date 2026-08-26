# AI 定制简历复用设计

**日期：** 2026-08-26  
**状态：** 待用户审阅  
**来源：** 复用同级 `resume_agent` 的 JD 驱动生成策略，落入 Starter Agent 的 CV Workbench 领域模型。

## 1. 目标

在 Starter Agent 工作台的“分析中”阶段提供真正的 AI 定制简历能力。用户选择一个已经验证或部分验证的岗位匹配分析后，系统基于当前简历中的已验证事实生成多条定制改写建议，并写入一个可恢复的 Resume Draft。用户可以逐条编辑、接受或拒绝，也可以批量接受选中的建议；正式简历版本不会被自动覆盖。

成功标准：

1. “AI 定制简历”不再调用现有的弱动词固定替换，而是调用配置好的 Starter Agent Provider 生成针对 JD 的改写。
2. 只允许使用 `matched` / `partial` 要求对应的简历证据；`missing` / `conflict` 不得被写成用户经历。
3. 每条候选必须带要求 ID、证据引用、修改理由和风险提示，且通过服务端验证后才能成为 Suggestion。
4. 结果只进入 Draft；用户审批后才能保存为待确认版本，之后仍需显式确认才能用于导出或投递。
5. 刷新页面后能从后端恢复同一 Draft 和候选，不重复调用模型或重复创建 Suggestion。

## 2. 非目标

- 不复制 `resume_agent` 的 `content_json`、SQLite 表或 Chroma 全局集合。
- 不直接覆盖 ResumeVersion，也不自动确认或自动投递。
- 不根据能力缺口编造项目、技能、年限或量化结果。
- 不新增第二套 LLM 配置、聊天 Runtime、PDF 导出器或前端框架。
- 本阶段不生成完全脱离原简历结构的一页式新简历；输出仍以现有 Markdown Block 为修改单元。

## 3. 方案比较

### 方案 A：直接复制 `resume_agent/api/generate.py`

优点是开发快，能保留原有整份并行生成。缺点是它依赖结构化 `content_json`、全局 Chroma 和独立 LLMClient，并会直接更新节点内容；这会绕过 Starter Agent 的 workspace 隔离、证据绑定、Draft revision 和不可变版本机制。因此不采用。

### 方案 B：仅通过聊天 Agent 调用现有 Resume Tools

优点是少写领域代码，用户可以自然语言控制。缺点是生成结果难以稳定恢复，无法可靠形成批量 Suggestion，也难以保证每条改写都绑定 MatchAnalysis 中的证据。适合后续作为自然语言入口，不适合作为工作台主流程。因此不采用。

### 方案 C：新增证据约束的 TailoredResumeService（采用）

复用 `resume_agent` 的“证据检索 → 反思审核 → 定向撰写”思想，但使用 MatchAnalysis 已绑定的知识库证据、Starter Agent Provider、Suggestion 和 ResumeDraft。该方案改造量中等，但能保持现有安全与版本语义，并支持 UI 恢复和测试替身。

## 4. 架构与组件

### 4.1 TailoredResumeService

新增 `backend/src/starter_agent/cv_workbench/tailoring.py`，职责限定为：

1. 校验 MatchAnalysis、ResumeDraft、Workspace 和当前 revision 的一致性。
2. 读取 Draft Markdown，并把 evidence quote 对齐到现有 Resume Block。
3. 只收集 `matched` / `partial` 要求及其已验证 EvidenceReference。
4. 调用 TailoredResumeGenerator 获得结构化候选。
5. 对模型输出执行确定性验证，再通过现有 `SuggestionService.create()` 持久化。
6. 对相同 `analysis_id + draft_id + draft_revision + prompt_version` 返回已有候选，保证刷新幂等。

服务不直接调用 OpenAI SDK、不操作 HTTP，也不保存 ResumeVersion。

### 4.2 TailoredResumeGenerator

在同一模块定义协议和 Provider 适配器：

```python
class TailoredResumeGenerator(Protocol):
    async def generate(
        self,
        request: TailoringGenerationRequest,
    ) -> TailoringGenerationResult: ...
```

生产适配器通过 `ProviderRegistry.get(settings.model.default_provider)` 延迟取得 Provider，并调用其 `complete()`；模型使用 `settings.model.default_model`。测试注入 Fake Generator，不访问网络。

提示词复用 `resume_agent` 的关键规则：严格依据证据、避免套话和夸大、向目标岗位靠拢、优先保留可验证结果。流程分两步：

1. **反思：** 检测套话、职责升级、量化信息新增和证据冲突。
2. **撰写：** 针对可安全修改的 Block 返回最多 8 条候选。

模型输出必须是 JSON，只允许包含 `block_id`、`proposed_text`、`reason`、`requirement_ids`、`evidence_ids` 和 `risk`。模型不能自行提供 source hash；服务端根据受信输入补全 EvidenceReference。

### 4.3 证据验证

每条模型候选必须满足：

- `block_id` 存在于当前 Draft，且候选创建时 Block 原文仍未变化。
- 每个 requirement ID 属于当前 MatchAnalysis，并且 verdict 为 `matched` 或 `partial`。
- 每个 evidence ID 属于上述 requirement，quote 是对应 Resume Block 中的连续原文。
- `proposed_text` 非空、与原文不同，长度不超过 10,000 字符。
- 同一次结果中一个 Block 最多出现一条候选。
- 候选不得包含输入证据中不存在的新数字；发现新数字时拒绝该候选并记录风险原因。

没有安全候选时返回空列表和可读原因，不降级为虚构内容。

### 4.4 Workbench Runtime

`WorkbenchRuntime` 增加 `tailoring` 服务。`create_workbench_runtime()` 接受可选的 `tailoring_generator`：

- 生产环境由 `bootstrap.create_cv_workbench_runtime()` 使用 Starter Agent 的 ProviderRegistry 和默认模型构造。
- 单元/集成测试可注入 Fake Generator。
- 未配置真实 Provider 时，端点返回 `tailoring_provider_unavailable`，现有工作台其他功能保持可用。

不新增 Chroma 依赖。MatchAnalysis 中的 EvidenceReference 已来自 workspace 范围内的 Knowledge Store，TailoredResumeService 只消费这些经过验证的证据。

## 5. API

新增：

```text
POST /v1/workbench/match-analyses/{analysis_id}/tailored-resume-candidates
```

请求：

```json
{
  "workspace_id": "ws_123",
  "draft_id": "rd_123"
}
```

响应包含 `draft_id`、`draft_revision`、`items`、`reflection`、`reused`。`items` 使用现有 Suggestion 合同。

现有 `/suggestion-candidates` 保留，继续表示无需 LLM 的保守固定改写，避免破坏已有调用方。

同时暴露现有 `SuggestionService.apply_batch()`：

```text
POST /v1/workbench/suggestions/batch-decisions
```

请求包含 `workspace_id`、`accept_ids`、`reject_ids` 和可选的 `edited_text_by_id`。服务先验证所有接受项属于同一 Draft/revision、Block 不重复，再一次性写入 Draft；拒绝项只更新状态。任一验证失败时整批不写 Draft。

主要错误码：

- `tailoring_provider_unavailable`
- `tailoring_analysis_not_ready`
- `tailoring_draft_base_mismatch`
- `tailoring_no_verified_evidence`
- `tailoring_output_invalid`
- `tailoring_candidate_unsafe`
- `suggestion_batch_target_mismatch`
- `revision_conflict`

## 6. 前端流程

修改 `frontend/web/app/features/job-matching.js`：

1. 用户点击 Starter Agent 气泡或快捷操作中的“AI 定制简历”。
2. 前端读取匹配分析；若该分析 revision 已有活跃 Draft，则复用，否则从分析绑定的 ResumeVersion 创建 Draft。
3. 调用新的 `tailored-resume-candidates` 端点。
4. 中间区域展示“AI 定制简历”审批页：原文、建议文本、理由、关联岗位要求、证据摘录和风险。
5. 用户可以编辑建议、勾选多条后批量接受，也可以拒绝单条。
6. 接受后提示“已写入 Draft，正式版本未改变”，并提供“打开 Draft 继续编辑”和“保存为待确认版本”。

生成期间禁用重复提交；失败时保留当前分析和 Draft，允许重试。刷新后通过后端 Suggestion 列表恢复审批页，不再次生成。

`workbench-shell.js` 继续负责阶段快捷操作和当前上下文，不承载生成逻辑。现有“AI 修改简历”保持为聊天式建议入口，“AI 定制简历”表示岗位绑定、可审批、可版本化的工作台流程。

## 7. 数据与安全

- 不新增业务表，复用 ResumeDraft、Suggestion、MatchAnalysis、BusinessEvent 和 ContentReference。
- 所有读取都校验 principal 与 workspace 归属。
- 模型只接收当前 Draft、当前 JobSnapshot/requirements 和已授权 EvidenceReference；不读取其他 workspace 内容。
- Prompt 和原始模型输出不写入普通 API 响应。审计事件仅保存 provider/model、prompt_version、候选数量、拒绝原因和内容 hash。
- 模型调用失败不修改 Draft；候选验证失败不创建 Suggestion。
- Draft 保存继续使用 expected revision 和 expected content hash，防止并发覆盖。

## 8. 测试策略

### 单元测试

- 仅 matched/partial 要求进入生成请求。
- 缺失要求、未知 evidence、未知 Block、新数字和重复 Block 被拒绝。
- 相同 revision 重试返回已有 Suggestion，Fake Generator 只调用一次。
- Provider 输出非 JSON、超长文本和空候选返回稳定错误。
- 批量接受一次 autosave，多条建议同时变为 accepted；失败时 Draft 不变。

### 集成测试

- 从 MatchAnalysis 创建 Draft，生成候选，批量接受，保存待确认版本，原 ResumeVersion 内容不变。
- principal/workspace 越权返回 404/403，不泄露实体是否存在。
- Provider 未配置时返回可恢复错误，其他工作台端点不受影响。

### 前端合同测试

- “AI 定制简历”调用新端点而非 `/suggestion-candidates`。
- 审批页包含证据、风险、批量接受和 Draft 状态文案。
- 刷新恢复已有结果，不重复创建 Draft 或发起模型调用。

## 9. 迁移与发布

该功能不需要数据库迁移。先保留旧的保守候选端点作为降级路径，新增端点通过已有 Provider 配置启用。实现时同时删除 `SuggestionService` 中重复定义的 `generate_safe_candidates()`，但不改变其外部行为。

发布验收顺序：单元测试 → Workbench API 集成测试 → 前端合同测试 → 完整后端测试 → 前端测试。真实 Provider 的人工验收只使用测试简历和测试 JD，不将模型输出自动保存为正式版本。
