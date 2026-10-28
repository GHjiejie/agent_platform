{{- define "platform.fullname" -}}
{{- default .Chart.Name .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- define "platform.selectorLabels" -}}
app: platform-{{ include "platform.fullname" . }}
{{- end -}}
{{- define "platform.labels" -}}
{{ include "platform.selectorLabels" . }}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | quote }}
{{- end -}}

{{- define "platform.validate" -}}
{{- if and .Values.database.url (not .Values.sqliteStorage.enabled) -}}
{{- fail "database.url is only allowed with sqliteStorage.enabled=true; PostgreSQL URLs belong in an existing Secret" -}}
{{- end -}}
{{- if and .Values.database.url (not (regexMatch "^sqlite:/{3,4}[^[:space:]]+$" .Values.database.url)) -}}
{{- fail "database.url must be a SQLite URL without whitespace; PostgreSQL URLs belong in an existing Secret" -}}
{{- end -}}
{{- if .Values.sqliteStorage.enabled -}}
{{- if not .Values.database.url -}}
{{- fail "sqliteStorage.enabled requires an explicit SQLite database.url" -}}
{{- end -}}
{{- if not .Values.nodeSelector -}}
{{- fail "sqliteStorage.enabled requires nodeSelector to pin the shared local database" -}}
{{- end -}}
{{- if gt (int .Values.replicaCount) 1 -}}
{{- fail "SQLite requires replicaCount <= 1; use PostgreSQL before scaling" -}}
{{- end -}}
{{- if ne .Values.strategy.type "Recreate" -}}
{{- fail "SQLite requires strategy.type=Recreate to prevent overlapping replicas" -}}
{{- end -}}
{{- end -}}
{{- if lt (int .Values.terminationGracePeriodSeconds) (add (int .Values.config.runTimeout) 30) -}}
{{- fail "worker terminationGracePeriodSeconds must be at least config.runTimeout + 30" -}}
{{- end -}}
{{- end -}}
