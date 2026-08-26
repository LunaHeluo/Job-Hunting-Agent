# 面向 AI 定制简历的范围化证据检索设计

**日期：** 2026-08-26  
**状态：** 用户已确认
**目标：** 解决岗位匹配选择无关简历行、遗漏真实相关项目，进而导致 AI 定制简历没有安全候选的问题。

## 1. 问题与根因

当前 `deterministic_requirements()` 对岗位要求做关键词重叠判断，再从简历中选择第一条包含任意命中词的非空行作为证据。这个策略有三个问题：

1. “第一条命中”不是相关度排序，可能优先选择 AI 研究、课程或论文，而漏掉后面的 React 项目。
2. 一个岗位要求只得到一条证据，无法比较多个项目区块的相关性。
3. AI 定制服务只允许使用 MatchAnalysis 中已经绑定的 positive evidence，因此后续模型无法自行纠正错误证据。

截图中的 React 项目存在于简历，但没有进入 `ev_1 / ev_2 / ev_3`，属于证据选择错误，不是写作模型能力不足。

## 2. 采用方案

复用同级 `resume_agent` 的“JD 查询词 → 知识库 Top-K 检索 → 反思 → 撰写”结构，但不复制其全局 Chroma、独立 LLMClient 或整份简历覆盖逻辑。

Starter Agent 新增 `ResumeEvidenceSelector`，使用现有 `KnowledgeRetriever`，并强制通过当前 ResumeVersion 的 `document_id` 限定检索范围。检索结果先进入新的 `match-rule.v2` MatchAnalysis，再由现有 TailoredResumeService 消费。

不采用以下方案：

- 仅调整 Prompt：模型仍看不到遗漏的 React 证据，不能解决问题。
- 让定制服务绕过 MatchAnalysis 自行引用知识库：会破坏 Suggestion 的证据验证和分数一致性。
- 直接复制 `resume_agent.generate_full()`：会绕过 Draft、版本不可变性和 workspace 隔离。

## 3. 数据流

```text
当前 ResumeVersion + JobSnapshot
  → 从 JD 提取逐条岗位要求
  → 为每条要求生成检索查询
  → KnowledgeRetriever（只查当前简历 document_id）
  → Top-K 简历 chunk 去重、排序、阈值过滤
  → chunk 对齐 Markdown block
  → 生成 RequirementResult + EvidenceReference
  → match-rule.v2 MatchAnalysis
  → TailoredResumeService 反思、撰写和安全校验
  → Suggestion
  → 用户批量接受到 Draft
```

## 4. ResumeEvidenceSelector

新增 `backend/src/starter_agent/cv_workbench/tailoring_evidence.py`。

输入：

- principal 与 workspace_id
- 当前 ResumeVersion 及其 ContentReference
- 简历 Markdown
- 一条结构化岗位要求

行为：

1. 从要求原文提取英文技术词、中文技能短语和规范化别名。
2. 调用 `KnowledgeRetriever.retrieve()`，固定传入当前 ResumeVersion 的 `document_id`。
3. 每条要求最多读取 5 个候选 chunk，按检索 rank、完整技术词覆盖率和原文顺序重新排序。
4. 将 chunk 文本映射到规范化 Markdown block；不能映射到当前简历的结果全部丢弃。
5. 最多保留 3 条不同 block 的证据，生成 `knowledge-chunk://...` EvidenceReference，并使用 chunk 的真实 content hash。

Selector 不调用 LLM、不修改分析、不创建 Suggestion，因此其输出可确定性测试。

## 5. 匹配规则 v2

整理 `matching.py`：删除重复的 `deterministic_requirements()` 和重复 import，将证据选择作为显式依赖注入。

每条岗位要求的 verdict 规则：

- `matched`：至少一条当前简历证据，并且核心技术词覆盖率不低于 60%。
- `partial`：存在当前简历证据，但核心技术词覆盖率低于 60%。
- `missing`：没有通过范围、hash 和 block 对齐校验的证据。
- `conflict`：保留现有显式冲突语义，不由检索自动产生。

MatchAnalysis 的 `rule_version` 更新为 `match-rule.v2`，`validator_version` 同步升级。每个 positive RequirementResult 保存 1–3 条 EvidenceReference，而不是第一条关键词命中行。

## 6. 旧分析与缓存

MatchAnalysis 结果不可变，因此不修改历史 `match-rule.v1` 数据。

- 工作台恢复到 v1 分析时继续允许查看旧分数。
- 点击 AI 定制简历时，如果分析是 v1，界面提示“证据策略已升级，需要重新分析一次”，并提供直接重新分析操作。
- 同一 `resume_content_sha256 + job_content_sha256 + match-rule.v2` 只创建一次有效分析；后续刷新和再次打开均复用该分析。
- ResumeVersion 或 JobSnapshot 内容 hash 变化时，现有 stale 机制继续使旧分析失效。

升级不会在每次刷新时重新匹配，只在首次从 v1 切换到 v2 或输入内容变化时执行。

## 7. AI 定制简历集成

TailoredResumeService 的安全规则保持不变：

- 只消费 v2 分析中 `matched` / `partial` 的证据。
- evidence quote 必须映射到当前 Draft block。
- 候选必须引用正确 requirement ID、evidence ID 和 block ID。
- 新数字、未知证据、跨 block 引用继续被拦截。
- 结果只写 Suggestion；用户接受后才写 Draft，正式版本不变。

当相关 React 项目被 v2 分析绑定为证据时，写作模型才能安全地把它用于前端岗位定制。

## 8. API 与 UI

现有匹配分析与定制端点保持 URL 不变。API 响应增加可选的规则升级提示，不新增第二套生成端点。

前端行为：

1. 分析页显示 `match-rule.v2` 和证据数量。
2. 每条要求可展开查看最多 3 条证据及来源区块。
3. v1 分析触发定制时显示“一次性升级分析”按钮，不进入无效生成。
4. v2 分析进入现有 AI 定制建议页，显示关联要求、证据摘录和风险。
5. 刷新时优先恢复同一输入 hash 的最新 v2 分析，不重复生成。

## 9. 安全与隐私

- 检索必须同时限定 principal、workspace、knowledge_base 和当前 resume document_id。
- 不允许从其他简历、JD、聊天记录或其他 workspace 获取定制证据。
- EvidenceReference 必须使用存储中的 chunk ID 与 content hash，不接受模型生成的来源信息。
- 检索或 block 对齐失败时 requirement 变为 missing，不降级为全工作区搜索。
- 模型无法改变 MatchAnalysis；只能基于已验证证据提出 Draft 建议。

## 10. 测试与验收

单元测试：

- React 项目排在 AI 论文、课程成绩等无关区块之前。
- 检索始终携带当前 ResumeVersion 的 document_id。
- 其他简历中的高分 chunk 不得进入证据。
- 无法映射到当前 Markdown block 的 chunk 被丢弃。
- 每条要求最多保留 3 个不同 block。
- v1 第一行命中实现被移除且不存在重复定义。

集成测试：

- 导入简历与前端 JD 后，v2 分析把 React 项目绑定到前端要求。
- AI 定制请求生成至少一条引用该 evidence 的 Suggestion。
- 相同输入再次打开复用 v2 分析，不重复检索或调用模型。
- 修改简历或 JD 后旧分析变 stale。
- 正式 ResumeVersion 在生成和接受 Suggestion 前后均不改变。

前端合同测试：

- v1 显示一次性升级入口。
- v2 展示多条证据和规则版本。
- 不再把无关 evidence 作为可定制依据。

验收用例使用截图对应的数据语义：岗位要求包含前端开发、系统联调和前端 AI 应用；简历包含 React 推荐系统项目及无关 AI 研究、课程和金融论文。最终 React 项目必须成为该要求的首选证据。

## 11. 发布边界

不需要数据库迁移。发布时保留 v1 历史分析，只将新分析写为 v2。完整验证通过后，真实 Provider 人工验收仍只写 Draft，不自动保存为正式版本。
