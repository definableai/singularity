# 03 — Auth (bring your own)

Status: building — shipped: Principal/AuthProvider protocol, Auth() chain dep (first
match wins, pin, dotted BYO paths), jwt provider (HS + JWKS modes, alg pinning before
signature check, iss/aud mandatory in JWKS mode, https-only, TTL cache + capped
unknown-kid refresh + negative cache, leeway), api_key provider (generator, in-process
TTL cache, bounded negative cache) + model + migration, WS token via
Sec-WebSocket-Protocol, AUTH_DEV_PRINCIPAL (dev-only validated), principal_id in
obs envelope + auth journey step. Wiring docs in README. Complete for v1.

No RBAC baked in. Authentication is a pluggable provider chain; projects bring Stytch,
their own SSO/OIDC, or anything else by implementing one protocol.

## Protocol

```python
class Principal(BaseModel):
  id: str
  kind: Literal["user", "api_key", "service"]
  claims: dict[str, Any] = {}

class AuthProvider(Protocol):
  name: str
  async def authenticate(self, request: Request | WebSocket) -> Principal | None:
    """Return Principal, None (not my credential type), or raise AuthError (mine, invalid)."""
```

## Dependency

```python
user: Principal = Depends(Auth())            # provider chain from settings, first match wins
user: Principal = Depends(Auth("api_key"))   # pin one provider
```

- Chain configured in settings: `AUTH_PROVIDERS=["jwt", "api_key"]` — entries are shipped
  names or dotted paths to project-defined classes (`"myapp.auth.StytchProvider"`).
- HTTP: `Authorization` header / `x-api-key`. **WebSocket: token read from the
  `Sec-WebSocket-Protocol` handshake header, pre-accept** — never the query string, which
  lands `?token=eyJ...` in uvicorn's access log where 05's stdlib bridge captures it as
  raw text the structured redactor never touches. The plan documents the browser snippet:
  `new WebSocket(url, ["bearer", token])`. No query fallback, no first-message variant.
- Auth outcome recorded as a journey step (06): provider tried, principal kind, duration —
  never the credential itself.

## Shipped providers

- **jwt** — static secret or JWKS URL. Hardening is spec, not option:
  - **Algorithm pinning**: secret mode accepts HS* only; JWKS mode takes an asymmetric
    allowlist from settings, and a header `alg` outside it is rejected *before* signature
    verification (kills alg=none and HS256-with-public-RSA confusion).
  - **`iss` and `aud` mandatory in JWKS mode** — boot error if unset; unpinned issuer =
    accepting any tenant's tokens. JWKS URL must be https.
  - Clock skew tolerance (`leeway`, constant 30s) on `exp`/`nbf`/`iat`.
  - **JWKS cache reuses 01's degradation machinery** (single-flight, breaker): key set
    cached with TTL (constant 15m); unknown `kid` → refresh capped at one per ~60s
    (bogus-kid floods must not force unbounded refetches); ~10s negative caching so a
    cold cache during an issuer outage is not a per-request fetch stampede. Fetch uses
    `ctx.http` (timeout, retry, traced). Warm cache + fetch failure → serve from cache
    and log; cold cache → auth errors loudly.
- **api_key** — `x-api-key` header → sha256 lookup against the included `api_key` model.
  - Unsalted sha256 is sound **only for generated high-entropy keys** — so the generator
    ships (`secrets.token_urlsafe(32)`, plaintext shown once, hash + prefix stored) and
    user-chosen key material is rejected.
  - Lookup is an **in-process TTL cache** (`API_KEY_CACHE_TTL`, default 60s) with
    size-bounded LRU negative caching (invalid-key floods must not hammer PG) — never a
    per-request Redis GET. Provider-internal, like the JWKS cache; `ctx.cache` stays the
    only app-facing cache. Stated caveat: a revoked key stays valid up to TTL per
    replica — the short default is the price of the hot path.
- **stytch** (example, `providers/stytch.py`) — working adapter demonstrating BYO: verifies
  Stytch session JWT, maps to Principal. Copy this file to integrate any vendor.

## Dev principal

`AUTH_DEV_PRINCIPAL` (owned here): requests without credentials get a fixed principal so
curl/docs work before real auth is wired. Requires `environment == dev` exactly — set in
any other environment, boot error (01's dev-affordance rule).

## Out of scope (per-project)

RBAC, orgs/multi-tenancy, user CRUD/signup flows, billing guards. The `Principal.claims`
dict is the extension point for projects to hang roles/permissions on.

## Settings (owned by this plan)

`AUTH_PROVIDERS`, `JWT_SECRET`, `JWT_JWKS_URL`, `JWT_ALGORITHMS`, `JWT_ISSUER`,
`JWT_AUDIENCE`, `API_KEY_CACHE_TTL`, `AUTH_DEV_PRINCIPAL`.
