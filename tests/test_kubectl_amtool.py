import json
import subprocess

import click
import pytest
from click.testing import CliRunner

from kubectl_amtool import cli as ka

MAIN = ka.Alertmanager("openshift-monitoring", "main")
UW = ka.Alertmanager("openshift-user-workload-monitoring", "user-workload")
URL = "--alertmanager.url=http://localhost:9093"


# --- fakes ------------------------------------------------------------------


def am(ns, name, replicas=None):
    """An Alertmanager resource; the operator runs it as StatefulSet alertmanager-<name>."""
    spec = {} if replicas is None else {"replicas": replicas}
    return {"metadata": {"namespace": ns, "name": name}, "spec": spec}


# A small slice of amtool's command tree, for `amtool --completion-bash`
AMTOOL_COMMANDS = {
    (): ["alert", "silence", "check-config", "cluster", "config", "template", "help"],
    ("alert",): ["add", "query"],
    ("silence",): ["add", "expire", "import", "query", "update"],
}
# Abridged from a real `amtool --completion-bash alert --`
AMTOOL_FLAGS = [
    "--inhibited",
    "--silenced",
    "--active",
    "--help",
    "--verbose",
    "--alertmanager.url",
    "--output",
    "--timeout",
    "--version",
]


def fake_amtool_completion(words):
    """Rough emulation of kingpin's --completion-bash.

    Like the real amtool, it only answers when every word is valid: an empty or
    partly typed word gives nothing (amtool: no output, or "error: unknown long
    flag"). A bare "--" lists the flags. The plugin filters the result by prefix.
    """
    if "" in words:
        return []
    curr = words[-1] if words else ""
    prev = words[-2] if len(words) > 1 else ""
    if curr == "--":
        return AMTOOL_FLAGS
    if curr.startswith("--") or prev.startswith("--"):
        flag = curr if curr.startswith("--") else prev
        if flag not in AMTOOL_FLAGS:
            return []  # amtool: error: unknown long flag
        return ["simple", "extended", "json"] if flag == "--output" else []
    path: tuple[str, ...] = ()
    for word in words:  # deepest command matched so far
        if word in AMTOOL_COMMANDS.get(path, []) and path + (word,) in AMTOOL_COMMANDS:
            path += (word,)
        elif word in AMTOOL_COMMANDS.get(path, []):
            return []  # a leaf command such as "silence import": no subcommands
        else:
            return []  # unknown or partly typed command word
    return AMTOOL_COMMANDS[path]


class FakeKubectl:
    def __init__(self, openshift=False, items=(), list_rc=0, exec_rc=0):
        self.openshift = openshift
        self.items = list(items)
        self.list_rc = list_rc
        self.exec_rc = exec_rc
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        if "api-resources" in cmd:
            out = "clusterversions.config.openshift.io\n" if self.openshift else ""
            return subprocess.CompletedProcess(cmd, 0, out, "")
        if "get" in cmd and "alertmanagers.monitoring.coreos.com" in cmd:
            if self.list_rc:
                return subprocess.CompletedProcess(cmd, self.list_rc, "", "Forbidden\n")
            items = self.items
            if "-n" in cmd:  # namespaced get
                ns = cmd[cmd.index("-n") + 1]
                items = [i for i in items if i["metadata"]["namespace"] == ns]
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"items": items}), "")
        if "exec" in cmd and "--completion-bash" in cmd:
            if self.exec_rc:
                return subprocess.CompletedProcess(cmd, self.exec_rc, "", "exec denied\n")
            words = cmd[cmd.index("--completion-bash") + 1 :]
            out = "".join(f"{w}\n" for w in fake_amtool_completion(words))
            return subprocess.CompletedProcess(cmd, 0, out, "")
        raise AssertionError(f"unexpected kubectl call: {cmd}")


@pytest.fixture
def run(monkeypatch):
    """Invoke the CLI with a fake kubectl; returns (result, exec'd commands)."""
    monkeypatch.setattr(ka.shutil, "which", lambda _: "/usr/bin/kubectl")

    def invoke(argv, fake=None, input=None):
        execs = []

        def fake_exec(file, args):
            execs.append(args)
            raise SystemExit(0)

        monkeypatch.setattr(ka.os, "execvp", fake_exec)
        monkeypatch.setattr(ka.subprocess, "run", fake or FakeKubectl())
        result = CliRunner().invoke(ka.cli, argv, input=input)
        return result, execs

    return invoke


def amtool_args(cmd):
    return cmd[cmd.index("amtool") + 2 :]  # skip "amtool" and --alertmanager.url


def target(cmd):
    return cmd[cmd.index("-n") + 1], cmd[cmd.index("-c") - 1]


# --- option parsing ---------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["-n", "mon", "-a", "am", "alert"],
        ["-nmon", "-aam", "alert"],
        ["--namespace=mon", "--alertmanager=am", "alert"],
        ["--namespace", "mon", "--alertmanager", "am", "alert"],
    ],
)
def test_namespace_and_alertmanager_forms(run, argv):
    result, execs = run(argv)
    assert result.exit_code == 0, result.output
    assert target(execs[0]) == ("mon", "sts/alertmanager-am")
    assert amtool_args(execs[0]) == ["alert"]


def test_stops_at_first_positional(run):
    result, execs = run(["-n", "mon", "-a", "am", "alert", "-n", "x", "--foo"])
    assert result.exit_code == 0, result.output
    assert target(execs[0]) == ("mon", "sts/alertmanager-am")
    assert amtool_args(execs[0]) == ["alert", "-n", "x", "--foo"]


def test_double_dash_passes_rest_through(run):
    result, execs = run(["-n", "mon", "-a", "am", "--", "-l", "--", "x"])
    assert result.exit_code == 0, result.output
    assert amtool_args(execs[0]) == ["-l", "--", "x"]


def test_unknown_flag_is_usage_error(run):
    result, execs = run(["--bogus", "alert"])
    assert result.exit_code == 2
    assert "--bogus" in result.output and not execs


def test_missing_value(run):
    result, _ = run(["-n"])
    assert result.exit_code == 2
    assert "requires an argument" in result.output


def test_as_and_no_as_conflict(run):
    result, _ = run(["--as=x", "--no-as", "alert"])
    assert result.exit_code == 2
    assert "mutually exclusive" in result.output


def test_help(run):
    result, _ = run(["-h"])
    assert result.exit_code == 0
    assert "auto-detected" in result.output
    assert "--no-as" in result.output


@pytest.mark.parametrize("argv", [[], ["-n", "mon"], ["--context=prod", "-n", "mon", "-a", "am"]])
def test_no_amtool_args_shows_plugin_help(run, argv):
    fake = FakeKubectl(False)
    result, execs = run(argv, fake)
    assert result.exit_code == 0
    assert "Run amtool against an Alertmanager" in result.output
    assert "--no-as" in result.output
    assert not execs and fake.calls == []  # no kubectl call at all


def test_double_dash_h_is_amtools_help(run):
    result, execs = run(["-n", "mon", "-a", "am", "--", "-h"])
    assert result.exit_code == 0, result.output
    assert amtool_args(execs[0]) == ["-h"]


# --- Alertmanager selection --------------------------------------------------


def test_resolve_alertmanager():
    assert ka.resolve_alertmanager("mon", "am") == ("mon", "am")


def test_alertmanager_requires_namespace():
    with pytest.raises(click.UsageError, match="-a requires -n"):
        ka.resolve_alertmanager(None, "my-am")


def test_former_alias_is_just_a_name():
    with pytest.raises(click.UsageError, match="-a requires -n"):
        ka.resolve_alertmanager(None, "main")


def test_old_statefulset_option_is_gone(run):
    result, execs = run(["-n", "mon", "-s", "alertmanager-am", "alert"])
    assert result.exit_code == 2 and not execs
    assert "No such option '-s'" in result.output


def test_no_selection():
    assert ka.resolve_alertmanager(None, None) is None
    assert ka.resolve_alertmanager("mon", None) is None


def test_namespace_narrows_autodetection(run):
    fake = FakeKubectl(items=[am(MAIN.namespace, "main"), am("mon", "am")])
    result, execs = run(["-n", "mon", "alert"], fake)
    assert result.exit_code == 0, result.output
    assert fake.calls[1][fake.calls[1].index("-n") + 1] == "mon"
    assert target(execs[0]) == ("mon", "sts/alertmanager-am")


def test_namespace_with_several_alertmanagers_asks_for_s(run):
    fake = FakeKubectl(items=[am("mon", "am"), am("mon", "am-2")])
    result, execs = run(["-n", "mon", "alert"], fake)
    assert result.exit_code == 1 and not execs
    assert "in namespace 'mon', select one with -a" in result.output
    assert "  mon\tam-2" in result.output


def test_positional_word_is_passed_to_amtool(run):
    fake = FakeKubectl(items=[am("mon", "am")])
    result, execs = run(["main", "alert"], fake)
    assert result.exit_code == 0, result.output
    assert target(execs[0]) == ("mon", "sts/alertmanager-am")  # auto-detected
    assert amtool_args(execs[0]) == ["main", "alert"]


# --- find_alertmanagers: operator-managed Alertmanagers ----------------------------


def test_find_alertmanagers_from_alertmanager_resources(monkeypatch):
    fake = FakeKubectl(
        items=[
            am("openshift-monitoring", "main"),  # no replicas field: default 1
            am("mon", "am", replicas=2),
            am("mon", "paused", replicas=0),  # scaled down: skipped
        ]
    )
    monkeypatch.setattr(ka.subprocess, "run", fake)
    alertmanagers = ka.find_alertmanagers(ka.Kubectl(), None)
    assert alertmanagers == [MAIN, ("mon", "am")]
    assert fake.calls == [
        ["kubectl", "get", "alertmanagers.monitoring.coreos.com", "-A", "-o", "json"]
    ]


def test_find_alertmanagers_in_namespace(monkeypatch):
    fake = FakeKubectl(items=[am("mon", "am"), am("other", "am")])
    monkeypatch.setattr(ka.subprocess, "run", fake)
    assert ka.find_alertmanagers(ka.Kubectl(), "mon") == [("mon", "am")]
    assert fake.calls[0][-4:] == ["-n", "mon", "-o", "json"]


def test_find_alertmanagers_without_operator(monkeypatch, capsys):
    monkeypatch.setattr(ka.subprocess, "run", FakeKubectl(list_rc=1))
    with pytest.raises(click.ClickException, match="is the Prometheus Operator installed"):
        ka.find_alertmanagers(ka.Kubectl(), None)


# --- impersonation / choose_alertmanager -------------------------------------------


@pytest.mark.parametrize(
    "as_user,no_as,openshift,expected",
    [
        (None, False, True, "system:admin"),
        (None, False, False, None),
        (None, True, True, None),
        ("me", False, True, "me"),
        ("me", False, False, "me"),
    ],
)
def test_impersonation(as_user, no_as, openshift, expected):
    assert ka.impersonation(as_user, no_as, openshift) == expected


def test_choose_single():
    assert ka.choose_alertmanager([ka.Alertmanager("a", "b")], False, None) == (
        "a",
        "b",
    )


def test_choose_none():
    with pytest.raises(click.ClickException, match="no Alertmanagers found in namespace 'ns'"):
        ka.choose_alertmanager([], False, "ns")


def test_choose_multiple_openshift_defaults_to_main(capsys):
    assert ka.choose_alertmanager([UW, MAIN], True, None) == MAIN
    assert "defaulting to openshift-monitoring" in capsys.readouterr().err


SYN = ka.Alertmanager("syn-monitoring", "alertmanager-monitoring")


def test_choose_multiple_non_openshift_defaults_to_syn(capsys):
    assert ka.choose_alertmanager([UW, SYN, ka.Alertmanager("mon", "am")], False, None) == SYN
    assert "defaulting to syn-monitoring" in capsys.readouterr().err


def test_choose_syn_is_not_the_default_on_openshift():
    with pytest.raises(click.ClickException, match="multiple Alertmanagers found"):
        ka.choose_alertmanager([UW, SYN], True, None)


def test_choose_multiple_elsewhere_errors():
    with pytest.raises(click.ClickException) as exc:
        ka.choose_alertmanager([UW, MAIN], False, None)
    assert exc.value.exit_code == 1
    assert "select one with -n (and -a if needed)" in exc.value.message
    assert "  openshift-monitoring\tmain" in exc.value.message


# --- exec command -----------------------------------------------------------


def test_alertmanager_str_and_statefulset():
    assert str(ka.Alertmanager("mon", "am")) == "mon\tam"
    assert ka.Alertmanager("mon", "am").statefulset == "alertmanager-am"
    assert SYN.statefulset == "alertmanager-alertmanager-monitoring"


@pytest.mark.parametrize("interactive", [True, False])
def test_exec_command(interactive):
    kc = ka.Kubectl(context="prod", as_user="me")
    cmd = ka.exec_command(kc, ka.Alertmanager("ns", "am"), "http://x:1", ["alert"], interactive)
    assert cmd == [
        "kubectl",
        "--context=prod",
        "--as=me",
        "-n",
        "ns",
        "exec",
        *(["-i"] if interactive else []),
        "sts/alertmanager-am",
        "-c",
        "alertmanager",
        "--",
        "amtool",
        "--alertmanager.url=http://x:1",
        "alert",
    ]


# --- end to end with mocked kubectl -----------------------------------------


def test_autodetect_openshift(run):
    fake = FakeKubectl(
        True,
        [
            am(MAIN.namespace, "main"),
            am(UW.namespace, "user-workload"),
            am("x", "scaled-down", replicas=0),
        ],
    )
    result, execs = run(["--context=prod", "alert", "query"], fake)
    assert result.exit_code == 0, result.output
    assert "defaulting to openshift-monitoring" in result.output
    get_call = fake.calls[1]
    assert get_call[:3] == ["kubectl", "--context=prod", "--as=system:admin"]
    assert get_call.index("get") < get_call.index("-A")
    # CliRunner's stdin is not a TTY, so -i is added
    assert execs == [
        [
            "kubectl",
            "--context=prod",
            "--as=system:admin",
            "-n",
            "openshift-monitoring",
            "exec",
            "-i",
            "sts/alertmanager-main",
            "-c",
            "alertmanager",
            "--",
            "amtool",
            URL,
            "alert",
            "query",
        ]
    ]


def test_explicit_alertmanager_skips_detection_and_no_as(run):
    fake = FakeKubectl(True)
    result, execs = run(["--no-as", "-n", UW.namespace, "-a", UW.name, "silence"], fake)
    assert result.exit_code == 0, result.output
    assert len(fake.calls) == 1  # only api-resources
    assert not any(a.startswith("--as") for a in execs[0])
    assert target(execs[0]) == (UW.namespace, "sts/alertmanager-user-workload")


def test_multiple_alertmanagers_elsewhere_errors(run):
    fake = FakeKubectl(False, [am(MAIN.namespace, "main"), am("mon", "am")])
    result, execs = run(["alert"], fake)
    assert result.exit_code == 1 and not execs
    assert "multiple Alertmanagers found" in result.output


@pytest.mark.parametrize(
    "argv,scope",
    [
        (["-l"], ["-A"]),
        (["-l", "-n", "mon"], ["-n", "mon"]),
        (["-l", "-n", "mon", "-a", "am"], ["-n", "mon", "am"]),
    ],
)
def test_list_hands_over_to_kubectl_get(run, argv, scope):
    fake = FakeKubectl(False)
    result, execs = run(argv, fake)
    assert result.exit_code == 0, result.output
    assert execs == [["kubectl", "get", "alertmanagers.monitoring.coreos.com", *scope]]
    assert len(fake.calls) == 1  # only the OpenShift check, no own listing


def test_list_uses_connection_options_and_impersonation(run):
    result, execs = run(["--context=prod", "-l"], FakeKubectl(True))
    assert result.exit_code == 0, result.output
    assert execs[0][:4] == ["kubectl", "--context=prod", "--as=system:admin", "get"]


def test_get_failure_is_reported(run):
    result, _ = run(["alert"], FakeKubectl(list_rc=1))
    assert result.exit_code == 1
    assert "Forbidden" in result.output and "cannot list Alertmanager resources" in result.output


def test_kubectl_missing(run, monkeypatch):
    monkeypatch.setattr(ka.shutil, "which", lambda _: None)
    result, _ = run(["alert"])
    assert result.exit_code == 1
    assert "kubectl not found" in result.output


# --- shell completion (kubectl_complete-amtool) -----------------------------

COMPLETION_ITEMS = [
    am(MAIN.namespace, "main"),
    am(UW.namespace, "user-workload"),
    am("mon", "am"),
    am("mon", "am-2"),
    am("web", "scaled-down", replicas=0),
]


@pytest.fixture
def complete(monkeypatch, capsys):
    """Run cobra_complete with a fake kubectl; returns (lines, fake)."""
    monkeypatch.setattr(ka.shutil, "which", lambda _: "/usr/bin/kubectl")

    def invoke(argv, fake=None):
        fake = fake or FakeKubectl(False, COMPLETION_ITEMS)
        monkeypatch.setattr(ka.subprocess, "run", fake)
        ka.cobra_complete(argv)
        captured = capsys.readouterr()
        assert captured.err == ""
        return captured.out.splitlines(), fake

    return invoke


def test_complete_namespace_only_with_alertmanagers(complete):
    lines, _ = complete(["-n", ""])
    assert lines == [
        "mon",
        "openshift-monitoring",
        "openshift-user-workload-monitoring",
        ":4",
    ]


@pytest.mark.parametrize(
    "argv",
    [
        ["-n", "openshift-m"],
        ["--namespace=openshift-m"],
        ["--namespace", "openshift-m"],
    ],
)
def test_complete_namespace_prefix(complete, argv):
    lines, _ = complete(argv)
    assert lines == ["openshift-monitoring", ":4"]


def test_complete_uses_typed_connection_options(complete):
    _, fake = complete(["--context=prod", "--kubeconfig", "/k", "-n", ""])
    get_call = fake.calls[-1]
    assert get_call[:3] == ["kubectl", "--context=prod", "--kubeconfig=/k"]
    assert "-A" in get_call


@pytest.mark.parametrize(
    "argv,expected_as",
    [
        (["-n", ""], "--as=system:admin"),
        (["--as=me", "-n", ""], "--as=me"),
        (["--no-as", "-n", ""], None),
    ],
)
def test_complete_impersonation_on_openshift(complete, argv, expected_as):
    _, fake = complete(argv, FakeKubectl(True, COMPLETION_ITEMS))
    as_flags = [a for a in fake.calls[-1] if a.startswith("--as")]
    assert as_flags == ([expected_as] if expected_as else [])


def test_complete_alertmanager_needs_namespace(complete):
    lines, fake = complete(["-a", ""])
    assert lines == [":4"]
    assert fake.calls == []


def test_complete_alertmanager_in_namespace(complete):
    lines, _ = complete(["-n", "mon", "-a", "am"])
    assert lines == ["am", "am-2", ":4"]


def test_complete_kubectl_failure_is_silent(complete):
    lines, _ = complete(["-n", ""], FakeKubectl(False, list_rc=1))
    assert lines == [":4"]


def test_complete_kubectl_missing_is_silent(complete, monkeypatch):
    monkeypatch.setattr(ka.shutil, "which", lambda _: None)
    lines, _ = complete(["-n", ""])
    assert lines == [":4"]


def test_complete_option_names(complete):
    lines, _ = complete(["--na"])
    assert lines == ["--namespace", ":4"]


# --- amtool argument completion ---------------------------------------------

ONE_ALERTMANAGER = [am("mon", "am")]


def exec_calls(fake):
    return [c for c in fake.calls if "exec" in c]


@pytest.mark.parametrize(
    "argv,expected",
    [
        (
            [""],
            [
                "alert",
                "silence",
                "check-config",
                "cluster",
                "config",
                "template",
                "help",
            ],
        ),
        (["-n", "mon", "si"], ["silence"]),
        (["silence", ""], ["add", "expire", "import", "query", "update"]),
        (["silence", "q"], ["query"]),
        (["alert", "--o"], ["--output"]),
        (["alert", "--output", ""], ["simple", "extended", "json"]),
        (["alert", "--output", "j"], ["json"]),
    ],
)
def test_complete_amtool_args(complete, argv, expected):
    lines, _ = complete(argv, FakeKubectl(False, ONE_ALERTMANAGER))
    assert lines == [*expected, ":4"]


def test_complete_amtool_args_exec_command(complete):
    fake = FakeKubectl(True, ONE_ALERTMANAGER)
    complete(["--context=prod", "-n", "mon", "silence", "query", ""], fake)
    assert exec_calls(fake) == [
        [
            "kubectl",
            "--context=prod",
            "--as=system:admin",
            "--request-timeout=5s",
            "-n",
            "mon",
            "exec",
            "sts/alertmanager-am",
            "-c",
            "alertmanager",
            "--",
            "amtool",
            "--completion-bash",
            "silence",
            "query",
        ]
    ]  # no trailing "" for the empty current word


@pytest.mark.parametrize(
    "incomplete,expected",
    [
        ("--o", ["--output"]),
        ("--ver", ["--verbose", "--version"]),
        (
            "-",
            [
                "--inhibited",
                "--silenced",
                "--active",
                "--help",
                "--verbose",
                "--alertmanager.url",
                "--output",
                "--timeout",
                "--version",
            ],
        ),
    ],
)
def test_complete_amtool_args_flags_via_double_dash(complete, incomplete, expected):
    fake = FakeKubectl(False, ONE_ALERTMANAGER)
    lines, _ = complete(["-n", "mon", "alert", incomplete], fake)
    assert lines == [*expected, ":4"]
    # the partly typed flag would make amtool fail; "--" lists all flags instead
    assert exec_calls(fake)[0][-3:] == ["--completion-bash", "alert", "--"]


def test_complete_amtool_args_keeps_partial_command_word_local(complete):
    fake = FakeKubectl(False, ONE_ALERTMANAGER)
    lines, _ = complete(["-n", "mon", "silence", "ex"], fake)
    assert lines == ["expire", ":4"]
    assert exec_calls(fake)[0][-2:] == ["--completion-bash", "silence"]


def test_complete_amtool_args_explicit_alertmanager_skips_discovery(complete):
    fake = FakeKubectl(False, ONE_ALERTMANAGER)
    lines, _ = complete(["-n", "other", "-a", "am-x", "alert", ""], fake)
    assert lines == ["add", "query", ":4"]
    assert not any("get" in c for c in fake.calls)
    assert exec_calls(fake)[0][exec_calls(fake)[0].index("exec") + 1] == "sts/alertmanager-am-x"


def test_complete_amtool_args_uses_default_alertmanager(complete):
    fake = FakeKubectl(False, [am("syn-monitoring", "alertmanager-monitoring"), *ONE_ALERTMANAGER])
    complete(["alert", ""], fake)
    assert exec_calls(fake)[0][exec_calls(fake)[0].index("-n") + 1] == "syn-monitoring"


def test_complete_amtool_args_no_suggestions_allows_files(complete):
    # e.g. `silence import FILE`: amtool has nothing, so the shell completes file names
    lines, _ = complete(["silence", "import", ""], FakeKubectl(False, ONE_ALERTMANAGER))
    assert lines == [":0"]


@pytest.mark.parametrize(
    "fake",
    [
        FakeKubectl(False, ONE_ALERTMANAGER, exec_rc=1),  # exec fails
        FakeKubectl(False, [*ONE_ALERTMANAGER, am("x", "am2")]),  # ambiguous choice
        FakeKubectl(False, [], list_rc=1),  # cannot list
    ],
)
def test_complete_amtool_args_failures_are_silent(complete, fake):
    lines, _ = complete(["silence", ""], fake)
    assert lines == [":0"]


def test_complete_amtool_args_timeout_is_silent(complete, monkeypatch):
    def slow(cmd, **kwargs):
        if "exec" in cmd:
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))
        return FakeKubectl(False, ONE_ALERTMANAGER)(cmd)

    lines, _ = complete(["silence", ""], slow)
    assert lines == [":0"]


@pytest.mark.parametrize("argv", [["-n", ""], ["-n", "mon", "-a", ""], ["silence", ""]])
def test_complete_kubectl_calls_use_request_timeout(complete, argv):
    _, fake = complete(argv, FakeKubectl(True, ONE_ALERTMANAGER))
    assert fake.calls
    for call in fake.calls:
        assert "--request-timeout=5s" in call


def test_run_has_no_request_timeout(run):
    _, execs = run(["alert"], FakeKubectl(True, ONE_ALERTMANAGER))
    assert not any(a.startswith("--request-timeout") for a in execs[0])


def test_complete_plugin_options_still_work_before_amtool_args(complete):
    lines, fake = complete(["--na"])
    assert lines == ["--namespace", ":4"]
    assert exec_calls(fake) == []
