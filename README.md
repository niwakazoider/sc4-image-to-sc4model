# Image to SC4Model Generator

レンダリング済みの建物画像から、SimCity 4 の BAT 形式に近い `.SC4Model` を生成するための Python ツールです。

## 主な機能

- OBJ 生成
- S3D 生成
- DXT1 FSH 生成
- QFS / RefPack 圧縮
- DBPF / SC4Model 生成
- BAT 用 XML の生成
- BMP / JFIF プレビューリソースの生成

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
  --run-fshgen
```

生成結果の例:

```text
output/
├── *.obj
├── *_Day.png
├── *_Night.png
└── building.SC4Model
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
