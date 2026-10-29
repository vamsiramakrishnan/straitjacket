export default function straitjacket(pi) {
  pi.on("tool_call", async (event, context) => {
    const result = await bridge("omp", "pre-tool-use", {
      cwd: context.cwd, tool_name: event.toolName, tool_input: event.input,
      tool_use_id: event.toolCallId, session_id: context.sessionManager?.getSessionId?.() ?? "",
    });
    if (result.action === "rewrite") return { input: result.input };
    if (result.action === "block" || result.action === "ask")
      return { block: true, reason: result.reason || "Straitjacket requires approval in an interactive workflow." };
  });
  pi.on("tool_result", async (event, context) => {
    const result = await bridge("omp", "post-tool-use", {
      cwd: context.cwd, tool_name: event.toolName, tool_input: event.input,
      tool_use_id: event.toolCallId, tool_response: textBlocks(event.content), is_error: event.isError,
      session_id: context.sessionManager?.getSessionId?.() ?? "",
    });
    if (typeof result.output === "string") return { content: replaceText(event.content, result.output) };
  });
}
