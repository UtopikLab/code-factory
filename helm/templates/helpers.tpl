{{/*
Name helper: returns the namespace-scoped name for a resource.
Set .FileNamespace (gpu, inference, orchestrator, monitoring) in the
namespace-scoped template, or use .Release.Namespace for cluster-scoped.
*/}}
{{- define "k8s-inference.name" -}}
{{- if .FileNamespace -}}{{- .FileNamespace }}{{- else -}}{{- with .Release -}}{{ .Namespace }}{{- else -}}k8s-inference{{- end -}}{{- end -}}
{{- end -}}

{{/*
Labels helper: standard labels applied to every resource.
Works whether called with a dict (adds Component) or root context (uses .Chart/.Release).
*/}}
{{- define "k8s-inference.labels" -}}
{{- $r := merge . (dict "Component" (default "generic")) -}}
app.k8s.io/name: {{ with $r.Release }}{{ .Name }}{{- else -}}k8s-inference{{- end -}}
app.k8s.io/instance: {{ with $r.Release }}{{ .Name }}{{- else -}}k8s-inference{{- end -}}
app.k8s.io/version: {{ with $r.Chart }}{{ .AppVersion | quote }}{{- else -}}"0.0.0"{{- end -}}
app.k8s.io/component: {{ $r.Component }}
{{- end -}}

{{/*
Host network / IPC / PID flags for K3s daemon pods (device-plugin, node-exporter).
*/}}
{{- define "k8s-inference.hostAccess" -}}
hostNetwork: {{ .hostNetwork | default true }}
hostIPC: {{ .hostIPC | default true }}
hostPID: {{ .hostPID | default true }}
tolerations:
  - operator: Exists
    effect: NoExecute
    tolerationSeconds: 30
  - operator: Exists
    effect: NoSchedule
    tolerationSeconds: 0
  - operator: Exists
    effect: PreferNoSchedule
    tolerationSeconds: 0
{{- end -}}
