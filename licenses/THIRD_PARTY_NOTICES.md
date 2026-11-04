# 师兄证照（ShiIDPhoto）· 第三方组件与模型声明

- 适用版本：`0.7.0`
- 最后核对：2026-11-04
- 上游内核：`aoguai/LiYing` `v3.3.0`
- 发行档位：`personal`（个人自用；`public` 档目前被门禁阻断，属设计行为）
- 代码仓库：**不含任何模型权重**。构建时由 `scripts/fetch_assets.py` 按 SHA256
  从上游与各模型官方仓库拉取，仓库本身只放外壳、配置与构建脚本。

本文件区分**代码许可证**与**模型权重许可证**——两者的判断互相独立，不能用一个覆盖另一个。
本项目自身代码以 AGPL-3.0 发布，全文见仓库根目录 `LICENSE`。

## 一、代码与运行时

| 组件 | 版本 | 用途 | 许可证 | 义务 |
|---|---|---|---|---|
| LiYing | v3.3.0 | 图像处理内核 | AGPL-3.0 | 分发二进制/整合包时须提供完整对应源码；发行包内已附 `src/`，本仓库只含外壳代码 |
| CPython | 3.11.9 embeddable | 运行时 | PSF License | 保留版权声明 |
| Gradio | 4.x | WebUI | Apache-2.0 | 保留 NOTICE |
| ONNX Runtime | 1.17.3 (CPU) | 推理 | MIT | 保留版权声明 |
| OpenCV (opencv-python) | 4.x | 图像处理 | Apache-2.0 | 保留 NOTICE |
| NumPy / Pillow / pandas / orjson / pydantic / starlette / piexif | 见 wheelhouse | 依赖 | BSD / HPND / BSD / MIT / MIT / BSD / MIT | 各自保留声明 |
| mozjpeg_lossless_optimization | 见 wheelhouse | JPEG 无损优化 | BSD-3-Clause / IJG | 保留声明 |
| pngquant（可选外置） | 2.x | PNG 压缩 | GPL-3.0 或商业授权 | **未随包分发**；缺失时压缩功能关闭 |

## 二、模型权重

| 模型文件 | 用途 | 许可证 | 再分发状态 | 判断依据 |
|---|---|---|---|---|
| `yolov8n-pose.onnx` | 人体姿态/角度纠正 | AGPL-3.0 | approved | Ultralytics 权重为 AGPL-3.0；本包整体以 AGPL-3.0 分发并附对应源码，满足源码提供义务 |
| `face_detection_yunet_2023mar.onnx` | 人脸检测 | Apache-2.0 | approved | 已核对 `opencv_zoo/LICENSE` 为 Apache-2.0（注意：不是 MIT） |
| `RMBG-1.4-model.onnx` | 抠图换底 | BRIA RMBG-1.4（自定义，非商用） | **restricted** | BRIA 模型卡标注 source-available、非商业用途，模型仓库为 gated，**禁止随公开包再分发**。上游 LiYing 的 Releases 虽挂有该权重，但未附任何许可声明——他人分发不能作为本项目再分发的授权依据 |
| `skin_retouch_mask.onnx` 等美肤组 | 自动美肤 | 未核实 | pending | ModelScope 模型条款与 ONNX 导出权未确认，本版不随包分发，功能关闭 |

## 三、许可证义务说明

1. **AGPL-3.0（本项目代码、LiYing 内核与 YOLOv8 权重）**：若对外分发本整合包，必须同时
   提供完整对应源码，并保留许可证与版权声明。本项目自身代码亦以 AGPL-3.0 发布以保持一致；
   若有人把本项目改造成网络服务，须按 AGPL-3.0 第 13 条向使用者提供源码。
2. **BRIA RMBG-1.4**：仅限非商业用途，且不允许再分发权重文件。因此本包在
   `public` 档构建时会被门禁拒绝——这是设计行为，不是缺陷。
   如需公开分发，必须取得 BRIA 商业授权，或改用许可干净的替代模型（判断依据见
   `config/model-manifest.json` 的 `redistribution_status`）。
   本仓库与 GitHub Releases 均不得附带含该权重的构建产物。
3. **pngquant**：GPL-3.0。为避免许可证冲突，**不随包分发**；用户可自行放置到
   `src/tool/ext/pngquant.exe` 启用压缩。
4. **Microsoft Visual C++ Redistributable**：不随包分发，用户机器缺失时需自行安装官方运行库。

## 四、示例素材

`design/drafts/assets/` 下的示例照片取自上游仓库的 `images/test1.jpg`（与上游文件字节一致），
仅用于版式演示，随上游以 AGPL-3.0 分发。照片中人物的肖像权不在上游或本项目的授权范围内；
若要用于其它场景，请自行取得肖像权许可，或改用合成人像。

## 五、免责

本项目为个人自用工具，不对证件照的合规性作任何担保。各国/各机构的证件照规格可能随时调整，
使用前请以受理单位的最新要求为准。
