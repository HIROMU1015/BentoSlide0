# Start here

このリポジトリは1資料分のローカルBentoSlide制作環境です。一次資料を`sources/private/`へ置き、ChatGPT Workへ作りたい資料を普段の言葉で伝えてください。依頼内容は`REQUEST.md`へ保存され、参照資料が1件ならmanifestへ自動登録されます。

画像は`images/`へ置きます。自分で追加する画像は`images/user/`、Work/GPTが資料から切り出した画像は`images/extracted/`、生成した説明画像は`images/generated/`です。登録・出典・Bentoへの埋め込みはエージェントが行うため、`deck/`やregistryを直接編集する必要はありません。

例えば「この資料を、初見の人にも分かる8枚の説明資料にして」「第2部の図を簡潔にして」のように依頼できます。従来の短文コマンドも互換経路として使えます。

Work/Codexは自分で`deck.yaml`と`python -m scripts.deck_workflow status --json`を確認し、`workflow/WORKFLOW.md`の現在stageだけを実行します。ユーザーにYAML更新、ファイル名、section番号、registry更新、log、port、CLI操作を求めません。

## Sources and roles

- 一次資料は`sources/source-manifest.yaml`で列挙し、`deck.yaml`の`project.primarySource`で曖昧さを解消します。private sourceはGitへ追加しません。
- ChatGPT Workは資料理解、説明方針、story、single HTML/registry、視覚review、Bento内容編集、最終layout判断を担当します。
- Codexは状態管理、HTML-first変換、diagnostics、browser evidence、transaction/recovery、launcher、技術検証を担当します。

通常はsectionを1つずつ、`HTML作成→見た目確認→Bentoへ昇格→Bento編集→section確定`の順で仕上げます。人が判断するのは各sectionのHTML昇格可否とBento版の確定、最後の資料全体の内容、finalの仕上がりです。承認は自動で通過しません。

Workは各slideで「文章だけより図が理解を助けるか」も判断します。単純な構造・関係・flow・比較・状態変化は編集可能なBento native図を優先し、原図そのものが重要なら出典付きでPDF等から切り出し、視覚的な比喩が必要な場合だけ生成画像をlocal assetとして使います。架空のデータ、実験・benchmark結果、定量plot、数式画像は生成しません。詳細は`docs/visual-workflow.md`です。

Windowsでは`start_deck_workspace.cmd`をダブルクリックすると、現在位置に応じてHTML preview、authoring editor、final editor、または完成版viewerを自動選択し、URLをclipboardへ入れます。既存finalのresetや通常ブラウザーの自動起動は行いません。内部ID、revision、registry、CLIは通常表示せず、必要な場合だけ`status --json`で完全な機械状態を確認できます。
