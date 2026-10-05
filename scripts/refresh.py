#!/usr/bin/env python3
"""second-brain refresh: ナレッジのフォルダの古い情報を見つけ、更新・統合・退避し、Claude Code が読む索引を作り直す。

使い方（vault のルートで実行）:
  python3 refresh.py scan                 ページを読み、状態を .refresh/state.json に書く
  python3 refresh.py judge [--limit N]    Jev で各ページと似たページの組を判定する（結果はキャッシュ）
  python3 refresh.py plan                 判定から「残す・更新・統合・退避」を決め、.refresh/plan.md に書く
  python3 refresh.py apply [--yes]        plan を反映する。--yes が無ければ何も書き換えない
  python3 refresh.py new <file>           新しく入った情報と食い違う既存ページを探す
  python3 refresh.py stats                前後の数字（索引に載るページ数、文字数）を出す

設定は vault ルートの refresh.config.json（無ければ既定値）。標準ライブラリだけで動く。
"""
import argparse, datetime as dt, hashlib, json, os, re, shutil, subprocess, sys, urllib.request

JEV_API = os.environ.get("JEV_API", "https://api.typesafe.ai/v1/systemone")
JEV_KEY = os.environ.get("TYPESAFE_API_KEY", "x")  # プロキシが認証を足す環境では何でもよい

DEFAULT_CONFIG = {
    "dir": "wiki",
    "archive_dir": "wiki/_archive",
    "index_file": "wiki/INDEX.md",
    "exclude": ["index.md", "log.md", "_archive/"],
    "axis": "AI・デザインの最新情報、トレンド、学術（技術の原理）",
    # 種類ごとの「何か月で古くなるか」。null は期限なし。index:false は索引に載せない
    "kinds": {
        "news": {"label": "最新ニュース・発表", "months": 3},
        "tool": {"label": "製品・ツール・サービスの紹介", "months": 6},
        "trend": {"label": "トレンド・手法・事例", "months": 6},
        "howto": {"label": "手順・設定・使い方", "months": 12},
        "academic": {"label": "学術・原理・普遍的な考え方", "months": None},
        "personal": {"label": "自分の企画・個人のメモ", "months": None, "index": False},
        "hub": {"label": "目次・まとめページ", "months": None},
    },
    "thresholds": {"superseded": 0.6, "maybe_superseded": 0.4, "time_sensitive": 0.5, "conflict": 0.6, "same_topic": 0.75, "off_axis": 0.7},
    "max_pairs": 60,
    "unread_days": 90,
}

WORK = ".refresh"
TODAY = dt.date.today()


# ---------- 読み込み ----------

def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if os.path.exists("refresh.config.json"):
        user = json.load(open("refresh.config.json"))
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    return cfg


def split_frontmatter(text):
    m = re.match(r"---\n(.*?)\n---\n?", text, re.S)
    if not m:
        return {}, text, ""
    fm = {}
    for line in m.group(1).splitlines():
        km = re.match(r"^([\w-]+):\s*(.*)$", line)
        if km:
            fm[km.group(1)] = km.group(2).strip().strip('"')
    return fm, text[m.end():], m.group(1)


def parse_date(s):
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s or "")
    return dt.date(int(m[1]), int(m[2]), int(m[3])) if m else None


def iter_pages(cfg):
    root = cfg["dir"]
    for d, _, files in os.walk(root):
        for f in sorted(files):
            if not f.endswith(".md"):
                continue
            p = os.path.join(d, f)
            rel = os.path.relpath(p, root)
            if any(rel == e or rel.startswith(e) for e in cfg["exclude"]):
                continue
            if os.path.abspath(p) == os.path.abspath(cfg["index_file"]):
                continue
            yield p


def first_sentence(body, n=70):
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "|", "```", "-", ">", "!")):
            continue
        line = re.sub(r"\[\[([^\]|#]+)(?:[^\]]*)\]\]", r"\1", line)
        line = re.sub(r"\*\*|`", "", line)
        s = re.split(r"(?<=。)", line)[0]
        return s[:n] + ("…" if len(s) > n else "")
    return ""


def read_log():
    """hooks/log_read.py が書いた参照の記録。ページごとの最後に読まれた日と回数。"""
    out = {}
    p = os.path.join(WORK, "reads.jsonl")
    if not os.path.exists(p):
        return out
    for line in open(p):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        o = out.setdefault(r["path"], {"count": 0, "last": None})
        o["count"] += 1
        o["last"] = max(o["last"] or r["date"], r["date"])
    return out


def info_date(path, fm):
    """いつの情報か: frontmatter の日付 > 元の資料（source）の日付 > git でページが作られた日。"""
    for k in ("updated", "created", "published"):
        if parse_date(fm.get(k)):
            return fm[k][:10], k
    src = fm.get("source", "")
    if src.startswith("raw/") and os.path.exists(src):
        sfm, _, _ = split_frontmatter(open(src, encoding="utf-8", errors="ignore").read())
        for k in ("published", "created"):
            if parse_date(sfm.get(k)):
                return sfm[k][:10], "source"
    try:
        out = subprocess.run(["git", "log", "--reverse", "--format=%cs", "--", path], capture_output=True, text=True).stdout.split()
        if out:
            return out[0], "git"
    except OSError:
        pass
    return "", ""


def scan(cfg):
    pages = {}
    for p in iter_pages(cfg):
        text = open(p, encoding="utf-8").read()
        fm, body, _ = split_frontmatter(text)
        name = os.path.splitext(os.path.basename(p))[0]
        links = sorted(set(re.findall(r"\[\[([^\]|#]+)", body)))
        pages[p] = {
            "name": name,
            "title": fm.get("title") or name,
            "tags": re.findall(r"[^\[\],\"'\s-][^\[\],\"']*", fm.get("tags", "")),
            "updated": info_date(p, fm)[0],
            "date_from": info_date(p, fm)[1],
            "tldr": fm.get("tldr") or first_sentence(body),
            "chars": len(text),
            "hash": hashlib.sha256(text.encode()).hexdigest()[:16],
            "links_out": links,
            "head": re.sub(r"\s+", " ", body)[:700],
        }
    names = {v["name"]: k for k, v in pages.items()}
    for v in pages.values():
        v["links_in"] = 0
    for v in pages.values():
        for l in v["links_out"]:
            if l in names and pages[names[l]] is not v:
                pages[names[l]]["links_in"] += 1
    reads = read_log()
    for k, v in pages.items():
        v["reads"] = reads.get(k, {"count": 0, "last": None})
    os.makedirs(WORK, exist_ok=True)
    json.dump({"date": TODAY.isoformat(), "pages": pages}, open(os.path.join(WORK, "state.json"), "w"), ensure_ascii=False, indent=1)
    print(f"{len(pages)} ページ、合計 {sum(v['chars'] for v in pages.values()):,} 文字")
    return pages


# ---------- Jev ----------

def jev(state, questions):
    body = {"model": "jev-latest", "state": state, "questions": questions}
    req = urllib.request.Request(JEV_API, data=json.dumps(body).encode(),
                                 headers={"Authorization": "Bearer " + JEV_KEY, "Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=180))


def periods():
    y = TODAY.year
    return {
        "old": f"{y - 2}年以前",
        "y1": f"{y - 1}年",
        "recent": f"{y}年（今年）",
        "timeless": "時期と無関係",
    }


def page_questions(i, cfg):
    kinds = {k: v["label"] for k, v in cfg["kinds"].items()}
    k = f"p{i}"
    return {
        f"{k}__kind": {"type": "choice", "instructions": f"`{k}` のページの種類", "criteria": kinds},
        f"{k}__era": {"type": "choice", "instructions": f"`{k}` に書かれている情報はいつの時点のものか（`{k}.updated` が空のときは本文から推定する）", "criteria": periods()},
        f"{k}__time_sensitive": {"type": "noul", "instructions": f"`{k}` の中心は、時間がたつと変わる情報（版、価格、順位、最新の製品、数字）である"},
        f"{k}__superseded": {"type": "noul", "instructions": f"`today.date` の時点で、`{k}` の内容はより新しい手段・製品・版・研究に置き換わっている"},
        f"{k}__off_axis": {"type": "noul", "instructions": f"`{k}` は `axis.text` のどれにも当たらず、このナレッジに置く理由が無い"},
    }


def judge(cfg, limit=None):
    state = json.load(open(os.path.join(WORK, "state.json")))
    pages = state["pages"]
    cache_p = os.path.join(WORK, "judgments.json")
    cache = json.load(open(cache_p)) if os.path.exists(cache_p) else {"pages": {}, "pairs": {}}
    todo = [p for p, v in pages.items() if cache["pages"].get(p, {}).get("hash") != v["hash"]]
    if limit:
        todo = todo[:limit]
    base = {"today": {"date": TODAY.isoformat()}, "axis": {"text": cfg["axis"]}}
    for b in range(0, len(todo), 10):
        batch = todo[b:b + 10]
        st, qs = dict(base), {}
        for i, p in enumerate(batch):
            v = pages[p]
            st[f"p{i}"] = {"title": v["title"], "tags": ", ".join(v["tags"]), "updated": v["updated"], "text": v["head"]}
            qs.update(page_questions(i, cfg))
        a = jev(st, qs)["answers"]
        for i, p in enumerate(batch):
            k = f"p{i}"
            cache["pages"][p] = {
                "hash": pages[p]["hash"],
                "kind": a[f"{k}__kind"]["choice"],
                "era": a[f"{k}__era"]["choice"],
                "time_sensitive": a[f"{k}__time_sensitive"]["noul"],
                "superseded": a[f"{k}__superseded"]["noul"],
                "off_axis": a[f"{k}__off_axis"]["noul"],
            }
        json.dump(cache, open(cache_p, "w"), ensure_ascii=False, indent=1)
        print(f"ページ判定 {min(b + 10, len(todo))}/{len(todo)}")

    # 似たページの組: タグが2つ以上重なる、または題の語が重なる組を候補にして、重複と矛盾を聞く
    def words(v):
        return set(re.findall(r"[A-Za-z][A-Za-z0-9.+-]{2,}|[一-龥ァ-ヴー]{2,}", v["title"]))
    keys = list(pages)
    cands = []
    for x in range(len(keys)):
        for y in range(x + 1, len(keys)):
            a1, b1 = pages[keys[x]], pages[keys[y]]
            score = len(set(a1["tags"]) & set(b1["tags"])) + 2 * len(words(a1) & words(b1))
            if a1["name"] in b1["links_out"] or b1["name"] in a1["links_out"]:
                score += 1
            if score >= 3:
                cands.append((score, keys[x], keys[y]))
    cands.sort(reverse=True)
    cands = cands[: cfg["max_pairs"]]
    todo_pairs = [(x, y) for _, x, y in cands
                  if cache["pairs"].get(f"{x}||{y}", {}).get("hash") != pages[x]["hash"] + pages[y]["hash"]]
    for b in range(0, len(todo_pairs), 8):
        batch = todo_pairs[b:b + 8]
        st, qs = {}, {}
        for i, (x, y) in enumerate(batch):
            st[f"a{i}"] = {"title": pages[x]["title"], "updated": pages[x]["updated"], "text": pages[x]["head"]}
            st[f"b{i}"] = {"title": pages[y]["title"], "updated": pages[y]["updated"], "text": pages[y]["head"]}
            qs[f"r{i}__same"] = {"type": "noul", "instructions": f"`a{i}` と `b{i}` は同じトピックを扱っていて、1ページにまとめた方がよい"}
            qs[f"r{i}__conflict"] = {"type": "noul", "instructions": f"`a{i}` と `b{i}` には、同じ事実について食い違う記述がある"}
            qs[f"r{i}__newer"] = {"type": "choice", "instructions": f"`a{i}` と `b{i}` のどちらが新しく正しい情報を持っているか", "criteria": {"a": f"a{i}", "b": f"b{i}", "same": "差が無い"}}
        a = jev(st, qs)["answers"]
        for i, (x, y) in enumerate(batch):
            cache["pairs"][f"{x}||{y}"] = {
                "hash": pages[x]["hash"] + pages[y]["hash"],
                "same": a[f"r{i}__same"]["noul"],
                "conflict": a[f"r{i}__conflict"]["noul"],
                "newer": a[f"r{i}__newer"]["choice"],
            }
        json.dump(cache, open(cache_p, "w"), ensure_ascii=False, indent=1)
        print(f"組の判定 {min(b + 8, len(todo_pairs))}/{len(todo_pairs)}")


# ---------- 決める ----------

def age_months(updated, era):
    d = parse_date(updated)
    if d:
        return (TODAY - d).days / 30.4
    return {"old": 30, "y1": 15, "recent": 3, "timeless": None}.get(era)


def plan(cfg):
    state = json.load(open(os.path.join(WORK, "state.json")))
    pages = state["pages"]
    cache = json.load(open(os.path.join(WORK, "judgments.json")))
    th = cfg["thresholds"]
    actions = {}
    for p, v in pages.items():
        j = cache["pages"].get(p)
        if not j:
            continue
        kind = cfg["kinds"].get(j["kind"], {})
        age = age_months(v["updated"], j["era"])
        r = v["reads"]
        unread = r["last"] is None or (TODAY - parse_date(r["last"])).days > cfg["unread_days"]
        act, why = "keep", ""
        if j["superseded"] >= th["superseded"]:
            act, why = "archive", f"新しい情報に置き換わっている（{j['superseded']:.2f}）"
        elif j["off_axis"] >= th["off_axis"] and kind.get("index", True):
            act, why = "archive", f"ナレッジの軸の外（{j['off_axis']:.2f}）"
        elif kind.get("months") and age is not None and age > kind["months"] and j["time_sensitive"] >= th["time_sensitive"]:
            act, why = "update", f"{kind['label']}で{age:.0f}か月たっている（期限{kind['months']}か月、時間で変わる度合い{j['time_sensitive']:.2f}）"
        elif j["superseded"] >= th["maybe_superseded"] and j["time_sensitive"] >= th["time_sensitive"]:
            act, why = "update", f"置き換わっている可能性（{j['superseded']:.2f}）があり、時間で変わる情報が中心（{j['time_sensitive']:.2f}）"
        actions[p] = {"action": act, "why": why, "kind": j["kind"], "era": j["era"], "unread": unread,
                      "in_index": kind.get("index", True) and act != "archive"}
    merges, conflicts = [], []
    for key, r in cache["pairs"].items():
        x, y = key.split("||")
        if x not in actions or y not in actions or "archive" in (actions[x]["action"], actions[y]["action"]):
            continue
        if "personal" in (actions[x]["kind"], actions[y]["kind"]):
            continue  # 自分の企画は元の資料と違って当然なので、食い違いとして扱わない
        old = y if r["newer"] == "a" else x if r["newer"] == "b" else None
        if r["same"] >= th["same_topic"]:
            merges.append({"pages": [x, y], "score": r["same"], "older": old})
        elif r["conflict"] >= th["conflict"]:
            conflicts.append({"pages": [x, y], "score": r["conflict"], "older": old})
            if old and actions[old]["action"] == "keep":
                actions[old].update(action="update", why=f"{os.path.basename(x if old == y else y)} と食い違う（{r['conflict']:.2f}）")
    out = {"date": TODAY.isoformat(), "actions": actions, "merges": merges, "conflicts": conflicts}
    json.dump(out, open(os.path.join(WORK, "plan.json"), "w"), ensure_ascii=False, indent=1)

    def nm(p):
        return pages[p]["title"]
    lines = [f"# refresh の計画（{TODAY}）", "", "まだ何も書き換えていない。`apply --yes` で反映する。", ""]
    cnt = {a: sum(1 for v in actions.values() if v["action"] == a) for a in ("keep", "update", "archive")}
    lines += [f"残す {cnt['keep']}、更新 {cnt['update']}、退避 {cnt['archive']}、統合の候補 {len(merges)} 組、食い違い {len(conflicts)} 組", ""]
    for a, h in (("archive", "退避（_archive へ移し、索引から外す）"), ("update", "更新（索引には「確認待ち」で載せる）")):
        lines += [f"## {h}", "", "| ページ | 種類 | 理由 |", "|---|---|---|"]
        for p, v in sorted(actions.items(), key=lambda kv: nm(kv[0])):
            if v["action"] == a:
                lines.append(f"| {nm(p)} | {cfg['kinds'].get(v['kind'], {}).get('label', v['kind'])} | {v['why']} |")
        lines.append("")
    lines += ["## 統合の候補", ""]
    lines += [f"- {nm(m['pages'][0])} ＋ {nm(m['pages'][1])}（{m['score']:.2f}）" for m in merges] or ["なし"]
    lines += ["", "## 食い違い", ""]
    lines += [f"- {nm(c['pages'][0])} ↔ {nm(c['pages'][1])}（{c['score']:.2f}、古い方: {nm(c['older']) if c['older'] else '不明'}）" for c in conflicts] or ["なし"]
    open(os.path.join(WORK, "plan.md"), "w").write("\n".join(lines) + "\n")
    print("\n".join(lines[:5]))


# ---------- 反映 ----------

def build_index(cfg, pages, actions, out=None):
    lines = ["---", "title: INDEX（refresh が作る）", f"updated: {TODAY}", "---", "",
             "# INDEX", "",
             "Claude Code はまずこのファイルだけを読み、必要なページだけを開く。ここに無いページ（_archive、個人のメモ）は、聞かれたときだけ読む。", ""]
    stale = []
    for k, kind in cfg["kinds"].items():
        rows = [(p, v) for p, v in pages.items() if actions.get(p, {}).get("kind") == k and actions[p]["in_index"]]
        rows = [r for r in rows if actions[r[0]]["action"] == "keep"] + [r for r in rows if actions[r[0]]["action"] == "update"]
        if not rows:
            continue
        lines += [f"## {kind['label']}", ""]
        # よく参照されるページ（読まれた回数、被リンク）を上に置く
        rows.sort(key=lambda r: (actions[r[0]]["action"] != "keep", -r[1]["reads"]["count"], -r[1]["links_in"], r[1]["title"]))
        for p, v in rows:
            mark = "（確認待ち: 古い可能性）" if actions[p]["action"] == "update" else ""
            lines.append(f"- [[{v['name']}]] {v['tldr']}{mark}")
            if mark:
                stale.append(v["name"])
        lines.append("")
    open(out or cfg["index_file"], "w", encoding="utf-8").write("\n".join(lines))


def set_frontmatter(path, fields):
    text = open(path, encoding="utf-8").read()
    fm, body, raw = split_frontmatter(text)
    lines = [l for l in raw.splitlines() if not re.match(rf"^({'|'.join(fields)}):", l)] if raw else []
    lines += [f"{k}: {v}" for k, v in fields.items()]
    open(path, "w", encoding="utf-8").write("---\n" + "\n".join(lines) + "\n---\n" + body)


def apply(cfg, yes):
    state = json.load(open(os.path.join(WORK, "state.json")))
    pages = state["pages"]
    pl = json.load(open(os.path.join(WORK, "plan.json")))
    actions = pl["actions"]
    if not yes:
        keep = {p: v for p, v in pages.items() if actions.get(p, {}).get("action") != "archive"}
        build_index(cfg, keep, actions, os.path.join(WORK, "INDEX.preview.md"))
        print("--yes が無いので、ページは書き換えていない。索引の見本を .refresh/INDEX.preview.md に書いた。")
        return
    os.makedirs(cfg["archive_dir"], exist_ok=True)
    for p, v in actions.items():
        kind = cfg["kinds"].get(v["kind"], {})
        fields = {"kind": v["kind"], "refreshed": TODAY.isoformat()}
        if kind.get("months"):
            base = parse_date(pages[p]["updated"]) or TODAY
            fields["expires"] = (base + dt.timedelta(days=int(30.4 * kind["months"]))).isoformat()
        if v["action"] == "update":
            fields["status"] = "stale"
        set_frontmatter(p, fields)
        if v["action"] == "archive":
            set_frontmatter(p, {"archived": TODAY.isoformat(), "archived_reason": v["why"]})
            shutil.move(p, os.path.join(cfg["archive_dir"], os.path.basename(p)))
    remaining = {p: v for p, v in pages.items() if actions.get(p, {}).get("action") != "archive"}
    build_index(cfg, remaining, actions)
    print(f"反映した。索引: {cfg['index_file']}")


def stats(cfg):
    state = json.load(open(os.path.join(WORK, "state.json")))
    pages = state["pages"]
    pl = json.load(open(os.path.join(WORK, "plan.json")))
    acts = pl["actions"]
    all_chars = sum(v["chars"] for v in pages.values())
    live = [p for p in pages if acts.get(p, {}).get("in_index", True)]
    live_chars = sum(pages[p]["chars"] for p in live)
    fresh = [p for p in live if acts.get(p, {}).get("action") == "keep"]
    print(f"前: Claude Code の読む候補 {len(pages)} ページ（{all_chars:,} 文字）、古さの印 0 ページ")
    print(f"後: 索引に載るページ {len(live)}（{live_chars:,} 文字、{100 - 100 * live_chars / all_chars:.0f}% 減）。そのうち確かな情報 {len(fresh)}、確認待ち {len(live) - len(fresh)}")


def new_info(cfg, path):
    """新しく入った情報が、既存ページのどれを古くするかを判定する（記憶の更新）。"""
    text = open(path, encoding="utf-8").read()
    fm, body, _ = split_frontmatter(text)
    state = json.load(open(os.path.join(WORK, "state.json")))
    pages = state["pages"]
    words = set(re.findall(r"[A-Za-z][A-Za-z0-9.+-]{2,}|[一-龥ァ-ヴー]{2,}", (fm.get("title", "") + " " + body)[:3000]))
    scored = []
    for p, v in pages.items():
        if os.path.abspath(p) == os.path.abspath(path):
            continue
        w = set(re.findall(r"[A-Za-z][A-Za-z0-9.+-]{2,}|[一-龥ァ-ヴー]{2,}", v["title"] + " " + v["head"]))
        scored.append((len(words & w), p))
    scored.sort(reverse=True)
    cands = [p for s, p in scored[:12] if s > 0]
    st = {"new": {"title": fm.get("title", os.path.basename(path)), "text": re.sub(r"\s+", " ", body)[:1500]}, "today": {"date": TODAY.isoformat()}}
    qs = {}
    for i, p in enumerate(cands):
        st[f"p{i}"] = {"title": pages[p]["title"], "updated": pages[p]["updated"], "text": pages[p]["head"]}
        qs[f"q{i}__replace"] = {"type": "noul", "instructions": f"`new` の情報によって、`p{i}` の記述の一部が古くなった、または誤りになった"}
        qs[f"q{i}__same"] = {"type": "noul", "instructions": f"`new` は `p{i}` と同じトピックで、新しいページを作らず `p{i}` に足した方がよい"}
    if not qs:
        print("関係しそうなページが無い")
        return
    a = jev(st, qs)["answers"]
    for i, p in enumerate(cands):
        r, s = a[f"q{i}__replace"]["noul"], a[f"q{i}__same"]["noul"]
        flag = "更新する" if r >= cfg["thresholds"]["superseded"] else "足す" if s >= cfg["thresholds"]["same_topic"] else ""
        print(f"{r:.2f} {s:.2f} {flag:4} {pages[p]['title']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["scan", "judge", "plan", "apply", "stats", "new"])
    ap.add_argument("path", nargs="?")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--yes", action="store_true")
    a = ap.parse_args()
    cfg = load_config()
    if a.cmd == "scan":
        scan(cfg)
    elif a.cmd == "judge":
        judge(cfg, a.limit)
    elif a.cmd == "plan":
        plan(cfg)
    elif a.cmd == "apply":
        apply(cfg, a.yes)
    elif a.cmd == "stats":
        stats(cfg)
    elif a.cmd == "new":
        if not a.path:
            sys.exit("new にはファイルのパスが要る")
        new_info(cfg, a.path)


if __name__ == "__main__":
    main()
