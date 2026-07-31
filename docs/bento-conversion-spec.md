# GPT設計JSONからBento Slidesへの変換仕様

## 1. 目的と不変条件

この変換器は、GPTが決めた内容・座標・寸法・色・文字組みを、Bento Slides v1のネイティブ文書JSONへ決定論的に写像する。Codex側では再デザインや文言修正を行わない。

- 対応入力は `text`、`shape`、`latex`。
- 出力は既存の `Bento_Slides.base.bento.html` の `#bento-doc` だけを差し替える。
- CSS、JavaScript、圧縮ランタイムなど、スクリプト要素外の内容は変更しない。
- 同じbase、設計JSON、`docId`、`modified`からは同一バイト列を生成する。
- 変換不能な必須情報はエラー、軽微な未知styleやBentoに対応先がないtheme tokenは警告とする。

現在の参照fixtureは `gpt_bento_design.json`、`demo.bento.html`、`Bento_Slides.base.bento.html` から独立して固定した `tests/fixtures/` 内の3ファイルである。

## 2. GPT設計JSON

ルートは次の構造を持つ。

```json
{
  "format": "gpt-bento-design/demo-v1",
  "document": {},
  "slides": []
}
```

### document

| フィールド | 要件 |
| --- | --- |
| `title` | 文字列 |
| `docId` | 任意。指定時はUUID |
| `modified` | ISO-8601、タイムゾーン必須 |
| `canvas.width`, `canvas.height` | 正の数 |
| `theme` | オブジェクト |
| `theme.background`, `text`, `accent` | Bento themeへ変換する色文字列 |
| `theme.fontFamily` | 任意。省略時は `sans-serif` |
| `theme.surface`, `primary`, `muted`, `line` | 設計用token。警告してBento themeから省略 |

### slide

- `id` は文書内で一意な空でない文字列。
- `background` は文字列。
- `elements` は配列。

### element共通

- `id` は同一スライド内で一意。
- `type` は `text`、`shape`、`latex` のいずれか。
- `role` は任意のメタ情報としてそのまま保持。
- `x`、`y`、`w`、`h` は有限数。`w` と `h` は正で、フレーム全体がキャンバス内に収まること。
- `z` は有限数または省略可能。省略時は0。
- `style` は要素種別ごとの必須項目を持つオブジェクト。

### text

- `content` はプレーンテキスト。
- `style.fontSize` は正の数。
- `style.align` は `left`、`center`、`right`、`justify`。
- `style.valign` は `top`、`middle`、`bottom`。
- 対応styleは `fontSize`、`fontFamily`、`fontWeight`、`color`、`align`、`valign`、`lineHeight`、`letterSpacing`。

### shape

- `shape` は `rounded-rectangle`、`rectangle`、`rect`。
- `style.fill`、`style.stroke` は文字列。
- `style.strokeWidth` は0以上。
- `style.cornerRadius` は任意で0以上。

### latex

- `latex` または `bentoSource` の少なくとも一方が必要。
- `bentoSource` は `$$...$$` 形式で、`latex` もある場合は中身が一致すること。
- `equationId` は任意だが、指定時は空でない文字列。
- 数式は画像・SVGへ変換しない。

## 3. BentoネイティブJSON

出力ルートは以下を持つ。

```text
format: "bento/slides"
version: 1
docId: UUID
title: string
size: { width, height }
theme: { background, color, accent, fontFamily }
slides: Slide[]
modified: ISO-8601 string
```

各slideは `id`、`background`、`transition: "none"`、`notes: ""`、`elements` を持つ。現在の変換対象となるネイティブ要素は `type: "text"` と `type: "shape"` である。

## 4. フィールド対応表

| GPT設計JSON | BentoネイティブJSON | 分類 | 処理 |
| --- | --- | --- | --- |
| `document.title` | `title` | 完全対応 | そのまま |
| `document.canvas` | `size` | 完全対応 | `width` / `height`を保持 |
| `theme.background` | `theme.background` | 完全対応 | そのまま |
| `theme.text` | `theme.color` | 完全対応 | 名称変更 |
| `theme.accent` | `theme.accent` | 完全対応 | そのまま |
| `theme.surface/primary/muted/line` | なし | 警告して省略 | 設計JSONだけに保持 |
| `slide.background` | `slide.background` | 完全対応 | そのまま |
| `role` | `role` | メタデータとして保持 | そのまま |
| `x/y/w/h` | `x/y/w/h` | 完全対応 | 値を変更しない |
| `z` | `elements` 配列順 | 完全対応 | 昇順の安定ソート。同値は入力順 |
| `type: text` | `type: text` | 完全対応 | `content`を`html`へ変換 |
| `content` | `html` | 完全対応 | HTMLエスケープ後、改行を`<br>`へ |
| `style.fontSize` | `fontSize` | 完全対応 | そのまま |
| 対応text style | 同名フィールド | 完全対応 | style階層から要素直下へ移動 |
| `shape: rounded-rectangle` | `shape: rect` | 近似変換 | `cornerRadius`を`radius`へ保持 |
| `shape: rectangle/rect` | `shape: rect` | 完全対応 | `radius: 0` |
| shape style | `fill/stroke/strokeWidth/radius` | 完全対応 | style階層から要素直下へ移動 |
| `type: latex` | `type: text` | 完全対応 | 編集可能な `$$...$$` を `html` に保存 |
| `equationId` | `equationId` | メタデータとして保持 | Bento未知フィールドとして維持 |
| `latex` | `latexSource` | メタデータとして保持 | ビルド時点の素のLaTeXを保存 |
| 未知element type | なし | 未対応でエラー | 実装を追加するまで変換しない |
| 未知style | なし | 警告して省略 | フィールド名と修正方針を報告 |

## 5. 文字列とHTML埋め込み

`content` はHTMLではなくプレーンテキストとして扱う。`&`、`<`、`>` をエスケープし、CRLF・CR・LFを正規化したうえで改行を `<br>` にする。JSON化後、`#bento-doc` 内のすべてのリテラル `<` は `\u003c` に置換する。日本語は `ensure_ascii=False` で保持し、インデントは2空白、キー順は変換器の定義順に固定する。

## 6. z-order

各スライドの要素を `(z、入力配列index)` で昇順ソートし、その順でBentoの `elements` 配列へ格納する。Bentoでは後の要素が前面になるため、値が大きい `z` ほど前面になる。同一 `z` はPythonの安定ソートと入力indexで入力順を必ず維持する。

## 7. 数式メタデータと正のデータ

Chrome上で `window.bento.loadDoc()` と `window.bento.serialize()` を使って実測した結果は次の通り。

- `equationId` は読み込み・再保存後も保持される。
- `latexSource` も保持される。
- Bento上で `html` のLaTeXを変更しても `latexSource` は自動更新されず、古い値が残る。

したがって、ランタイム編集後は `html` の `$$...$$` を唯一の正とする。`latexSource` は生成時の監査用メタデータであり、編集後の同期値としては使用しない。再ビルド時にはGPT設計JSONの `latex` / `bentoSource` から `html` と `latexSource` を再生成する。外部sidecarは、未知フィールドが削除されない現バージョンでは不要である。

## 8. 決定性

`docId` の優先順位は `--doc-id`、`document.docId`、設計JSON全体の正規化文字列から生成するUUIDv5。`modified` の優先順位は `--modified`、`document.modified`。どちらもない場合、決定性を曖昧にしないためエラーとする。現在時刻が必要な場合のみ `--modified now` を明示する。

## 9. HTML差し替え

標準ライブラリの `html.parser` で、`type="application/bento+json"` かつ `id="bento-doc"` のscriptを探す。対象が0個または複数ならエラー。開始tagと終了tagの間だけを置換し、外側は元文字列を連結して維持する。出力先がbaseと同じ実パスの場合はエラーとする。

ランタイム非改変は、baseと出力の両方から同script内容だけを除外した文字列をSHA-256化し、同一性を確認する。

## 10. 検証とエラー

入力検証とBento出力検証は別モジュールで行う。エラーには `slideId`、`elementId`、問題フィールド、実値、修正方法を含める。出力検証ではformat/version、必須ルート、ID一意性、フレーム、text/shape構造、数式ソース、HTML内の `<` エスケープ、ランタイム同一性を確認する。

## 11. 対応外と拡張点

現時点では `image`、`svg`、`line`、`arrow`、`table`、`chart`、`media`、speaker notes、morph/state、animation/fx、外部registry、章別JSON結合、PPTX変換を実装しない。新しいtypeは、入力検証、変換、Bento検証、独立fixture、単体・統合・ブラウザテストを同時に追加して拡張する。

## 12. 標準コマンド

```powershell
python -m scripts.build_bento --base Bento_Slides.base.bento.html --design gpt_bento_design.json --output demo.generated.bento.html
python -m scripts.inspect_bento demo.generated.bento.html
python -m scripts.validate_bento demo.generated.bento.html --base Bento_Slides.base.bento.html
python -m scripts.check_bento_browser demo.generated.bento.html --design gpt_bento_design.json --screenshots-dir . --screenshot-prefix demo-slide
python -m unittest discover -v
```
