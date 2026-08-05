# HTML-first Bento変換用オーサリング契約

この契約は、GPTがCanvasで生成するHTMLと、CodexのHTML-first変換器の境界を定義します。

## 1. 成果物

章ごとに次を作成します。

- `chapters/chapter-XX.preview.html`
- `chapters/chapter-XX.registry.json`

HTMLは変換前の視覚デザインの正本です。
registryは論文出典、数式、図表、削除禁止ロジックの正本です。

## 2. キャンバス

- 幅：1280px
- 高さ：720px
- レスポンシブ変形は不要
- 各スライドは固定サイズ
- 印刷ページではなくブラウザ表示を基準にする

推奨構造：

```html
<main id="deck">
  <section class="slide" data-slide-id="ch1-s1" data-layout="claim-evidence-boundary">
    ...
  </section>
</main>
```

## 3. slide属性

必須：

- `data-slide-id`

推奨：

- `data-layout`
- `data-transition`
- `data-state-of`
- `data-slide-name`
- `data-notes-id`

slideIdは文書内で一意にします。

## 4. element属性

主要要素には次を付けます。

必須：

- `data-bento-id`

推奨：

- `data-bento-type`
- `data-bento-role`
- `data-bento-export`
- `data-bento-z`
- `data-paper-source`
- `data-equation-id`
- `data-figure-id`
- `data-chart-id`
- `data-table-id`
- `data-link`
- `data-morph-id`
- `data-layout-group`

`data-bento-id`は同一スライド内で一意にします。

## 5. data-bento-type

推奨値：

- `text`
- `equation`
- `shape`
- `table`
- `chart`
- `image`
- `svg`
- `media`
- `group`

属性がない場合、Codexが要素とcomputed styleから推定できますが、主要要素では明示を優先します。

## 6. data-bento-export

使用可能値：

- `auto`：既定。Codexがnative、部分SVG、画像を判断
- `native`：Bentoネイティブ要素への変換を優先
- `svg`：対象ブロックを局所SVGとして保持
- `image`：対象ブロックを局所画像として保持
- `ignore`：変換対象外

`native`指定でも、意味または見た目を維持できない場合は局所フォールバックを許可します。
理由はconversion reportへ記録します。

## 7. HTML/CSS

使用してよいもの：

- semantic HTML
- CSS Grid
- Flexbox
- absolute positioning
- SVG
- HTML table
- 単純なgradient
- shadow、border、radius
- 画像
- 構造化chart

注意：

- 必須内容を`::before`や`::after`だけに置かない
- layout確定後も動き続けるanimationを使わない
- WebGLやcanvasは原則画像フォールバック前提
- iframeを主要内容に使わない
- 外部CDNが失敗すると主要内容が消える構成にしない
- 複雑なfilter、mask、clip-pathは必要なブロックだけに使う

## 8. テキスト

テキストはHTML要素として記述します。

推奨：

```html
<h1
  data-bento-id="ch1-s1-title"
  data-bento-type="text"
  data-bento-role="title"
  data-bento-export="native"
>
  提案手法は誤差と回路コストを分離して制御する
</h1>
```

主要文章はDOM textとして保持します。
画像内だけに文章を埋め込みません。

## 9. 数式

推奨：

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

- `data-equation-id`を付ける
- `data-latex`とregistryのlatexを一致させる
- 画像化を前提にしない

## 10. 表

単純な比較表は実際の`<table>`を使います。

- ヘッダーは`<thead>`
- 本文は`<tbody>`
- セル内容はDOM text
- native変換したい表では複雑なrowspan/colspanを避ける
- 複雑表が必要な場合は`data-bento-export="auto"`または`svg`

## 11. チャート

chartは元データを構造化して保持します。
画像から値を推測しません。

```html
<div
  data-bento-id="runtime-chart"
  data-bento-type="chart"
  data-bento-export="native"
  data-chart-id="runtime_chart"
>
  <script type="application/json" data-chart-option>
    {
      "preset": "bar",
      "xAxis": {"type": "category", "data": ["2nd", "4th", "8th"]},
      "yAxis": {"type": "value"},
      "series": [{"type": "bar", "data": [120, 75, 48]}]
    }
  </script>
</div>
```

## 12. 画像・論文図

```html
<img
  src="assets/paper-fig-3.png"
  data-bento-id="paper-fig-3"
  data-bento-type="image"
  data-bento-role="primary-visual"
  data-bento-export="native"
  data-figure-id="fig_3"
  data-paper-source="Fig. 3"
  alt="Figure 3 from the paper"
>
```

- 論文図を勝手に再生成しない
- 元assetを使う
- figureIdとpaperSourceを保持する

## 13. SVG・矢印・線

単純な矢印・線はHTML/SVGで表現してよいです。
変換可能な場合はBento shape/connectorへ変換されます。

複雑な概念図は、テキストをDOM要素として分離し、装飾部分だけSVGにすると編集性を保ちやすくなります。

## 14. Morph・State・Link

- 隣接スライドで同一要素をMorphさせる場合、安定した`data-bento-id`または`data-morph-id`を使う
- 詳細stateはslideの`data-state-of`で指定する
- linkは`data-link`で指定する
- 参照先IDは必ず存在させる

## 15. asset

- ローカル相対パスまたはasset manifestを使う
- ネットワーク専用URLだけに依存しない
- 変換時にCodexがdata URIまたはBento assetへ解決する

## 16. レンダリング安定性

- フォント、画像、数式、chartが表示された後にスクリーンショット可能な状態にする
- layoutが時間依存で変化し続けないようにする
- hoverしないと主要内容が見えない構成にしない
- source screenshotで全情報が確認できる状態にする

## 17. 自己検証

- すべてのslideIdが一意
- 主要elementIdが一意
- 1280×720内に収まる
- 見切れがない
- 意図しない重なりがない
- 主要テキストがDOM text
- 数式とregistryが一致
- chartに構造化データがある
- 画像参照が存在する
- link/state/morph参照が解決できる

## 18. Codexの裁量

Codexは次を調整してよいです。

- x、y、w、h
- 余白、gap、padding
- 改行、行間、文字サイズ
- 表の列幅・行高
- chartのplot領域・ラベル位置
- connector接続点
- Bentoで再現可能な近似style

Codexは次を変更しません。

- 文章の意味
- 数式、記号、数値、条件
- スライドの中心メッセージ
- 基本構図
- 強調順位
- 図表の意味
