# 师兄证照（ShiIDPhoto）

[English](README.en.md) | 简体中文

师兄证照是一个解压即用的 Windows 证件照工具。图像内核复用
[aoguai/LiYing](https://github.com/aoguai/LiYing)，本项目提供启动器、自研前端、
预设、模型许可门禁与构建管线。

照片处理在本机完成，服务只监听 `127.0.0.1`；不写注册表、不要求管理员、不安装服务。

## 特性

- **拖入即用**：拖一张照片，选规格与底色，生成电子版或相纸排版。
- **本地处理**：照片与模型都在本机，不经过远端服务。
- **首次向导**：不带模型的瘦包在页面里下载缺失模型，支持断点续传、暂停/继续、手动安装。
- **自定义尺寸**：像素或毫米 + DPI 二选一，常用规格可保存。
- **输出可控**：JPG / JPEG / PNG，四档质量，可设体积上限。
- **便携**：解压即用，不写注册表、不要求管理员、不装服务。

## 从源码构建

需要 Windows 10/11 x64、Python 3.11+ 与 Git。

```bat
python scripts\fetch_assets.py --tier personal --pin
python scripts\build_portable.py --tier personal
python scripts\verify_portable.py
```

- `fetch_assets.py` 下载 Python 嵌入式运行时、上游源码、模型与 wheel；约 400 MB。
- `build_portable.py` 在 `dist/staging` 组装，产物在 `dist/`。
- `verify_portable.py` 检查结构、预设、模型哈希与许可门禁。

不带模型的瘦包：

```bat
python scripts\build_portable.py --tier personal --no-models
```

构建后双击 `启动师兄证照.bat` 即可运行。`build-assets/`、`dist/` 与 `user-data/` 都是
生成物，不进版本库。

## 项目结构

```
├─ 启动师兄证照.bat
├─ launcher.py              # 自检 → 找端口 → 拉起服务 → 开浏览器 → 盯加载
├─ portable_checks.py       # 运行期与构建期共用的校验
├─ shell/                   # 自研前端与本地服务
├─ config/                  # 产品配置、模型清单、预设、构建资产
├─ scripts/                 # 取资产、构建、预设注入、验收
├─ docs/                    # 使用说明
└─ licenses/                # 第三方组件与模型声明
```

## 模型与许可边界

本仓库**不含任何模型权重**；构建时按 SHA256 从上游与模型官方仓库拉取。

- 代码以 AGPL-3.0 发布，全文见 [`LICENSE`](LICENSE)。
- 上游 LiYing 是 AGPL-3.0；第三方组件与各模型权重的许可证、再分发状态见
  [`licenses/THIRD_PARTY_NOTICES.md`](licenses/THIRD_PARTY_NOTICES.md)。
- 默认的 `personal` 档允许受限的抠图模型进入本地包，仅限自用；`public` 档会被模型许可
  门禁拒绝，这是设计行为。要公开发行，必须先取得授权或替换抠图模型。

## 已知限制

- PNG 无损压缩不提供体积上限；需要控体积请用 JPG。
- `pngquant` 因许可证原因不随包，压缩功能关闭。
- 自动美肤模型许可未核实，功能关闭。
- 构图参数按通用证件照口径，护照/签证等「脸占更大」的类别做不出来。
- 只承诺 Windows 10/11 x64，只监听 `127.0.0.1`。

## 使用说明

见 [`docs/PORTABLE_USER_GUIDE.md`](docs/PORTABLE_USER_GUIDE.md)。

## 参与

欢迎提交 issue 与 pull request；较大的改动请先开 issue 对齐。提交前请先跑
`python scripts/verify_portable.py`（需要先构建）。
