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
| Dev Mesh Observer | `dev-mesh.observer.status@20260812.1` | `infra.local.unix-socket` |

Each provider owns its request, response, framing, errors, limits, privacy contract, and optional
Console link. Sentinel normalizes only bounded status, headline metrics, issues, observation time,
and a loopback Console URL into its private UI projection. Provider extensions never cross that
boundary.

The Dev Mesh adapter accepts only its exact Unix-socket offer and retains only bounded aggregate
metrics and issues plus a literal-loopback Console URL. Its privacy declaration must exclude
coordination owner IDs, workspace paths, Git revisions, branch names, event payloads, raw errors,
database paths, and claim scopes. Sentinel never reads the Observer database, collaboration graph,
or workspace event stream directly.

For `dev-mesh.observer.status@20260812.1`, Sentinel sends the exact snapshot request in a single LF
frame, accepts at most 256 KiB within two seconds, and requires EOF after the single response frame.
Only the contract's three headline aggregates, known aggregate metric and issue IDs, and the fixed
issue subject `observer` enter the private projection. Unknown provider fields and extension members
are ignored. Historical integrity totals remain metrics; Sentinel trusts the producer's current-cycle
status instead of deriving a permanent failure from cumulative history. Ordinary Agents, claims,
handoffs, conflicts, transactions, and branches are not facility failures.

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
