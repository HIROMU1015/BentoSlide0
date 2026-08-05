# 数式レジストリ仕様

## 1. 目的

HTML内の数式と、論文出典・意味・使用スライドを安定したIDで対応付けます。
Codex変換後もBento文書内の数式と追跡できるようにします。

## 2. equationId

内容ベースの安定名を使用します。

推奨：
- `interaction_picture_hamiltonian`
- `trotter_error_bound`
- `magnus_first_order`
- `effective_generator`

禁止：
- `eq1`
- `formula_a`
- `slide3_equation`

## 3. HTML

```html
<div
  class="equation"
  data-bento-id="interaction-picture-equation"
  data-bento-type="equation"
  data-bento-role="equation"
  data-bento-export="native"
  data-equation-id="interaction_picture_hamiltonian"
  data-latex="H_I(t)=e^{itH_0}H_1e^{-itH_0}"
>
  $$H_I(t)=e^{itH_0}H_1e^{-itH_0}$$
</div>
```

- `data-equation-id`を必須とする
- `data-latex`に区切り記号を付けない
- 表示文字列は`$$...$$`としてもよい
- HTML表示とregistryの原文を一致させる

## 4. registry JSON

```json
{
  "equations": {
    "interaction_picture_hamiltonian": {
      "latex": "H_I(t)=e^{itH_0}H_1e^{-itH_0}",
      "bentoSource": "$$H_I(t)=e^{itH_0}H_1e^{-itH_0}$$",
      "meaning": "interaction pictureでの摂動Hamiltonian",
      "paperSource": "Eq. 4",
      "usedOnSlides": ["ch2-s3"]
    }
  }
}
```

## 5. 整合性

- latexには`$$`を付けない
- bentoSourceには`$$...$$`
- 中身を一致させる
- equationIdをHTML、registry、Bento文書で一致させる
- 論文PDFにない数式を登録しない
- 章で使わない数式を大量に登録しない

## 6. Codex変換時

Codexは数式をBento text要素として保持し、可能な限り次を残します。

- `equationId`
- `latexSource`
- `html`内の`$$...$$`

数式を画像化するのは最終手段です。

## 7. Bento最終編集後

最終Bento編集では、表示上の正はBento文書内`html`の`$$...$$`です。
`latexSource`は生成時点の監査情報であり、自動同期されない場合があります。

位置、寸法、色、文字サイズだけを変更した場合、registry更新は不要です。
数式内容を変更した場合は、次を行います。

1. 論文PDFと照合
2. Bento文書内`html`を更新
3. `latexSource`を更新
4. registryを更新または再生成
5. equationIdの対応を検証

## 8. 禁止事項

- レイアウト都合で数式を省略する
- 符号、添字、係数を勝手に変更する
- registryと表示数式を不一致のまま保存する
- 画像化した数式だけを残してLaTeX原文を失う
