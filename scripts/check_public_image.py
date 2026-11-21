"""The published app must be pullable without user credentials."""
import json
import time
from urllib.parse import urlencode
from urllib.request import Request, ProxyHandler, build_opener

from common import IMAGE


def accessible():
    repository, version = IMAGE.removeprefix("ghcr.io/").rsplit(":", 1)
    opener = build_opener(ProxyHandler({}))
    endpoint = "https://ghcr.io/token?" + urlencode({"service": "ghcr.io", "scope": f"repository:{repository}:pull"})
    with opener.open(endpoint, timeout=20) as response:
        token = json.load(response)["token"]
    request = Request(f"https://ghcr.io/v2/{repository}/manifests/{version}", method="HEAD", headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json",
    })
    with opener.open(request, timeout=20) as response:
        return response.status == 200


if __name__ == "__main__":
    deadline = time.monotonic() + 1200
    next_message = 0
    while time.monotonic() < deadline:
        try:
            if accessible():
                print("Public GHCR image can be pulled without credentials", flush=True)
                break
        except (OSError, ValueError, KeyError):
            pass
        if time.monotonic() >= next_message:
            print("Waiting for GHCR package visibility to be public: " + IMAGE, flush=True)
            next_message = time.monotonic() + 60
        time.sleep(10)
    else:
        raise RuntimeError("The GHCR app image is not publicly readable; set the package visibility to public")
