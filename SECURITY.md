# Security Policy

## Reporting a vulnerability

Please **do not open a public issue** for security problems. Report them privately through
[GitHub Security Advisories](https://github.com/OmniCouncil/OmniCouncil/security/advisories/new).
Include the version (or commit), what you observed, and steps to reproduce. We aim to acknowledge reports
within a few days.

## Threat model

OmniCouncil is a local desktop app with no backend. It launches the official AI CLIs installed on your Mac
and talks to them over pipes. The main risks and how they are handled:

| Risk | Mitigation |
|---|---|
| **Shell injection** through prompts or file names | Every CLI is started with an argument list (`argv`), never through a shell. Covered by tests with shell metacharacters. |
| **Prompt injection** — a model answer or an attachment tries to steer the Judge | Answers and attachment text reach the Judge as length-capped data inside `<answer>` tags; look-alike tags are defused; the prompt declares the content untrusted. Answers are anonymized and shuffled. This reduces, but cannot eliminate, the risk — always read important answers critically. |
| **Unintended API billing** | API-key environment variables are removed per CLI (`env_unset`) so calls use subscription sign-ins. |
| **Models changing local files** | Claude-based workers and Leaders get no file or memory tools; with web search on they get only the read-only `WebSearch`/`WebFetch` tools, pre-approved. Attachment pre-processing enables only `Read`, limited to the attachment folder. Codex runs with `--sandbox read-only`; agy with `--sandbox`, and its permission requests are denied in non-interactive mode (never auto-approved). |
| **Credential exposure** | OmniCouncil never reads credential files (for example `~/.codex/auth.json`); account status comes from each CLI's own status command. Login and account switching run in your terminal through the CLI's own flow. |
| **Local data** | History (`history.sqlite`), attachments, voice recordings and the usage cache are stored unencrypted under `data/` (or `~/Library/Application Support/OmniCouncil/data`), protected by your macOS account. Delete that folder to remove them. |
| **Data leaving the machine** | Prompts, conversation context and attachment contents are sent to the providers you select, under their terms. OmniCouncil itself sends nothing anywhere else. |
| **Orphaned processes** | Every child process is tracked and terminated on exit; persistent processes are killed on timeout or cancellation. |

## Supported versions

Security fixes are made on the latest release and `main`.
