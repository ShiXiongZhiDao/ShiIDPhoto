# ShiIDPhoto

[简体中文](README.md) | English

ShiIDPhoto is a portable Windows ID photo maker. It reuses
[aoguai/LiYing](https://github.com/aoguai/LiYing) as the image core and adds a launcher,
a custom local UI, presets, model license gates, and a build pipeline.

Photos are processed locally. The local service listens on `127.0.0.1` only; there are no
registry writes, no admin requirements, and no background service.

## Features

- **Drag and go**: drop in a photo, pick a size and background color, and generate a digital
  copy or a print sheet.
- **Local processing**: photos and models stay on your machine; no remote service is involved.
- **Remembered choices**: size, background, output mode, sheet, custom size, and output settings
  are stored with the package and restored after a refresh or restart.
- **First-run setup**: the slim package downloads missing models in the page, with resume,
  pause/continue, and manual install.
- **Custom sizes**: enter pixels, or millimeters plus DPI; save frequently used sizes.
- **Output control**: JPG / JPEG / PNG, four quality levels, optional size cap.
- **Portable**: unzip and run; no registry writes, no admin rights, no installation.

## Build from source

Requires Windows 10/11 x64, Python 3.11+, and Git.

```bat
python scripts\fetch_assets.py --tier personal --pin
python scripts\build_portable.py --tier personal
python scripts\verify_portable.py
```

- `fetch_assets.py` downloads the embedded Python runtime, upstream source, models, and
  wheels; about 400 MB.
- `build_portable.py` assembles the package in `dist/staging` and writes the artifact to `dist/`.
- `verify_portable.py` checks structure, presets, model hashes, and the license gate.

For the slim package without models:

```bat
python scripts\build_portable.py --tier personal --no-models
```

After building, double-click `启动师兄证照.bat` to run. `build-assets/`, `dist/`, and
`user-data/` are generated and are not committed.

## Layout

```
├─ 启动师兄证照.bat
├─ launcher.py              # self-check → find a port → start the service → open the browser
├─ portable_checks.py       # shared runtime/build validation
├─ shell/                   # custom UI and local service
├─ config/                  # product config, model manifest, presets, build assets
├─ scripts/                 # fetch, build, preset injection, verification
├─ docs/                    # user guide
└─ licenses/                # third-party notices
```

## Models and licensing

This repository contains **no model weights**. The build downloads them from upstream and
the official model repositories and verifies each SHA256.

- The code is licensed under AGPL-3.0; see [`LICENSE`](LICENSE).
- Upstream LiYing is AGPL-3.0. Component and model licenses, including redistribution status,
  are listed in [`licenses/THIRD_PARTY_NOTICES.md`](licenses/THIRD_PARTY_NOTICES.md).
- The default `personal` tier allows restricted models in a local package for personal use.
  The `public` tier is blocked by the model license gate by design. Public distribution
  requires a license or a replacement background-removal model.

## Known limitations

- PNG is lossless and has no size cap; use JPG when you need a size limit.
- `pngquant` is not bundled because of its license; compression is disabled.
- The skin-retouch model license is unverified; that feature is disabled.
- Composition follows general ID-photo rules; passports and visas that require a larger face
  are out of scope.
- Windows 10/11 x64 only; the service listens on `127.0.0.1` only.

## User guide

See [`docs/PORTABLE_USER_GUIDE.md`](docs/PORTABLE_USER_GUIDE.md) (Chinese).

Version history is in [`CHANGELOG.md`](CHANGELOG.md); release packages are on
[Releases](https://github.com/ShiXiongZhiDao/ShiIDPhoto/releases).

## Contributing

Issues and pull requests are welcome. For larger changes, please open an issue first.
Before submitting, run `python scripts/verify_portable.py` after building.
