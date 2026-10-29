export const StraitjacketPlugin = async ({ directory }) => ({
  "tool.execute.before": async (event, output) => {
    const result = await bridge("opencode", "pre-tool-use", {
      cwd: directory, tool_name: event.tool, tool_input: output.args,
      session_id: event.sessionID, tool_use_id: event.callID,
    });
    if (result.action === "rewrite") {
      // OpenCode executes the original args object after the callback.
      for (const key of Object.keys(output.args)) delete output.args[key];
      Object.assign(output.args, result.input);
    }
    if (result.action === "block" || result.action === "ask")
      throw new Error(result.reason || "Straitjacket requires approval in an interactive workflow.");
  },
  "tool.execute.after": async (event, output) => {
    const result = await bridge("opencode", "post-tool-use", {
      cwd: directory, tool_name: event.tool, tool_input: event.args,
      session_id: event.sessionID, tool_use_id: event.callID, tool_response: output.output,
    });
    if (typeof result.output === "string") output.output = result.output;
  },
});
