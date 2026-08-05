# Bentoスライド論理構図

各スライドでは、内容に自然に対応する構図を原則1つだけ選びます。
HTMLの自由度を使ってよいですが、構図を装飾で曖昧にしません。

各slideの`data-layout`には、対応する識別子を付けます。

## 1. Figure + reading guide

`data-layout="figure-reading-guide"`

用途：
- 論文図の導入
- 評価結果の読み方
- 概念図の解釈

構成：
- 論文図または元assetを主要領域に大きく配置
- 別領域に「見る軸」「観察」「解釈」
- 必要なら結論を1文

注意：
- 図番号とpaperSourceを保持する
- 図を勝手に再生成しない
- 1枚に複数図を詰め込まない

## 2. Equation dissection

`data-layout="equation-dissection"`

用途：
- Hamiltonian定義
- 時間発展
- 近似式
- 誤差上界
- 定理・命題の条件

構成：
- 数式を主要視覚要素として配置
- 記号、直感、成立条件、章内での役割を短く分ける

注意：
- 数式だけで終えない
- 1枚に複数の複雑な式を置かない
- `data-equation-id`とregistryを一致させる

## 3. Observation → Interpretation

`data-layout="observation-interpretation"`

用途：
- 数値実験
- ベンチマーク
- 手法比較
- スケーリング

構成：
- ObservationとInterpretationを左右または上下に分離
- Observationには直接読める事実だけ
- Interpretationには論文の主張に接続できる内容だけ

矢印を使う場合、HTML/SVGで実線・矢印として表現し、文字記号で代用しません。

## 4. Claim → Evidence → Boundary

`data-layout="claim-evidence-boundary"`

用途：
- 中心貢献
- 定理の位置づけ
- 提案手法の有効性

構成：
- Claimを最も強く
- Evidenceを次の視線位置
- Boundary / Assumptionを独立要素として残す

## 5. Before → Gap → Paper's view

`data-layout="before-gap-paper-view"`

用途：
- 導入
- 先行研究との差分
- なぜ論文が必要か

構成：
- 既存理解
- 不足点
- 本論文の見方

注意：
- 先行研究を過度に単純化しない
- 3カードを機械的に並べるだけの構図にしない

## 6. Evaluation protocol

`data-layout="evaluation-protocol"`

用途：
- 評価対象
- 比較手法
- 固定条件
- 評価指標

結果は別スライドに分けます。
条件が表として自然ならHTML tableを使用できます。

## 7. Input → Process → Output

`data-layout="input-process-output"`

用途：
- アルゴリズム全体像
- simulation手順
- 評価パイプライン

最大3ステップ。
矢印はHTML/SVGの線・arrowとして作成し、主要ノードに安定IDを付けます。

## 8. Two-column contrast

`data-layout="two-column-contrast"`

明確な左右比較がある場合だけ使用します。
単なる説明を二つの箱へ分けません。
左右の意味をCodex変換後も入れ替えません。

## 9. Matrix / positioning map

`data-layout="matrix-positioning-map"`

2軸が論文または評価設定から明確な場合だけ使用します。
4象限を埋めるために論文にない分類を作りません。

## 10. Table + takeaway

`data-layout="table-takeaway"`

用途：
- 条件比較
- 手法比較
- ablationの整理

構成：
- 表を主要領域に配置
- 表の下または横に1文のtakeaway

注意：
- 数値を画像から推測しない
- 複雑なセル結合を目的なく使わない
- 表を読み上げるだけのスライドにしない

## 11. Chart + interpretation

`data-layout="chart-interpretation"`

用途：
- 定量比較
- スケーリング
- 感度分析

構成：
- chartを主要視覚要素にする
- 観察と解釈を短く分離する
- 元データを構造化JSONで保持する

## 12. 共通デザイン規則

- 1スライド1メッセージ
- 主要ブロックは最大3つを目安
- 四角枠を標準形にしない
- 空白を埋めるために図形を増やさない
- 箇条書きは最大3項目を目安
- タイトルはSo Whatを言い切る
- 下端・右端の見切れを避ける
- 文字を小さくして詰め込まず、必要なら分割
- 章末に機械的まとめを追加しない
- 必須内容を装飾SVGや画像の中だけに閉じ込めない
- Bento変換後に編集したいテキストはDOM textとして残す
