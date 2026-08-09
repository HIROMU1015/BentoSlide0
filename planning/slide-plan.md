# Slide plan — BentoSlide0を実際に使う

## Section 1: promise

### Slide 1 — PDFを置いて、話す。編集できるBentoSlideができる

- 左に一次資料、中央にChatGPT Workの会話、右にBento editorの完成画面を置く。
- 主文は「手順を覚えるのではなく、資料について会話する」。
- 小さく「local / editable / reviewable」を添える。

### Slide 2 — 利用者が最初にすること

- 3枚の大きなstep card：リポジトリを資料ごとに複製／一次資料を置く／ChatGPT Workへ希望を伝える。
- Windowsでは同じlauncherから現在位置に合う画面へ入れることを補足する。
- 内部ファイル名やCLIは主画面から外す。

## Section 2: request-and-plan

### Slide 3 — 最初の依頼は、普段の言葉でよい

- 実例の吹き出し：「この論文を、初見の人にも分かる学会発表資料に。数式は残して、結果を先に伝えたい」
- 右側に、依頼と一次資料が制作の起点として保存される様子を小さく示す。
- journey barは「依頼」を強調。

### Slide 4 — 最初に確認するのは構成案

- ChatGPT Workが返す説明方針、story、slide planを3枚のcardで示す。
- 利用者の返答例：「結論を先に」「方法は2枚に」「この順番で進めて」。
- 「承認前に勝手に制作を完了しない」を安心材料として示す。
- journey barは「計画」を強調。

## Section 3: section-work

### Slide 5 — 1 sectionずつ、見て・言って・触って・確定する

- 横方向の4場面：HTML previewを見る／自然文で直す／Bentoで内容・構造を編集／sectionを確定。
- 発話例：「図を大きく」「説明を1行減らす」「ここはこの内容でOK」。
- 全体一括のやり直しではなく、対象sectionだけを更新することを補足。
- journey barは「section制作」を強調。

### Slide 6 — 開く入口は同じ。画面は現在位置で変わる

- `start_deck_workspace.cmd`を入口として中央に置き、HTML preview／Bento authoring editor／final editor／完成版viewerへ分岐するUI mock。
- 利用者がstage、port、出力ファイルを選ばないことを明記。
- 内部では`deck.yaml`が現在位置を一元管理する、と小さく示す。
- journey barは「section制作」を継続。

## Section 4: approval-and-final

### Slide 7 — 内容を確定してから、見た目を仕上げる

- 左半分にBento authoring：文章、数式、図表、slide構造。
- 右半分にfinalization：位置、サイズ、style、背景、重なり。
- 中央に資料全体の内容確認checkpointを置く。
- 「finalで内容を直接書き換えない」ことを、制限ではなく保護として説明。
- journey barは「全体確認→最終調整」を強調。

## Section 5: revision-and-summary

### Slide 8 — 完成後の修正にも、戻り道がある

- 2分岐：見た目だけならfinalization再開／内容なら該当sectionへ戻り、再確認後に新finalへ。
- 内容修正中も旧finalは保持され、切替時には旧finalとbaselineがarchiveされることを示す。
- generatedを上書きして帳尻を合わせないことを補足。
- journey barは「完成後の修正」を強調。

### Slide 9 — 覚えるのは、資料を置く・話す・節目で確認する

- 3つの大きな動詞を再提示する。
- 下段に「裏側では source of truth / approval / revision / transaction が守る」を小さく配置。
- 最後の一文：「BentoSlide0は、変換器ではなく、完成まで戻れる共同制作workflow」。
- 1枚目のbefore/after visualへ呼応して閉じる。

## 共通レイアウト

- Slide 3〜8の下端に `依頼 → 計画 → section制作 → 全体確認 → 最終調整 → 完成後の修正` を固定表示する。
- 現在位置だけ青緑、利用者の承認箇所はオレンジのdiamondで示す。
- 技術用語は本文より小さな「裏側」cardに限定し、利用者の操作と混ぜない。
