#!/usr/bin/env python3
"""PostToolUse(Read) フック: Claude Code が読んだナレッジのページを .refresh/reads.jsonl に記録する。
refresh.py はこの記録を使い、よく読まれるページを索引の上に置き、読まれないページを見分ける。"""
import datetime, json, os, sys

root = os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())
try:
    path = json.load(sys.stdin)["tool_input"]["file_path"]
except (ValueError, KeyError):
    sys.exit(0)
rel = os.path.relpath(path, root)
if rel.endswith(".md") and not rel.startswith(".."):
    os.makedirs(os.path.join(root, ".refresh"), exist_ok=True)
    with open(os.path.join(root, ".refresh", "reads.jsonl"), "a") as f:
        f.write(json.dumps({"path": rel, "date": datetime.date.today().isoformat()}) + "\n")
