# Client compatibility matrix (P0-9, Days 23–24)

The rule: **one row per real, dated run — no run, no row.** The matrix is the
niche's real contract; competitor trackers are overwhelmingly "client X broke
in way Y", so every claim here must trace back to a run someone performed.

## Method

For each client, run exactly this ladder against a running instance and record:
client name + version, date, which turns passed (`chat` / `stream` / `tool`),
and any quirks observed (field omissions, retries, body mangling):

1. one non-streaming chat turn
2. one streaming chat turn
3. one tool-call turn (if the client supports tools)

## Rows

| Client | Version | Chat | Stream | Tool call | Date | Caveats |
|---|---|---|---|---|---|---|
| Claude Desktop | — | UNVERIFIED | UNVERIFIED | UNVERIFIED | — | setup guide: [CLAUDE_DESKTOP_SETUP.md](../CLAUDE_DESKTOP_SETUP.md) — run the ladder above and replace this row |
| OpenAI Python SDK | — | UNVERIFIED | UNVERIFIED | UNVERIFIED | — | — |
| Anthropic Python SDK | — | UNVERIFIED | UNVERIFIED | UNVERIFIED | — | check usage fields + stop reasons |
| Cline / Claude Code | — | UNVERIFIED | UNVERIFIED | UNVERIFIED | — | one real session incl. a tool call and a file flow |
| LobeChat / Chatbox | — | UNVERIFIED | UNVERIFIED | UNVERIFIED | — | chat + stream |

No row above is a claim; every UNVERIFIED cell awaits its dated run. When you
perform one, replace the whole row (client, version, date, results, caveats)
and delete nothing — history lives in git.
