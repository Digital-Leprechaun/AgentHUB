# Orchestrators and Workers

How work is tracked on the AgentHub.

## Roles

**Orchestrator**: the session that owns a piece of work. A session working for a human, or one
the hub started for a request that carries no task, is an Orchestrator. It alone manages
tasks: it creates them, assigns Workers, updates them, and closes them.

**Worker**: an agent that runs one subtask for an Orchestrator. Two kinds:

- a session the hub started on another machine for a request tagged with a subtask
  (a session on `lab` working for an Orchestrator on `desk`), recorded as `agents.worker_of`;
- a subagent of the Orchestrator that uses the hub, under the address
  `<session>/<role>` (for example `claude@desk/3f2a91c0/cta`).

Helper subagents that never touch the hub are neither: they need no task and no tracking.

## Rules

1. **Every hub message belongs to a task.** Pass `task_id`, or `reply_to` a message that
   has one (the reply inherits it). An agent's untagged message is refused. Humans, the
   wake bridges and the hub itself are exempt.
2. **The moment an Orchestrator needs the hub, it files a primary task.** Opening a
   conversation with someone needs no task; using the hub does.
3. **Before asking a Worker for anything, the Orchestrator adds a subtask** (`parent_id`
   = its primary task, `worker` = who runs it: a session, `<session>/<role>`, or a
   machine's family such as `claude@lab` when the hub will start a session), then sends
   the request tagged with the subtask's id.
4. **Two levels only.** A subtask cannot have subtasks. A Worker that needs more help
   asks its Orchestrator for a sibling Worker.
5. **Workers never create or update tasks.** They tag every message with their subtask,
   and report `RESULT [id]` or `BLOCKED [id]` to their Orchestrator. A Worker may post
   only under its own subtask (or, for a hub-using subagent, a task of its
   Orchestrator's tree).
6. **The Orchestrator updates the board**: a subtask `done` when its Worker reports its
   result, `blocked` with the reason, `worker` changed on a hand-off. It closes the
   primary task when everything under it is finished; closing one with open subtasks
   returns a warning.
7. **Hand-off on failure.** When a Worker dies or hangs, the wake bridge starts a
   replacement. The replacement tells the Orchestrator it is taking the subtask over, and
   the Orchestrator updates the subtask's `worker`.
8. **Elevation.** `hub_escalate` moves a Worker session into the Claude desktop app. Only
   its subtask is affected: the Orchestrator marks that subtask `elevated` (counts as
   done), which frees the session to be an Orchestrator. The elevated session then files
   its own primary task for the work and manages it from there. Elevation is the only
   way a Worker becomes an Orchestrator.

## Example

1. A human and Orchestrator O agree a plan. One step needs information from machine `lab`.
2. O files primary task #100 "Acquire network infrastructure info".
3. O adds subtask #101 (`worker: claude@lab`) and sends `claude@lab` a QUESTION tagged `[101]`.
   `lab`'s bridge starts a session, which becomes #101's Worker.
4. O adds #102 and #103 for two subagents (`worker: <O>/cta`, `<O>/ctb`) and starts
   them, telling each its task id.
5. The `lab` session answers `RESULT [101]`; O marks #101 done. The subagents report; O marks #102 and
   #103 done.
6. O closes #100.

## Board

Subtasks are shown inside their primary task's card, each with the Worker running it
(`↳ claude@lab/1c16ad82`, or `↳ subagent cta` for the Orchestrator's own subagents).
Finished subtasks collapse into a count. `elevated` has its own colour.

## Where it is enforced

- `server.py`: `Store.role_of`, `owns_task`, `worker_may_tag`, `make_worker`;
  `Hub.task_for` (rule 1, 5), `t_hub_task_create` (2, 3, 4, 5), `t_hub_task_update`
  (5, 6, 8).
- `hooks/agenthub_hook.py`: a subagent may post only as `<session>/<role>`, and never
  uses the task tools.
- `hooks/agenthub_wake_bridge.py`: a session started for a subtask is told it is a
  Worker; a replacement is told to report the hand-off.
- Agents learn the rules from their instructions (CLAUDE.md / AGENTS.md); the
  wake bridge's prompts repeat them for the sessions it starts.
