# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Added
- Initial production-grade helm chart structure
- Orchestrator supervisor loop with LangGraph
- GPU-aware inference server deployments
- Node exporter monitoring for TrueNAS Prometheus
- Basic secrets management for orchestrator

### Security
- Added GitHub token authentication to task acceptor
- Environment-based repo whitelisting
- Secret key references for API credentials

### Observability
- Health checks on orchestrator containers
- Readiness and liveness probes for inference servers
- Node metrics exposed to central Prometheus

---

## [0.1.0] - 2026-10-12

### Initial Release
- Helm chart with basic templates
- Orchestrator supervisor loop (partial)
- Coder worker jobs (placeholder)
- Inference server deployments (llama.cpp)
- GPU device plugin configuration
- Monitoring stack setup

---

[Unreleased]: https://github.com/UtopikLab/k8s-inference/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/UtopikLab/k8s-inference/releases/tag/v0.1.0
