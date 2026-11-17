# Atlas on Kubernetes (optional)

Plain manifests for a local cluster (kind or minikube). They are the same shape you would use on
AKS, EKS or GKE; only the image location and the secret source change.

| File | What it does |
| --- | --- |
| `configmap.yaml` | Non-secret settings: model mode, dataset, rerank, tracing |
| `secret.example.yaml` | The shape of the secret (model key, database URL). An example: create the real one from your shell |
| `deployment.yaml` | 2 replicas, liveness on `/v1/health`, readiness on `/v1/ready`, CPU and memory requests, non-root, read-only root filesystem, all capabilities dropped |
| `service.yaml` | A ClusterIP service on port 80 to the pods' port 8000 |
| `hpa.yaml` | Scale 2 to 6 replicas at 70% CPU (`autoscaling/v2`) |

Why two probes: liveness asks "is the process stuck?" and a failure restarts the pod. Readiness
asks "should this pod get traffic?" and a failure only takes it out of the service. Atlas's
readiness checks the model adapter, the budget and the knowledge base, so a pod with a broken
model setting stops getting traffic instead of being restarted forever.

## Run on kind

```powershell
docker build -t atlas:solved --build-arg APPLY_SOLUTIONS=1 .     # from atlas-starter
kind create cluster --name atlas
kind load docker-image atlas:solved --name atlas
kubectl apply -f deploy/k8s/configmap.yaml -f deploy/k8s/secret.example.yaml
kubectl apply -f deploy/k8s/deployment.yaml -f deploy/k8s/service.yaml -f deploy/k8s/hpa.yaml
kubectl rollout status deployment/atlas
kubectl port-forward service/atlas 8000:80
curl http://localhost:8000/v1/ready
```

On minikube, replace the two kind lines with `minikube start` and `minikube image load atlas:solved`.

The HPA needs metrics-server in the cluster. Without it the HPA shows `<unknown>` targets and
never scales; everything else works. See the HPA walkthrough linked below.

For real secrets, skip `secret.example.yaml`:

```powershell
kubectl create secret generic atlas-secrets --from-literal=LLM_API_KEY=... --from-literal=ATLAS_DB_URL=postgresql://...
```

## Clean up

```powershell
kubectl delete -f deploy/k8s/
kind delete cluster --name atlas
```

## What was validated

- All five files pass `kubeconform -strict` against the Kubernetes 1.30 schemas.
- `tests/test_deploy.py` checks that they parse, that the probes point at real Atlas routes and
  that the pod cannot run as root.
- The image builds, runs as uid 10001 and reports `healthy` with Docker's own HEALTHCHECK.
- They were not applied to a running cluster while writing the course (no kind or minikube was
  available). Do that as part of the lab.

## Sources

- Configure a security context: https://kubernetes.io/docs/tasks/configure-pod-container/security-context/
- Pod Security Standards (restricted): https://kubernetes.io/docs/concepts/security/pod-security-standards/
- HPA walkthrough (`autoscaling/v2`, metrics-server): https://kubernetes.io/docs/tasks/run-application/horizontal-pod-autoscale-walkthrough/
- kind quick start (`kind load docker-image`): https://kind.sigs.k8s.io/docs/user/quick-start/
- minikube image load: https://minikube.sigs.k8s.io/docs/commands/image/
