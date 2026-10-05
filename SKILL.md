---
name: second-brain
description: ナレッジのフォルダ（wiki、Claude Code の memory、rules）の古い情報を見つけ、更新・統合・退避して、Claude Code が最初に読む索引を作り直す。「refresh」「古い情報を整理」「記憶を更新」「ナレッジの棚卸し」、新しい資料を入れた直後に使う。
---

# second-brain

溜めるだけのナレッジは、古い情報と新しい情報が並び、Claude Code がどれを信じればよいか分からなくなる。このスキルは、ページごとに「種類」「いつの情報か」「何か月で古くなるか」を付け、Jev（判断専用のモデル）に判定させ、古いページを索引から外す。

## 手順

vault（ナレッジのフォルダの親）のルートで実行する。スクリプトは `scripts/refresh.py`。

1. `python3 scripts/refresh.py scan` — ページを読む。いつの情報かは、frontmatter の日付、元の資料の日付、git でページが作られた日の順に取る
2. `python3 scripts/refresh.py judge` — Jev に、ページごとに種類、時間で変わる情報か、新しい情報に置き換わっているか、軸の外か、を聞く。似たページの組には、まとめるべきか、食い違いがあるか、どちらが新しいかを聞く。結果は内容のハッシュ値でキャッシュし、変わったページだけ聞き直す
3. `python3 scripts/refresh.py plan` — 判定から「残す・更新・統合・退避」を決め、`.refresh/plan.md` に書く
4. `.refresh/plan.md` をユーザーに見せる。**退避と統合はユーザーの承認のあとだけ行う**
5. `python3 scripts/refresh.py apply --yes` — frontmatter に `kind` `expires` `refreshed` を書き、退避するページを `_archive/` に移し、索引（既定は `wiki/INDEX.md`）を作り直す。`--yes` を付けなければ、索引の見本だけを `.refresh/INDEX.preview.md` に書く

新しい資料を wiki に入れた直後は `python3 scripts/refresh.py new <ファイル>` を実行する。新しい情報で古くなった既存ページ（「更新する」）と、新しいページを作らずに足すべき既存ページ（「足す」）が出る。

## 決め方（既定値。`refresh.config.json` で変えられる）

| 判定 | 結果 |
|---|---|
| 置き換わっている 0.6 以上 | 退避 |
| 軸の外 0.7 以上 | 退避 |
| 種類ごとの期限を過ぎ、時間で変わる情報 0.5 以上 | 更新（索引に「確認待ち」で載せる） |
| 置き換わっている 0.4 以上、かつ時間で変わる情報 0.5 以上 | 更新 |
| 2ページが同じトピック 0.75 以上 | 統合の候補 |
| 2ページに食い違い 0.6 以上 | 古い方を更新 |

期限の既定値: 最新ニュース3か月、製品・ツール6か月、トレンド6か月、手順12か月、学術は期限なし。自分の企画・個人のメモは索引に載せない。

## 参照の記録

`hooks/log_read.py` を PostToolUse（Read）のフックに入れると、Claude Code が読んだページを `.refresh/reads.jsonl` に記録する。索引では、よく読まれるページを上に置く。

```json
{"hooks": {"PostToolUse": [{"matcher": "Read", "hooks": [{"type": "command", "command": "python3 .claude/skills/second-brain/hooks/log_read.py"}]}]}}
```

## Claude Code が読むとき

- 最初に索引だけを読み、必要なページだけを開く
- 「確認待ち」のページを根拠にするときは、古い可能性があると添える
- `_archive/` は、ユーザーに聞かれたときだけ読む

## 外に送るもの

Jev に送るのは、ページのタイトル、タグ、日付、本文の先頭700字だけ。日記や個人の文書のフォルダは `dir` に入れない。
