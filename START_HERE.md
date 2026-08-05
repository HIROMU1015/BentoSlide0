# Start here

このリポジトリは1資料分のローカルBentoSlide制作環境です。一次資料を`sources/private/`へ置き、必要なら`REQUEST.md`へ希望を書いたら、ChatGPT Workへ次だけ伝えて開始できます。

```text
この資料を作成して
```

Work/Codexは自分で`deck.yaml`と`python -m scripts.deck_workflow status --json`を確認し、`workflow/WORKFLOW.md`の現在stageだけを実行します。ユーザーにYAML更新、ファイル名、section番号、registry更新、log、port、CLI操作を求めません。

## Sources and roles

- 一次資料は`sources/source-manifest.yaml`で列挙し、`deck.yaml`の`project.primarySource`で曖昧さを解消します。private sourceはGitへ追加しません。
- ChatGPT Workは資料理解、説明方針、story、single HTML/registry、視覚review、Bento内容編集、最終layout判断を担当します。
- Codexは状態管理、HTML-first変換、diagnostics、browser evidence、transaction/recovery、launcher、技術検証を担当します。

ユーザーが承認するのは、全体方針、各sectionの主要構図、Bento authoringの内容、完成finalの4種類です。承認は自動で通過しません。内容承認はauthoring documentとregistryのrevisionへ固定され、後続変更時は自動的にpendingへ戻ります。

Windowsでは`start_deck_workspace.cmd`をダブルクリックすると、現在stageに合うHTML previewまたはWork editorが起動し、URLがclipboardへ入ります。既存finalのresetや通常ブラウザーの起動は行いません。
