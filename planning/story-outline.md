# Story outline — 「仕組み」ではなく「使う一日」を見せる

## 中心メッセージ

BentoSlide0では、利用者は一次資料を置いて普段の言葉で相談し、節目だけ判断する。ChatGPT WorkとCodexが、計画、HTML preview、編集可能なBento、finalを現在位置に応じてつなぎ、内部の状態・承認・保存整合性を引き受ける。

## 物語

### 1. Promise — 何が変わるか

最初に、PDFや既存資料が「会話しながら直せるBentoSlide」になるbefore/afterを見せる。次に、利用者が開始時に行うのはリポジトリの複製、一次資料の配置、ChatGPT Workへの依頼だけだと具体化する。

### 2. Request and plan — 最初の会話

学会発表資料を依頼する実際の文章例を示す。システムはすぐに完成版を決め打ちせず、説明方針と構成案を返す。利用者は専門内容と話の順序を確認し、必要なら自然な言葉で直す。

### 3. Section work — 小さく確認しながら作る

全スライドを一括生成する印象を避け、section単位でHTMLを見て、フィードバックし、Bentoへ昇格し、Bento editorでも触り、確定する反復を見せる。同じ`start_deck_workspace.cmd`が現在位置に合う画面を選ぶため、利用者が内部stageやportを選ばないことも体験として示す。

### 4. Approval and final — 内容と見た目を分ける

全section確定後に資料全体の内容と構造を確認する。その後の最終調整では、文章やデータではなく配置、style、背景、重なりなどの見た目だけを仕上げる。この分離が意図しない内容変更を防ぐ。

### 5. Revision and takeaway — 完成後も戻れる

完成後の「少し位置を直したい」はfinalizationを再開し、「内容を直したい」は該当sectionへ戻るという2経路を並べる。内容変更時は再承認されるまで旧finalが保持され、新finalへ切り替えるときも旧版がarchiveされる。最後は、利用者が覚えるのは「資料を置く・話す・節目で確認する」であるとまとめる。

## 見終わった人に残す理解

- BentoSlide0は変換コマンド集ではなく、資料制作の共同作業環境である。
- 利用者は自然な言葉で進められ、内部IDやrevisionを扱わない。
- HTML previewは構成と見た目の確認、Bento authoringは内容・構造編集、finalizationは見た目の仕上げである。
- 人の承認は省略されず、generated、authoring、finalの役割が混ざらない。
- 完成後の修正にも安全な戻り道がある。
