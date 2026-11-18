# Mvx engine fork

This is the terminal engine used by Mvx. It is a
fork of [Ghostty](https://github.com/ghostty-org/ghostty) and is not an official
Ghostty project. Ghostty's MIT license and attribution apply and stay in place.

The `mvx` branch holds the engine. `main` follows upstream and is not developed.

| Repository | Responsibility |
|---|---|
| This fork | Zig engine, renderer, PTY, and the embedding API |
| Mvx | Swift interface, native bridge, workspaces, and agent integration |

## Working with an Mvx checkout

Keep both repositories side by side, then run this from the Mvx checkout:

```sh
scripts/ghostty-dev --open
```

It builds this engine and links it into an isolated copy of Mvx. Your installed
app, its profile, and the package pinned by Mvx are left alone.

## Packages

Every push to `mvx` runs the engine tests and publishes a test package named
`engine-test-<commit>`. A tag named `engine-<version>-mvx.<n>` publishes a
permanent package. Mvx adopts a package by updating its
`vendor/ghostty/ghosttykit.lock.json` in a pull request.

`-Di18n=false` is required. Turning it on links GNU libintl (LGPL) into the app.

## Syncing upstream

Merge an upstream release in its own pull request and update
`mvx/upstream-base.json` in the same change. Upstream's workflows are removed on
this branch because they need Ghostty's own runners, so keep them deleted when
resolving a merge. `mvx/ghostty_provenance.py` is a copy
of the verifier in the Mvx repository, so change them together.
