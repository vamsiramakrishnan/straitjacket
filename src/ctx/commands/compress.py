"""Local compression and deterministic prompt wrapping; no model requests."""
def cmd_compress(ws, ns):
    from pathlib import Path
    import json

    from ctx.context_compression import compress_stored, cache_wrap
    from ctx.store import Store

    try:
        text = Path(ns.file).read_bytes().decode('utf-8')
        store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
        result = compress_stored(store, text, ns.query, ns.budget_bytes, tuple(ns.require_line))
        if ns.cache_prefix:
            result['cache_wrap'] = cache_wrap(Path(ns.cache_prefix).read_text(), result['text'], ns.query)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result['status'] == 'selected' else 3
    except (OSError, ValueError) as exc:
        print(f"ctx compress: {exc}")
        return 2
