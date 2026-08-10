# Facility discovery and provider adapters

Infra Sentinel uses `infra.discovery.registration@20260810.1` only to discover local services and
select an exact `(protocol, version, binding)` offer. Discovery does not define a common facility
snapshot. The canonical contract is maintained by the separate Infra Protocol project; Sentinel
vendors an exact schema copy as an auditable conformance fixture while its packaged runtime uses a
dependency-free strict parser for the same fields and bounds.

The packaged app resolves the Infra Protocol runtime root, validates owner-only registration files
and bounded leases, selects a supported offer, validates the owner-only endpoint, and then hands the
connection to the selected provider adapter. Unknown protocols remain valid declarations but are
ignored by Sentinel.

Current adapters are deliberately independent:

| Provider | Application protocol | Local binding |
| --- | --- | --- |
| PCP Runtime | `pcp.runtime.observer@20260810.1` | `infra.local.unix-socket` |
| Infer Runtime | `infer-runtime.status@20260810.1` | `infra.local.unix-socket` |

Each provider owns its request, response, framing, errors, limits, privacy contract, and optional
Console link. Sentinel normalizes only bounded status, headline metrics, issues, observation time,
and a loopback Console URL into its private UI projection. Provider extensions never cross that
boundary.

There is no additional discovery handshake. The complete sequence is:

1. validate a live registration;
2. intersect exact protocol versions and supported bindings;
3. connect to the selected endpoint;
4. let the binding enforce its peer-user rule;
5. exchange one provider-owned application request and response; and
6. reject the result if the registration generation or selected offer changed during the request.

`INFRA_PROTOCOL_RUNTIME_DIR` may set the final shared runtime root in managed deployments. Without
an override, Sentinel follows the platform paths in the canonical Infra Discovery specification.
The old `INFRA_SENTINEL_REGISTRATION_DIR` and `infra-observer.*` wire are intentionally unsupported.
