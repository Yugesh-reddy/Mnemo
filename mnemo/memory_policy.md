## Shared memory (Mnemo)

You share one persistent memory with the other coding agents on this machine through
the `mnemo` MCP tools.

- **Check it first.** Before answering or acting on anything about this project's
  conventions, commands, decisions or ongoing work, or the user's preferences, search
  memory with `memory_search`. Another agent may already know.
- **Save what lasts.** When the user tells you something worth keeping for later
  sessions (a convention, command, decision, handoff note, correction or preference),
  save it with `memory_create`, or `memory_update` if a memory on the same subject
  exists. Only say it is saved after the call succeeds.
- **Keep it true.** When the user corrects or withdraws a memory, update it so it
  states the current truth (for example "no on-call handoff day").
- **Scope.** Memories belong to the current project by default. Use `scope="global"`
  only for things the user says apply to all of their projects.
- Don't save small talk, one-off questions or secrets.
