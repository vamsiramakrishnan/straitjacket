export const name = "straitjacket";
export function apply(ctx) {
  const payload = exec => ({
    cwd: exec.agent?.session.header.cwd ?? process.cwd(),
    session_id: exec.agent?.session.header.id ?? "",
    tool_name: exec.name, tool_input: exec.arguments, tool_use_id: exec.callId,
  });
  ctx.on("tools/pre-execute", async (exec, next) => {
    const result = await bridge("dsh", "pre-tool-use", payload(exec));
    if (result.action === "block") return { kind: "deny", reason: result.reason };
    if (result.action === "ask") return { kind: "ask", reason: result.reason };
    return next(); // preserve downstream guards; never force-allow
  });
  ctx.on("tools/post-execute", async (exec, result, next) => {
    const downstream = await next();
    if (downstream.kind === "block") return downstream;
    // Canonical-value rewrites own their schema/projector. Do not replace a
    // downstream value with presentation from the original result.
    if (Object.hasOwn(downstream, "value")) return downstream;
    const content = downstream.content ?? result.content;
    const bounded = await bridge("dsh", "post-tool-use", {
      ...payload(exec), tool_response: textBlocks(content), is_error: result.isError,
    });
    return typeof bounded.output === "string"
      ? { ...downstream, content: replaceText(content, bounded.output) } : downstream;
  });
}
