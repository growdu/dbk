# Architecture Review

## Scope

Companion execution plan:

- [ARCHITECTURE_DEVELOPMENT_PLAN.md](ARCHITECTURE_DEVELOPMENT_PLAN.md)

This review compares the current architecture documents with the actual
execution paths in the codebase, focusing on:

- agent execution flow
- workflow orchestration
- session and memory lifecycle
- plugin extensibility
- sub-agent integration
- API service runtime model

The goal is not to restate the design, but to identify where the current
implementation and the intended architecture have diverged in ways that create
maintenance or correctness risk.

## Current Assessment

The largest issue is not a single broken module. The larger problem is that the
system currently has multiple overlapping control planes:

- `Agent` handles the main message-processing path
- `WorkflowOrchestrator` adds stage context on top of `Agent`
- `MainAgent` adds sub-agent delegation on top of `Agent`

Because these are not collapsed into one authoritative execution path, several
capabilities exist in code and docs but are not consistently active for all
entry points.

## Findings

### 1. Session persistence is not part of the hot path

Severity: High

`Agent.get_session()` can load from the persistent store, but
`Agent.process_message()` and `Agent.process_stream()` only check the in-memory
`SessionManager`. If the session is not already loaded in memory, the code
creates a fresh session instead of restoring the persisted one.

Impact:

- cross-process resume is unreliable
- workflow stage may silently reset
- prior conversation context may disappear
- API and CLI behavior can drift depending on process lifetime

Required correction:

- resolve session through a single loader function
- load from store on memory miss
- repopulate in-memory cache after restore

### 2. Workflow is advisory, not authoritative

Severity: High

The design treats the workflow orchestrator as the kernel of the system, but
the implementation mostly injects stage-specific prompt text into a normal
message flow. Tool access, stage transitions, and completion rules are not
centrally enforced by the orchestrator.

There is also a design drift issue: the design doc includes a `research` stage,
while the actual `WorkflowStage` enum does not.

Impact:

- stage behavior depends on prompt quality instead of program logic
- workflow cannot reliably gate or constrain tool usage
- design docs overstate what the orchestrator currently guarantees

Required correction:

- decide whether `research` is a real stage or remove it from docs
- move stage rules into executable policy
- make the orchestrator own transition validation and stage outputs

### 3. Plugin contract is broader than the real integration path

Severity: Medium-High

The plugin system exposes hooks for:

- tool registration
- system prompt modification
- post-message callbacks
- API routes
- cleanup

In practice, only tool registration and API route registration are reliably
connected to the main runtime path. System prompt hooks and post-message hooks
exist in the registry, but are not consistently invoked by the core agent
message-processing flow.

Impact:

- plugin authors can build against hooks that appear supported but do not affect
  the runtime
- behavior differs between documented capability and effective capability

Required correction:

- define one canonical plugin lifecycle
- execute all supported hooks from the main path
- remove or demote hooks that are not part of the stable contract

### 4. Sub-agent architecture is not the default system architecture

Severity: Medium

The design document presents a `main agent -> subagent -> merge` model, but the
main API and CLI entry points still instantiate `Agent` rather than
`MainAgent`.

Impact:

- documented delegation architecture is not active by default
- there are effectively two agent systems to maintain
- behavior differs across entry points

Required correction:

- choose `MainAgent` or `Agent` as the single production entry type
- if `MainAgent` wins, make API, CLI, and REPL all instantiate it
- if delegation remains optional, make that a runtime mode, not a separate
  architecture

### 5. Memory exists mostly as a side channel

Severity: Medium

Memory storage and retrieval APIs exist, and memory is archived after some
interactions, but memory context is not consistently injected into the actual
LLM prompt in the core processing path.

Impact:

- memory improves observability and inspection, but not decision quality
- long-lived agent behavior does not materially benefit from stored memory

Required correction:

- fetch memory context during request assembly
- inject it into the system or pre-user context
- unify memory writeback after every completed turn

### 6. API runtime model is not safe enough for concurrent session mutation

Severity: Medium

The API server keeps a shared agent instance and in-memory session map, while
the in-memory session manager itself is not synchronized.

Impact:

- concurrent requests for the same session can overwrite each other
- last-writer-wins updates may silently drop turns or workflow transitions

Required correction:

- add per-session locking or serialized mutation
- define session consistency expectations clearly
- avoid relying on an unsynchronized in-memory cache as the primary authority

## Target Architecture

The system should converge to one authoritative request path:

`request -> resolve session -> load memory context -> workflow policy -> tool or sub-agent execution -> plugin post-hooks -> persist session and memory`

The important part is not the exact class layout. The important part is that
every entry point goes through the same lifecycle.

## Recommended Execution Model

### 1. Single agent type

Use one production agent type for all user-facing entry points.

Recommended direction:

- make `MainAgent` the canonical runtime agent
- keep direct execution as an internal mode of `MainAgent`
- do not instantiate plain `Agent` from API, REPL, or `dbk run`

### 2. One session resolver

Add a single internal method that:

- checks in-memory session state
- falls back to persistent storage
- rehydrates in-memory cache after load
- creates a new session only if no persisted session exists

### 3. Workflow as policy, not text

The workflow layer should own:

- current stage resolution
- valid transitions
- stage-specific tool eligibility
- stage output recording
- auto-transition rules

Prompt shaping can remain part of workflow, but it should be the last layer, not
the primary enforcement mechanism.

### 4. Memory in the prompt assembly pipeline

Prompt building should become an explicit pipeline:

- base system prompt
- plugin system prompt extensions
- workflow stage prompt
- memory context
- conversation history
- current user message

### 5. Stable plugin lifecycle

Supported hooks should be executed in this order:

1. tool registration during startup
2. system prompt extension during prompt assembly
3. post-message hook after a successful or failed turn result
4. cleanup hook during shutdown

If a hook is not run in production, it should not be documented as a stable
capability.

## Remediation Plan

### Iteration A: Converge the execution path

Priority: P0

Tasks:

1. make one runtime agent type authoritative
2. route API, REPL, and CLI through that type
3. fix session restore on message-processing entry
4. wire plugin system prompt and post-message hooks into the main flow
5. align docs with the actually shipped workflow stages

Expected outcome:

- one real architecture instead of multiple overlapping ones
- session continuity becomes reliable
- plugin behavior becomes predictable

### Iteration B: Move control into workflow policy

Priority: P1

Tasks:

1. promote workflow from prompt decoration to execution policy
2. formalize stage-to-tool routing and enforcement
3. record stage outputs and completion metadata
4. decide and implement the final stage model, including `research` if retained

Expected outcome:

- workflow becomes enforceable and testable
- docs can describe runtime guarantees instead of intent

### Iteration C: Close the memory loop

Priority: P1

Tasks:

1. inject memory context into prompt assembly
2. unify archival and pruning behavior across API and REPL
3. define what belongs in short-term state vs. long-term memory

Expected outcome:

- stored memory improves live reasoning rather than only post hoc inspection

### Iteration D: Hardening

Priority: P2

Tasks:

1. add concurrency control around session mutation
2. document consistency guarantees for shared sessions
3. add end-to-end tests around resume, stage transition, plugin hooks, and
   delegated execution

Expected outcome:

- architecture becomes safe enough for long-running API service usage

## Immediate Task List

These are the first changes worth doing in code:

1. add an internal `resolve_session()` path and use it in `process_message()`
   and `process_stream()`
2. instantiate `MainAgent` in API, REPL, and `dbk run`
3. switch prompt construction to use plugin system prompt assembly
4. invoke post-message hooks after result construction
5. reconcile workflow docs with the real stage enum

## Non-Goals

This review does not recommend:

- introducing a vector database yet
- expanding plugin surface area before the current hooks are stabilized
- adding more workflow stages before the current control model is coherent

## Summary

The project does not need a brand new architecture. It needs architectural
convergence.

The fastest path to a stable system is:

- choose one runtime agent path
- make session and memory part of that path
- make workflow executable policy
- keep the plugin contract honest

Once that is done, the current design becomes much easier to evolve without
creating parallel systems.
