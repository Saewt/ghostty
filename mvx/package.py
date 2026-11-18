#!/usr/bin/env python3
"""Build the GhosttyKit package that Mvx links against.

`dev` rebuilds the engine in place for local work with an Mvx checkout.
`test` runs the engine tests in the configuration Mvx ships.
`publish` tests and packages a pushed tag on this Mac and publishes the package.
`release` is the clean, recorded build that `publish` runs.
"""

import argparse
import datetime
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

# A release is recorded from a clean checkout, so importing must not write files.
sys.dont_write_bytecode = True

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mvx"))
import ghostty_provenance as provenance  # noqa: E402

FORK_URL = "https://github.com/Saewt/ghostty.git"
FORK_REPOSITORY = "Saewt/ghostty"
# Provenance records absolute paths and is published, so packages are built here
# rather than under the maintainer's home directory.
PUBLISH_DIRECTORY = pathlib.Path("/private/tmp/mvx-engine-release")
RELEASE_TAG = re.compile(r"engine-\d+\.\d+\.\d+-mvx\.\d+\Z")
OUTPUT = ROOT / "macos/GhosttyKit.xcframework"
LIBRARY = OUTPUT / "macos-arm64/libghostty-internal.a"
# i18n stays off: enabling it links GNU libintl (LGPL) into the Mvx app.
BUILD_FLAGS = [
    "-Doptimize=ReleaseFast",
    "-Dapp-runtime=none",
    "-Demit-macos-app=false",
    "-Dxcframework-target=native",
    "-Demit-docs=false",
    "-Demit-terminfo=true",
    "-Demit-themes=false",
    "-Di18n=false",
]
# codeberg.org is unreachable from hosted runners and some networks. Zig checks
# the fetched content against the hash pinned by the translate_c package.
MIRRORED_DEPENDENCIES = [
    (
        "git+https://github.com/Vexu/arocc#d0c8c4d9c55daa7ef6e40cf0f630a5b5e900989b",
        "aro-0.0.0-JSD1QtuBNwCASyBtNF3pqTl_W3oAJQGEVyFAtrBSE_Pa",
    ),
]


def engine_version():
    text = (ROOT / "build.zig.zon").read_text(encoding="utf-8")
    return re.search(r'\.version\s*=\s*"([^"]+)"', text).group(1)


def zig_executable():
    zig = shutil.which(os.environ.get("ZIG", "zig"))
    if not zig:
        raise SystemExit("ERROR: zig was not found; set ZIG to the Zig executable")
    return zig


def build_command():
    return [zig_executable(), "build", "-Dversion-string=" + engine_version(), *BUILD_FLAGS]


def prefetch_dependencies():
    for url, expected in MIRRORED_DEPENDENCIES:
        fetched = subprocess.run(
            [zig_executable(), "fetch", url],
            cwd=tempfile.gettempdir(),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if fetched != expected:
            raise SystemExit("ERROR: {} has content hash {}, expected {}".format(url, fetched, expected))


def upstream_base():
    base = json.loads((ROOT / "mvx/upstream-base.json").read_text(encoding="utf-8"))
    remote = subprocess.run(
        ["git", "ls-remote", base["repository"], "refs/tags/{}^{{}}".format(base["tag"])],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    if not remote or remote[0].lower() != base["commit"].lower():
        raise SystemExit("ERROR: mvx/upstream-base.json does not match the upstream tag")
    return base


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    return path


def command_prefetch(_args):
    prefetch_dependencies()


def command_test(_args):
    # The version is explicit because the engine build rejects any other tag name.
    # Mvx ships the embedded library, so Ghostty's own macOS app tests do not apply.
    subprocess.run([
        zig_executable(), "build", "test",
        "-Dversion-string=" + engine_version(),
        "-Di18n=false",
        "-Demit-macos-app=false",
        "-Demit-xcframework=false",
    ], cwd=ROOT, check=True)


def git_output(*args):
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True, text=True).stdout.strip()


def require_no_private_paths(package):
    """Refuse to publish files that name this Mac's home directory."""
    home = str(pathlib.Path.home()).encode()
    for path in sorted(package.iterdir()):
        if path.suffix == ".zip" and path.name.startswith("ghostty-build-evidence"):
            with zipfile.ZipFile(path) as bundle:
                entries = [(path.name + ":" + name, bundle.read(name)) for name in bundle.namelist()]
        elif path.suffix == ".zip":
            continue
        else:
            entries = [(path.name, path.read_bytes())]
        for name, contents in entries:
            if home in contents:
                raise SystemExit("ERROR: {} names the local home directory".format(name))


def command_publish(args):
    if not RELEASE_TAG.fullmatch(args.tag):
        raise SystemExit("ERROR: release tags look like engine-1.4.0-mvx.1")
    if git_output("status", "--porcelain"):
        raise SystemExit("ERROR: commit your changes before publishing")
    head = git_output("rev-parse", "HEAD")
    remote = git_output("ls-remote", FORK_URL, "refs/tags/{0}^{{}}".format(args.tag)).split()
    if not remote or remote[0] != head:
        raise SystemExit("ERROR: push tag {} at this commit first".format(args.tag))

    shutil.rmtree(PUBLISH_DIRECTORY, ignore_errors=True)
    checkout = PUBLISH_DIRECTORY / "ghostty"
    package = PUBLISH_DIRECTORY / "package"
    zig_link = PUBLISH_DIRECTORY / "zig/zig"
    zig_link.parent.mkdir(parents=True)
    zig_link.symlink_to(zig_executable())
    environment = dict(os.environ, ZIG=str(zig_link))
    try:
        subprocess.run(["git", "clone", "--quiet", "--reference", str(ROOT), FORK_URL, str(checkout)], check=True)
        subprocess.run(["git", "checkout", "--quiet", "--detach", args.tag], cwd=checkout, check=True)
        script = str(checkout / "mvx/package.py")
        subprocess.run([sys.executable, script, "prefetch"], cwd=checkout, check=True, env=environment)
        if not args.skip_tests:
            subprocess.run([sys.executable, script, "test"], cwd=checkout, check=True, env=environment)
        subprocess.run(
            [sys.executable, script, "release", "--output", str(package), "--release-tag", args.tag],
            cwd=checkout,
            check=True,
            env=environment,
        )
        require_no_private_paths(package)
        subprocess.run([
            "gh", "release", "create", args.tag, "--repo", FORK_REPOSITORY, "--verify-tag", "--latest=false",
            "--title", "GhosttyKit " + args.tag, "--notes-file", str(package / "release-notes.md"),
            str(package / "GhosttyKit.xcframework.zip"), str(package / "ghostty-provenance.json"),
            str(package / "ghostty-build-evidence.zip"), str(package / "package-summary.json"),
        ], check=True)
    finally:
        shutil.rmtree(PUBLISH_DIRECTORY, ignore_errors=True)


def command_dev(_args):
    prefetch_dependencies()
    subprocess.run(build_command(), cwd=ROOT, check=True)
    print(OUTPUT)


def command_release(args):
    base = upstream_base()
    prefetch_dependencies()
    output = pathlib.Path(args.output).resolve()
    evidence = output / "evidence"
    evidence.mkdir(parents=True)

    before = provenance.current_source_snapshot(ROOT)
    if before["dirty"]:
        raise SystemExit("ERROR: a release package needs a clean checkout")
    for disposable in (OUTPUT, ROOT / ".zig-cache", ROOT / "zig-out"):
        if disposable.exists():
            shutil.rmtree(disposable)

    command = build_command()
    manifest = evidence / "source-manifest.txt"
    manifest.write_bytes(provenance.source_manifest_bytes(ROOT))
    recipe = write_json(evidence / "build-recipe.json", {
        "schema_version": 1,
        "command": command,
        "working_directory": str(ROOT),
        "clean": True,
        "outputs": ["static_library", "xcframework"],
    })
    toolchain = write_json(evidence / "toolchain.json", {
        "schema_version": 1,
        "zig": provenance.run(command[0], "version", cwd=ROOT).strip(),
        "xcode": provenance.run("xcodebuild", "-version", cwd=ROOT).strip(),
        "macos_sdk": provenance.run("xcrun", "--sdk", "macosx", "--show-sdk-version", cwd=ROOT).strip(),
        "clang": provenance.run("xcrun", "clang", "--version", cwd=ROOT).strip(),
    })

    started = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with (evidence / "build.log").open("w") as log:
        result = subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
    finished = datetime.datetime.now(datetime.timezone.utc).isoformat()
    after = provenance.current_source_snapshot(ROOT)
    if result.returncode:
        print((evidence / "build.log").read_text()[-4000:])
        raise SystemExit(result.returncode)

    build_result = write_json(evidence / "build-result.json", {
        "schema_version": 1,
        "success": True,
        "exit_code": 0,
        "source_before": before,
        "source_after": after,
        "build": {
            "started_at": started,
            "finished_at": finished,
            "recipe_sha256": provenance.sha256_file(recipe),
            "toolchain_sha256": provenance.sha256_file(toolchain),
            "outputs": {
                label: {
                    "path": str(path),
                    "sha256": provenance.sha256_path(path),
                    "mtime_ns": provenance.newest_mtime_ns(path),
                }
                for label, path in (("static_library", LIBRARY), ("xcframework", OUTPUT))
            },
        },
    })

    record = output / "ghostty-provenance.json"
    subprocess.run([
        sys.executable, str(ROOT / "mvx/ghostty_provenance.py"), "record",
        "--source-dir", str(ROOT),
        "--source-manifest", str(manifest),
        "--build-recipe", str(recipe),
        "--toolchain-record", str(toolchain),
        "--build-result", str(build_result),
        "--vendor-artifact", str(LIBRARY),
        "--vendor-xcframework", str(OUTPUT),
        "--verifier", "mvx engine CI: recorded subprocess build",
        "--upstream-base-commit", base["commit"],
        "--output", str(record),
    ], cwd=ROOT, check=True)

    archive = output / "GhosttyKit.xcframework.zip"
    subprocess.run(
        ["/usr/bin/ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(OUTPUT), str(archive)],
        check=True,
    )
    with zipfile.ZipFile(output / "ghostty-build-evidence.zip", "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(evidence.iterdir()):
            bundle.write(path, path.name)
    shutil.rmtree(evidence)

    summary = {
        "release_tag": args.release_tag,
        "fork_commit": before["commit"],
        "upstream_tag": base["tag"],
        "upstream_base_commit": base["commit"],
        "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "provenance_sha256": hashlib.sha256(record.read_bytes()).hexdigest(),
    }
    write_json(output / "package-summary.json", summary)
    (output / "release-notes.md").write_text(
        "GhosttyKit for Mvx, built from this fork.\n\n"
        "Fork commit: `{fork_commit}`\n"
        "Upstream base: Ghostty `{upstream_tag}` (`{upstream_base_commit}`)\n"
        "Archive SHA-256: `{archive_sha256}`\n"
        "Provenance SHA-256: `{provenance_sha256}`\n\n"
        "`arocc` was fetched from `github.com/Vexu/arocc` because `codeberg.org` is unreachable "
        "from the build host. Zig verified the content hash pinned by the tag.\n".format(**summary),
        encoding="utf-8",
    )
    print(json.dumps(summary, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("prefetch").set_defaults(run=command_prefetch)
    commands.add_parser("test").set_defaults(run=command_test)
    commands.add_parser("dev").set_defaults(run=command_dev)
    publish = commands.add_parser("publish")
    publish.add_argument("--tag", required=True)
    publish.add_argument("--skip-tests", action="store_true", help="Only when this commit already passed.")
    publish.set_defaults(run=command_publish)
    release = commands.add_parser("release")
    release.add_argument("--output", required=True)
    release.add_argument("--release-tag", required=True)
    release.set_defaults(run=command_release)
    args = parser.parse_args()
    args.run(args)


if __name__ == "__main__":
    main()
