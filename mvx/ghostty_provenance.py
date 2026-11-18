#!/usr/bin/env python3
"""Create and verify an attestation for a rebuilt GhosttyKit artifact.

The recorder intentionally does not run Ghostty and does not treat a command
line as a build result.  A separate build step must provide a successful,
structured result containing the source snapshots, recipe/toolchain digests,
and output hashes.  This tool verifies those claims against the checkout and
the files on disk before it writes ``verified: true``.
"""

import argparse
import datetime as datetime_module
import hashlib
import json
import os
import pathlib
import re
import subprocess


SCHEMA_VERSION = 2
# A package built from the Mvx fork names the fork commit it was built from and
# the upstream commit that fork is based on as separate, verified fields.
FORK_SCHEMA_VERSION = 3
BUILD_RESULT_SCHEMA_VERSION = 1
RECIPE_SCHEMA_VERSION = 1
UPSTREAM_REPOSITORY = "https://github.com/ghostty-org/ghostty"
FORK_REPOSITORY = "https://github.com/saewt/ghostty"
SOURCE_REPOSITORIES = (UPSTREAM_REPOSITORY, FORK_REPOSITORY)
SHA256 = re.compile(r"[0-9a-fA-F]{64}\Z")
COMMIT = re.compile(r"[0-9a-fA-F]{40}\Z")


def fail(message):
    raise SystemExit("ERROR: " + message)


def read_bytes(path, label):
    try:
        return pathlib.Path(path).read_bytes()
    except OSError as error:
        fail("cannot read {} {}: {}".format(label, path, error))


def read_json(path, label):
    try:
        return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        fail("cannot read valid {} JSON from {}: {}".format(label, path, error))


def sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    try:
        with pathlib.Path(path).open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        fail("cannot read {}: {}".format(path, error))
    return digest.hexdigest()


def sha256_path(path):
    """Hash a file or directory deterministically, including symlink targets."""
    candidate = pathlib.Path(path)
    if candidate.is_file():
        return sha256_file(candidate)
    if not candidate.is_dir():
        fail("output path does not exist: {}".format(candidate))

    digest = hashlib.sha256()
    for current, directories, files in os.walk(candidate, followlinks=False):
        directories.sort()
        files.sort()
        for name in directories + files:
            entry = pathlib.Path(current) / name
            relative = entry.relative_to(candidate).as_posix()
            if entry.is_symlink():
                digest.update(
                    "symlink\0{}\0{}\n".format(relative, os.readlink(entry)).encode("utf-8")
                )
            elif entry.is_file():
                digest.update("file\0{}\0".format(relative).encode("utf-8"))
                digest.update(bytes.fromhex(sha256_file(entry)))
                digest.update(b"\n")
            elif entry.is_dir():
                digest.update("directory\0{}\n".format(relative).encode("utf-8"))
    return digest.hexdigest()


def newest_mtime_ns(path):
    candidate = pathlib.Path(path)
    try:
        newest = candidate.lstat().st_mtime_ns
    except OSError as error:
        fail("cannot stat {}: {}".format(candidate, error))
    if candidate.is_dir():
        for current, directories, files in os.walk(candidate, followlinks=False):
            for name in directories + files:
                entry = pathlib.Path(current) / name
                try:
                    newest = max(newest, entry.lstat().st_mtime_ns)
                except OSError as error:
                    fail("cannot stat {}: {}".format(entry, error))
    return newest


def run(*args, cwd):
    try:
        result = subprocess.run(
            list(args), cwd=cwd, text=True, capture_output=True, check=False
        )
    except OSError as error:
        fail("could not run {}: {}".format(args[0], error))
    if result.returncode != 0:
        fail(result.stderr.strip() or "{} failed".format(" ".join(args)))
    return result.stdout


def run_bytes(*args, cwd):
    try:
        result = subprocess.run(
            list(args), cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False
        )
    except OSError as error:
        fail("could not run {}: {}".format(args[0], error))
    if result.returncode != 0:
        fail(result.stderr.decode("utf-8", "replace").strip() or "{} failed".format(" ".join(args)))
    return result.stdout


def validate_sha(value, label):
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        fail("{} must be a 64-character SHA-256".format(label))
    return value.lower()


def validate_commit(value, label):
    if not isinstance(value, str) or not COMMIT.fullmatch(value):
        fail("{} must be a full 40-character Git commit".format(label))
    return value.lower()


def normalize_repository(value):
    if not isinstance(value, str) or not value.startswith("https://"):
        return ""
    normalized = value.rstrip("/").lower()
    return normalized[:-4] if normalized.endswith(".git") else normalized


def source_repository(source):
    repository = run("git", "remote", "get-url", "origin", cwd=source).strip()
    normalized = normalize_repository(repository)
    if normalized not in SOURCE_REPOSITORIES:
        fail(
            "Ghostty origin must be exactly {} or {}; got {}".format(
                UPSTREAM_REPOSITORY, FORK_REPOSITORY, repository
            )
        )
    return normalized


def require_upstream_ancestor(source, upstream_commit):
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", upstream_commit, "HEAD"],
        cwd=source,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        fail("Ghostty fork is not based on upstream commit {}".format(upstream_commit))


def empty_status_sha256():
    return sha256_bytes(b"")


def source_manifest_bytes(source):
    # This is a commit-bound manifest, not a listing of whatever files happen
    # to be present in a working directory.
    return run_bytes("git", "ls-tree", "-r", "--full-tree", "HEAD", cwd=source)


def git_archive_sha256(source):
    return sha256_bytes(run_bytes("git", "archive", "--format=tar", "HEAD", cwd=source))


def current_source_snapshot(source, manifest_path=None):
    commit = run("git", "rev-parse", "HEAD", cwd=source).strip()
    validate_commit(commit, "upstream commit")
    tree = run("git", "rev-parse", "HEAD^{tree}", cwd=source).strip()
    validate_commit(tree, "upstream tree")
    status = run(
        "git",
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        cwd=source,
    )
    manifest = source_manifest_bytes(source)
    if manifest_path is not None:
        recorded_manifest = read_bytes(manifest_path, "source manifest")
        if recorded_manifest != manifest:
            fail("source manifest does not describe the current upstream commit")
    return {
        "repository": source_repository(source),
        "commit": commit.lower(),
        "tree": tree.lower(),
        "manifest_sha256": sha256_bytes(manifest),
        "archive_sha256": git_archive_sha256(source),
        "status_sha256": sha256_bytes(status.encode("utf-8")),
        "dirty": bool(status),
    }


def path_for_record(path):
    resolved = pathlib.Path(path).resolve()
    try:
        return resolved.relative_to(pathlib.Path.cwd().resolve()).as_posix()
    except ValueError:
        return str(resolved)


def parse_timestamp(value, label):
    if not isinstance(value, str) or not value.strip():
        fail("{} is missing".format(label))
    try:
        parsed = datetime_module.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        fail("{} is not an ISO-8601 timestamp: {}".format(label, error))
    if parsed.tzinfo is None:
        fail("{} must include a timezone".format(label))
    return parsed.timestamp()


def validate_source_snapshot(snapshot, expected, label):
    if not isinstance(snapshot, dict):
        fail("build result {} source snapshot is missing".format(label))
    if normalize_repository(snapshot.get("repository")) not in SOURCE_REPOSITORIES:
        fail("build result {} source repository is not Ghostty upstream or the Mvx fork".format(label))
    for key, validator in (
        ("commit", validate_commit),
        ("tree", validate_commit),
        ("manifest_sha256", validate_sha),
        ("archive_sha256", validate_sha),
        ("status_sha256", validate_sha),
    ):
        validator(snapshot.get(key), "build result {} source {}".format(label, key))
    if snapshot.get("dirty") is not False:
        fail("build result {} source snapshot is dirty".format(label))
    for key in ("repository", "commit", "tree", "manifest_sha256", "archive_sha256", "status_sha256", "dirty"):
        expected_value = expected[key]
        actual_value = snapshot[key]
        if key == "repository":
            expected_value = normalize_repository(expected_value)
            actual_value = normalize_repository(actual_value)
        else:
            if isinstance(expected_value, str):
                expected_value = expected_value.lower()
            if isinstance(actual_value, str):
                actual_value = actual_value.lower()
        if actual_value != expected_value:
            fail("build result {} source {} differs from the checkout".format(label, key))


def validate_recipe(path):
    recipe = read_json(path, "build recipe")
    if not isinstance(recipe, dict) or recipe.get("schema_version") != RECIPE_SCHEMA_VERSION:
        fail("build recipe must use schema_version {}".format(RECIPE_SCHEMA_VERSION))
    command = recipe.get("command")
    if (
        not isinstance(command, list)
        or not command
        or any(not isinstance(part, str) or not part for part in command)
    ):
        fail("build recipe command must be a non-empty argument array")
    if pathlib.Path(command[0]).name != "zig":
        fail("build recipe must invoke Zig directly")
    if not isinstance(recipe.get("working_directory"), str) or not recipe["working_directory"].strip():
        fail("build recipe working_directory is missing")
    if recipe.get("clean") is not True:
        fail("build recipe must prove that output paths were cleaned before the build")
    outputs = recipe.get("outputs")
    if not isinstance(outputs, list) or set(outputs) != {"static_library", "xcframework"}:
        fail("build recipe must name static_library and xcframework outputs")
    return recipe


def validate_toolchain(path):
    toolchain = read_json(path, "toolchain record")
    if not isinstance(toolchain, dict) or toolchain.get("schema_version") != 1:
        fail("toolchain record must use schema_version 1")
    # Keep the names explicit so a generic compiler label cannot stand in for
    # the Zig version used to produce GhosttyKit.
    required = ("zig", "xcode", "macos_sdk", "clang")
    for key in required:
        if not isinstance(toolchain.get(key), str) or not toolchain[key].strip():
            fail("toolchain record is missing {} version".format(key))
    return toolchain


def validate_output_result(result, label, expected_path, started, finished):
    if not isinstance(result, dict):
        fail("build result {} output record is missing".format(label))
    actual_path = pathlib.Path(result.get("path", "")).resolve()
    expected_path = pathlib.Path(expected_path).resolve()
    if actual_path != expected_path:
        fail("build result {} output path differs from the requested output".format(label))
    expected_hash = validate_sha(result.get("sha256"), "build result {} output SHA-256".format(label))
    actual_hash = sha256_path(expected_path)
    if actual_hash != expected_hash:
        fail("{} output is stale or differs from the successful build result".format(label))
    if not isinstance(result.get("mtime_ns"), int) or result["mtime_ns"] <= 0:
        fail("build result {} output mtime_ns is missing".format(label))
    actual_mtime_ns = newest_mtime_ns(expected_path)
    if actual_mtime_ns != result["mtime_ns"]:
        fail("{} output changed after the successful build result was recorded".format(label))
    if actual_mtime_ns < int(started * 1_000_000_000) or actual_mtime_ns > int(finished * 1_000_000_000):
        fail("{} output is stale or was not produced during the recorded build".format(label))
    return {
        "path": path_for_record(expected_path),
        "sha256": actual_hash,
        "mtime_ns": actual_mtime_ns,
    }


def validate_build_result(path, source, recipe, toolchain, static_library, xcframework):
    result = read_json(path, "build result")
    if not isinstance(result, dict) or result.get("schema_version") != BUILD_RESULT_SCHEMA_VERSION:
        fail("build result must use schema_version {}".format(BUILD_RESULT_SCHEMA_VERSION))
    if result.get("success") is not True or result.get("exit_code") != 0:
        fail("build result does not independently record a successful build")
    source_before = result.get("source_before")
    source_after = result.get("source_after")
    validate_source_snapshot(source_before, source, "before")
    validate_source_snapshot(source_after, source, "after")
    if source_before != source_after:
        fail("source checkout changed during the recorded build")

    build = result.get("build")
    if not isinstance(build, dict):
        fail("build result build record is missing")
    started = parse_timestamp(build.get("started_at"), "build.started_at")
    finished = parse_timestamp(build.get("finished_at"), "build.finished_at")
    if finished < started:
        fail("build.finished_at precedes build.started_at")
    recipe_path = pathlib.Path(recipe["__path"])
    toolchain_path = pathlib.Path(toolchain["__path"])
    if validate_sha(build.get("recipe_sha256"), "build recipe SHA-256") != sha256_file(recipe_path).lower():
        fail("build result recipe SHA-256 does not match the recipe file")
    if validate_sha(build.get("toolchain_sha256"), "toolchain SHA-256") != sha256_file(toolchain_path).lower():
        fail("build result toolchain SHA-256 does not match the toolchain file")
    outputs = build.get("outputs")
    if not isinstance(outputs, dict):
        fail("build result outputs are missing")
    static_record = validate_output_result(
        outputs.get("static_library"), "static library", static_library, started, finished
    )
    xcframework_record = validate_output_result(
        outputs.get("xcframework"), "XCFramework", xcframework, started, finished
    )
    return {
        "result_sha256": sha256_file(path),
        "result_path": path_for_record(path),
        "success": True,
        "exit_code": 0,
        "started_at": build["started_at"],
        "finished_at": build["finished_at"],
        "recipe_sha256": sha256_file(recipe_path),
        "toolchain_sha256": sha256_file(toolchain_path),
        "source_before": source_before,
        "source_after": source_after,
        "outputs": {
            "static_library": static_record,
            "xcframework": xcframework_record,
        },
    }


def validate_provenance_shape(provenance):
    if not isinstance(provenance, dict) or provenance.get("schema_version") not in (
        SCHEMA_VERSION,
        FORK_SCHEMA_VERSION,
    ):
        fail("Ghostty provenance must use schema_version {} or {}".format(SCHEMA_VERSION, FORK_SCHEMA_VERSION))
    is_fork = provenance["schema_version"] == FORK_SCHEMA_VERSION
    if provenance.get("verified") is not True:
        fail("Ghostty provenance is absent or not verified")
    if normalize_repository(provenance.get("upstream_repository")) != UPSTREAM_REPOSITORY:
        fail("Ghostty provenance does not name the exact upstream repository")
    validate_commit(provenance.get("upstream_commit"), "Ghostty upstream_commit")
    built_commit = provenance["upstream_commit"].lower()
    expected_repository = UPSTREAM_REPOSITORY
    if is_fork:
        if normalize_repository(provenance.get("source_repository")) != FORK_REPOSITORY:
            fail("Ghostty provenance does not name the Mvx fork repository")
        built_commit = validate_commit(provenance.get("fork_commit"), "Ghostty fork_commit")
        expected_repository = FORK_REPOSITORY
    validate_sha(provenance.get("source_archive_sha256"), "Ghostty source_archive_sha256")
    validate_sha(provenance.get("source_manifest_sha256"), "Ghostty source_manifest_sha256")
    verification = provenance.get("verification")
    if not isinstance(verification, dict) or verification.get("verified") is not True:
        fail("Ghostty verification record is missing or unverified")
    if not str(verification.get("method", "")).strip():
        fail("Ghostty verification method is missing")
    if not str(verification.get("verifier", "")).strip():
        fail("Ghostty verifier is missing")
    source = provenance.get("source")
    if not isinstance(source, dict):
        fail("Ghostty source snapshot is missing")
    if normalize_repository(source.get("repository")) != expected_repository:
        fail("Ghostty source snapshot does not name the exact {} repository".format(
            "fork" if is_fork else "upstream"
        ))
    for key, validator in (
        ("commit", validate_commit),
        ("tree", validate_commit),
        ("manifest_sha256", validate_sha),
        ("archive_sha256", validate_sha),
        ("status_sha256", validate_sha),
    ):
        validator(source.get(key), "Ghostty source {}".format(key))
    if source.get("commit", "").lower() != built_commit:
        fail("Ghostty source commit differs from the {} pin".format("fork" if is_fork else "upstream"))
    if source.get("archive_sha256", "").lower() != provenance["source_archive_sha256"].lower():
        fail("Ghostty source archive hash differs from the upstream record")
    if source.get("manifest_sha256", "").lower() != provenance["source_manifest_sha256"].lower():
        fail("Ghostty source manifest hash differs from the upstream record")
    if source.get("dirty") is not False or source.get("status_sha256") != empty_status_sha256():
        fail("Ghostty source snapshot does not prove a clean checkout")
    source_manifest = provenance.get("source_manifest")
    if not isinstance(source_manifest, dict) or not str(source_manifest.get("path", "")).strip():
        fail("Ghostty source manifest record is missing")
    if validate_sha(source_manifest.get("sha256"), "Ghostty source manifest SHA-256") != provenance["source_manifest_sha256"].lower():
        fail("Ghostty source manifest hash differs from the upstream record")

    recipe = provenance.get("build_recipe")
    if not isinstance(recipe, dict) or recipe.get("schema_version") != RECIPE_SCHEMA_VERSION:
        fail("Ghostty build recipe record is missing or has an unsupported schema")
    command = recipe.get("command")
    if (
        not isinstance(command, list)
        or not command
        or any(not isinstance(part, str) or not part for part in command)
        or pathlib.Path(command[0]).name != "zig"
    ):
        fail("Ghostty build recipe must contain a structured Zig argument array")
    if recipe.get("clean") is not True:
        fail("Ghostty build recipe does not prove clean output paths")
    if not isinstance(recipe.get("working_directory"), str) or not recipe["working_directory"].strip():
        fail("Ghostty build recipe working directory is missing")
    if recipe.get("outputs") != ["static_library", "xcframework"]:
        fail("Ghostty build recipe output list is incomplete")
    validate_sha(recipe.get("sha256"), "Ghostty build recipe SHA-256")

    toolchain = provenance.get("toolchain")
    if not isinstance(toolchain, dict) or toolchain.get("schema_version") != 1:
        fail("Ghostty toolchain record is missing or has an unsupported schema")
    if not str(toolchain.get("path", "")).strip():
        fail("Ghostty toolchain record path is missing")
    versions = toolchain.get("versions")
    if not isinstance(versions, dict):
        fail("Ghostty toolchain versions are missing")
    for key in ("zig", "xcode", "macos_sdk", "clang"):
        if not isinstance(versions.get(key), str) or not versions[key].strip():
            fail("Ghostty toolchain is missing {} version".format(key))
    validate_sha(toolchain.get("sha256"), "Ghostty toolchain SHA-256")

    outputs = provenance.get("outputs")
    if not isinstance(outputs, dict):
        fail("Ghostty output hashes are missing")
    for key in ("static_library", "xcframework"):
        output = outputs.get(key)
        if not isinstance(output, dict) or not str(output.get("path", "")).strip():
            fail("Ghostty {} output hash is missing".format(key))
        validate_sha(output.get("sha256"), "Ghostty {} output SHA-256".format(key))

    build_record = provenance.get("build_record")
    if not isinstance(build_record, dict):
        fail("Ghostty build_record is missing")
    if build_record.get("success") is not True or build_record.get("exit_code") != 0:
        fail("Ghostty build_record does not prove a successful build")
    if validate_commit(build_record.get("built_commit"), "Ghostty build_record.built_commit") != built_commit:
        fail("Ghostty build record commit differs from the {} pin".format("fork" if is_fork else "upstream"))
    if not str(build_record.get("result_path", "")).strip():
        fail("Ghostty build result path is missing")
    if not str(build_record.get("result_sha256", "")).strip():
        fail("Ghostty build result hash is missing")
    validate_sha(build_record["result_sha256"], "Ghostty build result SHA-256")
    validate_source_snapshot(build_record.get("source_before"), source, "before")
    validate_source_snapshot(build_record.get("source_after"), source, "after")
    if build_record["source_before"] != build_record["source_after"]:
        fail("Ghostty source changed during the recorded build")
    started = parse_timestamp(build_record.get("started_at"), "Ghostty build.started_at")
    finished = parse_timestamp(build_record.get("finished_at"), "Ghostty build.finished_at")
    if finished < started:
        fail("Ghostty build.finished_at precedes build.started_at")
    if build_record.get("recipe_sha256") != recipe["sha256"]:
        fail("Ghostty build recipe hash differs from the recorded recipe")
    if build_record.get("toolchain_sha256") != toolchain["sha256"]:
        fail("Ghostty toolchain hash differs from the recorded toolchain")
    validate_sha(build_record.get("output_sha256"), "Ghostty build_record.output_sha256")
    if build_record["output_sha256"] != outputs["static_library"]["sha256"]:
        fail("Ghostty build record static-library output differs from the output manifest")
    build_outputs = build_record.get("outputs")
    if not isinstance(build_outputs, dict):
        fail("Ghostty build_record output hashes are missing")
    for key in ("static_library", "xcframework"):
        build_output = build_outputs.get(key)
        if not isinstance(build_output, dict) or build_output.get("sha256") != outputs[key]["sha256"]:
            fail("Ghostty build record {} hash differs from the output manifest".format(key))


def verify_outputs(provenance, static_library, xcframework):
    outputs = provenance["outputs"]
    for label, path, key in (
        ("static library", static_library, "static_library"),
        ("XCFramework", xcframework, "xcframework"),
    ):
        actual = sha256_path(path)
        expected = outputs[key]["sha256"].lower()
        if actual != expected:
            fail("{} hash does not match the Ghostty provenance record".format(label))


def validate_for_release_evidence(provenance, project_root):
    """Shared publish-gate validation consumed by release_evidence.py.

    Enforces the current schema (upstream identity, source manifest,
    successful build result, Zig recipe/toolchain records, output hashes)
    and verifies the recorded static-library/XCFramework hashes against
    files resolved from project_root. Raises SystemExit on any mismatch,
    so release_evidence.py no longer carries a separate older policy.
    """
    validate_provenance_shape(provenance)
    outputs = provenance["outputs"]
    # Recorded paths describe the build machine. The release ships the vendored
    # copy, so that is the file the recorded hashes must describe.
    xcframework = pathlib.Path(project_root) / "vendor/ghostty/GhosttyKit.xcframework"
    library = xcframework / "macos-arm64" / pathlib.PurePath(outputs["static_library"]["path"]).name
    for key, label, path in (
        ("static_library", "static library", library),
        ("xcframework", "XCFramework", xcframework),
    ):
        if sha256_path(path) != outputs[key]["sha256"].lower():
            fail("Ghostty {} output does not match the provenance record".format(label))


def verify_source(provenance, source_dir):
    source_path = pathlib.Path(source_dir).resolve()
    if run("git", "rev-parse", "--is-shallow-repository", cwd=source_path).strip() != "false":
        fail("current upstream checkout must be a complete, non-shallow checkout")
    source = current_source_snapshot(source_path)
    recorded = provenance["source"]
    for key in ("repository", "commit", "tree", "manifest_sha256", "archive_sha256", "status_sha256", "dirty"):
        actual = source[key]
        expected = recorded[key]
        if isinstance(actual, str):
            actual = actual.lower()
        if isinstance(expected, str):
            expected = expected.lower()
        if actual != expected:
            fail("current upstream checkout differs from the Ghostty provenance {}".format(key))
    if source["dirty"]:
        fail("current upstream checkout is dirty")
    if provenance["schema_version"] == FORK_SCHEMA_VERSION:
        require_upstream_ancestor(source_path, provenance["upstream_commit"])


def record(args):
    source = pathlib.Path(args.source_dir).resolve()
    static_library = pathlib.Path(args.vendor_artifact).resolve()
    xcframework = pathlib.Path(args.vendor_xcframework).resolve()
    for path, label in (
        (source, "source checkout"),
        (static_library, "static library"),
        (xcframework, "XCFramework"),
    ):
        if not path.exists():
            fail("{} does not exist: {}".format(label, path))
    if not source.is_dir() or not static_library.is_file() or not xcframework.is_dir():
        fail("source checkout, static library, and XCFramework must have the expected types")

    shallow = run("git", "rev-parse", "--is-shallow-repository", cwd=source).strip()
    if shallow != "false":
        fail("Ghostty source checkout must be a complete, non-shallow checkout")
    manifest_path = pathlib.Path(args.source_manifest).resolve()
    recipe_path = pathlib.Path(args.build_recipe).resolve()
    toolchain_path = pathlib.Path(args.toolchain_record).resolve()
    source_snapshot = current_source_snapshot(source, manifest_path)
    if source_snapshot["dirty"]:
        fail("Ghostty source checkout is dirty; rebuild from a clean upstream commit")
    if source_snapshot["status_sha256"] != empty_status_sha256():
        fail("Ghostty source checkout did not record an empty clean status")

    is_fork = source_snapshot["repository"] == FORK_REPOSITORY
    upstream_commit = source_snapshot["commit"]
    if is_fork:
        if not args.upstream_base_commit:
            fail("a fork build must name its --upstream-base-commit")
        upstream_commit = validate_commit(args.upstream_base_commit, "--upstream-base-commit").lower()
        require_upstream_ancestor(source, upstream_commit)
    elif args.upstream_base_commit:
        fail("--upstream-base-commit only applies to a fork build")

    recipe = validate_recipe(recipe_path)
    toolchain = validate_toolchain(toolchain_path)
    recipe["__path"] = str(recipe_path)
    toolchain["__path"] = str(toolchain_path)
    build_record = validate_build_result(
        pathlib.Path(args.build_result).resolve(),
        source_snapshot,
        recipe,
        toolchain,
        static_library,
        xcframework,
    )

    static_hash = sha256_path(static_library)
    xcframework_hash = sha256_path(xcframework)
    record = {
        "schema_version": FORK_SCHEMA_VERSION if is_fork else SCHEMA_VERSION,
        "verified": True,
        "upstream_repository": UPSTREAM_REPOSITORY,
        "upstream_commit": upstream_commit,
        "source_archive_sha256": source_snapshot["archive_sha256"],
        "source_manifest_sha256": source_snapshot["manifest_sha256"],
        "source": dict(source_snapshot),
        "source_manifest": {
            "path": path_for_record(manifest_path),
            "sha256": source_snapshot["manifest_sha256"],
        },
        "build_recipe": {
            "schema_version": RECIPE_SCHEMA_VERSION,
            "path": path_for_record(recipe_path),
            "sha256": sha256_file(recipe_path),
            "command": recipe["command"],
            "working_directory": recipe["working_directory"],
            "clean": True,
            "outputs": ["static_library", "xcframework"],
        },
        "toolchain": {
            "schema_version": 1,
            "path": path_for_record(toolchain_path),
            "sha256": sha256_file(toolchain_path),
            "versions": {key: toolchain[key] for key in ("zig", "xcode", "macos_sdk", "clang")},
        },
        "outputs": {
            "static_library": {
                "path": path_for_record(static_library),
                "sha256": static_hash,
            },
            "xcframework": {
                "path": path_for_record(xcframework),
                "sha256": xcframework_hash,
            },
        },
        "build_record": {
            **build_record,
            "built_commit": source_snapshot["commit"],
            # Kept as a compatibility field for release evidence consumers;
            # it is backed by the independently verified output map above.
            "output_sha256": static_hash,
            "command": recipe["command"],
        },
        "vendor_artifact": {
            "path": path_for_record(static_library),
            "sha256": static_hash,
        },
        "verification": {
            "verified": True,
            "method": (
                "complete clean upstream checkout, matching source manifest, "
                "independent successful build result, structured Zig recipe/toolchain, "
                "fresh output hashes, and unchanged post-build source"
            ),
            "verifier": args.verifier,
        },
    }
    if is_fork:
        record["source_repository"] = FORK_REPOSITORY
        record["fork_commit"] = source_snapshot["commit"]
    destination = pathlib.Path(args.output).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")


def verify(args):
    provenance = read_json(args.provenance, "Ghostty provenance")
    validate_provenance_shape(provenance)
    static_library = pathlib.Path(args.vendor_artifact or provenance["outputs"]["static_library"]["path"]).resolve()
    xcframework = pathlib.Path(args.vendor_xcframework or provenance["outputs"]["xcframework"]["path"]).resolve()
    verify_outputs(provenance, static_library, xcframework)
    if args.source_dir:
        verify_source(provenance, args.source_dir)
    print("Ghostty provenance valid: {}".format(args.provenance))


def main():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    record_parser = subparsers.add_parser("record")
    record_parser.add_argument("--source-dir", required=True)
    record_parser.add_argument("--source-manifest", required=True)
    record_parser.add_argument("--build-recipe", required=True)
    record_parser.add_argument("--toolchain-record", required=True)
    record_parser.add_argument("--build-result", required=True)
    record_parser.add_argument("--vendor-artifact", required=True)
    record_parser.add_argument("--vendor-xcframework", required=True)
    record_parser.add_argument("--verifier", required=True)
    record_parser.add_argument("--upstream-base-commit")
    record_parser.add_argument("--output", required=True)

    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--provenance", required=True)
    verify_parser.add_argument("--vendor-artifact")
    verify_parser.add_argument("--vendor-xcframework")
    verify_parser.add_argument("--source-dir")

    args = parser.parse_args()
    if args.command == "record":
        record(args)
    else:
        verify(args)


if __name__ == "__main__":
    main()
