# Dependency content locks

The managed installer supports **CPython 3.12 on Ubuntu 24.04 / Linux x86_64**
(including the supported WSL2 backend). `requirements-core.lock` pins every core
runtime dependency and its selected official PyPI wheel SHA-256. It is not a
cross-platform lock: an unsupported interpreter/architecture or an unavailable
wheel must fail, not fall back to an unverified source build.

The installer passes `--require-hashes --only-binary=:all:` explicitly. The lock
also declares these options, so documented direct installations retain the same
checks. `scripts/check-core-deps.py` checks installed **versions**; it does not
claim to re-verify every installed file. A matching download hash proves content
consistency, not publisher identity, absence of vulnerabilities, or reproducible
builds.

## Updating a lock

1. Use a separate environment on the supported platform. Keep the running
   instance and its venv untouched. Use the explicitly selected network route;
   a failed proxy/download must not silently switch routes.
2. Resolve the intended update and its complete dependency closure. Download
   wheels from official PyPI, record the package/version/filename/source and
   retrieval time, and compare each local SHA-256 with that filename's PyPI
   metadata. Do not add a hash for an unreviewed file just to suppress an error.
3. Pin every direct and transitive requirement. Core entries use one
   `name==version --hash=sha256:…` line per package. Keep overlapping pins in the
   optional locks consistent; a newly required transitive dependency needs its
   own exact version and hash.
4. In a fresh disposable venv run:

   ```bash
   python -m pip install --require-hashes --only-binary=:all: -r requirements-core.lock
   python scripts/check-core-deps.py
   python -m pip check
   ```

   Also test a deliberately wrong hash against a copied lock in that disposable
   environment: installation must fail without installing the wrong content.
5. Validate each optional lock independently. The historical Web Push runtime
   includes `http-ece`, which has only a source distribution: first install
   `requirements-webpush-build.lock`, then install `requirements-webpush.lock`
   with `--no-build-isolation --require-hashes`. This exception does not apply to
   the core installer and does not enable Web Push in a managed instance.
6. Run the five dependency audits in [CI](CI.md), the full Python suite and the
   affected offline UI checks. Fix resolver errors as errors, not as a successful
   scan. Review applicable advisories; do not add blanket ignore lists or skip
   transitive dependencies to make an audit green.
7. Publish only the reviewed commit and verified artifacts after the actual
   public CI run succeeds. Preserve prior release files and record checks that
   were not performed. A local successful run is not evidence of a GitHub run.

## CI tooling

`ci/package-lock.json` fixes the Node-side Playwright dependency tree and npm
integrity values. Use `npm ci --prefix ci --ignore-scripts`, not an unbounded
`npm install`, for checks. Browser binaries and runner OS packages are installed
separately; neither this file nor the workflow claims a completely offline or
bit-for-bit reproducible bootstrap. Third-party Actions are pinned to full
commit IDs. The audit tool itself is version-pinned; advisory data changes over
time, so record the scan date and results.
