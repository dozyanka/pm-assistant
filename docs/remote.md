# Remote Server / Client mode

Remote mode keeps the project database and AI inference on one Windows PC while a second device uses the application through a private HTTPS endpoint.

## Server PC
1. Install Ollama and pull the two required models.
2. Install/sign in to Tailscale.
3. Build Remote mode:

```bat
scripts\windows\build_remote.cmd
```

4. In the generated `Server` folder run `setup_remote_server.cmd` once.
5. Run `start_remote_server.cmd` for normal use.

## Client PC
1. Install/sign in to Tailscale using an authorized identity.
2. Copy only the generated `Client` folder to the client PC.
3. Run `configure_remote_client.cmd` once and save the private `https://...ts.net` URL.
4. Start `PM Assistant Remote.exe`.

The Remote Client opens the web application in Microsoft Edge app mode when Edge is available; no Python runtime, model weights or PM Assistant SQLite database are required on the client PC.

## Network rule
Do not expose ports 8765 or 11434 directly to the public internet and do not enable Tailscale Funnel.
