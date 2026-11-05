# Privacy Policy / 隐私政策

> English first, 简体中文在后。Last updated / 最后更新：2026-11-05

---

## English

### In one line

**ShiIDPhoto collects nothing about you.** There is no account, no telemetry, no analytics, and
no server of the author's that your photos could reach.

### Where your data lives

Everything stays inside the folder you unzipped, under `user-data\`:

| What | Where | Notes |
| --- | --- | --- |
| The photo you drop in | `user-data\input\` | A copy of your file, kept so a re-run does not need you to pick it again |
| The generated ID photo and print sheet | `user-data\output\` | Filenames carry the original name and a timestamp |
| AI model weights | `user-data\models\` | Downloaded by you on first run; ~181 MB |
| Your remembered choices | `user-data\prefs.json` | Size, background, output mode, sheet, custom size, format/quality/cap, interface language, dark/light appearance |
| Startup and errors | `user-data\logs\webui.log` | Timestamps, the port chosen, AI model paths, error text |

Nothing in that table is sent anywhere. Delete the folder and there is no trace left: no
registry entries, no installed service, no scheduled task, no administrator rights were ever
requested.

### The only outbound requests

Two things fetch from the network, and both are **inbound only** — no photo, no filename, no
identifier goes out with them:

1. **First-run AI model download.** The published package does not contain the model weights, so the
   in-page wizard fetches them from public mirrors (Hugging Face and its China mirror),
   with a SHA256 check on every file. About 181 MB, once, resumable.
2. **The three QR-code images on the *About* screen.** They are hosted on GitHub, with a
   fallback to Gitee. They are static images; nothing about you is in the request.

Beyond those, the app loads no external CDN, no remote fonts, no analytics SDK.

### On the local network

The service binds `127.0.0.1` — loopback only — on the first free port between 7860 and 7899.
It is not reachable from another machine on your network, and it accepts no remote addresses.
Your browser talks to it; nothing else can.

### AI model licences are not privacy, but they are worth reading

Downloading them is not the same as being granted a licence. One background-removal model
(BRIA RMBG-1.4) is non-commercial and forbids redistribution, which is exactly why the package
with weights bundled is not distributed. Per-model terms, including redistribution
status, are listed in
[`licenses/THIRD_PARTY_NOTICES.md`](licenses/THIRD_PARTY_NOTICES.md).

### No sharing, no selling

There is no account, so there is no account data to link. Nothing is shared, exchanged or sold
with any third party — there is nothing to share.

### Contact

Open an issue on the repository, or use the contacts at the end of the README.

---

## 简体中文

### 一句话

**师兄证照不收集你的任何数据。** 没有账号、没有遥测、没有统计，也没有一个能让你的照片到达的
作者的服务器。

### 你的数据在哪

全部在你解压的那个文件夹里，`user-data\` 下面：

| 内容 | 位置 | 说明 |
| --- | --- | --- |
| 你拖进来的照片 | `user-data\input\` | 存一份副本，重算时不用再选一次文件 |
| 生成的电子版与相纸排版 | `user-data\output\` | 文件名带原图名与时间戳 |
| AI 模型权重 | `user-data\models\` | 由你在首次运行时下载，约 181 MB |
| 你记住的选择 | `user-data\prefs.json` | 规格、底色、输出模式、相纸、自定义尺寸、格式/质量/体积上限、界面语言、深浅外观 |
| 启动信息与报错 | `user-data\logs\webui.log` | 时间、最终端口、AI 模型路径、错误文本 |

这张表里的任何东西都不会被发出去。删掉文件夹就没有残留：不写注册表、不装服务、没有计划任务，
也从头到尾没要求过管理员权限。

### 唯一会出本机的请求

两件事会从网络取东西，而且都**只是「取进来」**——请求里不含照片、不含文件名、不含任何标识：

1. **首次运行的 AI 模型下载。** 提供的安装包不含权重，页面里的首次向导会从公开镜像
   （Hugging Face 及其国内镜像）取那三个文件，每个都按 SHA256 校验。约 181 MB，只下这一次，
   断点可续。
2. **「关于」页那三张二维码图。** 放在 GitHub 上，失败时退到 Gitee。就是三张静态图，
   请求里没有关于你的任何信息。

除此之外，本软件不加载外部 CDN、不用远程字体、不接统计 SDK。

### 局域网层面

服务只绑定 `127.0.0.1`（回环地址），端口在 7860–7899 里取第一个空的。同一网络里的其他机器
连不上它，它也不接受远程地址。只有你这台机器上的浏览器和它说话。

### AI 模型许可不是隐私，但值得一读

**下载到 AI 模型不等于拿到授权。** 其中一个抠图 AI 模型（BRIA RMBG-1.4）禁止商用、禁止再分发——
这正是带权重的完整包不公开提供的原因。逐个 AI 模型的许可与再分发状态写在
[`licenses/THIRD_PARTY_NOTICES.md`](licenses/THIRD_PARTY_NOTICES.md)。

### 不共享、不出售

没有账号，也就没有可关联的账户数据。不与任何第三方共享、交换或出售数据——因为手里没有数据。

### 联系

在仓库提 issue，或者用 README 末尾的联系方式。
