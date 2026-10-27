# Source lifecycle propagation

`open_builtin_runtime()` exposes `runtime.lifecycle.for_scope(scope_id)`.
Use a single writer for each scope. Register captured Source references before
registering derived Memory citations and Topic Memory references. Parent nodes
must already exist. Dependencies are fixed after registration.

```python
from powercontext.builtin.runtime.source_lifecycle import SourceLifecycleEvent

lifecycle = runtime.lifecycle.for_scope(scope_id)
await lifecycle.register_source("source", source_ref, revision=3)
await lifecycle.register_memory("fact", citation, parents=("source",), policy="all")
await lifecycle.register_topic("topic", topic_ref, parents=("fact",), policy="all")
active = await lifecycle.apply(SourceLifecycleEvent(
    node_id="source", revision=3, sequence=1, available=False, reason="withdrawn"
))
original = await lifecycle.resolve("fact", purpose="audit")
```

`all` requires every parent; `any` requires at least one independently sufficient
parent. Registration immediately applies the current dependency state. Repeated
identical bindings are idempotent; changing a binding is rejected. Events apply
only to Sources, require an exact lifecycle revision, and use increasing
per-Source sequence numbers. Only exact replays of the latest event are accepted.

An unavailable Memory entry is deactivated through the native Memory service.
Reactivation restores its eligibility without creating a new body version.
Unsupported Topics are excluded from native Topic search and browse, including
Context assembly. Exact Artifact revision reads remain available. `generation`
resolution rejects unavailable nodes; `audit` resolution returns the original
reference regardless of availability. Different scopes are independent.

The database stores bindings, latest events, and desired states in
`pc_source_lifecycle_nodes`, created by normal built-in schema initialization.
Accepted events and desired states commit before native Memory transitions.
If a transition fails, retry the same event, including after reopening the
Runtime. Reconciliation compares the native manifest, finishes outstanding work,
and skips transitions already committed. Successful replays add no Memory
revision. No graph state depends on module globals or process-local caches.

Run the public tests with `make -C competition test-lifecycle`. Additional
regressions in `tests/builtin/runtime/test_source_lifecycle.py` cover validation,
registration identity, scope isolation, and failures both before a native write
and after its commit but before acknowledgement.
