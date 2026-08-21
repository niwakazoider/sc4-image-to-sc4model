# Image to SC4Model Generator

レンダリング済みの建物画像から、SimCity 4 の BAT 形式に近い `.SC4Model` およびそれを使用する Ploppable `.dat` を生成するための Python ツールです。

## 主な機能

- OBJ 生成
- S3D 生成
- FSH 生成
- SC4Model 生成

## 必要環境

- Python 3.10 以上
- Pillow

依存ライブラリのインストール:

```bash
pip install pillow
```

## 使い方

例:

```bash
python sc4_i2b_model_generator.py \
  --width 24 \
  --depth 24 \
  --height 36 \
  --gid b5ec2727 \
  --model-name building \
  --out output \
  --quad-image day.png \
  --quad-night-image night.png \
  --run-fshgen \
  --generate-ploppable \
  --preset landmark
```

生成結果の例:

```text
output/
├── *.obj
├── *_Day.png
├── *_Night.png
├── building.SC4Model
└── building_Ploppable.dat
```

--quad-night-image（夜の画像）は任意です。

## Quad Image の配置

4方向を1枚にまとめた入力画像は、以下の配置を使用します。

```text
+-------+-------+
| North | West  |
+-------+-------+
| South | East  |
+-------+-------+
```

## Credits

このプロジェクトは、SimCity 4 Modding コミュニティの公開情報およびオープンソース実装を参考にして作成されています。

特に、ファイル形式や圧縮処理の実装にあたり、以下のプロジェクトを参考にしています。

- memo33/fshgen
- memo33/scdbpf

ライセンス表記については [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) を参照してください。



## その他

`make_ploppable_dat.py` をコマンドラインで使用すると、生成済みの `.SC4Model` に対応する Ploppable `.dat` を作成できます。

最小構成:

```bash
python src/make_ploppable_dat.py output/building_Ploppable.dat \
  --gid 0xb5ec2727 \
  --width 24 \
  --height 36 \
  --depth 24
  --name "My Building"
```

生成後は、以下の2ファイルを SimCity 4 の `Plugins` フォルダに入れてください。

```text
Plugins/
├── building.SC4Model
└── building_Ploppable.dat
```

## Ploppable DAT の主なオプション

### 基本

```text
--preset landmark
--gid
--width
--height
--depth
--name
--description
--item-order
```

`--gid`, `--width`, `--height`, `--depth`, `--name` が必須です。

### プリセット

landmark, park, plaza, garden

```bash
--preset park
```


### Lot

Lot サイズは手動指定が可能です。

```bash
--lot-size 4x3
```

モデルの向き:

```bash
--orientation south
--orientation west
--orientation north
--orientation east
```

Lot 内でモデルを移動する場合:

```bash
--offset-x 4
--offset-y 0
--offset-z -2
```

### コスト

```bash
--plop-cost 1000
--bulldoze-cost 100
--monthly-cost 20
```

### 電力・水

```bash
--power-consumed 10
--water-consumed 5
```

### Landmark 効果

```bash
--landmark-effect 40,20
--mayor-rating-effect 10,256
```


### メニューアイコン

```bash
--icon menu_icon_176x44.png
```
