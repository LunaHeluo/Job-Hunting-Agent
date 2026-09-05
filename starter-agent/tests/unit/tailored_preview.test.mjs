import assert from "node:assert/strict";
import { renderTailoredPreview } from "../../frontend/web/app/features/tailored-preview.js";

class Element {
  children = []; handlers = {}; textContent = ""; disabled = false;
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  setAttribute() {}
  addEventListener(name, handler) { this.handlers[name] = handler; }
}
globalThis.document = { createElement: () => new Element() };
const flatten = element => [element, ...element.children.flatMap(flatten)];
async function fixture({ unchanged = false, current = true, failSave = false, legacy = false } = {}) {
  const container = new Element(); const writes = [];
  const request = async (url, options) => {
    if (options) {
      writes.push({ url, body: JSON.parse(options.body) });
      if (url.endsWith("/branches")) return { branch_id: "rb_tailored_new" };
      if (url.endsWith("/drafts")) return { draft_id: "copy", revision: 1, content: { content_sha256: "hash" } };
      if (options.method === "PATCH") return { draft_id: "copy", revision: 2 };
      if (failSave && url.endsWith("/versions")) throw new Error("revision_conflict");
      return { version_id: "saved", revision: 1 };
    }
    if (url.includes("job-snapshots")) return { company: "公司", title: "前端" };
    if (url.includes("resume-versions")) return { markdown: "# 姓名\n原始经历" };
    if (url.includes("/content")) return { revision: 3, markdown: unchanged ? "# 姓名\n原始经历" : "# 姓名\n<script>安全文本</script>" };
    return { status: "active", revision: 3, branch_id: legacy ? "original" : "rb_tailored_new", resume_id: "r" };
  };
  await renderTailoredPreview({ request, container, workspaceId: "w", analysis: { resume_version_id: "v", job_snapshot_id: "j" }, draftId: "d", isCurrent: () => current, onBack() {} });
  return { container, writes, find: text => flatten(container).find(node => node.textContent === text) };
}
const flow = await fixture();
assert.equal(flow.writes.length, 0, "Preview must be read-only");
assert.ok(flow.find("<script>安全文本</script>\n"), "Resume content is rendered as text");
await flow.find("保存为岗位定制版本").handlers.click();
assert.equal(flow.writes[0].body.expected_draft_revision, 3);
assert.equal(flow.writes[0].body.label, "公司 · 前端 定制版");
assert.equal(flow.find("确认版本").hidden, false);
assert.equal(flow.writes.length, 1, "Saving must not auto-confirm");
await flow.find("确认版本").handlers.click();
assert.match(flow.writes[1].url, /saved\/confirm$/);
assert.equal((await fixture({ unchanged: true })).find("保存为岗位定制版本").disabled, true);
assert.equal((await fixture({ current: false })).container.children.length, 0);
const failure = await fixture({ failSave: true });
await failure.find("保存为岗位定制版本").handlers.click();
assert.equal(failure.find("保存为岗位定制版本").disabled, false);
assert.equal(failure.find("确认版本").hidden, true);
assert.ok(failure.find("保存失败：revision_conflict"));
console.log("tailored preview: save, explicit confirmation, escaping, empty/stale states and failure recovery passed");
const legacy = await fixture({ legacy: true });
await legacy.find("保存为岗位定制版本").handlers.click();
assert.equal(legacy.writes[0].body.job_snapshot_id, "j");
assert.equal(legacy.writes[2].body.markdown, "# 姓名\n<script>安全文本</script>");
assert.match(legacy.writes[3].url, /drafts\/copy\/versions$/);
assert.equal(legacy.writes[3].body.expected_draft_revision, 2);
console.log("legacy Draft safely copied to JD-bound branch passed");
