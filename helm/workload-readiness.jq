[.items[] | {
  kind,
  name: .metadata.name,
  desired: (if .kind == "DaemonSet" then (.status.desiredNumberScheduled // 0) else (.spec.replicas // 1) end),
  ready: (if .kind == "DaemonSet" then (.status.numberReady // 0) elif .kind == "StatefulSet" then (.status.readyReplicas // 0) else (.status.availableReplicas // 0) end)
}] as $workloads
| ($workloads | length) > 0
  and all($workloads[]; .ready >= .desired)
