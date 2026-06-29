# Aborted attempt 1 (infrastructure, no measurement)

Every phase exited within seconds with `Connect call failed ... 5432`: Docker
Desktop, and so Postgres, was not running, and Ollama was down. No replay
completed, no verifier or embedding call was made, and no result exists; the
replay outputs hold only their initial `complete: false` headers. The services
were restarted and the frozen protocol was run again unchanged. These files are
kept rather than deleted.
