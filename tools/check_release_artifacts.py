"""Validate the archives users will install before exposing a release candidate.

Run after ``python -m build``. The optional tag check is useful when preparing
a GitHub release: ``python tools/check_release_artifacts.py dist --tag v1.0.0``.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import stat
import tarfile
import tomllib
import zipfile
from email.parser import Parser
from pathlib import Path


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def metadata(raw: bytes) -> tuple[str, str]:
    fields = Parser().parsestr(raw.decode("utf-8"), headersonly=True)
    return fields["Name"], fields["Version"]


def check(dist: Path, tag: str | None) -> list[Path]:
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    name, version = project["name"], project["version"]
    require(name == "h5reclaim", f"unexpected distribution name: {name}")
    if tag is not None:
        require(tag == f"v{version}", f"tag {tag!r} does not match package version v{version}")

    wheel_path = dist / f"h5reclaim-{version}-py3-none-any.whl"
    sdist_path = dist / f"h5reclaim-{version}.tar.gz"
    archives = sorted(path for path in dist.iterdir() if path.suffix in {".whl", ".gz"})
    require(set(archives) == {wheel_path, sdist_path},
            f"expected exactly one sdist and one universal wheel for {version}: {archives}")

    with tarfile.open(sdist_path, "r:gz") as tar:
        names = tar.getnames()
        prefix = f"h5reclaim-{version}/"
        require(len(names) == len(set(names)), "source distribution contains duplicate member names")
        require(all((member.name == prefix.rstrip("/") and member.isdir()
                     or member.name.startswith(prefix))
                    and ".." not in Path(member.name).parts and (member.isfile() or member.isdir())
                    for member in tar.getmembers()), "source distribution contains an unsafe member")
        contents = {
            name[len(prefix):]: tar.extractfile(name).read()
            for name in names if name.startswith(prefix) and tar.getmember(name).isfile()
        }
    require(metadata(contents["PKG-INFO"]) == (name, version), "sdist metadata version/name differs")
    for required in ("README.md", "LICENSE", "CHANGELOG.md", "SECURITY.md",
                     "pyproject.toml", "assets/h5reclaim-banner.png",
                     "docs/report-schema.md"):
        require(required in contents, f"source distribution is missing {required}")
    for locale in ("pl", "de", "fr", "es", "pt-BR", "zh-CN", "ja", "ko", "ru", "ar"):
        required = f"README.{locale}.md"
        require(required in contents, f"source distribution is missing {required}")
    sources = {path.removeprefix("src/"): data for path, data in contents.items()
               if path.startswith("src/h5reclaim/") and path.endswith(".py")}
    require(sources and "h5reclaim/__main__.py" in sources,
            "source distribution lacks package entry point")
    version_assignments = [node.value.value for node in ast.parse(
        sources["h5reclaim/recovery.py"], filename="recovery.py"
    ).body if isinstance(node, ast.Assign)
                           and any(isinstance(target, ast.Name) and target.id == "VERSION"
                                   for target in node.targets)
                           and isinstance(node.value, ast.Constant)
                           and isinstance(node.value.value, str)]
    require(version_assignments == [version], "CLI/report version differs from package metadata")
    require(all(not path.startswith(("tests/", "benchmarks/", "corpus/", "tools/", ".github/"))
                for path in contents), "source distribution contains development data or test corpus")

    with zipfile.ZipFile(wheel_path) as wheel:
        names = wheel.namelist()
        require(len(names) == len(set(names)), "wheel contains duplicate member names")
        require(all(not item.startswith("/") and ".." not in Path(item).parts and
                    (stat.S_IFMT(info.external_attr >> 16) in
                     ((stat.S_IFDIR,) if info.is_dir() else (0, stat.S_IFREG)))
                    for info in wheel.infolist() for item in [info.filename]),
                "wheel contains an unsafe member")
        packaged_sources = {path: wheel.read(path) for path in names
                            if path.startswith("h5reclaim/") and path.endswith(".py")}
        require(packaged_sources == sources,
                "wheel and source distribution contain different Python package code")
        dist_info = f"h5reclaim-{version}.dist-info/"
        require(metadata(wheel.read(dist_info + "METADATA")) == (name, version),
                "wheel metadata version/name differs")
        entry_points = wheel.read(dist_info + "entry_points.txt").decode("utf-8")
        require("h5reclaim = h5reclaim.__main__:main" in entry_points,
                "wheel is missing the h5reclaim console entry point")
        require(any(path.startswith(dist_info + "licenses/") and path.endswith("LICENSE")
                    for path in names), "wheel is missing its license")
        require(all(not path.startswith(("tests/", "benchmarks/", "corpus/", "tools/"))
                    for path in names), "wheel contains development data or test corpus")

    manifest = dist / "SHA256SUMS"
    manifest.write_text("".join(
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
        for path in (sdist_path, wheel_path)
    ), encoding="ascii")
    return [sdist_path, wheel_path, manifest]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dist", type=Path)
    parser.add_argument("--tag", help="expected Git tag, for example v1.0.0")
    args = parser.parse_args()
    for path in check(args.dist, args.tag):
        print(f"Checked: {path}")


if __name__ == "__main__":
    main()
