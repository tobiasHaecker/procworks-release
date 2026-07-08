{{/* Common naming + label helpers. */}}

{{- define "procworks.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "procworks.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "procworks.labels" -}}
app.kubernetes.io/name: {{ include "procworks.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{- define "procworks.api.fullname" -}}
{{- printf "%s-api" (include "procworks.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "procworks.web.fullname" -}}
{{- printf "%s-web" (include "procworks.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Name of the secret holding DATABASE_URL. */}}
{{- define "procworks.database.secretName" -}}
{{- if .Values.database.createSecret -}}
{{- printf "%s-db" (include "procworks.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- .Values.database.existingSecret -}}
{{- end -}}
{{- end -}}

{{- define "procworks.api.image" -}}
{{- $tag := .Values.image.apiTag | default .Chart.AppVersion -}}
{{- printf "%s/%s-api:%s" .Values.image.registry .Values.image.repository $tag -}}
{{- end -}}

{{- define "procworks.web.image" -}}
{{- $tag := .Values.image.webTag | default .Chart.AppVersion -}}
{{- printf "%s/%s-web:%s" .Values.image.registry .Values.image.repository $tag -}}
{{- end -}}

{{/* Backup component names. */}}
{{- define "procworks.backup.fullname" -}}
{{- printf "%s-backup" (include "procworks.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "procworks.backup.configMapName" -}}
{{- printf "%s-backup-scripts" (include "procworks.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Name of the PVC holding the backups (existing claim wins). */}}
{{- define "procworks.backup.pvcName" -}}
{{- if .Values.backup.storage.existingClaim -}}
{{- .Values.backup.storage.existingClaim -}}
{{- else -}}
{{- printf "%s-backups" (include "procworks.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
