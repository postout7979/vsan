#!/usr/bin/env python3
"""VCF Operations(vROps) Suite API에서 vSAN 어댑터의 "vSAN Cluster" 리소스가 갖는
Metrics 중 "vSAN > Disk Space" 그룹(statKey 접두사 vsan|diskSpace|) 전체 항목의
최신 값을 조회한다. vSAN 클러스터가 여러 개면 모두 순회해서 출력한다.

사용 예:
    python3 vsan_diskspace_metrics.py --host https://vcf-ops01.example.com \
        --username admin --password '...' --insecure
"""

from __future__ import annotations

import argparse
import getpass
import sys

import httpx

DISKSPACE_PREFIX = "vsan|diskspace|"


class OpsApiError(Exception):
    pass


def acquire_token(host: str, username: str, password: str, auth_source: str, verify: bool) -> str:
    try:
        resp = httpx.post(
            f"{host}/suite-api/api/auth/token/acquire",
            json={"username": username, "password": password, "authSource": auth_source},
            headers={"Accept": "application/json"},
            verify=verify,
            timeout=20.0,
        )
    except httpx.HTTPError as exc:
        raise OpsApiError(f"접속 실패: {type(exc).__name__}: {exc}") from exc
    if resp.status_code >= 400:
        raise OpsApiError(f"로그인 실패 ({resp.status_code}): {resp.text[:300]}")
    token = resp.json().get("token")
    if not token:
        raise OpsApiError("로그인 응답에 token이 없습니다.")
    return token


def api_get(host: str, token: str, path: str, verify: bool, **params) -> dict:
    resp = httpx.get(
        f"{host}/suite-api/api{path}",
        params=params,
        headers={"Accept": "application/json", "Authorization": f"vRealizeOpsToken {token}"},
        verify=verify,
        timeout=60.0,
    )
    if resp.status_code >= 400:
        raise OpsApiError(f"GET {path} 실패 ({resp.status_code}): {resp.text[:300]}")
    return resp.json() if resp.content else {}


def api_post(host: str, token: str, path: str, body: dict, verify: bool, **params) -> dict:
    resp = httpx.post(
        f"{host}/suite-api/api{path}",
        json=body,
        params=params or None,
        headers={"Accept": "application/json", "Authorization": f"vRealizeOpsToken {token}"},
        verify=verify,
        timeout=60.0,
    )
    if resp.status_code >= 400:
        raise OpsApiError(f"POST {path} 실패 ({resp.status_code}): {resp.text[:300]}")
    return resp.json() if resp.content else {}


def list_vsan_clusters(host: str, token: str, verify: bool) -> list[dict]:
    """모든 vSAN Cluster(VirtualSANDCCluster) 리소스를 페이지 순회로 전부 가져온다."""
    out: list[dict] = []
    page = 0
    while True:
        data = api_get(
            host, token, "/resources", verify,
            adapterKind="VirtualAndPhysicalSANAdapter", resourceKind="VirtualSANDCCluster",
            page=page, pageSize=1000,
        )
        rows = data.get("resourceList") or []
        out.extend(rows)
        total = (data.get("pageInfo") or {}).get("totalCount", 0)
        page += 1
        if len(out) >= total or not rows:
            return out


def diskspace_statkeys(host: str, token: str, resource_id: str, verify: bool) -> list[str]:
    """이 리소스가 실제로 가진 statKey 중 'vsan|diskSpace|' 접두사(= vSAN > Disk Space
    메트릭 그룹)에 해당하는 것만 추린다."""
    data = api_get(host, token, f"/resources/{resource_id}/statkeys", verify)
    keys = [k["key"] for k in data.get("stat-key") or []]
    return sorted(k for k in keys if k.lower().startswith(DISKSPACE_PREFIX))


def latest_values(host: str, token: str, resource_id: str, keys: list[str], verify: bool) -> dict[str, tuple[float, int]]:
    if not keys:
        return {}
    data = api_post(
        host, token, "/resources/stats/latest/query",
        {"resourceId": [resource_id], "statKey": keys}, verify,
    )
    out: dict[str, tuple[float, int]] = {}
    for v in data.get("values") or []:
        for s in (v.get("stat-list") or {}).get("stat") or []:
            if s.get("data"):
                out[s["statKey"]["key"]] = (s["data"][0], (s.get("timestamps") or [0])[0])
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", required=True, help="VCF Operations 주소 (예: https://vcf-ops01.example.com)")
    ap.add_argument("--username", required=True, help="로그인 계정")
    ap.add_argument("--password", help="로그인 비밀번호 (생략 시 안전하게 프롬프트로 입력받음)")
    ap.add_argument("--auth-source", default="local", help="인증 소스 (기본: local)")
    ap.add_argument("--insecure", action="store_true", help="TLS 인증서 검증을 생략한다 (verify=False)")
    args = ap.parse_args()

    host = args.host.rstrip("/")
    password = args.password or getpass.getpass("비밀번호: ")
    verify = not args.insecure

    try:
        token = acquire_token(host, args.username, password, args.auth_source, verify)
        clusters = list_vsan_clusters(host, token, verify)
    except OpsApiError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1

    if not clusters:
        print("vSAN Cluster 리소스를 찾지 못했습니다 (VirtualAndPhysicalSANAdapter / VirtualSANDCCluster).")
        return 0

    print(f"vSAN 클러스터 {len(clusters)}개 발견\n")

    for res in clusters:
        rid = res["identifier"]
        name = res.get("resourceKey", {}).get("name", rid)
        try:
            keys = diskspace_statkeys(host, token, rid, verify)
            values = latest_values(host, token, rid, keys, verify)
        except OpsApiError as exc:
            print(f"=== {name} ===\n  조회 실패: {exc}\n")
            continue

        print(f"=== {name} (id={rid}) ===")
        if not keys:
            print("  'vSAN > Disk Space' 메트릭을 찾지 못했습니다.")
        for key in keys:
            if key in values:
                value, ts_ms = values[key]
                print(f"  {key:45s} = {value}")
            else:
                print(f"  {key:45s} = (값 없음)")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
