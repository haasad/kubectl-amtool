# kubectl-amtool

A kubectl plugin that runs [`amtool`](https://github.com/prometheus/alertmanager#amtool)
inside an Alertmanager pod on the current cluster. It needs no local amtool, no
port-forward and no URL.

## Requirements
- [uv](https://docs.astral.sh/uv/) (or another installer for Python tools, e.g. pipx)
- `kubectl`
- Alertmanagers managed by the [Prometheus Operator](https://prometheus-operator.dev/). Other installations aren't detected.

## Install
```bash
uv tool install git+https://github.com/haasad/kubectl-amtool
kubectl plugin list   # should list kubectl-amtool
```
This installs two executables into uv's tool directory (usually
`~/.local/bin`, which has to be on `PATH`; `uv tool update-shell` adds it):
`kubectl-amtool`, the plugin, and `kubectl_complete-amtool`, which kubectl uses
for shell completion. uv installs Python and
[click](https://click.palletsprojects.com/) into an isolated environment.

Pin a tag or branch with `git+https://github.com/haasad/kubectl-amtool@<ref>`.
Update with `uv tool upgrade kubectl-amtool`, remove with
`uv tool uninstall kubectl-amtool`.

## Usage
```
kubectl amtool [OPTIONS] [AMTOOL_ARGS]...
```

| Option | Meaning |
|---|---|
| `-l`, `--list` | List the Alertmanagers (`kubectl get alertmanagers`) and exit |
| `-n`, `--namespace` | Look for the Alertmanager only in this namespace |
| `-a`, `--alertmanager` | Alertmanager resource name, only needed if the namespace has several Alertmanagers (requires `-n`) |
| `--as USER` | User to impersonate (default: `system:admin` on OpenShift, none otherwise) |
| `--no-as` | Disable the default impersonation on OpenShift |
| `--context`, `--kubeconfig` | Passed to every kubectl call |
| `--url` | Alertmanager URL inside the pod (default `http://localhost:9093`) |

Options must come before the amtool arguments. Everything from the first
non-option word on (or after `--`) is passed to amtool unchanged. Without
amtool arguments (plain `kubectl amtool`), the plugin's help is shown; use
`kubectl amtool -- -h` for amtool's own help.

The plugin finds the operator's `Alertmanager` resources
(`alertmanagers.monitoring.coreos.com`), in the `-n` namespace or across all
namespaces. The operator runs resource `<name>` as StatefulSet
`alertmanager-<name>`, which is where amtool is executed. Resources scaled to
0 replicas are skipped. One match is used directly. With
several matches it picks a default if present: `openshift-monitoring` on
OpenShift, `syn-monitoring` on other clusters. Otherwise it lists them and asks
you to narrow it down with `-n`, and with `-a` if one namespace holds more than
one Alertmanager.

`-l` runs `kubectl get alertmanagers.monitoring.coreos.com` (with `-A`, or the
`-n` namespace, and the same `--context`, `--kubeconfig` and impersonation).
Its `NAME` column is what `-a` takes; the other columns (version, replicas,
status, depending on the operator version) show which Alertmanagers are
healthy. Unlike auto-detection, it also lists Alertmanagers scaled to 0.

```bash
kubectl amtool alert                           # auto-detected Alertmanager
kubectl amtool -n openshift-user-workload-monitoring silence query
kubectl amtool -n monitoring -a ops alert
kubectl amtool --context prod -n syn-monitoring alert
kubectl amtool silence import < silences.json  # stdin is forwarded
kubectl amtool -l                              # list detected Alertmanagers
kubectl amtool -- -h                           # amtool's own help
```

With shell completion set up (below), `-n <TAB>` offers exactly the namespaces
that have an Alertmanager, so you don't have to remember them.

## Shell completion
With kubectl 1.26 or newer, `kubectl amtool <TAB>` is completed by
`kubectl_complete-amtool`, which the install puts on `PATH` next to the plugin.
kubectl's own completion has to be enabled, e.g.
`source <(kubectl completion bash)`.

- `-n <TAB>` suggests only namespaces that contain an Alertmanager.
- `-a <TAB>` suggests the Alertmanager names in the namespace given with `-n`
  (nothing without `-n`).
- Option names complete as well.
- amtool's own commands and flags complete too (`silence <TAB>` →
  `add expire import query update`). The plugin picks the Alertmanager the same
  way a real run would and asks amtool inside that pod, so the suggestions
  match its version. Where amtool has no suggestions, for example the file in
  `silence import FILE`, the shell falls back to file names. amtool doesn't
  suggest values such as alert names or silence IDs.

`--context`, `--kubeconfig`, `--as` and `--no-as` typed earlier on the line are
used for the lookup. Completing `-n` or `-a` runs two small kubectl calls
(OpenShift detection and listing Alertmanager resources). Completing amtool
arguments also runs a `kubectl exec` into the pod, so expect a short delay on
each TAB there. If a lookup fails, for example because you can't list
Alertmanager resources or exec into the pod, or if the choice is ambiguous,
nothing is suggested.

## Development
The code lives in `src/kubectl_amtool/cli.py`. uv installs the package in
editable mode into `.venv`, together with the dev dependencies:
```bash
uv run pytest                 # tests; creates .venv from uv.lock on first run
uv run kubectl-amtool --help  # run the plugin from the checkout
uv tool install -e .          # put the checkout's executables on PATH
```
`uv tool install` from git resolves the dependencies itself and ignores
`uv.lock`; the lock file only pins the development environment.
