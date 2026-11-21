"""Check immutable image configuration across Docker storage backends."""
import hashlib
import json
import subprocess
import tarfile
import tempfile


def saved_image_identity(stream, reference):
    documents = {}
    configurations = {}
    with tarfile.open(fileobj=stream, mode="r|") as archive:
        for member in archive:
            if not member.isfile() or member.size > 8 * 1024 * 1024:
                continue
            if member.name != "manifest.json" and not member.name.endswith(".json") and not member.name.startswith("blobs/sha256/"):
                continue
            content = archive.extractfile(member).read()
            try:
                value = json.loads(content)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if member.name == "manifest.json":
                documents[member.name] = value
            elif isinstance(value, dict) and {"architecture", "os", "rootfs"} <= value.keys():
                configurations[member.name] = (content, value)
    matches = [entry for entry in documents.get("manifest.json", []) if reference in (entry.get("RepoTags") or [])]
    if len(matches) != 1 or matches[0]["Config"] not in configurations:
        raise ValueError("Saved archive does not contain exactly one requested image configuration")
    content, config = configurations[matches[0]["Config"]]
    layers = config["rootfs"].get("diff_ids")
    if config["rootfs"].get("type") != "layers" or not isinstance(layers, list) or not layers:
        raise ValueError("Saved image has no immutable root filesystem layer identities")
    return {"config_sha256": hashlib.sha256(content).hexdigest(), "architecture": config["architecture"],
            "os": config["os"], "rootfs_diff_ids": layers}


def exported_image_identity(command, reference):
    # Read the daemon's export as a stream, without another multi-GiB archive.
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors)
        try:
            identity = saved_image_identity(process.stdout, reference)
            if process.wait(timeout=120):
                errors.seek(0)
                raise RuntimeError("Image verification export failed: " + errors.read().decode(errors="replace")[-2000:])
            return identity
        finally:
            process.stdout.close()
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
