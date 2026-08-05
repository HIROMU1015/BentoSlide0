# BentoSlide0

論文や既存HTMLから、編集可能なBento Slidesをローカルで制作するための自己完結型リポジトリです。標準経路は、単一の固定サイズHTMLとregistryを視覚設計の正本にし、Chromiumで計算済み座標を取得し、公式Bento runtimeを変更せずネイティブ要素へ変換します。旧coordinate design JSON経路も互換用として維持しています。

## 最短の使い方

1. このリポジトリを資料ごとに複製します。
2. 一次資料を`sources/private/`へ置き、必要なら`REQUEST.md`を記入します。
3. ChatGPT Workで`この資料を作成して`と伝えます。
4. 構成確認後、`この方針で進めて`と伝えます。
5. HTMLの見た目を確認し、`次へ`または修正内容だけを伝えます。
6. Codexへ`BentoSlideに変換して`と伝えます。
7. Bentoの内容確認後、`この内容で確定`と伝えます。
8. Workで`最終調整を開始して`と伝え、レイアウト・styleのみを仕上げます。

ファイル名、section番号、状態更新、ログ、port、変換コマンドはエージェントが`deck.yaml`から判断します。`deck.yaml`はschema v2の唯一の機械状態です。Windowsでは`start_deck_workspace.cmd`がstageに応じてHTML previewまたはBento Work editorを起動し、URLだけをclipboardへコピーします。通常ブラウザーは開きません。

標準の正本は次の順に切り替わります。

```text
sources + planning
  -> deck/deck.preview.html + deck/deck.registry.json
  -> output/presentation.generated.bento.* + generated registry
  -> output/presentation.authoring.bento.* + authoring registry
  -> 承認済みauthoring revision
  -> output/presentation.final.bento.* + frozen final registry + baseline
```

詳細なstage、承認、短文コマンドは[workflow/WORKFLOW.md](workflow/WORKFLOW.md)、正本ルールは[docs/source-of-truth-policy.md](docs/source-of-truth-policy.md)、保存保証は[docs/artifact-transactions.md](docs/artifact-transactions.md)を参照してください。

## Developer setup

```powershell
python -m pip install -r requirements-browser.txt
python -m playwright install chromium
python -m scripts.deck_workflow validate
python -m scripts.deck_workflow status --json
```

旧schema v1資料は、変更内容を先に確認してから移行できます。

```powershell
python -m scripts.deck_workflow migrate --dry-run
python -m scripts.deck_workflow migrate
```

後期stageの移行は既存final・sidecar・baseline・revisionを保持し、検証済みmerged registryからfinal registryとregistry baselineをtransactionで作成します。不足時は元artifactを変更せず失敗します。

## HTML-first conversion

schema v2の標準single-file build:

```powershell
python -m scripts.build_bento_from_html `
  --html deck/deck.preview.html `
  --registry deck/deck.registry.json `
  --base Bento_Slides.base.bento.html `
  --output output/presentation.generated.bento.html
```

移行済みのmodular資料では従来の`--html-dir chapters/ --registry-dir chapters/`を使えます。生成物にはHTML/JSON、registry、conversion report、computed layout、resource scan、browser check、source/Bento screenshotsが含まれます。ローカルresourceはdata URI化され、未解決resource、参照不整合、runtime変化、critical crop失敗、serialize失敗はbuildを失敗させます。

section承認はsection DOM、参照registry projection、参照asset content、global CSS/themeから決定論的digestを作ります。承認後の変更は該当section（global CSS/themeは全section）を未承認へ戻し、変換を拒否します。

## Bento authoringとfinalization

変換・検証後は、まず`bento_authoring`で内容と構造を編集します。Work editorのauthoring modeはBento HTML/JSON/registryの2つのrevisionを同時に検証し、3 artifactを同一transactionで保存します。内容承認はdocument revision、registry revision、および次のcanonical digestへ固定されます。

```text
sha256(UTF-8("bento/content-approval/v1\0" + documentRevision + "\0" + registryRevision))
```

承認後にどちらかが変わると、承認は同じstate transactionで無効化されます。`begin-finalization`は承認済みauthoringをfinal HTML/JSON/registryとdocument/registry baselineへ一括初期化します。

`bento_finalization`ではfinalの`#bento-doc`が正本です。内容・構造・registryは凍結され、geometry、presentation style、theme/background、z-orderだけを変更できます。正確な一括変更には`scripts.apply_bento_final_edits`を使用します。HTML-first変換でfinalを上書きせず、通常運用で`--reset-final`や`--allow-content-edit`を使いません。

Work editorを直接起動する場合:

```powershell
python -m scripts.run_bento_work_editor `
  --mode authoring `
  --source output/presentation.generated.bento.html `
  --target output/presentation.authoring.bento.html `
  --source-registry output/diagnostics/merged-registry.json `
  --target-registry output/presentation.authoring.registry.json `
  --repository . `
  --port 8765
```

stage-aware launcherの利用を推奨します。詳細は[docs/work-editor-finalization.md](docs/work-editor-finalization.md)と[docs/authoring-lifecycle.md](docs/authoring-lifecycle.md)にあります。`window.bento.serialize()`はtoolbar注入後も同期的にHTML文字列を返し、一時UIは保存結果へ入りません。

## Segment追加・置換と既存HTML import

`bento_authoring`では、`scratch/segments/`のHTML/registryペアを変換して追加、または明示したslide IDだけを置換できます。

```powershell
python -m scripts.bento_segment import --html scratch/segments/add.preview.html --registry scratch/segments/add.registry.json
python -m scripts.bento_segment replace --html scratch/segments/replacement.preview.html --registry scratch/segments/replacement.registry.json --slide-id target-slide
```

対象外slide hash、cross-slide reference、shared registry、resource、browser round-tripを検証し、generated/finalは変更しません。server起動中は一致するlocalhost APIだけをwriterとして使い、識別できなければ拒否します。

一般HTMLは`imports/`へ原本を隔離してから静的に正規化します。

```powershell
python -m scripts.import_html_deck --input imports/source.html --slide-selector ".slide"
```

scriptは実行・移入せず、networkを遮断し、event handlerと`javascript:` URLを除去し、remote resourceや危険な埋め込みをreportします。selectorを安全に決められない場合は明示指定が必要です。詳しくは[docs/html-import.md](docs/html-import.md)を参照してください。

## Crash safety and concurrency

複数artifactの更新は、OS排他writer lease、短時間transaction lock、fsync済みtemporary/backup、永続journalを使います。起動・status・操作前に未完了journalを復旧し、部分置換は全体rollback、全targetがnew revisionならcommit完了処理を行います。安全判定できない場合はartifactを変更せず停止します。report生成だけの失敗では正常artifactをrollbackせず、`report_failed`を次回復旧します。

## Legacy JSON-first

```powershell
python -m scripts.build_bento --base Bento_Slides.base.bento.html --design gpt_bento_design.json --output demo.generated.bento.html
python -m scripts.validate_bento demo.generated.bento.html --base Bento_Slides.base.bento.html
```

旧仕様は[docs/bento-conversion-spec.md](docs/bento-conversion-spec.md)にあります。

## Verification

```powershell
python -m unittest discover -v
$env:BENTO_BROWSER_TEST = "1"
python -m unittest discover -v
Remove-Item Env:BENTO_BROWSER_TEST
```

GitHub ActionsはLinuxでlegacy/HTML-first/Work editor/full browser suiteを、Windowsでlauncher testsと空白・日本語path smokeを実行し、`html-first-evidence`を保存します。
