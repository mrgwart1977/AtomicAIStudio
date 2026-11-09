# packages/contracts

Shared typed API contracts for Atomic AI Studio.

The backend is the single source of truth: FastAPI emits an OpenAPI
schema at `/openapi.json`. The desktop app regenerates its client types
from it — no hand-maintained parallel definitions.

```bash
# backend running on :8788 (dev token)
cd apps/desktop
npm run gen:api        # writes src/api/schema.d.ts
```

`apps/desktop/src/api/types.ts` currently hand-mirrors the small M0
surface for readability; the generated `schema.d.ts` becomes the
authoritative client as the API grows.
