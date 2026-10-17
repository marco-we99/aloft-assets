"""CI-side TikTok uploader run by .github/workflows/tiktok-upload.yml.

Reads a pending_tiktok/<name>.json manifest, refreshes the stored
TikTok refresh token, uploads the video to the operator's TikTok
account (inbox / drafts by default), waits for the publish to settle,
and prints a short result the workflow commits back to the repo.

Env vars (passed in by the workflow from repository secrets):
  TIKTOK_CLIENT_KEY       — app's client key
  TIKTOK_CLIENT_SECRET    — app's client secret
  TIKTOK_REFRESH_TOKEN    — long-lived refresh token from first-login

CLI:
  python upload_to_tiktok.py <manifest.json>

Manifest schema:
  {
    "video_path":   "neworleans/music_ready.mp4",   # path inside this repo
    "caption":      "Some TikTok caption + hashtags",
    "mode":         "inbox",                        # or "direct"
    "privacy":      "SELF_ONLY",                    # direct mode only
    "city":         "New Orleans",
    "reel_id":      "neworleans_01_music"
  }
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import requests


OPEN_API = "https://open.tiktokapis.com"
TOKEN_URL = f"{OPEN_API}/v2/oauth/token/"
REPO_ROOT = Path(__file__).resolve().parent.parent


def _env(key: str) -> str:
    v = os.environ.get(key, "").strip()
    if not v:
        sys.exit(f"env var {key} is not set")
    return v


def refresh_access_token() -> tuple[str, str]:
    """Use the stored refresh token to get a fresh access token.
    Returns (access_token, new_refresh_token). The new refresh token
    replaces the stored one — TikTok rotates them on each refresh."""
    r = requests.post(
        TOKEN_URL,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "client_key": _env("TIKTOK_CLIENT_KEY"),
            "client_secret": _env("TIKTOK_CLIENT_SECRET"),
            "grant_type": "refresh_token",
            "refresh_token": _env("TIKTOK_REFRESH_TOKEN"),
        },
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    if "access_token" not in data:
        sys.exit(f"TikTok refresh failed: {data}")
    return data["access_token"], data.get("refresh_token",
                                         _env("TIKTOK_REFRESH_TOKEN"))


def init_upload(access_token: str, video_bytes: int,
                mode: str, caption: str,
                privacy: str) -> tuple[str, str]:
    """Call init endpoint. Returns (publish_id, upload_url)."""
    endpoint = (
        f"{OPEN_API}/v2/post/publish/inbox/video/init/"
        if mode == "inbox"
        else f"{OPEN_API}/v2/post/publish/video/init/"
    )
    body: dict = {
        "source_info": {
            "source": "FILE_UPLOAD",
            "video_size": video_bytes,
            "chunk_size": video_bytes,
            "total_chunk_count": 1,
        },
    }
    if mode == "direct":
        body["post_info"] = {
            "title": caption,
            "privacy_level": privacy,
            "disable_duet": False,
            "disable_comment": False,
            "disable_stitch": False,
            "video_cover_timestamp_ms": 1000,
        }
    r = requests.post(
        endpoint,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
        },
        json=body,
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    err = (data.get("error") or {}).get("code", "ok")
    if err != "ok":
        sys.exit(f"publish init failed: {data}")
    d = data["data"]
    return d["publish_id"], d["upload_url"]


def put_video(upload_url: str, path: Path) -> None:
    size = path.stat().st_size
    with path.open("rb") as fh:
        body = fh.read()
    r = requests.put(
        upload_url,
        headers={
            "Content-Type": "video/mp4",
            "Content-Length": str(size),
            "Content-Range": f"bytes 0-{size - 1}/{size}",
        },
        data=body,
        timeout=600,
    )
    if r.status_code not in (200, 201, 206):
        sys.exit(f"video PUT failed: HTTP {r.status_code}: {r.text[:400]}")


def wait_for_publish(access_token: str, publish_id: str,
                     timeout_s: int = 300) -> dict:
    """Poll until TikTok reports a terminal state."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        r = requests.post(
            f"{OPEN_API}/v2/post/publish/status/fetch/",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json; charset=UTF-8",
            },
            json={"publish_id": publish_id},
            timeout=30,
        )
        r.raise_for_status()
        resp = r.json()
        err = (resp.get("error") or {}).get("code", "ok")
        if err != "ok":
            sys.exit(f"status fetch error: {resp}")
        status = (resp.get("data") or {}).get("status", "UNKNOWN")
        if status == "FAILED":
            sys.exit(f"TikTok publish FAILED: {resp}")
        if status in ("PUBLISH_COMPLETE", "SEND_TO_USER_INBOX"):
            return resp
        time.sleep(5)
    sys.exit(f"TikTok publish didn't complete in {timeout_s}s "
             f"(publish_id={publish_id})")


def _export_output(key: str, value: str) -> None:
    """Write a single key=value line into GITHUB_OUTPUT so a later
    workflow step can read it via steps.<id>.outputs.<key>."""
    gh_env = os.environ.get("GITHUB_OUTPUT")
    if gh_env:
        with open(gh_env, "a") as fh:
            fh.write(f"{key}={value}\n")


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("usage: upload_to_tiktok.py <manifest.json>")
    manifest_path = Path(sys.argv[1])
    manifest = json.loads(manifest_path.read_text())

    video_path = REPO_ROOT / manifest["video_path"]
    caption = manifest.get("caption", "")
    mode = manifest.get("mode", "inbox")
    privacy = manifest.get("privacy", "SELF_ONLY")
    city = manifest.get("city", "")
    reel_id = manifest.get("reel_id", "")

    if not video_path.exists():
        sys.exit(f"video not found: {video_path}")
    size = video_path.stat().st_size
    print(f"[{reel_id}] {city} — uploading {video_path} ({size:,} bytes) "
          f"mode={mode}")

    # Refresh FIRST. TikTok rotates the refresh_token on every refresh
    # call — if we skip saving the new one and anything downstream
    # fails, the stored secret is now stale and the next run can't
    # authenticate. So export the new token to the workflow output
    # BEFORE anything else that could throw.
    access_token, new_refresh_token = refresh_access_token()
    _export_output("new_refresh_token", new_refresh_token)
    print(f"[{reel_id}] token refreshed + exported for secret rotation")

    publish_id, upload_url = init_upload(
        access_token, size, mode, caption, privacy)
    print(f"[{reel_id}] publish_id={publish_id}")

    put_video(upload_url, video_path)
    print(f"[{reel_id}] video bytes uploaded")

    # The TikTok inbox endpoint reports SEND_TO_USER_INBOX somewhere in
    # the first 2-10 minutes. If we time out we treat it as "likely
    # succeeded, finalize later" rather than fail — the video is in
    # TikTok's hands at this point, and marking the run failed just so
    # we can hunt a non-terminal status doesn't help anyone.
    try:
        final = wait_for_publish(access_token, publish_id, timeout_s=600)
        status = (final.get("data") or {}).get("status")
        print(f"[{reel_id}] final status: {status}")
    except SystemExit as e:
        if "didn't complete in" in str(e):
            status = "PENDING_LONG"
            print(f"[{reel_id}] status still pending after 10 min — "
                  f"treating as uploaded; verify in the TikTok app")
        else:
            raise

    # Write a result file next to the manifest so the workflow can
    # commit it as a receipt (and so we never silently lose the fact
    # that an upload actually happened).
    result = {
        "reel_id": reel_id,
        "city": city,
        "video_path": manifest["video_path"],
        "mode": mode,
        "publish_id": publish_id,
        "status": status,
        "uploaded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                     time.gmtime()),
    }
    result_dir = REPO_ROOT / "tiktok_uploads"
    result_dir.mkdir(parents=True, exist_ok=True)
    result_path = result_dir / (manifest_path.stem + ".json")
    result_path.write_text(json.dumps(result, indent=2))
    print(f"[{reel_id}] receipt → {result_path.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
