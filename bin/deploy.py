#!/usr/bin/env python3
"""Deploy the agent-relay hub to a NEW public Railway project.

Creates project + environment + service + persistent volume (/srv/data for
SQLite) via Railway GraphQL, uploads the source tarball through the direct
code-upload endpoint, creates the public domain, and polls until live.

Usage: python3 bin/deploy.py [--redeploy]
Re-running without --redeploy creates ANOTHER project; for redeploys fill in
PROJECT_ID/ENV_ID/SERVICE_ID below and pass --redeploy (uploads a fresh
tarball only).

Uses the stored custom.railway credential via authd surrogates.
"""
from __future__ import annotations

import io
import json
import sys
import tarfile
import time
import urllib.error
import urllib.request

sys.path.insert(0, "/opt/hatch/skills/skill-creator/bin")
from dynamic_credentials import (  # noqa: E402
    add_surrogate_to_request,
    read_json_response,
)

WORKSPACE_ID = "be0a9dd0-e6b4-4184-8b78-df5d3a4d957d"
PROJECT_NAME = "agent-relay"
SERVICE_NAME = "hub"
VOLUME_MOUNT = "/srv/data"  # ./data/relay.db lives here (WORKDIR /srv)

# Filled in after first deploy; set these + pass --redeploy to skip creation.
PROJECT_ID = "3c467a0f-ba43-4a79-8cff-0564120f19b5"
ENV_ID = "7568fa27-ade1-4d1e-bef2-55bb2a535dc0"
SERVICE_ID = "f74fd7b2-2c21-4c09-8128-4f9d0f61a903"
PUBLIC_URL = "https://hub-production-4d2b.up.railway.app"

SRC_DIR = "/home/hatch/workspace/agent-relay"
FILES = ["app.py", "requirements.txt", "Dockerfile"]

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
ALLOWED_HOSTS = ["backboard.railway.com"]
GRAPHQL_URL = "https://backboard.railway.com/graphql/v2"


def gql(query, variables=None):
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(GRAPHQL_URL, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", UA)
    req.add_header("Origin", "https://railway.com")
    req.add_header("Referer", "https://railway.com/")
    add_surrogate_to_request(req, "custom.railway", allowed_hosts=ALLOWED_HOSTS)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = read_json_response(resp)
    except urllib.error.HTTPError as exc:
        print(f"GraphQL HTTP {exc.code}: {exc.read()[:500]}", file=sys.stderr)
        sys.exit(1)
    if payload.get("errors"):
        print(f"GraphQL errors: {json.dumps(payload['errors'])[:800]}",
              file=sys.stderr)
        sys.exit(1)
    return payload["data"]


def create_project():
    d = gql(
        "mutation($in: ProjectCreateInput!) { projectCreate(input: $in) { id } }",
        {"in": {"name": PROJECT_NAME, "workspaceId": WORKSPACE_ID}},
    )
    return d["projectCreate"]["id"]


def get_or_create_environment(project_id):
    d = gql(
        "{ project(id: \"" + project_id + "\")"
        " { environments { edges { node { id name } } } } }"
    )
    edges = d["project"]["environments"]["edges"]
    if edges:
        return edges[0]["node"]["id"]
    d = gql(
        "mutation($in: EnvironmentCreateInput!) { environmentCreate(input: $in) { id } }",
        {"in": {"projectId": project_id, "name": "production"}},
    )
    return d["environmentCreate"]["id"]


def create_service(project_id, env_id):
    d = gql(
        "mutation($in: ServiceCreateInput!) { serviceCreate(input: $in) { id } }",
        {"in": {"projectId": project_id, "environmentId": env_id,
                "name": SERVICE_NAME}},
    )
    return d["serviceCreate"]["id"]


def create_volume(project_id, env_id, service_id):
    d = gql(
        "mutation($in: VolumeCreateInput!) { volumeCreate(input: $in) { id } }",
        {"in": {"projectId": project_id, "environmentId": env_id,
                "serviceId": service_id, "mountPath": VOLUME_MOUNT}},
    )
    return d["volumeCreate"]["id"]


def build_tarball() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name in FILES:
            tar.add(f"{SRC_DIR}/{name}", arcname=f"./{name}")
    return buf.getvalue()


def upload(project_id, env_id, service_id) -> dict:
    url = (f"https://backboard.railway.com/project/{project_id}/environment/"
           f"{env_id}/up?serviceId={service_id}")
    body = build_tarball()
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", "application/gzip")
    request.add_header("Accept", "application/json")
    request.add_header("User-Agent", UA)
    request.add_header("Origin", "https://railway.com")
    request.add_header("Referer", "https://railway.com/")
    add_surrogate_to_request(request, "custom.railway",
                             allowed_hosts=ALLOWED_HOSTS)
    try:
        with urllib.request.urlopen(request, timeout=180) as resp:
            return read_json_response(resp)
    except urllib.error.HTTPError as exc:
        print(f"upload HTTP {exc.code}: {exc.read()[:500]}", file=sys.stderr)
        sys.exit(1)


def latest_deployment(service_id):
    d = gql(
        "{ service(id: \"" + service_id + "\") { deployments(first: 3) "
        "{ edges { node { id status createdAt } } } } }"
    )
    edges = d["service"]["deployments"]["edges"]
    return [e["node"] for e in edges]


def wait_for_live(service_id, timeout_s=600):
    deadline = time.time() + timeout_s
    seen = set()
    while time.time() < deadline:
        deps = latest_deployment(service_id)
        for dep in deps:
            if dep["id"] not in seen:
                seen.add(dep["id"])
                print(f"deployment {dep['id'][:8]}... status={dep['status']}")
        if deps and deps[0]["status"] in ("SUCCESS", "FAILED", "CRASHED"):
            return deps[0]
        time.sleep(15)
    print("timed out waiting for deployment", file=sys.stderr)
    sys.exit(1)


def create_domain(env_id, service_id):
    d = gql(
        "mutation($in: ServiceDomainCreateInput!) "
        "{ serviceDomainCreate(input: $in) { domain } }",
        {"in": {"environmentId": env_id, "serviceId": service_id}},
    )
    return d["serviceDomainCreate"]["domain"]


def main() -> int:
    redeploy = "--redeploy" in sys.argv
    if redeploy and not (PROJECT_ID and ENV_ID and SERVICE_ID):
        print("set PROJECT_ID/ENV_ID/SERVICE_ID for --redeploy", file=sys.stderr)
        return 1

    if redeploy:
        project_id, env_id, service_id = PROJECT_ID, ENV_ID, SERVICE_ID
    else:
        print("creating Railway project...")
        project_id = create_project()
        print("project:", project_id)
        env_id = get_or_create_environment(project_id)
        print("environment:", env_id)
        service_id = create_service(project_id, env_id)
        print("service:", service_id)
        vol_id = create_volume(project_id, env_id, service_id)
        print("volume:", vol_id, "mounted at", VOLUME_MOUNT)
        print("SAVE THESE: PROJECT_ID=%s ENV_ID=%s SERVICE_ID=%s"
              % (project_id, env_id, service_id))

    print("uploading tarball (%d files)..." % len(FILES))
    up = upload(project_id, env_id, service_id)
    print("upload response:", json.dumps(up)[:300])

    print("waiting for deployment to go live...")
    dep = wait_for_live(service_id)
    print("final status:", dep["status"])
    if dep["status"] != "SUCCESS":
        return 1

    if not redeploy:
        domain = create_domain(env_id, service_id)
        url = f"https://{domain}"
        print("PUBLIC URL:", url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
