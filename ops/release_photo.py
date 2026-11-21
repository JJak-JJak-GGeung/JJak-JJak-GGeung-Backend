"""Publish the reviewed release to the existing Seoul Lightsail container."""
import argparse
import copy
import getpass
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
COMMIT = "6906c429f3191b41b0e7a318b07e7d58f4dd2c10"
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
os.environ["AWS_PAGER"] = ""


def aws(action, *args, payload=None):
    cmd = ["aws", "lightsail", action, "--region", REGION, "--output", "json", *args]
    if payload is not None:
        cmd += ["--cli-input-json", "file:///dev/stdin"]
    run = subprocess.run(cmd, input=json.dumps(payload) if payload is not None else None,
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


def preview_available():
    with urllib.request.urlopen(ORIGIN + "/openapi.json", timeout=15) as response:
        paths = json.load(response)["paths"]
    return all("post" in paths.get(path, {}) for path in
               ("/api/photo-previews", "/api/photo-previews/confirm"))


def smoke():
    form = "photo-" + uuid.uuid4().hex
    data = (f'--{form}\r\nContent-Disposition: form-data; name="file"; filename="smoke.png"\r\n'
            'Content-Type: image/png\r\n\r\n').encode()
    data += (HERE / "photo-smoke.png").read_bytes() + f"\r\n--{form}--\r\n".encode()
    request = urllib.request.Request(ORIGIN + "/api/photo-previews", data=data,
                                    headers={"Content-Type": f"multipart/form-data; boundary={form}"})
    token = os.getenv("PUBLIC_A_ACCESS_TOKEN")
    if token:
        request.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(request, timeout=150) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 401 and not token and json.load(error).get("detail") == "OWNER_AUTH_REQUIRED":
            print("운영 사진 API 연결 확인. 점주 로그인이 필요합니다. 로그인한 앱에서 사진을 올려 판독을 검증하세요.")
            return
        raise
    rows = result.get("draft", {}).get("lines", [])
    if not result.get("signature") or not any(str(r.get("qty_read")) in {"7", "7.0", "7.00"}
                                             and str(r.get("amount_read")) == "2100" for r in rows):
        raise SystemExit("서버는 응답했지만 합성 사진의 기대 수량·금액 검증은 통과하지 못했습니다.")
    print("성공: 운영 LLM 판독 1건, 합성 품목 수량 7 · 금액 2100. 정산 DB 저장 요청 없음.")


def build_push(tmp):
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
    label = "vision-" + COMMIT[:7] + "-" + uuid.uuid4().hex[:8]
    tag = "jjak-photo:" + label
    print("서버 이미지 빌드 시작", flush=True)
    with subprocess.Popen(["git", "-C", str(ROOT), "archive", COMMIT], stdout=subprocess.PIPE) as archive:
        subprocess.run(["docker", "build", "--platform", "linux/amd64", "-t", tag, "-"],
                       stdin=archive.stdout, check=True)
        if archive.wait():
            raise SystemExit("Git 소스 읽기 실패")
    check_routes = ("from app.api.main import app; "
                    "paths=app.openapi()['paths']; "
                    "assert all('post' in paths.get(p,{}) for p in "
                    "('/api/photo-previews','/api/photo-previews/confirm'))")
    subprocess.run(["docker", "run", "--rm", "--entrypoint", "python", tag, "-c", check_routes], check=True)
    subprocess.run(["aws", "lightsail", "push-container-image", "--region", REGION, "--service-name",
                    SERVICE, "--label", label, "--image", tag], check=True)
    for _ in range(10):
        images = aws("get-container-images", "--service-name", SERVICE)["containerImages"]
        matches = [r["image"] for r in images if r["image"].startswith(f":{SERVICE}.{label}.")]
        if len(matches) == 1:
            return matches[0]
        time.sleep(3)
    raise SystemExit("새 이미지 등록 상태를 확인하지 못해 배포를 중단했습니다.")


def check_store_assignment(env):
    # Only check existence. Do not retrieve customer identities, documents or account tokens.
    base = env["SUPABASE_URL"].rstrip("/")
    if base != "https://tsawtrnveekgcrxqutif.supabase.co":
        raise SystemExit("운영 Supabase 프로젝트 주소를 확인하세요.")
    key = env["SUPABASE_SECRET_KEY"]
    query = base + "/rest/v1/stores?select=id&owner_user_id=not.is.null&limit=1"
    request = urllib.request.Request(query, headers={"apikey": key, "Authorization": "Bearer " + key})
    with urllib.request.urlopen(request, timeout=15) as response:
        assigned = json.load(response)
    if not assigned:
        raise SystemExit("검수 점포에 점주 계정이 연결되지 않았습니다. 소유자를 확인해 연결한 뒤 실행하세요. 새 배포 요청은 전송하지 않았습니다.")


def main(replace_key=False):
    original = service()
    if original.get("state") == "DEPLOYING":
        print("진행 중인 AWS 배포를 기다립니다. 새 배포를 중복 요청하지 않습니다.", flush=True)
        for _ in range(90):
            time.sleep(10)
            original = service()
            if original.get("state") != "DEPLOYING":
                break
    if preview_available() and not replace_key:
        smoke()
        return
    active = original.get("currentDeployment") or {}
    endpoint = active.get("publicEndpoint") or {}
    target = endpoint.get("containerName")
    containers = copy.deepcopy(active.get("containers", {}))
    if original.get("state") != "RUNNING" or target not in containers:
        raise SystemExit("서비스 상태 또는 운영 컨테이너를 확인할 수 없습니다. 기존 배포가 진행 중이면 완료 후 재실행하세요.")
    env = containers[target].setdefault("environment", {})
    if not all(env.get(k) for k in ("SUPABASE_URL","SUPABASE_SECRET_KEY")):
        raise SystemExit("선택된 운영 컨테이너에 기존 Supabase 설정이 없습니다. AWS 배포 설정을 확인하세요.")
    check_store_assignment(env)
    if not env.get("SUPABASE_PUBLISHABLE_KEY"):
        print("점주 로그인 검증에 사용할 Supabase 프로젝트의 Publishable key가 필요합니다.")
        if not sys.stdin.isatty():
            raise SystemExit("CloudShell에서 대화형으로 실행하세요.")
        key = getpass.getpass("Supabase Publishable key (sb_publishable_...): ").strip()
        if not key.startswith("sb_publishable_"):
            raise SystemExit("Supabase Publishable key 형식을 확인하세요.")
        env["SUPABASE_PUBLISHABLE_KEY"] = key
    if replace_key or not env.get("OPENAI_API_KEY", "").strip():
        if not sys.stdin.isatty():
            raise SystemExit("AWS 터미널에서 실행해 팀 OpenAI API 키를 입력하세요.")
        key = getpass.getpass("팀 OpenAI API 키 입력(화면에 숨김): ").strip()
        if not key:
            raise SystemExit("키 입력이 취소되었습니다.")
        env["OPENAI_API_KEY"] = key
    with tempfile.TemporaryDirectory() as tmp:
        image = build_push(tmp)
    current = service()
    if current.get("state") != "RUNNING" or current.get("currentDeployment") != active:
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
            if not preview_available():
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
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--replace-openai-key", action="store_true", help="기존 키 대신 새 팀 키를 숨김 입력")
        main(parser.parse_args().replace_openai_key)
    except (OSError, subprocess.CalledProcessError, ValueError) as e:
        sys.exit(f"작업 중단: {type(e).__name__}. " + (f"HTTP {e.code}. " if hasattr(e, "code") else "")
                 + "배포 로그·CloudShell Docker·OpenAI 키 및 API 한도를 확인하세요.")
