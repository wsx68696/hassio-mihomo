#!/usr/bin/env python3
"""Use the existing Git credential manager without printing credentials."""
import argparse
import base64
import ipaddress
import io
import json
from http.client import HTTPException
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, ProxyHandler, HTTPRedirectHandler, build_opener
import zipfile

REPO = "wsx68696/hassio-mihomo"


def trusted_download(url):
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    approved = (host in {"github.com", "api.github.com", "codeload.github.com", "githubusercontent.com"}
                or host.endswith(".githubusercontent.com")
                or re.fullmatch(r"productionresultssa\d+\.blob\.core\.windows\.net", host))
    if (parsed.scheme != "https" or not approved or parsed.username or parsed.password or parsed.port not in {None, 443}):
        raise RuntimeError("Unsafe GitHub download redirect")
    return parsed


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        return None


def client(proxy=None):
    credential = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n", text=True,
        capture_output=True, check=True,
    )
    fields = dict(line.split("=", 1) for line in credential.stdout.splitlines() if "=" in line)
    token = os.environ.get("GH_TOKEN") or fields.get("password")
    if not token:
        raise RuntimeError("No existing GitHub credential is available")
    handler = ProxyHandler({"https": proxy, "http": proxy}) if proxy else ProxyHandler()
    opener = build_opener(handler, NoRedirect())

    def call_once(path, method="GET", payload=None, *, raw=False):
        if not path.startswith("/") or ".." in path:
            raise ValueError("Invalid API path")
        request = Request("https://api.github.com" + path,
                          data=None if payload is None else json.dumps(payload).encode(), method=method,
                          headers={"Authorization": "Bearer " + token,
                                   "Accept": "application/vnd.github+json",
                                   "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "haos-mihomo-build"})
        try:
            with opener.open(request, timeout=45) as response:
                content = response.read()
                return content.replace(token.encode(), b"[redacted]") if raw else json.loads(content) if content else None
        except HTTPError as error:
            if raw and error.code in (301, 302, 303, 307, 308):
                destination = error.headers.get("Location", "")
                parsed = trusted_download(destination)
                # Signed log URLs never receive the GitHub credential.
                binary = "curl.exe" if os.name == "nt" else "curl"
                routes = []
                if proxy:
                    lookup = subprocess.run([binary, "--silent", "--fail", "--proxy", proxy, "--max-time", "15",
                                             "https://dns.google/resolve?" + urlencode({"name": parsed.hostname, "type": "A"})],
                                            capture_output=True, timeout=20)
                    if lookup.returncode == 0:
                        answers = json.loads(lookup.stdout).get("Answer", [])
                        addresses = [str(ipaddress.IPv4Address(item["data"])) for item in answers if item.get("type") == 1]
                        for address in addresses[:2]:
                            routes += [["--noproxy", "*", "--resolve", f"{parsed.hostname}:443:{address}"],
                                       ["--proxy", proxy, "--connect-to", f"{parsed.hostname}:443:{address}:443"]]
                    routes += [["--proxy", proxy]]
                else:
                    routes = [["--noproxy", "*"]]
                errors = []
                for route in routes:
                    transfer = subprocess.run([binary, "--silent", "--show-error", "--fail", "--proto", "=https",
                                               "--write-out", "\n%{http_code}", "--connect-timeout", "10", "--max-time", "30", *route, destination],
                                              capture_output=True, timeout=55)
                    body, _, status = transfer.stdout.rpartition(b"\n")
                    if transfer.returncode == 0 and status.startswith(b"2") and len(body) <= 50 * 1024 * 1024:
                        return body.replace(token.encode(), b"[redacted]")
                    errors.append(transfer.stderr.decode("utf-8", errors="replace").replace(destination, "[signed-url]")[-500:])
                raise RuntimeError(f"Signed log host {parsed.hostname}: " + " | ".join(errors)) from None
            raise RuntimeError(f"GitHub API {method} {path}: HTTP {error.code}") from None

    def call(path, method="GET", payload=None, *, raw=False):
        for attempt in range(4):
            try:
                return call_once(path, method, payload, raw=raw)
            except (OSError, HTTPException):
                if method != "GET" or attempt == 3:
                    raise
                time.sleep(2 * (attempt + 1))
    return call


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--proxy")
    parser.add_argument("mode", choices=["identity", "upstreams", "geodata", "contracts", "action-pins", "runs", "run", "dispatch", "default-branch", "job-logs", "artifact", "rerun", "cancel", "release", "licenses"])
    parser.add_argument("--run-id", type=int)
    parser.add_argument("--ref", default="main")
    parser.add_argument("--job-id", type=int)
    parser.add_argument("--artifact-id", type=int)
    args = parser.parse_args()
    call = client(args.proxy)
    if args.mode == "identity":
        repo = call(f"/repos/{REPO}")
        result = {key: repo[key] for key in ("full_name", "private", "default_branch", "permissions")}
        result["owner"] = {key: repo["owner"][key] for key in ("login", "id")}
    elif args.mode == "upstreams":
        result = {}
        for name, repo, tag in [
            ("haos", "home-assistant/operating-system", "18.4"),
            ("mihomo", "MetaCubeX/mihomo", "v1.19.34"),
            ("zashboard", "Zephyruso/zashboard", "latest"),
        ]:
            release = call(f"/repos/{repo}/releases/" + ("latest" if tag == "latest" else "tags/" + tag))
            result[name] = {"tag": release["tag_name"], "body": release.get("body") if name == "haos" else None, "asset_count": len(release["assets"]), "assets": [
                {key: asset.get(key) for key in ("name", "size", "digest", "browser_download_url")}
                for asset in release["assets"]
                if asset["name"] in ("dist.zip", "SHA256SUMS", "SHA256SUMS.sigstore.json")
                or "generic-x86-64" in asset["name"]
                or "ova-" in asset["name"]
                or asset["name"] == "mihomo-linux-amd64-compatible-v1.19.34.gz"
            ]}
    elif args.mode == "geodata":
        release = call("/repos/MetaCubeX/meta-rules-dat/releases/latest")
        result = {key: release.get(key) for key in ("tag_name", "target_commitish", "published_at")}
        reference = call("/repos/MetaCubeX/meta-rules-dat/git/ref/heads/meta")
        result["data_commit"] = reference["object"]["sha"]
        tree = call("/repos/MetaCubeX/meta-rules-dat/git/trees/" + result["data_commit"])
        result["data_files"] = [{key: item.get(key) for key in ("path", "size", "sha")}
                                for item in tree["tree"] if item["path"].endswith((".dat", ".mmdb"))]
        result["assets"] = [{key: asset.get(key) for key in ("name", "size", "digest", "browser_download_url")}
                            for asset in release["assets"] if asset["name"].endswith((".dat", ".mmdb"))]
    elif args.mode == "contracts":
        result = {}
        for repository, reference in (("home-assistant/cli", "master"), ("MetaCubeX/mihomo", "v1.19.34")):
            if repository.endswith("/cli"):
                reference = call(f"/repos/{repository}")["default_branch"]
            directory = "cmd" if repository.endswith("/cli") else "hub/route"
            listing = call(f"/repos/{repository}/contents/{directory}?ref={reference}")
            paths = [item["path"] for item in listing if item["type"] == "file"]
            wanted = ([path for path in paths if re.fullmatch(r"cmd/(?:apps|addons)(?:_list|List)?\.go", path)]
                      if repository.endswith("/cli") else [path for path in paths if "provider" in path])
            if repository.endswith("/mihomo"):
                entries = call(f"/repos/{repository}/contents/rules/provider?ref={reference}")
                wanted.extend(item["path"] for item in entries if item["type"] == "file" and "provider" in item["name"].lower())
            result[repository] = {"files": wanted, "excerpts": {}}
            for path in wanted:
                document = call(f"/repos/{repository}/contents/{path}?ref={reference}")
                lines = base64.b64decode(document["content"]).decode().splitlines()
                selected = [index for index, line in enumerate(lines) if repository.endswith("/cli") or any(word in line for word in (
                    "Use:", "Aliases:", "AddCommand", "ruleCount", "RuleCount", "VehicleType", "Inet6Address", "inet6-address", "SafePath"))]
                indices = sorted({near for index in selected for near in range(max(0, index - 2), min(len(lines), index + 4))})
                result[repository]["excerpts"][path] = [f"{index+1}: {lines[index]}" for index in indices]
            destination = Path(".work/contracts.json")
            destination.parent.mkdir(exist_ok=True)
            destination.write_text(json.dumps(result, indent=2), encoding="utf-8")
    elif args.mode == "action-pins":
        result = {}
        for repository, tag in [("actions/checkout", "v6"), ("sigstore/cosign-installer", "v3"), ("actions/upload-artifact", "v7"), ("actions/download-artifact", "v8")]:
            value = call(f"/repos/{repository}/git/ref/tags/{tag}")["object"]
            if value["type"] == "tag":
                value = call(f'/repos/{repository}/git/tags/{value["sha"]}')["object"]
            if value["type"] != "commit":
                raise ValueError("Action tag did not resolve to a commit")
            result[repository] = {"tag": tag, "sha": value["sha"]}
    elif args.mode == "runs":
        runs = call(f"/repos/{REPO}/actions/runs?" + urlencode({"per_page": 5, "branch": args.ref}))
        result = [{key: run.get(key) for key in ("id", "name", "status", "conclusion", "html_url", "head_sha", "run_attempt")}
                  for run in runs["workflow_runs"]]
    elif args.mode == "run":
        if not args.run_id:
            parser.error("--run-id is required")
        run = call(f"/repos/{REPO}/actions/runs/{args.run_id}")
        jobs = call(f"/repos/{REPO}/actions/runs/{args.run_id}/jobs")
        artifacts = call(f"/repos/{REPO}/actions/runs/{args.run_id}/artifacts")
        result = {key: run.get(key) for key in ("id", "status", "conclusion", "html_url", "head_sha")}
        result["jobs"] = [{key: job.get(key) for key in ("id", "name", "status", "conclusion", "steps")}
                          for job in jobs["jobs"]]
        result["artifacts"] = [{key: artifact.get(key) for key in ("id", "name", "size_in_bytes", "expired")}
                               for artifact in artifacts["artifacts"]]
    elif args.mode == "licenses":
        result = {}
        for repository, tag, filename in [("Zephyruso/zashboard", "v3.30.0", "Zashboard-LICENSE.txt")]:
            data = call(f"/repos/{repository}/license?ref={tag}")
            if data.get("encoding") != "base64":
                raise RuntimeError("Unexpected upstream license encoding")
            path = Path("mihomo/licenses") / filename
            path.write_bytes(base64.b64decode(data["content"]))
            result[repository] = {"license": data["license"]["spdx_id"], "path": str(path)}
    elif args.mode == "job-logs":
        if not args.job_id and args.run_id:
            jobs = call(f"/repos/{REPO}/actions/runs/{args.run_id}/jobs")
            failed = [job for job in jobs["jobs"] if job.get("conclusion") == "failure"]
            if len(failed) != 1:
                raise ValueError("Expected one failed job to diagnose")
            args.job_id = failed[0]["id"]
        if not args.job_id:
            parser.error("--job-id or --run-id is required")
        content = call(f"/repos/{REPO}/actions/jobs/{args.job_id}/logs", raw=True)
        path = Path(".work/logs") / f"job-{args.job_id}.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        result = {"path": str(path.resolve()), "tail": content.decode("utf-8", errors="replace").splitlines()[-90:]}
    elif args.mode in {"rerun", "cancel"}:
        if not args.run_id:
            parser.error("--run-id is required")
        call(f"/repos/{REPO}/actions/runs/{args.run_id}/{args.mode}", "POST")
        result = {args.mode: args.run_id}
    elif args.mode == "artifact":
        if not args.artifact_id:
            parser.error("--artifact-id is required")
        content = call(f"/repos/{REPO}/actions/artifacts/{args.artifact_id}/zip", raw=True)
        root = (Path(".work/diagnostics") / str(args.artifact_id)).resolve()
        total = 0
        files = []
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            for entry in archive.infolist():
                if entry.is_dir():
                    continue
                path = (root / entry.filename).resolve()
                total += entry.file_size
                if root not in path.parents or total > 50 * 1024 * 1024:
                    raise RuntimeError("Unsafe diagnostic archive")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(archive.read(entry))
                files.append(str(path.relative_to(root)))
        result = {"path": str(root), "files": files}
    elif args.mode == "release":
        release = call(f"/repos/{REPO}/releases/latest")
        result = {key: release.get(key) for key in ("tag_name", "html_url", "target_commitish", "published_at")}
        result["assets"] = [{key: asset.get(key) for key in ("name", "size", "digest", "browser_download_url")} for asset in release["assets"]]
    elif args.mode == "default-branch":
        result = call(f"/repos/{REPO}", "PATCH", {"default_branch": args.ref})
        result = {"default_branch": result["default_branch"]}
    else:
        call(f"/repos/{REPO}/actions/workflows/build.yml/dispatches", "POST", {"ref": args.ref})
        result = {"dispatched": True, "ref": args.ref}
    print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
