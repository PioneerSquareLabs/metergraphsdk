# Python test organization

The Python suite is organized by the contract each test protects. Existing
mixed-purpose tests remain at this level and move only when they are being
materially changed. A wholesale reorganization is not required.

## Test categories

- `unit/` contains deterministic tests for isolated SDK behavior. Create this
  directory when the first cohesive group moves; do not move unrelated tests
  merely to populate it.
- `contracts/` contains reusable application-behavior contracts, including
  wrapped-versus-unwrapped parity, stream lifecycle, and fail-open behavior.
  Create subdirectories as those contracts are introduced.
- `integrations/providers/` exercises real provider SDK clients with mocked or
  recorded transports. It must not require live credentials or billable calls.
- `integrations/instrumentation/` exercises MeterGraph with another telemetry
  or framework integration. Prefer in-memory exporters and deterministic local
  fixtures. A few modules here need a library that is not a dev dependency and
  `importorskip` themselves out of the default run; each has a CI job that
  installs it (`python-ddtrace-anthropic`, `upstream-dialects`).
  `test_upstream_dialects.py` is the one deliberate exception to the
  "deterministic local fixtures" rule: it drives the real upstream telemetry
  libraries *unpinned* to catch attribute drift, which is why it runs on a
  weekly schedule rather than as a required check. The unpinned dependency is
  the whole exception -- the network rule below still holds, and that module
  enforces it by replacing the library's transport and failing on any outbound
  connection.
- `package/` verifies the built wheel and source distribution, their public
  shape, and release workflow gates.
- `fixtures/` contains shared provider data and protocol doubles. Test-only
  helpers must remain outside production packages.

## Test rules

- Unit and contract tests must be deterministic and must not use live network
  services, real credentials, or sleeps for synchronization.
- Provider integration tests should assert application-visible behavior as
  well as the captured MeterGraph record.
- Every production bug keeps one minimal regression test. Exploratory cases
  that add no distinct contract coverage should not become permanent tests.
- Tests must leave MeterGraph runtime state, patched clients, threads, and
  network resources clean for the next test.
- The full suite must remain discoverable with `pytest python/tests -q`.
