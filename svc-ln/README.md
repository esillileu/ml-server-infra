# svc-ln

`svc-ln` creates opt-in links from local services to loopback-only ports on
remote targets. Each target uses one OpenSSH connection containing all of its
configured `ssh -R` forwards. A systemd user service keeps an explicitly
started connection running and reconnects after SSH or network failures.

No instance is enabled. Deploying does not start a target, and previously
started targets do not start automatically after reboot.

## Configuration

Service files live in `services/<name>.conf`:

```text
LOCAL_HOST=127.0.0.1
LOCAL_PORT=5001
REMOTE_PORT=15001
```

Target files live in `targets/<name>.conf`:

```text
HOST=server.example.com
USER=remote-user
SSH_PORT=22
SERVICES=mlflow-f1,mlflow-f2,postgres,s3
```

`SSH_PORT` is optional and defaults to 22. All other keys are required.
Names use letters, digits, `_`, `.`, and `-` and must begin with a letter or
digit. Hosts use IPv4/DNS-style labels; IPv6 literals are not supported by
this Bash version. Values are literal: quoting, expansion, whitespace around
`=`, and whitespace in `SERVICES` are not supported. Blank lines and lines
starting with `#` are allowed.

Configuration is parsed without `source` or `eval`. Unknown, duplicate,
missing, or empty keys; invalid names, hosts, users, or ports; missing or
duplicate service references; and duplicate remote ports within a target are
rejected. Ports must be in `1..65535`.

The checked-in `super` target creates one connection with these forwards:

```text
127.0.0.1:15001 -> 127.0.0.1:5001  (mlflow-f1)
127.0.0.1:15002 -> 127.0.0.1:5002  (mlflow-f2)
127.0.0.1:15432 -> 127.0.0.1:5432  (postgres)
127.0.0.1:19000 -> 127.0.0.1:9000  (s3)
```

## Deploy and migration

```bash
cd /opt/infra
just svc-ln-deploy
chezmoi apply ~/.bashrc
loginctl enable-linger "$USER"
```

Deploy creates symlinks below `~/.local/bin`, `~/.config/systemd/user`, and
`~/.config/svc-ln`, then runs `systemctl --user daemon-reload`. It is
idempotent for the expected links and refuses to replace regular files or
unexpected symlinks. It does not start or enable any instance.

Lingering lets an explicitly started user service survive logout. It does
not enable the instance or cause it to start after reboot.

If the former `mlflow-tunnel` was deployed, stop it first and manually remove
only the legacy symlinks after verifying that they point into this checkout:

```bash
systemctl --user stop 'mlflow-tunnel@*.service'
readlink ~/.local/bin/mlflow-tunnel
readlink ~/.config/systemd/user/mlflow-tunnel@.service
readlink ~/.config/mlflow-tunnel/targets
readlink ~/.config/mlflow-tunnel/login-hook.bash
# After all four paths have been verified as legacy symlinks:
unlink ~/.local/bin/mlflow-tunnel
unlink ~/.config/systemd/user/mlflow-tunnel@.service
unlink ~/.config/mlflow-tunnel/targets
unlink ~/.config/mlflow-tunnel/login-hook.bash
rmdir ~/.config/mlflow-tunnel
systemctl --user daemon-reload
```

The new deploy intentionally does not remove legacy paths automatically.

## Operate

```bash
svc-ln on super
svc-ln status super
svc-ln status
svc-ln restart super
svc-ln off super
journalctl --user -u svc-ln@super.service
```

`on` starts a validated target. `restart` validates the current configuration
and replaces its SSH process; like `systemctl restart`, it also starts a
currently stopped target. `off` needs only a valid target name, so it can stop
an existing unit even if its config was deleted or broken.

Status reports `ON`, `OFF`, `DOWN`, or `RECONNECTING`. `status` without a
target shows the compact target table used by the login hook. `status <target>`
also probes every configured local TCP endpoint and prints its local and remote
addresses. An endpoint that does not accept a connection is `DOWN`; when its
target is stopped or reconnecting it is respectively `OFF` or `RECONNECTING`.
A systemctl failure is `DOWN`, never `OFF`. Interactive SSH logins show only
the target table; local interactive shells show nothing.

SSH public-key authentication must work non-interactively before starting a
target, for example:

```bash
ssh -o BatchMode=yes -p 22 user3@163.152.23.181 true
```

The connection uses `BatchMode=yes`, `ExitOnForwardFailure=yes`, and SSH
keepalives. systemd retries five seconds after unexpected failure. Compression
is not enabled. `off` explicitly ends the connection and its restart loop.

## Security and health checks

Every remote listener request is explicitly bound to `127.0.0.1`. On the
remote target, verify the listeners and the MLflow static prefix:

```bash
ss -ltn '( sport = :15001 or sport = :15002 or sport = :15432 or sport = :19000 )'
curl http://127.0.0.1:15001/mlflow-f1/
curl http://127.0.0.1:15002/mlflow-f2/
```

Each listener must show `127.0.0.1`, never `0.0.0.0` or `::`. Also verify that
connecting to these ports through the target's non-loopback address fails.

Check the remote sshd `GatewayPorts` setting. `no` (the default) or
`clientspecified` preserves the requested loopback binding. `GatewayPorts yes`
can replace it with a wildcard listener and expose forwarded services on
external interfaces. After `svc-ln off super`, the listeners must disappear.
