"use strict";

function renderTurnTrace(trace) {
  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  const panel = element("details", "turn-trace");
  panel.dataset.traceId = trace.id;
  const spans = trace.spans;
  panel.append(element("summary", "trace-title", `工具追踪 · ${spans.length} 次调用`));
  panel.append(element("code", "trace-id", trace.id));
  if (trace.partial) panel.append(element("p", "trace-note", "部分追踪记录无法读取。"));
  if (!spans.length) {
    if (!trace.partial) panel.append(element("p", "trace-note", "本轮没有已记录的工具调用。"));
    return panel;
  }

  const cacheable = spans.filter(s => s.tool_name === "web_search" || s.tool_name === "web_fetch");
  const hits = cacheable.filter(s => s.cache_hit).length;
  const ratio = cacheable.length ? `${hits}/${cacheable.length}（${+(100 * hits / cacheable.length).toFixed(1)}%）` : "无可缓存调用";
  panel.append(element("p", "trace-cache", "联网只读工具缓存命中 · " + ratio));
  const longest = spans.reduce((max, span) => Math.max(max, span.latency_ms), 0);
  const ordered = [...spans].sort((a, b) => a.timestamp.localeCompare(b.timestamp));
  const nodes = new Map();
  const tree = element("ul", "trace-tree");

  for (const span of ordered) {
    const item = element("li", "trace-span");
    item.dataset.spanId = span.span_id;
    const detail = element("details");
    const line = element("summary", "trace-call");
    line.append(element("b", "trace-tool", span.tool_name));
    line.append(element("span", "trace-role", span.agent_role));
    const meter = element("meter");
    meter.min = 0;
    meter.max = longest || 1;
    meter.value = span.latency_ms;
    meter.setAttribute("aria-label", "相对于本轮最慢工具的耗时");
    line.append(meter, element("span", "trace-duration", span.latency_ms + " ms"));
    if (span.cache_hit) line.append(element("span", "trace-hit", "缓存命中"));
    if (!span.ok) line.append(element("span", "trace-failure", span.error_type || "执行失败"));
    detail.append(line);
    const fields = element("dl", "trace-fields");
    for (const [name, value] of [["Span ID", span.span_id], ["父 Span ID", span.parent_span_id || "—"],
         ["开始时间", span.timestamp], ["工具来源", span.source_type], ["状态", span.ok ? "成功" : "失败"],
         ["输出字符", span.output_chars]]) {
      fields.append(element("dt", "", name), element("dd", "", value));
    }
    detail.append(fields);
    item.append(detail);
    nodes.set(span.span_id, {item, children: null});
  }

  // Parent records can be written after children. Resolve links only once all nodes exist.
  for (const span of ordered) {
    const node = nodes.get(span.span_id);
    const parent = nodes.get(span.parent_span_id);
    if (parent && !node.item.contains(parent.item)) {
      if (!parent.children) {
        parent.children = element("ul", "trace-children");
        parent.item.append(parent.children);
      }
      parent.children.append(node.item);
    } else {
      if (span.parent_span_id) node.item.append(element("small", "trace-note", "父调用记录缺失或无效"));
      tree.append(node.item);
    }
  }
  panel.append(tree);
  return panel;
}
