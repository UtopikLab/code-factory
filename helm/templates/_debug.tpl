{{- $i := index .Values "inference" -}}
DEBUG inference type: {{ kindOf $i }}
DEBUG pvc type: {{ kindOf (index $i "pvc") }}
DEBUG storageClassName type: {{ kindOf (index (index $i "pvc") "storageClassName") }}
