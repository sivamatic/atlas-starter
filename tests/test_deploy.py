"""Deploy files are well formed: the container and Kubernetes manifests match the app.

Run:  pytest -m deploy
Needs PyYAML for the manifest checks (pip install pyyaml); they are skipped without it.
"""
from pathlib import Path

import pytest

from atlas.app import create_app

pytestmark = pytest.mark.deploy
ROOT = Path(__file__).resolve().parent.parent
K8S = ROOT / "deploy" / "k8s"


def manifests():
    yaml = pytest.importorskip("yaml")
    docs = {}
    for f in sorted(K8S.glob("*.yaml")):
        for d in yaml.safe_load_all(f.read_text(encoding="utf-8")):
            docs[d["kind"]] = d
    return docs


def test_dockerfile_runs_as_non_root_with_a_healthcheck():
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    users = [line.split()[1] for line in text.splitlines() if line.startswith("USER ")]
    assert users and users[-1].split(":")[0] not in ("root", "0")
    assert "HEALTHCHECK" in text and "/v1/health" in text
    assert "0.0.0.0" in text and "8000" in text


def test_manifests_parse_and_cover_the_pieces():
    assert set(manifests()) == {"ConfigMap", "Secret", "Deployment", "Service", "HorizontalPodAutoscaler"}


def test_probes_point_at_real_routes_and_the_pod_is_locked_down():
    d = manifests()["Deployment"]
    pod = d["spec"]["template"]["spec"]
    c = pod["containers"][0]
    routes = {r.path for r in create_app().routes}
    assert c["livenessProbe"]["httpGet"]["path"] == "/v1/health" in routes
    assert c["readinessProbe"]["httpGet"]["path"] == "/v1/ready" in routes
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert c["securityContext"]["allowPrivilegeEscalation"] is False
    assert c["securityContext"]["capabilities"]["drop"] == ["ALL"]
    assert c["resources"]["requests"]["cpu"] and c["resources"]["limits"]["memory"]
    assert c["ports"][0]["containerPort"] == 8000


def test_service_and_hpa_target_the_deployment():
    m = manifests()
    labels = m["Deployment"]["spec"]["template"]["metadata"]["labels"]
    assert m["Service"]["spec"]["selector"].items() <= labels.items()
    assert m["HorizontalPodAutoscaler"]["spec"]["scaleTargetRef"]["name"] == m["Deployment"]["metadata"]["name"]
    assert m["HorizontalPodAutoscaler"]["apiVersion"] == "autoscaling/v2"


def test_the_example_secret_holds_no_values():
    s = manifests()["Secret"]
    assert all(v == "" for v in s["stringData"].values())
