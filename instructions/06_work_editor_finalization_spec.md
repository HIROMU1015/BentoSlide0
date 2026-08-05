# ChatGPT WorkによるBento最終編集仕様

## 1. 対象と正本

HTML-first変換は再生成可能な`output/presentation.generated.bento.html`と同名JSON sidecarを生成します。最終編集では`output/presentation.final.bento.html`内の`#bento-doc`と同期した`presentation.final.bento.json`を扱います。

最終編集を開始した時点から、表示・配置・styleの正本はfinalの`#bento-doc`です。chapter HTMLは設計記録として残し、通常運用では再変換してfinalへ上書きしません。generatedを手動編集しません。

## 2. 起動

Windowsではリポジトリ直下の既存ランチャーを使用します。

```powershell
.\start_bento_editor.cmd
```

既定URLは`http://127.0.0.1:8765/`です。通常ブラウザーは自動起動せず、ChatGPT Workの内蔵ブラウザーで開くか既存タブを再読み込みします。停止時だけ`stop_bento_editor.cmd`を使用します。

ランチャーは`--reset-final`と`--allow-content-edit`を渡しません。finalがなければ既存Work editorがgeneratedから初期化し、finalがあれば継続使用します。
stage-awareランチャーは`deck.yaml`のgenerated/finalパスを渡し、registryはgenerated HTMLと同じ親の`diagnostics/merged-registry.json`を使用します。

## 3. 現行API契約

- `GET /`: final runtimeに一時ツールバーを注入した応答
- `GET /api/status`: target、revision、validation、runtime fingerprint
- `GET /api/document`: 最新documentとrevision
- `POST /api/validate`: serialize結果の保存前検証
- `POST /api/save`: revision付き保存
- `POST /api/revert`: 検証済みrevisionへの復元

古い`POST /api/document`契約を使用しません。保存は`serializedHtml`と`baseRevision`を送ります。

## 4. 同期serialize契約

Work editor注入後も`window.bento.serialize()`は同期的にHTML文字列を返します。Promiseまたはthenableへ変更しません。一時ツールバーはserialize直前にDOMから外し、成功・例外のどちらでも`finally`で元の親と位置へ戻します。一時UIは保存結果へ混入しません。

## 5. 保存保護

保存時は次を維持します。

1. SHA-256 revision競合を確認し、stale revisionは409にする
2. Bento schema、参照、registry、protected content、resource portabilityを検証する
3. runtime fingerprintを検証する
4. HTMLとJSONを同じディレクトリの一時ファイルへ書き、flush/fsyncする
5. revision backupを作成する
6. final HTMLとsidecarをatomic replaceし、不一致時はrollbackする
7. 永続化するHTML変更を検証済み`#bento-doc`だけに限定する
8. final引き渡し時のbaselineに対し、内容・ID・構造・数式・データ・参照を維持する

## 6. 編集範囲

通常の最終調整はx/y/w/h、余白、文字サイズ、自動折返し、行間、色、z-order、表・chart・connector配置に限定します。本文や明示的な改行、数式、数値、条件、図表データの変更には一次資料とregistryの再確認および明示的な内容編集許可が必要です。

UI上のドラッグや入力だけを「UI編集」と呼びます。`window.bento.loadDoc()`で文書モデルを変更した場合は「Bento API編集」と記録します。保存はどちらもWork editor APIを通します。

## 7. 完了確認

- final HTML内`#bento-doc`とfinal JSON sidecarが一致する
- generatedは変わっていない
- runtime fingerprintがgeneratedと一致する
- finalの内容・構造fingerprintが保存済みbaselineと一致する
- revisionとbackupが更新される
- toolbarがserialize結果と保存ファイルへ混入しない
- 保存後の再読み込みで位置・寸法・styleが維持される
- validator、registry/protected-content、resource scan、ブラウザーround-tripが成功する

構造的な再生成が必要な場合でも、既存finalを上書きする前にユーザーの明示承認を得ます。通常運用で`--reset-final`を使用しません。
