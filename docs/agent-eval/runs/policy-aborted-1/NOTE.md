# Aborted attempt 1 of the policy arm

The first policy-arm run (commit 0cb5a23, policy in AGENTS.md and CLAUDE.md) was
stopped when the operator's Claude Code session ended, during scenario 5 of 10.
Scenarios 1-4 (10 sessions) completed and are recorded in `results.json` and `raw/`;
`run.log` is the console output. The scratch database it created
(`mnemo_agent_eval_5c516d66`) and its temporary fixture directory were removed
afterwards. This attempt is kept as evidence and is not the arm's result; the arm
was rerun in full as `runs/policy`.
