# models/ —— ONNX 权重（不进 git，走 Release 附件）

本目录的两个权重是**运行时资产**，不提交进 git：

| 文件 | 体积 | 用途 | SHA256 |
| --- | --- | --- | --- |
| `yolo_v2.1_balls.onnx` | 10.7MB | 奖励球 / 小小英雄 / 调试图（70 类，imgsz 1280） | `95a61b76625079218480f4bbee47db3109a8c526604be723c97c196aa9c13be5` |
| `yolo_v4.1_units_11n.onnx` | 10.7MB | 棋子识别（11n，mAP50 0.774） | `6300f59b5641efff8fdb52b0d21c88632cb193207a573b0c281d0e6bf54145da` |

## 获取方式

```bash
# 一条命令搞定（缺什么下什么 + SHA256 校验）
python -m tools.fetch_models

# 只校验本地（CI / 自测用）
python -m tools.fetch_models --check-only

# 国内网络不通时走代理
python -m tools.fetch_models --proxy http://127.0.0.1:7890
```

默认从 Gitee 发行版 `<base>/v0.1.0/<文件名>` 下载；换镜像或换版本用
`--base https://gitee.com/<owner>/<repo>/releases/download/v0.2.0`，或用环境变量
`JCC_RELEASE_BASE`。

发行版页面：<https://gitee.com/bode135/jcc_agent/releases/tag/v0.1.0>

手动兜底：打开仓库的「发行版 / Release」页面，把两个 `.onnx` 下载到本目录即可 ——
`tools/fetch_models.py --check-only` 会告诉你还缺哪个、SHA256 对不对。

## 想换模型

* 换 11s（38MB，精度更高 mAP50 0.805）：把 `models/yolo_v4.1_units_11s.onnx` 放进本目录，
  改 `config/config.py` 的 `YOLO_UNITS_ONNX`；信任表按模型文件名自动选
  （`config/yolo_trust/yolo_v4.1_units_11s.json` 已随仓库提供）。
* 自己训练的权重：文件名随意，改 config 里的对应键（`YOLO_UNITS_ONNX` /
  `YOLO_BALL_ONNX` / `YOLO_LEGEND_ONNX` / `YOLO_DEBUG_ONNX`），并把 SHA256 从
  `tools/fetch_models.py::MODELS` 里删掉（否则 `--check-only` 会报不符）。

## 为什么不进 git

* 合计 21.3MB，占仓库体积一半；二进制改动不可 diff，历史只会越滚越大；
* 权重是**版本化整件资产**：一次 Release 对应一个可用组合，回滚就是换 tag；
* 代码仓库保持轻量，clone 秒级完成，权重按需拉取。

模型/模板/scrape_info 素材的版权与使用约束见仓库根 `LICENSE`（AGPL-3.0）与 README 第十六节。
