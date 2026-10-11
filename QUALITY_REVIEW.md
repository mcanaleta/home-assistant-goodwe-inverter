# Discovery and recovery quality review

Reviewed on 2026-10-11. Implementation validated at `480f134` against Home
Assistant 2026.10.0, Python 3.14.8, and goodwe 0.4.10 on Linux.

The discovery/recovery change is a credible contribution candidate. It has
bounded network work, stable serial identity, UI confirmation, reconfiguration,
and regression tests against real Home Assistant flow and registry objects.
The complete experimental integration still needs work before claiming an
Integration Quality Scale tier. As a third-party integration its designation
is **Custom**, rather than an awarded Bronze/Silver/Gold/Platinum tier.
[Home Assistant quality scale](https://developers.home-assistant.io/docs/core/integration-quality-scale/).

## Architecture

| Module | Responsibility |
| --- | --- |
| `discovery.py` | Local UDP discovery, bounded candidate verification, address recovery |
| `helpers.py` | Connection identification, serial validation, endpoint merging, cancellation preservation |
| `config_flow.py` | Home Assistant user, DHCP, confirmation, options and reconfiguration flows |
| `coordinator.py` | Polling failures, recovery threshold, connection replacement and reload |
| `__init__.py` | Setup, migration, device identity, entry update/unload lifecycle |

Runtime setup no longer imports the config flow. Shared entry-update rules live
in a common module. Discovery treats a logger reply as a hint: only an inverter
API response with the configured serial and readable telemetry can recover an
existing entry. MAC is a secondary network connection; serial remains the entry
and device identity. This follows Home Assistant's
[common-module guidance](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/common-modules/).

## Corrections made during review

- Probe candidates concurrently, with four simultaneous probes, 16 candidates,
  and a 20-second overall limit. A silent candidate cannot delay a responsive
  matching inverter behind sequential timeouts. Remaining tasks are cancelled
  and awaited, and broadcast transports close on failure or cancellation.
- Restore task cancellation when goodwe 0.4.10 converts cancellation into an API
  error. Explicit family probes prevent its internal discovery fallback from
  continuing after cancellation. Identification has an eight-second limit;
  recovery verification has a four-second per-candidate limit.
- Preserve all unrelated entry data and options; refresh overridden host,
  port and protocol together. Concurrent user/DHCP changes prevent stale
  recovery results from overwriting newer configuration.
- Refresh the coordinator connection and failure counter after recovery.
  Reload all platforms even when the verified endpoint is unchanged; an actual
  endpoint change reloads once through the existing update listener.
- Record logger MAC for both UDP and DHCP discovery. Respect ignored entries.
- Add Home Assistant's reconfiguration flow and verify the existing serial.
  Validate endpoint edits in options while allowing polling-only edits offline.
- Preserve extra fields during migration, avoid adding null settings, and
  reject newer config-entry versions rather than implicitly downgrading them.
- Redact host, MAC and serial identity from downloadable diagnostics.

## Verification

[GitHub Actions evidence](https://github.com/mcanaleta/home-assistant-goodwe-inverter/actions/runs/38167979160)
contains **48 passing tests**, lint, formatting, and coverage. HACS and hassfest
also passed on the same implementation commit. CI checks coverage across the
whole integration and enforces 100% for the four focused modules below.

| Module | Line and branch coverage, rounded by coverage.py |
| --- | ---: |
| Configuration flow | 100% |
| Discovery and recovery | 100% |
| Shared helpers | 100% |
| Diagnostics | 100% |
| Coordinator | 99% |
| Setup/migration/unload | 91% |
| Entire integration, including inherited platforms | 60% |

Tests cover malformed and spoofed-address replies, enabled-interface selection,
UDP broadcast, TCP fallback, wrong serial, unreadable telemetry, cooldown,
timeouts, library-swallowed cancellation, concurrent configuration changes,
startup failures, migration and unload. Real Home Assistant ConfigEntries,
FlowManager and entity registry tests verify creation only after confirmation,
deduplication, serial-preserving reconfiguration, stable entity IDs and one
reload per endpoint update. Physical inverter calls are mocked in automated
checks; these are not hardware compatibility tests for every GoodWe model.

The local macOS/Python 3.14.7/HA 2026.8.3 environment completes the assertions
but the expanded lifecycle suite crashes during interpreter shutdown (exit
139). Its cause has not been established. It is not counted as a clean local
pass; the clean validation above is Linux/Python 3.14.8/HA 2026.10.0 in CI.

A previous read-only live probe verified this site's logger broadcast and
serial/telemetry response. The reviewed override has not been installed in
production, so a full live install/reload/rollback remains unverified.

## Work before an upstream contribution

1. Put UDP logger discovery and cancellation/connection lifecycle fixes into
   the `goodwe` library where feasible, with its own tests and public API.
   Home Assistant should supply enabled interfaces and handle config entries.
   The present discovery module is a contained adapter, not the ideal final
   ownership of vendor protocol logic.
2. Submit discovery and serial-preserving recovery as focused changes to the
   native integration or the experimental upstream. Exclude personal manifest
   metadata and unrelated experimental controls. Port tests to the upstream's
   pytest/Home Assistant fixtures and run its supported checks.
3. Test hardware families and logger variants, reconnect after real DHCP
   changes, startup/reload/unload and TCP keep-alive. The library lacks a public
   close method for the integration's persistent connection; avoid depending
   on its private protocol internals.
4. For a whole-integration quality upgrade, cover inherited sensor/control
   platforms and services, define parallel update policy, complete strict
   typing and the quality checklist. Services are still registered in
   `async_setup_entry`; the baseline guideline requires registration in
   `async_setup`. These inherited gaps are not resolved by discovery tests.
   [Action setup rule](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/action-setup/).

The config flow meets the measurable
[full-coverage rule](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/config-flow-test-coverage/).
The whole integration does **not** meet the
[above-95% coverage rule](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/test-coverage/).
Coverage measures exercised code paths; it does not certify reliability,
security or acceptance by upstream maintainers.
