import threading
import time

from pico import FakeModelClient, Pico, SessionStore, WorkspaceContext
from pico.web import ASSET_PATHS, ApprovalBroker, WebRuntime


def build_agent(tmp_path, outputs, approval_policy="ask"):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    agent = Pico(
        model_client=FakeModelClient(outputs),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".pico" / "sessions"),
        approval_policy=approval_policy,
    )
    agent.web_config = {
        "workspace": str(tmp_path),
        "provider": "deepseek",
        "model": "fake-model",
        "endpoint": "",
    }
    return agent


def test_web_runtime_runs_message_and_exposes_state(tmp_path):
    runtime = WebRuntime(build_agent(tmp_path, ["<final>Web answer.</final>"]))

    runtime.submit("Use the web runtime")

    assert runtime.wait(2)
    state = runtime.state()
    assert state["runtime"]["active"] is False
    assert state["runtime"]["last_answer"] == "Web answer."
    assert state["task_state"]["status"] == "completed"
    assert [item["role"] for item in state["history"]] == ["user", "assistant"]


def test_approval_broker_waits_for_browser_decision():
    broker = ApprovalBroker(timeout=2)
    result = []
    worker = threading.Thread(target=lambda: result.append(broker.request("run_shell", {"command": "echo ok"})))
    worker.start()

    deadline = time.monotonic() + 1
    pending = None
    while time.monotonic() < deadline and pending is None:
        pending = broker.snapshot()
        time.sleep(0.01)

    assert pending["tool"] == "run_shell"
    assert broker.resolve(pending["id"], True) is True
    worker.join(1)
    assert result == [True]
    assert broker.snapshot() is None


def test_web_runtime_uses_browser_approval_callback(tmp_path):
    runtime = WebRuntime(build_agent(tmp_path, [], approval_policy="ask"))
    result = []
    worker = threading.Thread(
        target=lambda: result.append(
            runtime.agent.run_tool("write_file", {"path": "approved.txt", "content": "yes\n"})
        )
    )
    worker.start()

    deadline = time.monotonic() + 1
    pending = None
    while time.monotonic() < deadline and pending is None:
        pending = runtime.approvals.snapshot()
        time.sleep(0.01)

    assert pending["tool"] == "write_file"
    runtime.approvals.resolve(pending["id"], True)
    worker.join(1)
    assert result == ["wrote approved.txt (4 chars)"]
    assert (tmp_path / "approved.txt").read_text(encoding="utf-8") == "yes\n"


def test_web_runtime_can_create_and_switch_sessions(tmp_path):
    runtime = WebRuntime(build_agent(tmp_path, []))
    original_id = runtime.agent.session["id"]

    new_id = runtime.new_session()
    assert new_id != original_id
    assert runtime.switch_session(original_id) == original_id
    assert runtime.state()["agent"]["session_id"] == original_id
    assert runtime.state()["agent"]["configuration"]["model"] == "fake-model"


def test_web_runtime_rebuilds_agent_when_workspace_or_model_changes(tmp_path):
    other_workspace = tmp_path / "other-workspace"
    other_workspace.mkdir()
    (other_workspace / "README.md").write_text("other\n", encoding="utf-8")
    builds = []

    def factory(settings):
        workspace = WorkspaceContext.build(settings["workspace"])
        agent = Pico(
            model_client=FakeModelClient([]),
            workspace=workspace,
            session_store=SessionStore(workspace.repo_root + "/.pico/sessions"),
            approval_policy="ask",
        )
        agent.web_config = dict(settings)
        builds.append(agent)
        return agent

    runtime = WebRuntime(build_agent(tmp_path, []), agent_factory=factory)
    previous_id = runtime.agent.session["id"]

    next_id = runtime.configure(
        {
            "workspace": str(other_workspace),
            "provider": "ollama",
            "model": "qwen3.5:4b",
            "endpoint": "http://127.0.0.1:11434",
        }
    )

    assert len(builds) == 1
    assert next_id != previous_id
    state = runtime.state()
    assert state["agent"]["workspace"] == str(other_workspace)
    assert state["agent"]["configuration"] == {
        "workspace": str(other_workspace),
        "provider": "ollama",
        "model": "qwen3.5:4b",
        "endpoint": "http://127.0.0.1:11434",
    }


def test_web_runtime_rejects_session_path_escape(tmp_path):
    runtime = WebRuntime(build_agent(tmp_path, []))

    try:
        runtime.switch_session("../outside")
    except ValueError as exc:
        assert "invalid characters" in str(exc)
    else:
        raise AssertionError("session path escape was not rejected")


def test_web_assets_are_packaged_locally():
    assert set(ASSET_PATHS) == {"/", "/app.js", "/styles.css", "/lucide.min.js"}
    assert all(path.is_file() and path.stat().st_size > 0 for path in ASSET_PATHS.values())


def test_cli_parser_accepts_web_options():
    from pico.cli import build_arg_parser

    args = build_arg_parser().parse_args(["--web", "--web-host", "127.0.0.1", "--web-port", "9876"])

    assert args.web is True
    assert args.web_host == "127.0.0.1"
    assert args.web_port == 9876


def test_cli_web_factory_applies_workspace_and_model_settings(tmp_path):
    from pico.cli import build_arg_parser, build_web_agent_factory

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_text("demo\n", encoding="utf-8")
    args = build_arg_parser().parse_args(["--cwd", str(tmp_path), "--provider", "ollama"])

    agent = build_web_agent_factory(args)(
        {
            "workspace": str(workspace),
            "provider": "ollama",
            "model": "qwen3.5:4b",
            "endpoint": "http://127.0.0.1:11435",
        }
    )

    assert agent.workspace.cwd == str(workspace)
    assert agent.model_client.model == "qwen3.5:4b"
    assert agent.model_client.host == "http://127.0.0.1:11435"
    assert agent.web_config["provider"] == "ollama"


def test_session_title_is_derived_from_the_first_user_request(tmp_path):
    agent = build_agent(tmp_path, ["<final>Done.</final>"])

    agent.ask("Inspect the authentication failure in login.py")

    assert agent.session["title"] == "Inspect the authentication failure in login.py"
    assert agent.session_store.load(agent.session["id"])["title"] == agent.session["title"]
    agent.reset()
    assert agent.session["title"] == ""


def test_web_session_list_prefers_the_persisted_title(tmp_path):
    agent = build_agent(tmp_path, ["<final>Done.</final>"])
    agent.ask("Give this session a meaningful title")
    agent.session["history"][0]["content"] = "A different historical request"
    agent.session_store.save(agent.session)

    state = WebRuntime(agent).state()

    assert state["sessions"][0]["title"] == "Give this session a meaningful title"
