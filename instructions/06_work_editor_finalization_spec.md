# ChatGPT WorkによるBento authoring/finalization仕様

## 1. 二つの編集mode

### Authoring

`bento_authoring`と`content_review`では、`presentation.authoring.bento.html/.json`と`presentation.authoring.registry.json`が正本です。本文、notes、slide/element、chart/table data、media、link/morph/state/connector、provenanceを変更できます。既存elementのID/type変更は通常saveではなく明示的なreplaceとして扱います。

保存requestは`baseDocumentRevision`、`baseRegistryRevision`、`serializedHtml`、必要なら完全な`registry`を送ります。registryを省略しても現在revisionと相互参照を検証します。documentが新しいregistry定義を必要とする場合は同一transactionにregistryを含めない限り拒否します。

### Finalization

内容承認後は、`presentation.final.bento.html/.json`、凍結`presentation.final.registry.json`、document/registry baselineが正本です。変更可能なのはgeometry、presentation style、theme/background、z-orderだけです。内容、構造、ID/type、数式、data、media source、notes、behavior、references、registryは変更しません。

## 2. 起動

通常は`start_deck_workspace.cmd`を使用します。stage-aware launcherは`deck.yaml`の全custom pathを渡し、authoring/content reviewではauthoring mode、finalizationではfinalization modeを起動します。通常ブラウザー、変換、`--reset-final`、`--allow-content-edit`は起動しません。

既定URLは`http://127.0.0.1:8765/`です。ChatGPT Workの内蔵ブラウザーで開くか既存tabをreloadします。停止は`stop_deck_workspace.cmd`です。

## 3. API契約

- `GET /api/status`: repository、mode、target、両revision、validation、runtime fingerprint
- `GET /api/document`: consistent document/registry snapshot
- `POST /api/validate`: 保存前検証
- `POST /api/save`: 両revision付きtransaction保存
- `POST /api/revert`: 両revision付きartifact-set復元

stale document/registry revisionは409、schema/registry/reference/resource/runtime/protected違反は422です。authoring save responseは`documentRevision`、`registryRevision`、`contentApprovalInvalidated`、`transactionId`を含みます。

## 4. 同期serialize契約

toolbar注入後も`window.bento.serialize()`は同期的にHTML文字列を返し、Promise/thenableへ変更しません。一時toolbarをserialize直前にDOMから外し、成功・例外の両方で`finally`により元の親と位置へ戻します。`bento-work-editor`、host、loader、styleは保存結果へ混入しません。

## 5. Transactionとwriter

serverは起動から終了まで、repositoryとartifact setを識別するOS排他writer leaseを保持します。saveはさらに短時間transaction lockを取得します。HTML/JSON/registryと承認無効化stateをjournal付きtransactionで更新し、temporary/backupをflush/fsyncしてから置換します。read APIはconsistent snapshotだけを返します。

未完了journalはAPI提供前に復旧します。partial replacementは全体rollback、全new revisionはcommit完了、all-oldはrollback完了、安全判定不能は無変更で停止します。artifact commit後のreport-only failureでは新artifactを維持し、次回復旧でreportを再生成します。

server起動中のCLIは、同じwriter leaseを取得できなければrepository/mode/targetが一致するlocalhost APIだけを使用します。安全に識別できないwriterへは送信しません。

## 6. 承認とhandoff

内容承認はauthoring document/registryの両revisionと`bento/content-approval/v1` digestへ固定します。status、save、review、approval、final handoff、segment、offline操作時に再計算し、差があればpendingへ戻します。

finalization開始時は承認済みauthoringから、final HTML/JSON/registry、document baseline、registry baseline、`deck.yaml` metadataを1 transactionで初期化します。mismatchする既存finalを暗黙に上書きしません。

## 7. 完了確認

- HTML内`#bento-doc`とJSON sidecarが一致する
- lifecycleに対応するregistry revisionが一致する
- runtime fingerprintが変化しない
- generatedとauthoring/final境界が維持される
- finalが両baselineに対するpresentation-only descendantである
- revision、backup、transaction reportが更新される
- toolbarがserialize結果に含まれず、直後に同じDOM位置へ復元される
- save/validate/revert/reloadが継続して使える
- resource/reference/browser round-tripが成功する

UI上の入力/dragだけをUI編集と呼び、`window.bento.loadDoc()`による変更はBento API編集と記録します。
