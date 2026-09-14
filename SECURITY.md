# Security policy

PM Assistant is a portfolio/MVP project and should not be treated as a finished enterprise deployment without an IT/security review.

## Data model
- Project messages, registry state, roles and answer history are stored in local SQLite.
- The application only permits configured local Ollama hosts.
- Model prompts are sent to the local Ollama HTTP endpoint, not to a cloud LLM API by PM Assistant.
- Remote Client does not need a copy of the model weights or project database.

## Remote mode
Recommended configuration:
- keep PM Assistant bound to `127.0.0.1:8765`;
- keep Ollama on `127.0.0.1:11434`;
- expose PM Assistant only through a private Tailscale Serve endpoint;
- do **not** use Tailscale Funnel;
- do **not** port-forward 8765 or 11434 on the router;
- enable MFA on the identity used for the private network;
- use a separate PM Assistant account/password.

## Before production use
A company deployment should additionally define:
- corporate authentication and authorization;
- per-project access control;
- encryption at rest and encrypted backups;
- key management;
- audit logging and retention;
- backup/restore procedures;
- egress/network restrictions;
- an approved policy for real client/NDA data.

## Reporting a vulnerability
Please open a private GitHub security advisory if the repository enables that feature. Do not post credentials, real client data or exploit details in a public issue.
