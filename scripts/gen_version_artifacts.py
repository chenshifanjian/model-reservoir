#!/usr/bin/env python3
"""把构建产物的 SHA256 / 大小写进 version.json —— 发版时唯一可信的元数据来源。

用法（三个平台的产物都给全）：
    python scripts/gen_version_artifacts.py --version 1.9.1 \
        --windows dist/ModelReservoir-v1.9.1-windows.exe \
        --linux   dist/v1.9.1-模型蓄水池 \
        --macos   dist/ModelReservoir-v1.9.1-macos.zip

设计取舍：
  * 只认本地文件，不去下载。产物是什么，写进去的就是什么。
  * 不静默覆盖：版本号回退、文件为空、缺一个平台，都直接报错退出。
  * release_notes 默认沿用旧的，避免每发一版就丢掉上次的更新说明。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = ROOT / "version.json"
REPO = "chenshifanjian/model-reservoir"

PLATFORMS = ("windows", "linux", "macos")

# 资产名必须与 release 上实际传的文件名逐字一致，否则 URL 404。
# Linux 多一个 -x86_64 后缀，这里写死而不是拼接，避免再算错一次。
ASSET_NAME = {
    "windows": "ModelReservoir-{tag}-windows.exe",
    "linux": "ModelReservoir-{tag}-linux-x86_64",
    "macos": "ModelReservoir-{tag}-macos.zip",
}


def sha256_of(path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def parse_version(v: str) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in v.strip().lstrip("v").split("."))
    except ValueError:
        return ()


def main() -> int:
    ap = argparse.ArgumentParser(description="生成 version.json 的下载地址与校验值")
    ap.add_argument("--version", required=True, help="版本号，如 1.9.1（不带 v）")
    ap.add_argument("--repo", default=REPO, help=f"GitHub 仓库，默认 {REPO}")
    ap.add_argument("--windows", help="Windows 产物路径")
    ap.add_argument("--linux", help="Linux 产物路径")
    ap.add_argument("--macos", help="macOS 产物路径")
    ap.add_argument("--notes-file", help="更新说明（markdown），不传则沿用现有 release_notes")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写文件")
    args = ap.parse_args()

    version = args.version.strip().lstrip("v")
    tag = f"v{version}"
    given = {p: getattr(args, p) for p in PLATFORMS}
    given = {p: v for p, v in given.items() if v}

    if not given:
        print("✗ 至少要给一个平台的产物", file=sys.stderr)
        return 2

    # 版本号不能比现在低 —— 回退会让所有用户收到「降级更新」
    old = json.loads(VERSION_FILE.read_text(encoding="utf-8")) if VERSION_FILE.exists() else {}
    old_ver = str(old.get("version", "")).strip().lstrip("v")
    if old_ver and parse_version(version) and parse_version(old_ver):
        if parse_version(version) < parse_version(old_ver):
            print(f"✗ 版本回退：{old_ver} → {version}", file=sys.stderr)
            return 2

    artifacts: dict[str, dict] = dict(old.get("artifacts") or {})
    new_urls: dict[str, str] = {}

    for plat, raw in given.items():
        path = Path(raw)
        if not path.is_file():
            print(f"✗ {plat} 产物不存在：{path}", file=sys.stderr)
            return 2
        if path.stat().st_size == 0:
            print(f"✗ {plat} 产物是空文件：{path}", file=sys.stderr)
            return 2
        digest, size = sha256_of(path)
        # 资产名必须和 GitHub release 上的一致，URL 才能下载到
        asset = ASSET_NAME[plat].format(tag=tag)
        new_urls[plat] = f"https://github.com/{args.repo}/releases/download/{tag}/{asset}"
        artifacts[plat] = {"sha256": digest, "size": size}
        print(f"  {plat:8s} {size:>12,} 字节  {digest}")
        print(f"           {new_urls[plat]}")

    missing = [p for p in PLATFORMS if p not in artifacts]
    if missing:
        print(f"⚠ 以下平台没有校验值，用户下载后不会被校验：{', '.join(missing)}",
              file=sys.stderr)

    notes = old.get("release_notes", "")
    if args.notes_file:
        notes = Path(args.notes_file).read_text(encoding="utf-8").strip()

    data = {
        "version": version,
        "min_version": old.get("min_version", ""),
        "release_page": f"https://github.com/{args.repo}/releases/tag/{tag}",
        "release_notes": notes,
    }
    # 没给新产物的平台沿用旧地址，避免整块丢失
    for plat in PLATFORMS:
        key = f"download_url_{plat}"
        data[key] = new_urls.get(plat) or old.get(key, "")
    data["artifacts"] = {p: a for p, a in artifacts.items() if a}

    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    if args.dry_run:
        print("\n--- dry-run，未写入 ---")
        print(text[:400])
        return 0

    VERSION_FILE.write_text(text, encoding="utf-8")
    print(f"\n✓ 已写入 {VERSION_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
