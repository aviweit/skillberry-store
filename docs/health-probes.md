# Health and readiness probes

Skillberry Store exposes two probes. They run the same checks and always agree
on the assessment — what differs is the **status code**, because the two kinds of
caller want opposite things from one.

| Endpoint | Answers | Status while booting | Point this at |
| --- | --- | --- | --- |
| `GET /health` | "Is this process alive?" | **200** | Render health check path, Kubernetes `livenessProbe`, ALB/ELB target group, `docker HEALTHCHECK`, uptime monitors |
| `GET /health/ready` | "Can it serve content requests yet?" | **503** | Kubernetes `readinessProbe`, load-balancer pool membership, test harnesses that must not assert before the store is warm |

## Availability across access-control modes

Both probes answer in **every** ACL mode, with no credentials. There are exactly
two modes — `disabled` and `standalone` (`VALID_MODES` in
[`access_control/config.py`](../src/skillberry_store/access_control/config.py);
anything else is refused at config load):

| Mode | `GET /health` | `GET /skills/` without a token |
| --- | --- | --- |
| `disabled` | 200 + full payload | 200 (no PEP installed) |
| `standalone` | 200 + full payload | 401 |

`GET /health` and `GET /health/ready` are in `_DEFAULT_UNAUTH_PATHS`, and
`_effective_unauth_paths` makes config entries **add to** those defaults rather
than replace them. So the probes stay reachable even if an operator writes their
own `unauthenticated_paths` that omits them — the defaults are a floor, not a
starting point. In `standalone` mode the enforce dependency still runs and
short-circuits on the allow-list, and the full stage payload is returned (not a
bare 200 from some bypass).

This is asserted, not just intended: `tests/fast_api/test_health_probes.py`
parametrizes the payload assertion over both modes plus an operator-replaced
allow-list, and `tests/access_control/test_unauth_paths.py` checks every shipped
YAML — including `access_control_config.yaml.demo`, the one demo deployments run.

## Why `/health` must never fail during a boot

A platform health check reads a non-2xx as "this instance is broken, replace
it". If the probe fails *because the store is still initialising*, the platform
acts on a normal boot as if it were a crash.

Render's [documented thresholds](https://render.com/docs/health-checks) make the
consequence precise — a `2xx`/`3xx` within five seconds is healthy, `4xx`/`5xx`
is not, and then:

| Consecutive failures | What Render does |
| --- | --- |
| during a new deploy, for 15 min | cancels the deploy, keeps routing to the old instances |
| 15 s on a running instance | stops routing traffic to it |
| **60 s on a running instance** | **restarts the instance** |

That 60-second threshold is the one that bites. Encoder warmup on a shared-CPU
free instance can exceed it, so a `/health/ready`-based health check fails long
enough to trigger a restart — and the restarted instance enters the very same
window. Each restart also throws away the warmup progress, so the loop can
outlast several attempts before one happens to win the race. On the free plan,
where instances spin down when idle and spin back up on demand, that window is
re-entered on every wake, not just at deploy time.

So `/health` reports liveness in the status code and **progress in the body**:

```console
$ curl -s http://localhost:8000/health | jq
{
  "status": "healthy",
  "stage": "initializing",
  "checks": {
    "tool": true,
    "snippet": true,
    "skill": true,
    "vmcp": true,
    "vnfs": true,
    "encoder_warmup": false
  },
  "uptime_seconds": 4.182
}
```

`200` means "do not restart me". `stage` means "but do not expect semantic
search yet". Keeping those two answers in separate fields is the whole design.

### Stages

| `stage` | Meaning |
| --- | --- |
| `initializing` | At least one startup gate is still open. Normal and transient — **not** a failure. |
| `operational` | Every gate is closed. Semantic search and content requests are served in full. |

`checks` names the individual gates so a boot that is taking too long can be
diagnosed rather than just observed. The slow one is almost always
`encoder_warmup`: the ~80 MB ONNX sentence-encoder model being loaded (and, on
an image built without egress to HuggingFace, downloaded). The container image
pre-seeds those weights at build time — see the `encoder_cache_dir` note in the
[Dockerfile](../Dockerfile) — so a deployment running the published image should
reach `operational` quickly.

`uptime_seconds` is measured from a monotonic clock, so it cannot go backwards if
the host clock is stepped.

## Readiness

`GET /health/ready` returns `200` with `"status": "ready"` only once `stage` is
`operational`. Until then it returns **`503 Service Unavailable`** with a
`Retry-After: 5` header and the same assessment under `detail`:

```console
$ curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8000/health/ready
503
```

503 rather than 500: a store that has not finished booting is *temporarily
unable to serve*, which an orchestrator should act on by waiting. 500 says
"server fault", which many act on by restarting — the loop above.

Readiness gates on the description stores and on the encoder warmup. It
deliberately does **not** gate on CLI artifact preparation: a store whose `sbs`
download is not stamped yet is fully functional for everything else, and gating
on it would hold a healthy store out of the load balancer over a download
convenience (see [`new_cli.md` §5.3](design/new_cli.md)).

## Configuring a platform

### Render

Set the service's **Health Check Path** to `/health`.

```yaml
# render.yaml
services:
  - type: web
    name: skillberry-store
    runtime: image
    image:
      url: <your-registry>/skillberry-store:latest
    healthCheckPath: /health          # NOT /health/ready
    envVars:
      - key: SBS_PUBLIC_URL
        value: https://<your-service>.onrender.com
```

With `/health/ready` as the path, Render polls an endpoint that answers `503`
throughout a normal boot and restarts the instance before it can warm up. With
`/health`, the first poll after the port opens succeeds, and the store is left
alone to finish initialising.

Three things worth knowing beyond the probe — each produces the *same*
"restarted several times" symptom, and the Events tab distinguishes them
("deploy canceled" vs "instance restarted" vs an OOM kill):

* **The service does not read `$PORT`.** `SBS_PORT` defaults to `8000`, while
  Render sets `PORT` (default `10000`) and documents only that it is *"usually
  able to detect"* a server bound to a different port — and that **"if Render
  fails to detect a bound port, your web service's deploy fails"**. Set
  `SBS_PORT` to match the service's configured port explicitly rather than
  relying on detection.
* **Memory.** Peak RSS after initialisation plus encoder warmup measures
  **~414 MB on an *empty* store**, against the free plan's **512 MB** cap. A
  store with real content can be OOM-killed, which no probe change can fix.
  Check the instance's memory metrics before concluding the probe is at fault.
* **The port is closed until the app object is built.** Uvicorn binds only after
  `SBS.__init__` has loaded the access-control config, initialised the object
  handlers, discovered plugins, audited RBAC and mounted the Control MCP —
  measured **2.7 s** on a warm developer machine (1.6 s of it plugin discovery),
  longer on shared CPU. No HTTP probe can answer during that window; the
  platform sees a closed socket, not an error. It is far short of any plausible
  port-detection limit, so this is a note for completeness rather than a known
  failure — see "Why the probe is not fixed by binding earlier" below.

### Kubernetes

```yaml
livenessProbe:
  httpGet: { path: /health, port: 8000 }
  # Generous, because this probe's only job is to catch a wedged process.
  initialDelaySeconds: 10
  periodSeconds: 20
  failureThreshold: 3
readinessProbe:
  httpGet: { path: /health/ready, port: 8000 }
  initialDelaySeconds: 5
  periodSeconds: 5
```

### Docker

```dockerfile
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
  CMD curl -fsS http://localhost:8000/health || exit 1
```

## Why the probe is not fixed by binding earlier

A tempting alternative is to have uvicorn listen immediately in a limited form —
serving only `/health` — and register the real routes once initialisation
finishes. Measured against what actually fails, it does not pay:

* **The window is 2.7 s**, not minutes (0.4 s imports + 2.3 s `SBS.__init__`, of
  which **1.6 s is plugin discovery** eagerly constructing one LLM client per
  plugin). Render's restart trigger is 60 s of consecutive health-check
  *failures*, and its deploy allowance is 15 minutes. Nothing is lost in 2.7 s of
  closed socket; what mattered was the 60 s *after* it opens, which is what
  `/health` answering 200 immediately already fixes.
* **Deferring route registration would weaken RBAC.** `audit_rbac_coverage`
  refuses to boot when any non-allow-listed route lacks a `@requires` marker —
  [access-control.md](design/access-control.md) r13 calls this "a *loud*
  deploy-time failure rather than a silent fall-through to some default". Routes
  registered after the socket opens would move that audit *behind* a server that
  is already accepting traffic, trading a boot failure for a live unguarded
  route. The per-subject `FastApiMCP` mounts have the same shape of problem: each
  consumes the generated OpenAPI schema and needs every route already present.
* **The lower-risk shape, if it ever is needed**, is an outer "boot gate" ASGI
  app that serves `/health` and 503s everything else, delegating to the real app
  once it is built — which leaves `SBS.__init__` and the audit untouched, at the
  cost of driving the real app's lifespan by hand, a second copy of the health
  payload, and building the app on a thread whose imports stall the event loop
  under the GIL.

If the pre-bind window ever does become the constraint, the cheap fix is the
1.6 s of plugin discovery (make the per-plugin LLM client lazy), not the startup
ordering.

## Consuming the stage from a client

A client that needs semantic search polls `/health` and waits for
`stage == "operational"` rather than keying off the status code:

```js
const res = await fetch('/health');
const { stage } = await res.json();
if (stage === 'operational') enableSemanticSearch();
```

From the CLI:

```bash
sbs health          # the stage-aware liveness payload
sbs health-ready    # strict readiness; non-zero exit until operational
```

## Payload compatibility

The fields are additive. `/health` still carries `"status": "healthy"`, and a
ready `/health/ready` still carries `"status": "ready"` alongside its `checks` —
so consumers that only read the status code or that one field are unaffected.

The one behavioural change is the readiness **status code while initialising**:
it was `500`, it is now `503`. Anything asserting specifically on 500 (rather
than on "not 200") needs updating.
