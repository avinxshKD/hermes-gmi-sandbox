# GMI Sandbox for Hermes

A community terminal-backend plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent).
Hermes stays on your computer or server; its terminal commands and file operations run
inside an ephemeral [GMI Cloud Sandbox](https://docs.gmicloud.ai/agentbox-api/sandbox/overview).
The model powering Hermes is independent of this backend.

**Status: initial implementation, tested offline only. Not yet verified against a live
GMI sandbox or admitted to the Hermes plugin catalog.** This is not an official GMI or Nous product.

## Requirements

- Hermes 0.23.0 or newer with in-process plugin support. Tests currently use the checkout
  pinned in `.github/workflows/test.yml`; compatibility with older versions is not claimed.
- A GMI **Compute** API key, not an inference API key.
- A ready GMI Sandbox template in an IDC your organization can use. The image must provide
  `bash`, `python3`, standard POSIX utilities, and any development tools your tasks need.
  Install `git` and `ripgrep` for coding work. The plugin creates `/workspace`.
- A funded GMI account. Sandboxes are billed while running.

## Install and configure

Install from this repository using Hermes's plugin installer, or clone it into your
active profile's `plugins/gmi-sandbox` directory and enable `gmi-sandbox`.
Hermes must install the declared `gmi-sandbox-sdk` dependency through its plugin manager.
This repository does not install dependencies or change Hermes files itself.

Set `GMI_SANDBOX_API_KEY` through Hermes's secret setup. For a single-profile shell:

```sh
export GMI_SANDBOX_API_KEY="<compute-key>"
```

Merge these entries into your active Hermes `config.yaml` without replacing other settings:

```yaml
terminal:
  backend: gmi_sandbox
  cwd: /workspace
  container_persistent: false

plugins:
  entries:
    gmi-sandbox:
      enabled: true
      settings:
        template_id: "your-ready-template-id"
        idc_name: "your-idc-name"
        lifetime_seconds: 900
```

An empty `idc_name` uses your organization's default. The template determines CPU,
memory, disk and installed software; Hermes's generic resource knobs do not resize it.
Per-task image overrides and persistent filesystem mode are rejected.

For a first check, ask Hermes to run `pwd`, write a small file, read it back, and execute
a command that exits nonzero. Confirm the files exist in the sandbox, not your host.
The plugin does not upload your local repository: clone it inside the sandbox or transfer
files explicitly. Files disappear when the sandbox is deleted, so export work before
ending a session. Do not rely on automatic workspace synchronization.

## Execution and cleanup

- A new environment creates its own sandbox. It never attaches to or deletes a pre-existing sandbox.
- Commands use Hermes's bash wrapper, including working-directory markers and session exports.
- Long commands are polled rather than resubmitted. Interrupts and timeouts request cancellation
  of that execution, not a restart of the whole sandbox. The adapter polls an accepted cancellation
  for up to two seconds. If cancellation cannot be confirmed, further commands are refused until
  the environment is cleaned up; the server-side execution timeout remains the backstop.
- GMI limits command runtime to 1–3600 seconds. Its 25-second synchronous wait window is not
  used by this plugin.
- Output arrives after completion, with stdout followed by stderr; their interleaving is not preserved.
- If GMI reports truncated output, the command fails visibly rather than returning partial file
  content as a successful read. Read large files in smaller pieces.
- Cleanup deletes the sandbox. A finite lifetime is set at creation and renewed before commands;
  it is a backstop for a crashed controller, not a substitute for confirmed deletion.
- Snapshots, persistence, interactive stdin, Bot Desktop and browser streaming are not implemented.
  Payload stdin used by file tools is supported. Only `plugins.isolation: in_process` is supported.

## Security and costs

The plugin sends commands to GMI's sandbox data plane and lifecycle requests to
`https://console.gmicloud.ai/api/v2`. Outputs and files cross that connection.
The compute API key is obtained through Hermes's profile-scoped secret API and passed
explicitly to the SDK. It is not included in sandbox environment variables or commands.
The sandbox has its own data-plane access token. Do not bake credentials into your template.

Installing the plugin alone does not provision anything. Selecting it and running terminal
tools creates billable resources. There is no per-sandbox price estimate or billing approval
dialog. Existing dangerous-command guards remain enabled. Outbound sandbox networking and
isolation guarantees are GMI's, not protections added by this plugin.

The plugin does not automatically retry command submissions, add telemetry, run local shell
commands, update itself, or read credential files. The SDK has its own transport retry policy.
No code can guarantee cleanup after a hard process kill or lost create response; inspect the
GMI Console for orphaned resources after a failed run. Keep the lifetime short during testing.

## Development

Use a separate Python environment with the plugin's declared dependency, pytest, and the
dependencies needed by your Hermes checkout's public backend classes. Hermes itself is supplied
by that checkout, not installed as a plugin dependency.

```sh
python scripts/test.py --hermes /path/to/hermes-agent -q
python scripts/validate.py --hermes /path/to/hermes-agent
ruff check .
```

The tests import real Hermes classes and use the real GMI SDK with an in-memory transport.
They block outbound socket connections. They do not provision a sandbox and do not prove
GMI availability, image compatibility, or end-to-end agent behavior.
