"""构建只包含可安装插件的 ZIP；不包含测试环境和参考仓库。"""

import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    "__init__.py",
    "plugin.py",
    "config.py",
    "handlers.py",
    "request_scope.py",
    "commands.py",
    "logic.py",
    "state.py",
    "prompts.py",
    "manifest.json",
    "README.md",
    "LICENSE",
    "LICENSE.upstream",
)


def main() -> None:
    """使用 manifest 中的版本，并检查每一个待打包文件存在。"""
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    for filename in FILES:
        if not (ROOT / filename).is_file():
            raise FileNotFoundError(filename)
    destination = ROOT / "dist"
    destination.mkdir(exist_ok=True)
    output = destination / f"{manifest['name']}-{manifest['version']}.zip"
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        for filename in FILES:
            archive.write(ROOT / filename, f"{manifest['name']}/{filename}")
    print(output)


if __name__ == "__main__":
    main()
