# Start here

このリポジトリは1資料分のローカルBentoSlide制作環境です。一次資料を`sources/private/`へ置き、ChatGPT Workへ作りたい資料を普段の言葉で伝えてください。依頼内容は`REQUEST.md`へ保存され、参照資料が1件ならmanifestへ自動登録されます。

画像は`images/`へ置きます。自分で追加する画像は`images/user/`、Work/GPTが資料から切り出した画像は`images/extracted/`、生成した説明画像は`images/generated/`です。登録・出典・Bentoへの埋め込みはエージェントが行うため、`deck/`やregistryを直接編集する必要はありません。

例えば「この資料を、初見の人にも分かる8枚の説明資料にして」「第2部の図を簡潔にして」のように依頼できます。従来の短文コマンドも互換経路として使えます。

Work/Codexは自分で`deck.yaml`と`python -m scripts.deck_workflow status --json`を確認し、`workflow/WORKFLOW.md`の現在stageだけを実行します。ユーザーにYAML更新、ファイル名、section番号、registry更新、log、port、CLI操作を求めません。

## Sources and roles

- 一次資料は`sources/source-manifest.yaml`で列挙し、`deck.yaml`の`project.primarySource`で曖昧さを解消します。private sourceはGitへ追加しません。
- ChatGPT Workは資料理解、説明方針、story、single HTML/registry、視覚review、Bento内容編集、最終layout判断を担当します。
- Codexは状態管理、HTML-first変換、diagnostics、browser evidence、transaction/recovery、launcher、技術検証を担当します。

通常はまず資料全体のHTMLを作り、1つのpreviewで流れと見た目を確認します。気になる箇所を普段の言葉で伝えると、Work/GPTは正本を直接変更せず、指定slideだけでなく前後の接続、重複、用語、参照、まとめ、順序、枚数、section構成まで資料全体を見直します。局所修正で十分ならその旨を示し、関連slideも直すべきなら理由と対象を含む候補版を作ります。並べ替え・追加削除・section再編など大きな構成変更が望ましい場合は、局所修正へ黙って限定せず、また勝手に範囲を広げず、先に推奨案と影響範囲を提案します。slideごとに続けて修正を伝えた場合も、その都度、累積した変更が資料全体へ与える影響を再評価します。previewでは対象slideを`確認済み`または`要修正`にできます。全対象を確認した後の`この変更案全体を反映`で候補全体を一度だけ安全に適用し、自動検証後は更新後の現在案だけを表示します。slideごとの印は確認用で、部分的に適用する操作ではありません。その後、別の`このHTML全体でBentoSlideへ進む`で資料全体を確定します。sectionは内部の整理・出典・影響範囲に使い、通常はsectionごとの承認を求めません。最後にBento内容と最終の仕上がりをそれぞれ承認します。

Workは各slideで「文章だけより図が理解を助けるか」も判断します。単純な構造・関係・flow・比較・状態変化は編集可能なBento native図を優先し、原図そのものが重要なら出典付きでPDF等から切り出し、視覚的な比喩が必要な場合だけ生成画像をlocal assetとして使います。架空のデータ、実験・benchmark結果、定量plot、数式画像は生成しません。詳細は`docs/visual-workflow.md`です。

Windowsでは`start_deck_workspace.cmd`をダブルクリックすると、現在位置に応じてHTML preview、authoring editor、final editor、または完成版viewerを自動選択し、URLをclipboardへ入れます。GPTアプリから起動したHTML previewもアプリ本体とは独立したWindows processとして動き、アプリを終了・再起動しても`stop_deck_workspace.cmd`で停止するまで同じlocalhost URLを再利用できます。Windowsのサインアウト・再起動後はランチャーがstale sessionを安全に清掃して再起動します。既存finalのresetや通常ブラウザーの自動起動は行いません。内部ID、revision、registry、CLIは通常表示せず、必要な場合だけ`status --json`で完全な機械状態を確認できます。
