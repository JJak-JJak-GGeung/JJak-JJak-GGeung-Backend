"""Publish the reviewed release to the existing Seoul Lightsail container."""
import base64
import secrets
import copy
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid

REGION = "ap-northeast-2"
SERVICE = "jjak-jjak-ggeung-a1"
ORIGIN = "https://jjak-jjak-ggeung-a1.rkrbhm0ryexya.ap-northeast-2.cs.amazonlightsail.com"
COMMIT = "b7baec37499e510dd4ffde541ad98334cbe9447e"
NEW_ROUTES = {
    ("/api/photo-previews-v2", "post"),
    ("/api/photo-previews-v2/confirm", "post"),
    ("/api/photo-previews/confirm", "post"),
    ("/api/stores", "post"),
    ("/api/stores/{store_id}/suppliers", "post"),
    ("/api/sample-deliveries", "get"),
    ("/api/sample-deliveries/{code}", "get"),
}
METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
os.environ["AWS_PAGER"] = ""


def aws(action, *args, payload=None):
    cmd = ["aws", "lightsail", action, "--region", REGION, "--output", "json", *args]
    # AWS CLI v2 can see an empty payload when file:///dev/stdin is used.
    if payload is None:
        run = subprocess.run(cmd, capture_output=True, text=True)
    else:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as request:
            json.dump(payload, request)
            request.flush()
            run = subprocess.run([*cmd, "--cli-input-json", "file://" + request.name],
                                 capture_output=True, text=True)
    if run.returncode:
        # AWS responses and parameter-validation errors can contain environment values.
        code = re.search(r"An error occurred \(([A-Za-z0-9_.-]+)\)", run.stderr)
        raise SystemExit(f"AWS {action} 실패: {code[1] if code else '콘솔 권한과 AWS CLI를 확인하세요.'}")
    return json.loads(run.stdout) if run.stdout.strip() else {}


def service():
    services = aws("get-container-services", "--service-name", SERVICE)["containerServices"]
    if len(services) != 1 or services[0].get("url", "").rstrip("/") != ORIGIN:
        raise SystemExit("이 AWS 계정에서 대상 서울 서비스 주소를 확인할 수 없습니다.")
    return services[0]


def live_json(path, token=None):
    headers = {"Authorization": "Bearer " + token} if token else {}
    with urllib.request.urlopen(urllib.request.Request(ORIGIN + path, headers=headers), timeout=20) as response:
        return json.load(response)


def routes(schema):
    return {(path, method) for path, operations in schema["paths"].items()
            for method in operations if method in METHODS}


def verify_source():
    head = subprocess.check_output(["git", "-C", str(ROOT), "ls-remote", "--exit-code", "origin",
                                    "refs/heads/main"], text=True).split()[0]
    if head != COMMIT:
        raise SystemExit("Backend main이 바뀌었습니다. 새 커밋을 검토한 뒤 배포하세요.")


def verify_receipt_api(schema):
    kinds = schema["components"]["schemas"]["ConfirmationRequest"]["properties"]["kind"].get("enum", [])
    if "receipt_entry" not in kinds:
        raise SystemExit("검수 수정본에 직접 수령 등록 계약이 없습니다.")


def smoke():
    verify_receipt_api(live_json("/openapi.json"))
    try:
        live_json("/api/stores")
    except urllib.error.HTTPError as error:
        if error.code != 401 or json.load(error).get("detail") != "BROWSER_SESSION_REQUIRED":
            raise
    else:
        raise SystemExit("인증 없는 점포 목록 요청이 허용됐습니다.")
    token = "device-v1." + secrets.token_hex(32)
    result = live_json("/api/stores", token)
    if result.get("directory_registration") is not True or result.get("stores") != []:
        raise SystemExit("새 브라우저 보관함 또는 등록 기능 표시를 확인할 수 없습니다.")
    samples = live_json("/api/sample-deliveries", token)["samples"]
    if not {"S01", "S02", "S03"}.issubset({s["code"] for s in samples}):
        raise SystemExit("팀 DB 합성 명세표 목록을 확인할 수 없습니다.")
    for code in ("S01", "S02", "S03"):
        sample = live_json("/api/sample-deliveries/" + code, token)
        if not base64.b64decode(sample["image"], validate=True).startswith(bytes.fromhex("89504e470d0a1a0a")):
            raise SystemExit("합성 원본 PNG 검사 실패: " + code)
    print("운영 API, 브라우저 격리, 등록 기능 표시 및 합성 원본 3장 조회 성공.")


def build_push(tmp, previous_routes):
    for tool in ["git", "aws", "docker"]:
        if not shutil.which(tool):
            raise SystemExit(f"{tool} 명령이 필요합니다.")
    subprocess.run(["docker", "info"], stdout=subprocess.DEVNULL, check=True)
    if not shutil.which("lightsailctl"):
        arch = {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine())
        if sys.platform != "linux" or arch != "amd64":
            raise SystemExit("Lightsail Control 플러그인을 설치하거나 x86_64 Linux CloudShell을 사용하세요.")
        binary = Path(tmp) / "lightsailctl"
        urllib.request.urlretrieve("https://s3.us-west-2.amazonaws.com/lightsailctl/latest/linux-amd64/lightsailctl", binary)
        binary.chmod(0o700)
        os.environ["PATH"] = str(binary.parent) + os.pathsep + os.environ["PATH"]
    if subprocess.run(["git", "-C", str(ROOT), "cat-file", "-e", COMMIT+"^{commit}"],
                      stderr=subprocess.DEVNULL).returncode:
        subprocess.run(["git", "-C", str(ROOT), "fetch", "--depth=1", "origin", COMMIT], check=True)
    label = "directory-" + COMMIT[:12] + "-" + uuid.uuid4().hex[:8]
    tag = "jjak-photo:" + label
    print("서버 이미지 빌드 시작", flush=True)
    with subprocess.Popen(["git", "-C", str(ROOT), "archive", COMMIT], stdout=subprocess.PIPE) as archive:
        subprocess.run(["docker", "build", "--platform", "linux/amd64", "-t", tag, "-"],
                       stdin=archive.stdout, check=True)
        if archive.wait():
            raise SystemExit("Git 소스 읽기 실패")
    schema = subprocess.check_output(["docker", "run", "--rm", "--entrypoint", "python", tag,
        "-c", "import json; from app.api.main import app; print(json.dumps(app.openapi()))"], text=True)
    candidate = json.loads(schema)
    verify_receipt_api(candidate)
    missing = (previous_routes | NEW_ROUTES) - routes(candidate)
    if missing:
        raise SystemExit("배포 후보에서 누락된 운영 API: " + repr(sorted(missing)))
    subprocess.run(["aws", "lightsail", "push-container-image", "--region", REGION, "--service-name",
                    SERVICE, "--label", label, "--image", tag], check=True)
    for _ in range(10):
        images = aws("get-container-images", "--service-name", SERVICE)["containerImages"]
        matches = [r["image"] for r in images if r["image"].startswith(f":{SERVICE}.{label}.")]
        if len(matches) == 1:
            return matches[0]
        time.sleep(3)
    raise SystemExit("새 이미지 등록 상태를 확인하지 못해 배포를 중단했습니다.")


def main():
    if not shutil.which("aws"):
        raise SystemExit("AWS CLI와 팀 Lightsail 권한이 있는 CloudShell에서 실행하세요.")
    verify_source()
    original = service()
    if original.get("state") == "DEPLOYING":
        print("진행 중인 AWS 배포를 기다립니다. 새 배포를 중복 요청하지 않습니다.", flush=True)
        for _ in range(90):
            time.sleep(10)
            original = service()
            if original.get("state") != "DEPLOYING":
                break
    previous_routes = routes(live_json("/openapi.json"))
    active = original.get("currentDeployment") or {}
    endpoint = active.get("publicEndpoint") or {}
    target = endpoint.get("containerName")
    containers = copy.deepcopy(active.get("containers", {}))
    if original.get("state") != "RUNNING" or target not in containers:
        raise SystemExit("서비스 상태 또는 운영 컨테이너를 확인할 수 없습니다. 기존 배포가 진행 중이면 완료 후 재실행하세요.")
    if (original.get("nextDeployment") or {}).get("state") in ("ACTIVATING", "ACTIVE"):
        raise SystemExit("다른 배포가 처리 중입니다.")
    if containers[target].get("image","").startswith(f":{SERVICE}.directory-{COMMIT[:12]}-"):
        if not NEW_ROUTES.issubset(previous_routes):
            raise SystemExit("현재 이미지와 운영 API가 일치하지 않습니다.")
        smoke()
        return
    if (active.get("version") != 18 or
            not containers[target].get("image", "").endswith(":line-details-05916a1cdc3282a2")):
        raise SystemExit("검수한 운영 v18 이후 다른 배포가 있습니다. 현재 이미지를 먼저 대조하세요.")
    env = containers[target].setdefault("environment", {})
    if not all(env.get(k) for k in ("SUPABASE_URL","SUPABASE_SECRET_KEY")):
        raise SystemExit("선택된 운영 컨테이너에 기존 Supabase 설정이 없습니다. AWS 배포 설정을 확인하세요.")
    if env["SUPABASE_URL"].rstrip("/") != "https://tsawtrnveekgcrxqutif.supabase.co":
        raise SystemExit("운영 Supabase 대상이 일치하지 않습니다.")
    if not env.get("OPENAI_API_KEY", "").strip():
        raise SystemExit("기존 운영 컨테이너의 OpenAI 키 설정을 확인하세요.")
    with tempfile.TemporaryDirectory() as tmp:
        image = build_push(tmp, previous_routes)
    verify_source()
    current = service()
    if current.get("state") != "RUNNING" or current.get("currentDeployment") != active or current.get("nextDeployment") != original.get("nextDeployment"):
        raise SystemExit("작업 중 기존 배포가 변경됐습니다. 화면을 확인해 주세요.")
    containers[target]["image"] = image
    created = aws("create-container-service-deployment", payload={
        "serviceName": SERVICE, "containers": containers, "publicEndpoint": endpoint})
    nxt = (created.get("containerService") or {}).get("nextDeployment") or {}
    print(f"새 배포 요청 완료. 이전 버전 {active['version']}, 목표 이미지 {image}.", flush=True)
    for _ in range(90):
        state = service()
        live = state.get("currentDeployment") or {}
        if (live.get("containers", {}).get(target) or {}).get("image") == image and state.get("state") == "RUNNING":
            if not (previous_routes | NEW_ROUTES).issubset(routes(live_json("/openapi.json"))):
                time.sleep(10)
                continue
            smoke()
            return
        pending = state.get("nextDeployment") or {}
        if pending.get("state") == "FAILED" and pending.get("version") == nxt.get("version"):
            raise SystemExit("AWS가 새 배포를 실패 처리했습니다. Lightsail 배포 로그를 확인하세요.")
        if state.get("state") == "RUNNING" and not pending and live.get("version") == active["version"]:
            history = aws("get-container-service-deployments", "--service-name", SERVICE).get("deployments", [])
            if any(d.get("state") == "FAILED" and (d.get("containers",{}).get(target) or {}).get("image") == image for d in history):
                raise SystemExit("새 배포 실패. 기존 운영 버전이 활성 상태입니다.")
        time.sleep(10)
    raise SystemExit("배포 요청은 전송됐지만 제한 시간 안에 새 버전의 활성 상태를 확인하지 못했습니다.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, subprocess.CalledProcessError, ValueError) as e:
        sys.exit(f"작업 중단: {type(e).__name__}. " + (f"HTTP {e.code}. " if hasattr(e, "code") else "")
                 + "배포 로그·CloudShell Docker·OpenAI 키 및 API 한도를 확인하세요.")
