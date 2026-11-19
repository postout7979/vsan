#!/usr/bin/env python3
"""
vsan_diskspace_metrics.py

Fetch the "vSAN > Disk Space" metric group for a vSAN Cluster resource
(VSAN adapter) from VCF Operations (the former vRealize/Aria Operations),
using the Suite API (/suite-api/api/...).

Metrics collected (as shown under vSAN > Disk Space in the UI):
  - Disk Space Used (%)
  - Effective Capacity Remaining (%)
  - Free Effective Capacity (GB)
  - Provisioned Effective Capacity (GB)
  - Total Disk Space (GB)
  - Total Effective Capacity (GB)
  - Usable Capacity (GB)
  - Used Disk Space (GB)
  - Used Effective Capacity (GB)

How it works
------------
1. Log in and obtain a Suite API bearer token.
2. Find the vSAN Cluster resource (adapter kind "VSAN") by name.
3. Look up the resource kind's attribute (metric) definitions, and match
   the ones whose display name is in the list above. This avoids
   hard-coding the internal stat-key strings, which can differ slightly
   between versions.
4. Query the latest value for each matched stat key.
5. Print a table, and optionally write the result to a JSON or CSV file.

Assumptions / not verified
---------------------------
- VCF Operations 9.1 is assumed to expose the same Suite API
  (/suite-api/api/...) used by vRealize/Aria Operations (auth, resources,
  resources/query, adaptertypes/.../attributes, stats/latest). This has
  not been tested against a live VCF 9.1 Operations instance; if an
  endpoint path has changed, adjust the *_PATH constants near the top.
- The resource is looked up by adapter kind "VSAN" and a resource kind
  whose key contains "cluster" (case-insensitive). If your environment
  uses a different resource kind key, use --resource-kind to override.
- Self-signed certificates are common in closed/air-gapped environments;
  use --insecure to skip TLS verification (off by default).

Usage
-----
  # Fetch every vSAN Cluster resource found under vSAN Adapter > vSAN Cluster:
  python3 vsan_diskspace_metrics.py --host vcfops.example.local

  # Address scheme is optional - "https://" is added automatically if missing.
  # Username / password are prompted for if not supplied on the command line.

  # Limit to one cluster by name:
  python3 vsan_diskspace_metrics.py \
      --host vcfops.example.local \
      --username admin \
      --cluster "vSAN-Cluster-01" \
      --insecure \
      --output-json result.json \
      --output-csv result.csv
"""

import argparse
import csv
import getpass
import json
import sys
import urllib3
from typing import Any, Dict, List, Optional

import requests

# ---------------------------------------------------------------------------
# Suite API paths (standard vRealize / Aria / VCF Operations Suite API)
# ---------------------------------------------------------------------------
TOKEN_ACQUIRE_PATH = "/suite-api/api/auth/token/acquire"
TOKEN_RELEASE_PATH = "/suite-api/api/auth/token/release"
RESOURCES_QUERY_PATH = "/suite-api/api/resources/query"
ATTRIBUTES_PATH = "/suite-api/api/adaptertypes/{adapter_kind}/resourcekinds/{resource_kind}/attributes"
STATS_LATEST_PATH = "/suite-api/api/resources/{resource_id}/stats/latest"

# vSAN adapter kind key in VCF Operations / Aria Operations
VSAN_ADAPTER_KIND = "VSAN"

# Display names of the metrics shown under vSAN > Disk Space in the UI.
TARGET_METRIC_NAMES = [
    "Disk Space Used (%)",
    "Effective Capacity Remaining (%)",
    "Free Effective Capacity (GB)",
    "Provisioned Effective Capacity (GB)",
    "Total Disk Space (GB)",
    "Total Effective Capacity (GB)",
    "Usable Capacity (GB)",
    "Used Disk Space (GB)",
    "Used Effective Capacity (GB)",
]


def normalize_host(host: str) -> str:
    """Add https:// if the user did not include a scheme, and strip a trailing slash."""
    host = host.strip()
    if not host.lower().startswith(("http://", "https://")):
        host = "https://" + host
    return host.rstrip("/")


def acquire_token(
    session: requests.Session,
    base_url: str,
    username: str,
    password: str,
    verify: bool,
    auth_source: str = "LOCAL",
) -> str:
    """Log in to VCF Operations and return a Suite API bearer token.

    auth_source should be "LOCAL" for a local VCF Operations account (the default and the
    case this script targets). A VIDB/SSO account authenticates differently and is out of
    scope here.
    """
    url = base_url + TOKEN_ACQUIRE_PATH
    resp = session.post(
        url,
        json={"username": username, "password": password, "authSource": auth_source},
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        verify=verify,
    )
    resp.raise_for_status()
    token = resp.json().get("token")
    if not token:
        raise RuntimeError("Login succeeded but no token was returned by the server.")
    # "OpsToken" is the current scheme in VCF Operations 9.x. The older "vRealizeOpsToken"
    # scheme is still accepted if your environment needs it.
    session.headers.update({
        "Authorization": f"OpsToken {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    })
    return token


def release_token(session: requests.Session, base_url: str, verify: bool) -> None:
    """Best-effort logout; failures here should not break the script."""
    try:
        session.post(base_url + TOKEN_RELEASE_PATH, verify=verify)
    except requests.RequestException:
        pass


def find_vsan_cluster_resources(
    session: requests.Session,
    base_url: str,
    verify: bool,
    cluster_name: Optional[str] = None,
    resource_kind_override: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return every vSAN Cluster resource under the VSAN adapter.

    With cluster_name omitted (the default), every vSAN Cluster resource found under
    VSAN Adapter > vSAN Cluster is returned. Pass cluster_name to narrow the result to
    one cluster (case-insensitive exact match).
    """
    url = base_url + RESOURCES_QUERY_PATH
    body: Dict[str, Any] = {"adapterKind": [VSAN_ADAPTER_KIND]}
    if resource_kind_override:
        body["resourceKind"] = [resource_kind_override]
    if cluster_name:
        body["name"] = [cluster_name]

    resp = session.post(url, json=body, verify=verify)
    resp.raise_for_status()
    resources = resp.json().get("resourceList", [])

    if not resource_kind_override:
        # Keep only resources whose resource kind looks like a vSAN cluster (not hosts, disk
        # groups, etc. that also live under the VSAN adapter).
        resources = [
            r for r in resources
            if "cluster" in r.get("resourceKey", {}).get("resourceKindKey", "").lower()
        ] or resources

    if cluster_name:
        exact = [r for r in resources if r.get("resourceKey", {}).get("name", "").lower() == cluster_name.lower()]
        resources = exact or resources

    if not resources:
        where = f"named '{cluster_name}'" if cluster_name else ""
        raise RuntimeError(
            f"No VSAN-adapter vSAN Cluster resource {where} was found. "
            "Check VCF Operations under vSAN Adapter > vSAN Cluster, "
            "or pass --resource-kind if your environment uses a non-default resource kind."
        )
    return resources


def get_metric_keys(
    session: requests.Session,
    base_url: str,
    adapter_kind: str,
    resource_kind: str,
    verify: bool,
    target_names: List[str],
) -> Dict[str, str]:
    """Return {display_name: stat_key} for the attributes whose display name is in target_names."""
    url = base_url + ATTRIBUTES_PATH.format(adapter_kind=adapter_kind, resource_kind=resource_kind)
    resp = session.get(url, verify=verify)
    resp.raise_for_status()
    attrs = resp.json().get("resourceTypeAttributes", [])

    wanted = {name.lower(): name for name in target_names}
    found: Dict[str, str] = {}
    for attr in attrs:
        display_name = attr.get("name", "")
        key = attr.get("key", "")
        if display_name.lower() in wanted and key:
            found[wanted[display_name.lower()]] = key

    missing = [name for name in target_names if name not in found]
    if missing:
        print(
            "Warning: the following metrics were not found in this resource kind's attribute list "
            f"and will be skipped: {missing}",
            file=sys.stderr,
        )
    return found


def get_latest_stats(
    session: requests.Session,
    base_url: str,
    resource_id: str,
    stat_keys: List[str],
    verify: bool,
) -> Dict[str, float]:
    """Return {stat_key: latest_value} for the given resource and stat keys."""
    if not stat_keys:
        return {}
    url = base_url + STATS_LATEST_PATH.format(resource_id=resource_id)
    resp = session.get(url, params={"statKey": stat_keys}, verify=verify)
    resp.raise_for_status()
    payload = resp.json()

    values: Dict[str, float] = {}
    for entry in payload.get("values", []):
        for stat in entry.get("stat-list", {}).get("stat", []):
            key = stat.get("statKey", {}).get("key")
            data = stat.get("data", [])
            if key and data:
                values[key] = data[-1]  # most recent sample
    return values


def collect_metrics_for_resource(
    session: requests.Session,
    base_url: str,
    resource: Dict[str, Any],
    verify: bool,
    metric_key_cache: Dict[str, Dict[str, str]],
) -> Dict[str, Any]:
    """Resolve and fetch the Disk Space metrics for one already-found resource."""
    resource_id = resource["identifier"]
    adapter_kind = resource["resourceKey"]["adapterKindKey"]
    resource_kind = resource["resourceKey"]["resourceKindKey"]

    # Metric-key resolution depends only on (adapter kind, resource kind), so it is cached
    # and reused across clusters of the same resource kind instead of being looked up every time.
    cache_key = f"{adapter_kind}:{resource_kind}"
    if cache_key not in metric_key_cache:
        metric_key_cache[cache_key] = get_metric_keys(
            session, base_url, adapter_kind, resource_kind, verify, TARGET_METRIC_NAMES
        )
    name_to_key = metric_key_cache[cache_key]

    key_to_value = get_latest_stats(session, base_url, resource_id, list(name_to_key.values()), verify)
    metrics = {name: key_to_value.get(key) for name, key in name_to_key.items()}

    return {
        "cluster_name": resource["resourceKey"]["name"],
        "resource_id": resource_id,
        "adapter_kind": adapter_kind,
        "resource_kind": resource_kind,
        "metrics": metrics,
    }


def collect_disk_space_metrics(
    base_url: str,
    username: str,
    password: str,
    verify: bool,
    cluster_name: Optional[str] = None,
    resource_kind_override: Optional[str] = None,
    auth_source: str = "LOCAL",
) -> List[Dict[str, Any]]:
    """End-to-end: log in, find every vSAN Cluster resource (or just cluster_name if given),
    resolve metric keys, fetch latest values for each, log out."""
    session = requests.Session()
    token = acquire_token(session, base_url, username, password, verify, auth_source)
    try:
        resources = find_vsan_cluster_resources(session, base_url, verify, cluster_name, resource_kind_override)
        metric_key_cache: Dict[str, Dict[str, str]] = {}
        return [
            collect_metrics_for_resource(session, base_url, resource, verify, metric_key_cache)
            for resource in resources
        ]
    finally:
        release_token(session, base_url, verify)
        _ = token  # token itself is not needed after this point


def print_table(result: Dict[str, Any]) -> None:
    print(f"\nvSAN Cluster: {result['cluster_name']}  (resource id: {result['resource_id']})")
    print(f"Adapter kind: {result['adapter_kind']}   Resource kind: {result['resource_kind']}")
    print("-" * 60)
    name_width = max(len(name) for name in TARGET_METRIC_NAMES) + 2
    for name in TARGET_METRIC_NAMES:
        value = result["metrics"].get(name)
        shown = "N/A" if value is None else f"{value:,.2f}"
        print(f"{name:<{name_width}}: {shown}")
    print("-" * 60)


def print_results(results: List[Dict[str, Any]]) -> None:
    print(f"\nFound {len(results)} vSAN Cluster resource(s).")
    for result in results:
        print_table(result)


def write_json(results: List[Dict[str, Any]], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"JSON written to {path}")


def write_csv(results: List[Dict[str, Any]], path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["cluster_name", "metric", "value"])
        for result in results:
            for name, value in result["metrics"].items():
                writer.writerow([result["cluster_name"], name, value])
    print(f"CSV written to {path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch vSAN > Disk Space metrics from VCF Operations.")
    parser.add_argument("--host", help="VCF Operations address, e.g. vcfops.example.local (https:// is added automatically)")
    parser.add_argument("--username", help="VCF Operations username")
    parser.add_argument("--password", help="VCF Operations password (omit to be prompted securely)")
    parser.add_argument("--auth-source", default="LOCAL", help='Auth source for login (default: "LOCAL", for a local VCF Operations account)')
    parser.add_argument(
        "--cluster",
        help="Optional vSAN Cluster name to limit the query to one cluster. "
             "Omit to fetch every vSAN Cluster resource under vSAN Adapter > vSAN Cluster.",
    )
    parser.add_argument("--resource-kind", help="Override the resource kind key if auto-detection does not match your environment")
    parser.add_argument("--insecure", action="store_true", help="Skip TLS certificate verification (self-signed certificates)")
    parser.add_argument("--output-json", help="Optional path to also write the result as JSON")
    parser.add_argument("--output-csv", help="Optional path to also write the result as CSV")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    host = args.host or input("VCF Operations address (e.g. vcfops.example.local): ")
    base_url = normalize_host(host)

    username = args.username or input("Username: ")
    password = args.password or getpass.getpass("Password: ")

    cluster_name = args.cluster  # optional: None means "every vSAN Cluster resource"

    verify = not args.insecure
    if args.insecure:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    try:
        results = collect_disk_space_metrics(
            base_url=base_url,
            username=username,
            password=password,
            verify=verify,
            cluster_name=cluster_name,
            resource_kind_override=args.resource_kind,
            auth_source=args.auth_source,
        )
    except requests.HTTPError as exc:
        print(f"HTTP error: {exc}", file=sys.stderr)
        return 1
    except requests.RequestException as exc:
        print(f"Connection error: {exc}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print_results(results)
    if args.output_json:
        write_json(results, args.output_json)
    if args.output_csv:
        write_csv(results, args.output_csv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
