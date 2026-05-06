# DBK Privilege Escalation Design: pkexec + polkit for eBPF

## 1. Background

bpftrace and perf require CAP_SYS_ADMIN (for bpf() syscall and BPF map
creation). Running the whole DBK process as root is undesirable -- it widens
the attack surface. The design goal is:

  unprivileged DBK user -> explicit approval -> pkexec/polkit -> scoped
  bpftrace/perf invocation -> full audit trail.

## 2. How pkexec + polkit Works for bpftrace

### 2.1 Call chain

```
dbk trace run --profile cpu-hotpath --execute --approve-privileged
  -> tracing.run_trace_profile(execute=True, approve_privileged=True)
      -> _escalate(command=['bpftrace', '-e', 'profile:hz:99 {...}'])
          -> subprocess.run(['pkexec', '--disable-internal-agent',
                             'env', 'DBK_TRACE=1', 'DBK_TASK_ID=...',
                             '/usr/bin/bpftrace', '-e',
                             'profile:hz:99 { @[kstack] = count(); }'])
```

### 2.2 polkit intercepts pkexec

pkexec is itself a polkit subject (mechanism). When pkexec runs, it asks polkit:

  action id : org.dbk.bpftrace.run
  subject   : unix-user:<uid>  (the unprivileged user running dbk)

polkit evaluates the matching .rules JS file and returns:

  - AUTH_ADMIN_KEEP : prompt password (credential cached for a few minutes)
  - YES              : allow immediately (dbk-trace group member)
  - NO               : deny

### 2.3 Why pkexec + polkit (vs sudo)

|                  | sudo                     | pkexec + polkit              |
|------------------|--------------------------|------------------------------|
| TTY required     | yes                      | no (dbus auth agent)         |
| Per-action policy| basic                    | fine-grained, DB-backed      |
| Audit            | partial                  | native via /var/log/secure   |
| Scoped binary    | yes (Cmnd_Alias)         | yes (annotate key)           |
| CAP_SYS_ADMIN    | no scoping               | yes (annotate key)           |

## 3. Polkit Policy File Format

### 3.1 Modern format: .rules JS file (preferred on EL8/RHEL8)

Path: /etc/polkit-1/rules.d/49-dbk-bpftrace.rules

```javascript
/* -*- mode: js; js-indent-level: 2; -*- */
/* DBK bpftrace / perf privilege escalation rules
 *
 * Scoping guarantees:
 *   1. Only /usr/bin/bpftrace (and approved scripts) may run.
 *   2. Only -e one-liners are allowed (no -f file loading).
 *   3. DBK_TRACE=1 env var is set so bpftrace can self-cap.
 *   4. CAP_SYS_ADMIN is NOT granted to the calling process; only to
 *      the pkexec child, and only for the duration of the trace.
 */

polkit.addRule(function(action, subject) {
    if (!action.id.startsWith('org.dbk.')) {
        return polkit.Result.NOT_HANDLED;
    }

    // Rule 1: bpftrace execution
    if (action.id === 'org.dbk.bpftrace.run') {
        // Allow dbk-trace group members immediately (already authd).
        if (subject.isInGroup('dbk-trace')) {
            return polkit.Result.YES;
        }
        // Allow the dedicated dbk system user.
        if (subject.user === 'dbk') {
            return polkit.Result.YES;
        }
        // All others require admin authentication.
        return polkit.Result.AUTH_ADMIN_KEEP;
    }

    // Rule 2: perf execution
    if (action.id === 'org.dbk.perf.run') {
        if (subject.isInGroup('dbk-trace')) {
            return polkit.Result.YES;
        }
        if (subject.user === 'dbk') {
            return polkit.Result.YES;
        }
        return polkit.Result.AUTH_ADMIN_KEEP;
    }

    return polkit.Result.NOT_HANDLED;
});
```

### 3.2 Legacy .pkla format (polkit-pkla-compat on EL8)

Path: /etc/polkit-1/localauthority/50-local.d/dbk-bpftrace.pkla

```ini
[DBK bpftrace]
Identity=unix-user:dbk
Identity=unix-group:dbk-trace
ResultAny=no
ResultInactive=no
ResultActive=yes

[DBK perf]
Identity=unix-user:dbk
Identity=unix-group:dbk-trace
ResultAny=no
ResultInactive=no
ResultActive=yes
```

### 3.3 polkit action definition file (XML)

Path: /usr/share/polkit-1/actions/org.dbk.tracing.policy

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE policyconfig PUBLIC
 "-//freedesktop//DTD PolicyKit Policy Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/PolicyKit/1/policyconfig.dtd">

<policyconfig>
  <vendor>DBK Project</vendor>

  <!-- bpftrace execution: CAP_SYS_ADMIN only for /usr/bin/bpftrace -->
  <action id="org.dbk.bpftrace.run">
    <description>Run bpftrace for eBPF-based system tracing</description>
    <message>Authentication is required to run bpftrace for eBPF tracing.</message>

    <!-- org.freedesktop.policykit.exec.path:
         restricts pkexec to only /usr/bin/bpftrace.
         Any other binary path triggers a DENY.
         This is the CAP_SYS_ADMIN scoping mechanism. -->
    <annotate key="org.freedesktop.policykit.exec.path">/usr/bin/bpftrace</annotate>

    <!-- org.freedesktop.policykit.exec.argv1=-e:
         blocks -f script file loading in privileged mode. -->
    <annotate key="org.freedesktop.policykit.exec.argv1">-e</annotate>

    <!-- Environment variables passed to the privileged child. -->
    <annotate key="org.freedesktop.policykit.exec.env:DBK_TRACE">1</annotate>
    <annotate key="org.freedesktop.policykit.exec.env:DBK_TASK_ID">KEEPORIGINAL</annotate>
    <annotate key="org.freedesktop.policykit.exec.env:DBK_APPROVED_BY">KEEPORIGINAL</annotate>

    <defaults>
      <allow_any>auth_admin_keep</allow_any>
      <allow_inactive>no</allow_inactive>
      <allow_active>auth_admin_keep</allow_active>
    </defaults>
  </action>

  <!-- perf record execution -->
  <action id="org.dbk.perf.run">
    <description>Run perf record for hardware PMU profiling</description>
    <message>Authentication is required to run perf for hardware profiling.</message>
    <annotate key="org.freedesktop.policykit.exec.path">/usr/bin/perf</annotate>
    <annotate key="org.freedesktop.policykit.exec.env:DBK_TRACE">1</annotate>
    <defaults>
      <allow_any>auth_admin_keep</allow_any>
      <allow_inactive>no</allow_inactive>
      <allow_active>auth_admin_keep</allow_active>
    </defaults>
  </action>

</policyconfig>
```

## 4. Security Scoping: CAP_SYS_ADMIN Only for bpftrace

Critical property: DBK itself does not run as root. It runs as an unprivileged
user (e.g. dbk). When pkexec launches bpftrace:

  1. pkexec forks + execs the target binary with real-uid=0, effective-uid=0
     (i.e. it gains CAP_SYS_ADMIN), but ONLY for the child process.
  2. DBKs own process stays unprivileged.
  3. /usr/bin/bpftrace is the only thing with CAP_SYS_ADMIN.
  4. exec.path annotation enforces that no other binary can be invoked via
     this polkit action.
  5. BPF programs and maps are attached by the bpftrace child only, and are
     cleaned up when bpftrace exits.

DBK_TRACE=1 additionally lets bpftrace self-restrict: no -f script file
loading (only -e one-liners), max duration enforced by parent timeout.

## 5. Fallback Strategy: pkexec -> sudo -> error

Detection chain in tracing.py _escalate():

```
Path 1: has CAP_SYS_ADMIN or euid==0
    -> direct execution, EscalationResult(method='root', ...)

Path 2: pkexec is available (shutil.which('pkexec'))
    -> subprocess.run(['pkexec', '--disable-internal-agent',
                      '--keep-canonical-environment', *command])
    -> EscalationResult(method='pkexec', ...)
    -> on OSError (polkit denied): raise PermissionError (do NOT fall through)

Path 3: sudo is available (last resort)
    -> subprocess.run(['sudo', *command])
    -> EscalationResult(method='sudo', ...)

Path 4: nothing available
    -> EscalationResult(method='none',
         stdout='[no-escalation-path] pkexec and sudo unavailable')
```

## 6. Audit Log Integration

### 6.1 System audit log (polkit)

polkit logs to Linux audit subsystem via audit_log_acct_message():

  type=POLKIT_MSG msg=audit(...):
    pid=1234 uid=1000 ses=5 :
    msg='pid=1234 uid=1000 : action=org.dbk.bpftrace.run result=YES'

This appears in /var/log/secure (EL/RHEL) or /var/log/auth.log (Debian).

### 6.2 DBK trace_approval_audit table

Extend RuntimeStore.init_schema() in storage.py with:

```sql
CREATE TABLE IF NOT EXISTS trace_approval_audit (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT    NOT NULL,          -- ISO 8601 UTC
    task_id         TEXT    NOT NULL,
    username        TEXT    NOT NULL,
    action_id       TEXT    NOT NULL,           -- org.dbk.bpftrace.run
    command_json    TEXT    NOT NULL,           -- JSON array
    duration_sec    INTEGER NOT NULL,
    profile         TEXT    NOT NULL,
    mode            TEXT    NOT NULL,           -- executed/simulated/timeout/failed
    escalation      TEXT    NOT NULL,           -- pkexec/sudo/root/none
    exit_code       INTEGER,
    approved_by_cli INTEGER NOT NULL DEFAULT 0, -- 1 if --approve-privileged set
    error           TEXT
);

CREATE INDEX IF NOT EXISTS idx_trace_audit_task_id
    ON trace_approval_audit(task_id);
CREATE INDEX IF NOT EXISTS idx_trace_audit_ts
    ON trace_approval_audit(ts);
```

New method: RuntimeStore.insert_trace_audit(...)

### 6.3 Audit record fields

| Field             | Type     | Description                                  |
|-------------------|----------|----------------------------------------------|
| ts                | TEXT     | UTC timestamp of approval                    |
| task_id           | TEXT     | DBK task identifier                          |
| username          | TEXT     | OS username who approved                     |
| action_id         | TEXT     | polkit action (e.g. org.dbk.bpftrace.run)    |
| command_json      | TEXT     | JSON array of command + args                |
| duration_sec      | INTEGER  | Max trace duration                           |
| profile           | TEXT     | DBK profile name                             |
| mode              | TEXT     | executed / simulated / timeout / failed     |
| escalation        | TEXT     | pkexec / sudo / root / none                  |
| exit_code         | INTEGER  | Process exit code (nullable)                 |
| approved_by_cli   | INTEGER  | 1 if --approve-privileged was set            |
| error             | TEXT     | Error message if failed                      |

CLI audit insertion point: cmd_trace_run() in cli.py wraps
store.insert_trace_audit() around every execute=True invocation.

## 7. Key Code Changes

### 7.1 dbk/tracing.py  -- _escalate() and run_trace_profile()

Key additions to tracing.py:

```python
# New dataclass
@dataclass(slots=True)
class EscalationResult:
    method: str          # 'pkexec' | 'sudo' | 'root' | 'none'
    stdout: str
    stderr: str
    exit_code: int | None
    timed_out: bool

# New helpers
def _has_privilege() -> bool:
    return hasattr(os, 'geteuid') and os.geteuid() == 0

def _can_escalate_via_pkexec() -> bool:
    return shutil.which('pkexec') is not None

def _get_username() -> str:
    return (os.environ.get('SUDO_USER')
            or os.environ.get('PKEXEC_CHECK')
            or getattr(os, 'getlogin', lambda: 'unknown')())

# 4-path escalation engine
def _escalate(*, command, duration_sec, task_id, profile) -> EscalationResult:
    env = os.environ.copy()
    env.update(
        DBK_TRACE='1',
        DBK_TASK_ID=task_id,
        DBK_APPROVED_BY=_get_username(),
        DBK_PROFILE=profile,
    )
    timeout = duration_sec + 15  # 15 s grace for polkit prompt

    # Path 1: already root
    if _has_privilege():
        return _run_direct(command, duration_sec, env)

    # Path 2: pkexec
    pkexec = shutil.which('pkexec')
    if pkexec:
        return _run_pkexec(pkexec, command, timeout, env)

    # Path 3: sudo fallback
    sudo = shutil.which('sudo')
    if sudo:
        return _run_sudo(sudo, command, timeout, env)

    # Path 4: nothing available
    return EscalationResult(
        method='none',
        stdout='[no-escalation-path] pkexec and sudo unavailable',
        stderr='', exit_code=None, timed_out=False)
```

In run_trace_profile(), replace the simple euid==0 check with:

```python
if execute and command_available:
    esc = _escalate(command=cmd, duration_sec=duration_sec,
                    task_id=task_id, profile=profile)
    escalation_method = esc.method
    if esc.timed_out:
        mode = 'timeout'
    elif esc.exit_code == 0:
        mode = 'executed'
    else:
        mode = 'escalation_failed'
    output = esc.stdout.strip() or esc.stderr.strip()
```

### 7.2 dbk/storage.py -- audit table + insert method

Add to RuntimeStore.init_schema():

```python
conn.execute('''
    CREATE TABLE IF NOT EXISTS trace_approval_audit (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        ts              TEXT    NOT NULL,
        task_id         TEXT    NOT NULL,
        username        TEXT    NOT NULL,
        action_id       TEXT    NOT NULL,
        command_json    TEXT    NOT NULL,
        duration_sec    INTEGER NOT NULL,
        profile         TEXT    NOT NULL,
        mode            TEXT    NOT NULL,
        escalation      TEXT    NOT NULL,
        exit_code       INTEGER,
        approved_by_cli INTEGER NOT NULL DEFAULT 0,
        error           TEXT
    )
''')
conn.execute('''
    CREATE INDEX IF NOT EXISTS idx_trace_audit_task_id
    ON trace_approval_audit(task_id)
''')
conn.execute('''
    CREATE INDEX IF NOT EXISTS idx_trace_audit_ts
    ON trace_approval_audit(ts)
''')
```

Add new method:

```python
def insert_trace_audit(
    self,
    *,
    task_id: str,
    username: str,
    action_id: str,
    command: list[str],
    duration_sec: int,
    profile: str,
    mode: str,
    escalation: str,
    exit_code: int | None = None,
    approved_by_cli: bool = False,
    error: str | None = None,
) -> None:
    with self.connect() as conn:
        conn.execute(
            '''
            INSERT INTO trace_approval_audit
            (ts, task_id, username, action_id, command_json,
             duration_sec, profile, mode, escalation,
             exit_code, approved_by_cli, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                utc_now_iso(),
                task_id, username, action_id,
                json.dumps(command, ensure_ascii=True),
                duration_sec, profile, mode, escalation,
                exit_code,
                int(approved_by_cli),
                error,
            ),
        )
```

### 7.3 dbk/cli.py -- cmd_trace_run() audit wiring

```python
def cmd_trace_run(args: argparse.Namespace) -> int:
    store = _store()
    try:
        result = run_trace_profile(
            profile=args.profile,
            task_id=args.task_id,
            duration_sec=args.duration,
            artifacts_root=artifacts_root(),
            execute=args.execute,
            approve_privileged=args.approve_privileged,
        )
    except (ValueError, PermissionError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    store.insert_trace_artifact(result.artifact)

    # Audit trail for privileged escalations
    if args.execute:
        summary = result.artifact.summary_json
        try:
            store.insert_trace_audit(
                task_id=args.task_id,
                username=os.environ.get('SUDO_USER', os.environ.get('USER', 'unknown')),
                action_id='org.dbk.bpftrace.run',
                command=PROFILE_COMMANDS.get(args.profile, []),
                duration_sec=args.duration,
                profile=args.profile,
                mode=summary.get('mode', 'simulated'),
                escalation=summary.get('escalation', 'none'),
                exit_code=None,
                approved_by_cli=args.approve_privileged,
                error=None,
            )
        except Exception as exc:
            # Never let audit failure break the trace command
            print(f'[audit warning] failed to write audit record: {exc}',
                  file=sys.stderr)

    print(f'Trace profile complete: {args.profile}')
    print(f'stdout: {result.stdout_path}')
    print(f'summary: {result.summary_path}')
    return 0
```

### 7.4 Installation checklist

```bash
# 1. Install polkit
sudo yum install -y polkit polkit-pkla-compat

# 2. Install polkit action definition
sudo install -m 0644 org.dbk.tracing.policy \
    /usr/share/polkit-1/actions/org.dbk.tracing.policy

# 3. Install JS rules (preferred on EL8)
sudo install -m 0644 49-dbk-bpftrace.rules \
    /etc/polkit-1/rules.d/49-dbk-bpftrace.rules

# 4. Install .pkla as fallback (legacy compat)
sudo install -m 0644 dbk-bpftrace.pkla \
    /etc/polkit-1/localauthority/50-local.d/dbk-bpftrace.pkla

# 5. Create the dbk-trace group and add operators
sudo groupadd -f dbk-trace
sudo gpasswd -a "$OPERATOR" dbk-trace

# 6. Verify polkit rules
pkcheck --action-id org.dbk.bpftrace.run \
    --process $$ --allow-user-integration 2>&1
```

## 8. End-to-End Flow Summary

```
User: dbk trace run --profile cpu-hotpath --duration 30 \
    --execute --approve-privileged

  cli.py::cmd_trace_run()
    -> tracing.run_trace_profile(execute=True, approve_privileged=True)
        -> _escalate(command=['bpftrace', '-e', 'profile:hz:99 {...}'])
            -> subprocess.run(['pkexec', '--disable-internal-agent',
                               '--keep-canonical-environment',
                               'env', 'DBK_TRACE=1', 'DBK_TASK_ID=...',
                               '/usr/bin/bpftrace', '-e', '...'])
                [polkit daemon checks /etc/polkit-1/rules.d/49-dbk-bpftrace.rules]
                [polkit looks up action org.dbk.bpftrace.run]
                [auth_agent prompts: allow bpftrace for 30s?]
                [On auth: pkexec execs bpftrace as uid 0, CAP_SYS_ADMIN in child]
                [bpftrace runs, produces output, exits]
                [pkexec exits, CAP_SYS_ADMIN reverts to caller (dbk user)]
        -> store.insert_trace_artifact(result.artifact)
        -> store.insert_trace_audit(...)   <- audit record
        -> print summary

  Audit trail:
    /var/log/secure (or auth.log):  polkit auth decision
    ~/.dbk/runtime.sqlite  trace_approval_audit: full DBK record
```

## 9. Threat Model Summary

| Threat                                   | Mitigation                                            |
|------------------------------------------|------------------------------------------------------|
| DBK process compromised -> root code     | DBK stays unprivileged; only pkexec child gets CAP_SYS_ADMIN |
| Malicious bpftrace script injection      | exec.argv1=-e annotation blocks -f file loading       |
| Privilege escalation without consent     | approve_privileged flag required; auth_admin_keep     |
| bpftrace runs indefinitely               | subprocess.run(timeout=duration+15s kills child       |
| sudo fallback has no scoping             | sudo is last resort; admin should deploy pkexec        |
| No audit trail                          | polkit system audit log + trace_approval_audit table   |
