"""Static checks of the generated n8n workflow, and of the JavaScript unit tests."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = json.loads((ROOT / "workflows" / "case-triage.json").read_text(encoding="utf8"))
NODES = {n["name"]: n for n in WORKFLOW["nodes"]}


def test_the_committed_workflow_matches_the_generator():
    import importlib.util
    spec = importlib.util.spec_from_file_location("build_workflow", ROOT / "scripts" / "build_workflow.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert json.loads(json.dumps(module.WORKFLOW)) == WORKFLOW, "run: python scripts/build_workflow.py"


def test_node_ids_and_names_are_unique():
    assert len({n["id"] for n in WORKFLOW["nodes"]}) == len(WORKFLOW["nodes"])
    assert len(NODES) == len(WORKFLOW["nodes"])


def test_every_connection_points_at_a_real_node():
    for source, outputs in WORKFLOW["connections"].items():
        assert source in NODES
        for group in outputs["main"]:
            for edge in group:
                assert edge["node"] in NODES, f"{source} -> {edge['node']}"


def test_every_node_is_reachable_from_the_webhook():
    seen, todo = set(), ["Webhook"]
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        todo += [e["node"] for g in WORKFLOW["connections"].get(name, {"main": []})["main"] for e in g]
    assert seen == set(NODES)


def test_the_webhook_waits_for_a_respond_node_and_every_branch_ends_in_a_response():
    assert NODES["Webhook"]["parameters"]["responseMode"] == "responseNode"
    leaves = [n for n in NODES if n not in WORKFLOW["connections"]]
    assert sorted(leaves) == ["Respond 201", "Respond 400", "Respond 502"]
    assert all(NODES[n]["type"] == "n8n-nodes-base.respondToWebhook" for n in leaves)


def test_external_calls_retry_and_never_crash_the_run():
    for name in ("LLM Triage", "Find Account", "Create Case"):
        n = NODES[name]
        assert n["retryOnFail"] is True and n["maxTries"] >= 2
        assert n["onError"] == "continueRegularOutput"
        assert n["parameters"]["options"]["timeout"] > 0


def test_no_secrets_are_hard_coded():
    text = json.dumps(WORKFLOW)
    assert not re.search(r"(sk-[A-Za-z0-9]{10,}|gsk_[A-Za-z0-9]{10,}|hf_[A-Za-z0-9]{10,}|Bearer [A-Za-z0-9._-]{20,})", text)
    for name in ("LLM Triage", "Find Account", "Create Case"):
        auth = NODES[name]["parameters"]["headerParameters"]["parameters"][0]["value"]
        assert "$env." in auth


def test_code_nodes_carry_the_tested_library_not_a_copy_that_can_drift():
    lib = (ROOT / "nodes_js" / "lib.js").read_text(encoding="utf8")
    body = lib[: lib.index("if (typeof module")]
    for name, node in NODES.items():
        if node["type"] == "n8n-nodes-base.code":
            assert body.strip() in node["parameters"]["jsCode"], name


def test_the_node_branches_are_wired_true_then_false():
    assert WORKFLOW["connections"]["Valid?"]["main"][0][0]["node"] == "Build LLM Request"
    assert WORKFLOW["connections"]["Valid?"]["main"][1][0]["node"] == "Respond 400"
    assert WORKFLOW["connections"]["Created?"]["main"][0][0]["node"] == "Respond 201"
    assert WORKFLOW["connections"]["Created?"]["main"][1][0]["node"] == "Respond 502"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js not installed")
def test_the_javascript_unit_tests_pass():
    result = subprocess.run(["node", "--test", "tests/js/lib.test.js"], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout[-2000:]
    assert re.search(r"# fail 0", result.stdout)
