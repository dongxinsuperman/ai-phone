# Third-Party Notices

This file summarizes bundled third-party components and notable optional
dependencies used by ai-phone. Each component remains under its own license.
When updating a vendored component or binary, update this file and keep the
upstream license text where required.

## Bundled Components

### WebDriverAgent

- Path: `third_party/WebDriverAgent/`
- Upstream: https://github.com/appium/WebDriverAgent
- License: Apache License 2.0
- License file: `third_party/WebDriverAgent/LICENSE`

### scrcpy server

- Path: `backend/assets/scrcpy-server.jar`
- Upstream: https://github.com/Genymobile/scrcpy
- Version noted in code/docs: scrcpy server v2.4
- License: Apache License 2.0

### ADBKeyBoard

- Path: `backend/assets/ADBKeyBoard.apk`
- Upstream: https://github.com/senzhk/ADBKeyBoard
- Purpose: Android Unicode text input through an input method broadcast
- License: GNU GPLv2
- Note: The APK is bundled as an independent third-party component. When
  redistributing this repository or a package containing this APK, comply with
  the upstream GPLv2 terms and keep a clear notice of its origin and license.

## Optional Dependencies

### pymobiledevice3

- Upstream: https://github.com/doronz88/pymobiledevice3
- License: GNU GPLv3
- Usage: optional iOS support installed with `pip install -e ".[ios]"`
- Bundling status: not bundled in this repository

### hmdriver2

- Upstream: https://github.com/codematrixer/hmdriver2
- License: MIT License
- Usage: optional HarmonyOS support installed with `pip install -e ".[harmony]"`
- Bundling status: not bundled in this repository

## Python and Node Dependencies

Runtime Python dependencies are declared in `backend/pyproject.toml`. Web and
Midscene bridge dependencies are declared in their respective `package.json` and
lock files. See each dependency's package metadata for its license terms.
