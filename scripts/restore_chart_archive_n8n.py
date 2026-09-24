"""Restore the original Chart-Img -> GCS -> Supabase archive after webhook response.

This script never prints session values. Run with --dry-run first. For --apply,
provide the TradingView session ID and signature on separate stdin lines.
"""

import argparse
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import uuid


REFERENCE = Path(__file__).resolve().parents[1] / "n8n" / "sim" / "chart-archive-template.json"
CONTAINER = "n8n-docker-caddy-n8n-1"
IDS = {
    "7a86d6d4684846ea": "5m",
    "1449a84ff77e4f11": "5m",
    "096d30bc2c0a4ccd": "5m",
    "92060dd327f8431e": "15m",
    "db4dd0b4058d47ef": "30m",
}
NAMES = {
    "Supabase3": "Chart cache",
    "If1": "Cached chart fresh?",
    "HTTP Request2": "Fetch cached chart image",
    "Tradingview Chart1": "Capture TradingView chart",
    "Supabase4": "Update chart cache",
    "HTTP Request3": "Fetch new chart image",
    "Upload to GCS": "Upload chart to GCS",
    "Edit Fields2": "Build archive URL",
    "Supabase1": "Write archive URL",
}
SECRET_VALUES = []


def ssh(*args, input_bytes=None, timeout=90):
    ssh_command = (["wsl.exe", "--exec", "ssh"] if os.name == "nt" else ["ssh"])
    run = subprocess.run([*ssh_command, "-o", "BatchMode=yes", "pi", *args],
                         input=input_bytes, capture_output=True, timeout=timeout)
    if run.returncode:
        error = (run.stdout + run.stderr).decode(errors="replace")[-1200:]
        for secret in SECRET_VALUES:
            error = error.replace(secret, "<redacted>")
        raise RuntimeError(f"SSH command failed (code {run.returncode}): {error}")
    return run.stdout


def export_all():
    raw = ssh("docker", "exec", CONTAINER, "n8n", "export:workflow", "--all")
    output = raw.decode(errors="replace")
    return json.JSONDecoder().raw_decode(output[output.find("["):])[0]


def edge(name):
    return {"node": name, "type": "main", "index": 0}


def harden_archive(workflow, reference):
    nodes = {node["name"]: node for node in workflow["nodes"]}
    if "Archive uploaded?" in nodes:
        return workflow
    check = copy.deepcopy(next(n for n in reference["nodes"] if n["name"] == "If1"))
    check["id"] = str(uuid.uuid4())
    check["name"] = "Archive uploaded?"
    check["position"] = [nodes["Upload chart to GCS"]["position"][0] + 260,
                         nodes["Upload chart to GCS"]["position"][1]]
    condition = check["parameters"]["conditions"]["conditions"][0]
    condition["leftValue"] = "={{ $json.error ? 1 : 0 }}"
    condition["rightValue"] = 0
    condition["operator"] = {"type": "number", "operation": "equals"}
    nodes["Upload chart to GCS"]["onError"] = "continueRegularOutput"
    workflow["nodes"].append(check)
    workflow["connections"]["Upload chart to GCS"] = {
        "main": [[edge("Archive uploaded?")]],
    }
    workflow["connections"]["Archive uploaded?"] = {
        "main": [[edge("Build archive URL")], []],
    }
    workflow["versionId"] = str(uuid.uuid4())
    return workflow


def build(workflow, reference, timeframe, session_id, session_sign):
    workflow = copy.deepcopy(workflow)
    nodes = {node["name"]: node for node in workflow["nodes"]}
    ref_nodes = {node["name"]: node for node in reference["nodes"]}
    assert "Respond to Webhook" in nodes and "Supabase" in nodes
    if "Chart cache" in nodes:
        if "Archive uploaded?" in nodes:
            assert len(workflow["nodes"]) == 25
            return workflow
        assert len(workflow["nodes"]) == 24
        workflow = harden_archive(workflow, reference)
        assert len(workflow["nodes"]) == 25
        return workflow
    assert not any(name in nodes for name in NAMES.values()), "archive path already present"
    assert workflow["connections"]["Build Response"]["main"][0][0]["node"] == "Respond to Webhook"
    assert len(workflow["connections"]["Supabase"]["main"][0]) == 1

    response_xy = nodes["Respond to Webhook"]["position"]
    positions = {
        "Supabase3": (260, 220), "If1": (500, 220),
        "HTTP Request2": (760, 80), "Tradingview Chart1": (760, 360),
        "Supabase4": (1020, 360), "HTTP Request3": (1280, 360),
        "Upload to GCS": (1540, 220), "Edit Fields2": (1800, 220),
        "Supabase1": (2060, 220),
    }
    for old, new in NAMES.items():
        node = copy.deepcopy(ref_nodes[old])
        node["id"] = str(uuid.uuid4())
        node["name"] = new
        dx, dy = positions[old]
        node["position"] = [response_xy[0] + dx, response_xy[1] + dy]
        if old in ("Supabase3", "Supabase4", "Supabase1"):
            node["credentials"] = copy.deepcopy(nodes["Supabase"]["credentials"])
        workflow["nodes"].append(node)
        nodes[new] = node

    nodes["Chart cache"]["parameters"]["filters"]["conditions"][0]["keyValue"] = timeframe
    nodes["Cached chart fresh?"]["parameters"]["conditions"]["conditions"][0]["rightValue"] = {
        "5m": 4.8, "15m": 14.8, "30m": 29.8,
    }[timeframe]
    nodes["Fetch cached chart image"]["parameters"]["url"] = (
        "={{ $('Chart cache').item.json.chart_url }}"
    )
    for item in nodes["Capture TradingView chart"]["parameters"]["headerParameters"]["parameters"]:
        if item["name"] == "tradingview-session-id":
            item["value"] = session_id
        elif item["name"] == "tradingview-session-id-sign":
            item["value"] = session_sign
    for item in nodes["Capture TradingView chart"]["parameters"]["bodyParameters"]["parameters"]:
        if item["name"] == "interval":
            item["value"] = timeframe
    for item in nodes["Update chart cache"]["parameters"]["filters"]["conditions"]:
        if item["keyName"] == "timeframe":
            item["keyValue"] = timeframe
    nodes["Upload chart to GCS"]["parameters"]["objectName"] = (
        f"=charts/{{{{ $('Supabase').first().json.ai_decision_id }}}}/{timeframe}.jpg"
    )
    for item in nodes["Build archive URL"]["parameters"]["assignments"]["assignments"]:
        if item["name"] == "url":
            item["value"] = (
                "=https://storage.googleapis.com/tradingview-chart/charts/"
                f"{{{{ $('Supabase').first().json.ai_decision_id }}}}/{timeframe}.jpg"
            )
        elif item["name"] == "timeframe":
            item["value"] = timeframe
        elif item["name"] == "id":
            item["value"] = "={{ $('Supabase').first().json.ai_decision_id }}"

    # Respond to the broker before any Chart-Img, GCS, or archive DB work.
    workflow["connections"]["Respond to Webhook"] = {"main": [[edge("Chart cache")]]}
    workflow["connections"]["Chart cache"] = {"main": [[edge("Cached chart fresh?")]]}
    workflow["connections"]["Cached chart fresh?"] = {
        "main": [[edge("Fetch cached chart image")], [edge("Capture TradingView chart")]],
    }
    workflow["connections"]["Fetch cached chart image"] = {
        "main": [[edge("Upload chart to GCS")]],
    }
    workflow["connections"]["Capture TradingView chart"] = {
        "main": [[edge("Update chart cache")]],
    }
    workflow["connections"]["Update chart cache"] = {
        "main": [[edge("Fetch new chart image")]],
    }
    workflow["connections"]["Fetch new chart image"] = {
        "main": [[edge("Upload chart to GCS")]],
    }
    workflow["connections"]["Upload chart to GCS"] = {
        "main": [[edge("Build archive URL")]],
    }
    workflow["connections"]["Build archive URL"] = {
        "main": [[edge("Write archive URL")]],
    }
    workflow["versionId"] = str(uuid.uuid4())
    workflow["versionMetadata"] = {
        "name": f"Restore {timeframe} chart archive",
        "description": "Capture or reuse TradingView chart after decision response; archive to GCS and log URL.",
    }
    names = [node["name"] for node in workflow["nodes"]]
    assert len(names) == len(set(names))
    workflow = harden_archive(workflow, reference)
    assert len(workflow["nodes"]) == 25
    return workflow


def stage_and_import(workflow):
    remote_file = f"/tmp/chart-archive-{workflow['id']}-{uuid.uuid4().hex[:8]}.json"
    try:
        payload = json.dumps([workflow], ensure_ascii=False).encode()
        ssh("docker", "exec", "-i", CONTAINER, "sh", "-c",
            shlex.quote(f"umask 077; cat > {remote_file}"), input_bytes=payload)
        ssh("docker", "exec", CONTAINER, "test", "-s", remote_file)
        ssh("docker", "exec", CONTAINER, "n8n", "import:workflow",
            f"--input={remote_file}")
        ssh("docker", "exec", CONTAINER, "n8n", "publish:workflow",
            f"--id={workflow['id']}")
    finally:
        ssh("docker", "exec", CONTAINER, "rm", "-f", remote_file)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--id", choices=IDS.keys(), action="append")
    args = parser.parse_args()
    session_id = session_sign = "<session supplied on apply>"
    if args.apply:
        session_id = sys.stdin.readline().strip()
        session_sign = sys.stdin.readline().strip()
        if not session_id or not session_sign:
            raise SystemExit("Missing session values")
        SECRET_VALUES.extend((session_id, session_sign))
    reference = json.loads(REFERENCE.read_text())
    live = export_all()
    selected = args.id or list(IDS)
    by_id = {item["id"]: item for item in live}
    plans = [build(by_id[workflow_id], reference, IDS[workflow_id],
                   session_id, session_sign) for workflow_id in selected]
    for item in plans:
        print(json.dumps({"id": item["id"], "name": item["name"],
                          "nodes": len(item["nodes"]), "active": item["active"],
                          "change_needed": item != by_id[item["id"]]}))
    if not args.apply:
        return
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = "/home/thetopham/.config/tradingview-bot/n8n-backups"
    ssh("mkdir", "-p", backup_dir)
    backup_file = f"{backup_dir}/before-chart-archive-{stamp}.json"
    ssh("sh", "-c", shlex.quote(f"umask 077; cat > {backup_file}"),
        input_bytes=json.dumps([by_id[workflow_id] for workflow_id in selected]).encode())
    print(json.dumps({"backup": backup_file, "workflow_count": len(plans)}))
    for item in plans:
        if item == by_id[item["id"]]:
            print(json.dumps({"already_current": item["id"]}))
            continue
        stage_and_import(item)
        print(json.dumps({"imported_and_published": item["id"]}))


if __name__ == "__main__":
    main()
