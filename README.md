# second-brain

Claude Code のためのエージェントスキル。ナレッジのフォルダ（wiki、Claude Code の memory、rules）から古くなった情報を見つけ、更新・統合・退避して、Claude Code が最初に読む索引を作り直す。

溜めるだけのナレッジは、古い情報と新しい情報が並び、Claude Code がどれを信じればよいか分からなくなる。記憶するだけでなく、記憶を更新し続けるところまで行う。

## 何をするか

1. ページごとに「何の情報か（種類）」「いつの情報か」「何か月で古くなるか」を付ける
2. 判断専用のモデル [Jev](https://typesafe.ai) に、ページが新しい情報に置き換わっていないか、似たページと重複・食い違いがないかを数値で判定させる
3. 古いページは消さずに `_archive/` へ移し、索引から外す。期限を過ぎたページは「確認待ち」として索引に残す
4. Claude Code が読んだページを記録し、よく読まれるページを索引の上に置く

## 入れ方

```sh
git clone https://github.com/strickland-inc/second-brain .claude/skills/second-brain
```

Jev の API キーを環境変数 `TYPESAFE_API_KEY` に入れる。Python 3 の標準ライブラリだけで動く。

## 使い方

vault のルートで実行する。

```sh
python3 .claude/skills/second-brain/scripts/refresh.py scan    # ページを読む
python3 .claude/skills/second-brain/scripts/refresh.py judge   # Jev で判定する（変わったページだけ聞き直す）
python3 .claude/skills/second-brain/scripts/refresh.py plan    # .refresh/plan.md に計画を書く
python3 .claude/skills/second-brain/scripts/refresh.py apply   # 索引の見本だけ作る
python3 .claude/skills/second-brain/scripts/refresh.py apply --yes  # 計画を反映する
python3 .claude/skills/second-brain/scripts/refresh.py new wiki/新しいページ.md  # 新しい情報で古くなったページを探す
```

対象のフォルダ、種類ごとの期限、判定の境目は、vault のルートの `refresh.config.json` で変える（既定値は `scripts/refresh.py` の `DEFAULT_CONFIG`）。

## 試した結果

STRICKLAND の Claude Code の記憶（22件）で試すと、「退避」と判定された2件は、どちらも実際に新しい決定で上書きされていた記憶だった。

## Auto Dream との違い

Anthropic の Auto Dream は、Claude Code の memory を整理する。このスキルは、memory 以外のナレッジのフォルダにも使え、種類ごとの期限と Jev の数値で判定し、なぜ古いと判断したかを表で残す。

## ライセンス

MIT
