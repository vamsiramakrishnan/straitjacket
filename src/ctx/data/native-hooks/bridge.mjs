// Straitjacket managed native hooks v1
import { execFile } from "node:child_process";

const ctxArgv = __CTX_ARGV__;
function bridge(host, stage, payload) {
  return new Promise((resolve) => {
    const failed = () => resolve(stage === "pre-tool-use"
      ? { action: "block", reason: "Straitjacket hook failed; check ctx doctor before retrying." }
      : { output: "[ctx gate-failed] Tool output withheld: the hook process failed. Check ctx doctor." });
    const child = execFile(ctxArgv[0], [...ctxArgv.slice(1), "hook", host, stage],
      { timeout: 15000, maxBuffer: 2 * 1024 * 1024, cwd: payload.cwd },
      (error, stdout) => {
        if (error) return failed();
        try { resolve(JSON.parse(stdout)); } catch { failed(); }
      });
    child.stdin.on("error", () => {}); // execFile callback reports launch/pipe failure
    child.stdin.end(JSON.stringify(payload));
  });
}

function textBlocks(content) {
  return (content ?? []).filter(b => b.type === "text").map(b => b.text).join("\n");
}

function replaceText(content, text) {
  // Preserve images and other non-text blocks; this is a text-output gate.
  return [{ type: "text", text }, ...(content ?? []).filter(b => b.type !== "text")];
}
