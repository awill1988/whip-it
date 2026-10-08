# Synthetic hook examples

From the repository root:

```sh
python examples/run.py --executable target/release/whip-it
```

Use `target/release/whip-it.exe` on Windows. The runner reads `allow.json` and
`deny.json`, uses temporary configuration and state, and checks the responses.
It does not modify installed hooks or user sessions.

- `allow.json`: an unrelated tool produces empty stdout.
- `deny.json`: delegation with quota zero returns a Claude-compatible denial
  with instructions to continue in the main session.

These examples test deterministic policy behavior, not classifier accuracy.
