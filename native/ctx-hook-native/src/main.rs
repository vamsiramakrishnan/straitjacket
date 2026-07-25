//! ctx-hook-native: opportunistic native shim for the post-tool-use stage.
//!
//! Measured motivation (cleanup wave): CPython's startup floor is ~29 ms and
//! the post-tool-use governor fires on every Bash/Read/Edit/Write call —
//! ~80 spawns per session ≈ 2.7 s of pure interpreter startup. This binary
//! does the same work in ~2 ms. It is an accelerator, never a requirement:
//! `ctx wrap` uses it only when present (CTX_NATIVE_HOOK or on PATH), the
//! Python implementation remains canonical, and the parity test in
//! tests/test_native_hook.py asserts byte-identical output on golden cases.
//!
//! Contract (mirrors ctx.hook.main_post_tool_use exactly):
//!   stdin: one JSON payload. stdout: exactly one JSON object —
//!   `{}` for silence, or the emission-governor nudge in the host dialect.
//!   Every error path is silence. Argv: `hook <flavor> post-tool-use`.

use fs2::FileExt;
use serde_json::{json, Value};
use std::fs::OpenOptions;
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let flavor = args.get(2).map(String::as_str).unwrap_or("antigravity");
    // Accept both `ctx-hook-native hook <flavor> post-tool-use` and
    // `ctx-hook-native <flavor> post-tool-use`.
    let flavor = if flavor == "post-tool-use" {
        args.get(1).map(String::as_str).unwrap_or("antigravity")
    } else {
        flavor
    };
    let mut raw = String::new();
    let _ = std::io::stdin().read_to_string(&mut raw);
    let payload: Value = serde_json::from_str(&raw).unwrap_or(Value::Null);
    let nudge = emission_nudge(&payload);
    let out = match nudge {
        None => json!({}),
        Some(text) => {
            if flavor == "claude-code" {
                json!({"hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": text,
                }})
            } else {
                json!({"decision": "allow", "reason": text})
            }
        }
    };
    // Python emits json.dumps(..., sort_keys=True) + "\n": key-sorted (serde
    // Value without preserve_order already is) with ", " / ": " separators —
    // matched exactly so the parity contract is byte-for-byte.
    println!("{}", to_python_json(&out));
}

struct PySpacing;

impl serde_json::ser::Formatter for PySpacing {
    fn begin_object_key<W: ?Sized + std::io::Write>(
        &mut self, w: &mut W, first: bool,
    ) -> std::io::Result<()> {
        if first { Ok(()) } else { w.write_all(b", ") }
    }
    fn begin_object_value<W: ?Sized + std::io::Write>(
        &mut self, w: &mut W,
    ) -> std::io::Result<()> {
        w.write_all(b": ")
    }
    fn begin_array_value<W: ?Sized + std::io::Write>(
        &mut self, w: &mut W, first: bool,
    ) -> std::io::Result<()> {
        if first { Ok(()) } else { w.write_all(b", ") }
    }
}

fn to_python_json(v: &Value) -> String {
    use serde::Serialize;
    let mut buf = Vec::new();
    let mut ser = serde_json::Serializer::with_formatter(&mut buf, PySpacing);
    if v.serialize(&mut ser).is_err() {
        return "{}".into();
    }
    String::from_utf8(buf).unwrap_or_else(|_| "{}".into())
}

fn resolve_workspace_root(payload: &Value) -> Option<PathBuf> {
    // Mirror of ctx.hook._resolve_workspace_root, post-stage subset.
    let tool_input = payload
        .get("tool_input")
        .or_else(|| payload.get("toolInput"))
        .cloned()
        .unwrap_or(Value::Null);
    let mut probe: Option<String> = None;
    for key in [
        "AbsolutePath", "TargetFile", "file_path", "path", "Path", "Cwd", "cwd",
    ] {
        let v = tool_input.get(key).or_else(|| payload.get(key));
        if let Some(Value::String(s)) = v {
            if !s.is_empty() {
                probe = Some(s.clone());
                break;
            }
        }
    }
    let ws_paths_v = payload
        .get("workspacePaths")
        .or_else(|| payload.get("workspace_paths"))
        .or_else(|| payload.get("workspaces"));
    let ws_paths: Vec<String> = match ws_paths_v {
        Some(Value::String(s)) => vec![s.clone()],
        Some(Value::Array(a)) => a
            .iter()
            .filter_map(|v| v.as_str().map(str::to_string))
            .collect(),
        _ => vec![],
    };
    if ws_paths.is_empty() {
        if let Some(p) = &probe {
            if Path::new(p).is_dir() {
                return Some(PathBuf::from(p));
            }
        }
        return payload
            .get("cwd")
            .and_then(Value::as_str)
            .map(PathBuf::from);
    }
    if let Some(p) = &probe {
        let probe_abs = absolutize(p);
        let mut best: Option<String> = None;
        for wp in &ws_paths {
            let wp_abs = absolutize(wp);
            if probe_abs == wp_abs || probe_abs.starts_with(&format!("{wp_abs}/")) {
                if best.as_ref().map_or(true, |b| wp_abs.len() > b.len()) {
                    best = Some(wp_abs.clone());
                }
            }
        }
        if let Some(b) = best {
            return Some(PathBuf::from(b));
        }
    }
    if ws_paths.len() == 1 {
        return Some(PathBuf::from(absolutize(&ws_paths[0])));
    }
    None
}

fn absolutize(p: &str) -> String {
    let path = Path::new(p);
    if path.is_absolute() {
        // Normalize like os.path.abspath: resolve "." and ".." lexically.
        lexical_normalize(path)
    } else {
        let cwd = std::env::current_dir().unwrap_or_else(|_| PathBuf::from("/"));
        lexical_normalize(&cwd.join(path))
    }
}

fn lexical_normalize(path: &Path) -> String {
    let mut parts: Vec<String> = Vec::new();
    for comp in path.components() {
        use std::path::Component::*;
        match comp {
            RootDir => parts.clear(),
            CurDir => {}
            ParentDir => {
                parts.pop();
            }
            Normal(s) => parts.push(s.to_string_lossy().into_owned()),
            Prefix(_) => {}
        }
    }
    format!("/{}", parts.join("/"))
}

fn emission_nudge(payload: &Value) -> Option<String> {
    let ws = resolve_workspace_root(payload)?;
    let window_path = ws.join(".ctx-session-reads").join("proxy").join("window.json");
    let window: Value = serde_json::from_str(&std::fs::read_to_string(window_path).ok()?).ok()?;
    let cum_output = window.get("cum_output")?.as_i64().unwrap_or(0);
    let requests = window.get("requests")?.as_i64().unwrap_or(0);
    if requests <= 0 || cum_output <= 0 {
        return None;
    }
    let step = emission_step(&ws).max(1);
    let tier = cum_output / step;
    if tier < 1 {
        return None;
    }
    let per_request = cum_output as f64 / requests as f64;
    if per_request < 500.0 {
        return None;
    }
    if !claim_emission_tier(&ws, tier) {
        return None;
    }
    Some(format!(
        "CTX_EMISSION_GOVERNOR: session output ~{} tokens (avg {:.0}/turn). \
         Output volume is the dominant cost+latency driver. Keep narration \
         terse; cite coordinates (file:line, run:/span handles) instead of \
         restating content.",
        thousands(cum_output),
        per_request
    ))
}

/// Mirror of ctx.engagement.EMISSION_NUDGE_TOKENS_DEFAULT. Rust cannot
/// import the Python constant, so this is the one place the value is named
/// on this side; tests/test_cross_language_constants.py reads both sources
/// and fails if they drift.
const EMISSION_NUDGE_TOKENS_DEFAULT: i64 = 20_000;

fn emission_step(ws: &Path) -> i64 {
    // ctx.toml [engagement] emission_nudge_tokens. Fail-open at every step,
    // matching ctx.hook._load_guard_policy: an unreadable or unparseable
    // ctx.toml yields OUR default, never an empty/zero budget. (The Python
    // side additionally records `_guard_config = "failed"` so the guard can
    // tell "config says allow" from "we could not find out"; that provenance
    // exists for safety-class decisions, and this binary makes none — it is
    // the post-tool-use nudge path only.)
    let Ok(text) = std::fs::read_to_string(ws.join("ctx.toml")) else {
        return EMISSION_NUDGE_TOKENS_DEFAULT;
    };
    let Ok(doc) = text.parse::<toml::Table>() else {
        return EMISSION_NUDGE_TOKENS_DEFAULT;
    };
    doc.get("engagement")
        .and_then(|e| e.get("emission_nudge_tokens"))
        .and_then(|v| v.as_integer())
        .unwrap_or(EMISSION_NUDGE_TOKENS_DEFAULT)
}

fn claim_emission_tier(ws: &Path, tier: i64) -> bool {
    // Mirror of ctx.engagement.claim_emission_tier: flock'd RMW of
    // .ctx-session-reads/engagement.json, sorted keys on write.
    let run = || -> Option<bool> {
        let dir = ws.join(".ctx-session-reads");
        std::fs::create_dir_all(&dir).ok()?;
        let path = dir.join("engagement.json");
        let mut f = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .open(&path)
            .ok()?;
        f.lock_exclusive().ok()?;
        let mut raw = String::new();
        f.read_to_string(&mut raw).ok()?;
        let mut state: serde_json::Map<String, Value> = if raw.trim().is_empty() {
            serde_json::Map::new()
        } else {
            serde_json::from_str(&raw).unwrap_or_default()
        };
        let prev = state
            .get("emission_tier")
            .and_then(Value::as_i64)
            .unwrap_or(0);
        let newly = tier > prev;
        if newly {
            state.insert("emission_tier".into(), json!(tier));
            let payload = serde_json::to_string(&Value::Object(state)).ok()?;
            f.seek(SeekFrom::Start(0)).ok()?;
            f.set_len(0).ok()?;
            f.write_all(payload.as_bytes()).ok()?;
        }
        Some(newly)
    };
    run().unwrap_or(false)
}

fn thousands(n: i64) -> String {
    let s = n.to_string();
    let mut out = String::new();
    for (i, c) in s.chars().enumerate() {
        if i > 0 && (s.len() - i) % 3 == 0 {
            out.push(',');
        }
        out.push(c);
    }
    out
}
