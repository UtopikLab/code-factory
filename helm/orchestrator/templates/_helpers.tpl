{{- define "orchestrator.supervisorSecretName" -}}
{{- printf "%s-supervisor-service-auth" .Chart.Name -}}
{{- end -}}
{{- define "orchestrator.pullRequestSecretName" -}}
{{- printf "%s-github-pr-auth" .Chart.Name -}}
{{- end -}}
{{- define "orchestrator.workerSecretName" -}}
{{- printf "%s-coder-model-auth" .Chart.Name -}}
{{- end -}}
{{- define "orchestrator.agentRuntimeConfigJson" -}}
{{- $coderUrl := default (index .Values.supervisor.config "coder-url") .Values.worker.coderUrl -}}
{{- $coderNative := eq $coderUrl "http://llama-server-args-gpu0.inference.svc.cluster.local:8000/v1" -}}
{{- if kindIs "bool" .Values.worker.coderNativeDownload -}}
{{- $coderNative = .Values.worker.coderNativeDownload -}}
{{- end -}}
{{- $judgeNative := eq (index .Values.supervisor.config "judge-url") "http://llama-server-args-gpu1.inference.svc.cluster.local:8000/v1" -}}
{{- if kindIs "bool" .Values.worker.judgeNativeDownload -}}
{{- $judgeNative = .Values.worker.judgeNativeDownload -}}
{{- end -}}
{{- $config := dict
	"catalog" (.Files.Get "files/agent-catalog.yaml")
	"version" (.Values.agentCatalog.version | default "1")
	"acceptor_url" (index .Values.supervisor.config "acceptor-url")
	"coder_url" $coderUrl
	"coder_native_download" $coderNative
	"judge_url" (index .Values.supervisor.config "judge-url")
	"judge_native_download" $judgeNative
	"coder_model" (index .Values.supervisor.config "coder-model")
	"judge_model" (index .Values.supervisor.config "judge-model")
	"threshold" (index .Values.supervisor.config "acceptance-threshold")
	"max_total_attempts" (index .Values.supervisor.config "max-total-attempts")
	"allowed_base_branches" (index .Values.supervisor.config "allowed-base-branches")
	"reviewers" (index .Values.supervisor.config "reviewers")
	"repo_policies" .Values.worker.repoPolicies
	"worker_parallelism" (index .Values.supervisor.config "worker-parallelism")
-}}
{{- toJson $config -}}
{{- end -}}
{{- define "orchestrator.agentCatalogHash" -}}
{{- printf "%s%s%s" (.Files.Get "files/agent-catalog.yaml") (.Files.Get "files/agent-catalog.schema.json") (include "orchestrator.agentRuntimeConfigJson" .) | sha256sum -}}
{{- end -}}