# TLS compatibility regression (v2.4.0b2)

## Diagnosis

All 12 inspected Home Assistant config entries remained loaded, while their sensors were unavailable. The common error was a connection reset during TLS negotiation, rendered by aiohttp as `Cannot connect to host ... ssl:default [None]`. It was not a config migration or station-ID failure.

A controlled live comparison using Python 3.14 / OpenSSL 3.6 reproduced the reset with the shipped SSL context. Keeping the same CA trust and adding AES256-GCM-SHA384 produced HTTP 200 and valid Ministry JSON. The observed server negotiates TLS 1.2 static RSA; Python's default TLS 1.2 suites require forward secrecy. Adding a CA alone cannot resolve that cipher mismatch.

## Scope and tradeoff

Append only AES256-GCM-SHA384 to the existing cipher list in the Ministry client's private SSL context. Preserve certificate verification, hostname checks, minimum TLS version, security level and default modern suites. Do not use ssl=False, lower SECLEVEL, or change global TLS policy. Static RSA does not provide forward secrecy; this narrowly scoped compatibility exception can be removed when the Ministry supports the default suites.

The integration bundles its client and has an empty manifest requirements list, so updating the separate Python package alone cannot fix the installed integration. Apply the corresponding patch in both repositories.

## Verification

- A local RSA-only TLS server reproduces failure with the default context and succeeds with the corrected context.
- Regression checks reject an untrusted certificate and an incorrect hostname, and verify preservation of TLS policy.
- Library: 27 offline tests passed.
- Integration: 45 tests passed against Home Assistant 2026.10.3 / Python 3.14. Two existing coordinator tests now supply a config entry in SETUP_IN_PROGRESS, as required by current HA.
- Live bundled client: all 12 inspected station IDs returned valid gasoline-95 prices via three municipality requests. These checks ran outside the HA host; post-install verification on HA remains required.

## Release and installation plan

1. Review and merge the integration patch, then publish a new prerelease (proposed v2.4.0b3) using the existing release workflow. Ensure its ZIP contains the patched bundled client and matching manifest version. The fix PR itself does not publish a release.
2. In HACS, select the new prerelease for andresgr8887/ha-gas-station-spain and download it. Keep the existing config entries and entity registry.
3. Restart HA only with the user's explicit authorization to load the changed Python module.
4. Verify all 12 entries remain loaded, all 12 sensors have fresh numeric prices, and no new connection-reset warnings occur. Confirm another update cycle before declaring the installed version validated.
5. If needed, redownload v2.4.0b2 through HACS and perform an explicitly authorized restart; note that this restores the known TLS failure. No config deletion is required.

The failed-request cache serialization and the coordinator's abbreviated exception logging are secondary issues, deliberately outside this TLS fix.

