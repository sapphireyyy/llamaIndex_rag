#!/bin/sh
set -eu

: "${WEB_APP_ENVIRONMENT:=staging}"
: "${WEB_AUTH_MODE:=oidc}"
: "${WEB_OIDC_SCOPES:=profile,email}"
: "${WEB_OIDC_MIN_VALIDITY_SECONDS:=30}"

require_value() {
  variable_name="$1"
  eval "variable_value=\${$variable_name:-}"
  if [ -z "$variable_value" ]; then
    echo "runtime OIDC configuration is incomplete: $variable_name" >&2
    exit 1
  fi
}

safe_public_value() {
  variable_name="$1"
  eval "variable_value=\${$variable_name:-}"
  if ! printf '%s' "$variable_value" | grep -Eq '^[A-Za-z0-9:/._?&=%+@,#~-]+$'; then
    echo "runtime OIDC configuration contains unsupported characters: $variable_name" >&2
    exit 1
  fi
}

if [ "$WEB_AUTH_MODE" = "oidc" ]; then
  for variable_name in WEB_OIDC_URL WEB_OIDC_REALM WEB_OIDC_CLIENT_ID WEB_OIDC_REDIRECT_URI WEB_OIDC_POST_LOGOUT_REDIRECT_URI WEB_OIDC_SILENT_CHECK_SSO_REDIRECT_URI; do
    require_value "$variable_name"
  done
fi

for variable_name in WEB_APP_ENVIRONMENT WEB_AUTH_MODE WEB_OIDC_URL WEB_OIDC_REALM WEB_OIDC_CLIENT_ID WEB_OIDC_REDIRECT_URI WEB_OIDC_POST_LOGOUT_REDIRECT_URI WEB_OIDC_SILENT_CHECK_SSO_REDIRECT_URI WEB_OIDC_SCOPES WEB_OIDC_MIN_VALIDITY_SECONDS; do
  safe_public_value "$variable_name"
done

case "$WEB_OIDC_MIN_VALIDITY_SECONDS" in
  ''|*[!0-9]*) echo "runtime OIDC configuration has an invalid renewal window" >&2; exit 1 ;;
esac

export WEB_APP_ENVIRONMENT WEB_AUTH_MODE WEB_OIDC_URL WEB_OIDC_REALM WEB_OIDC_CLIENT_ID
export WEB_OIDC_REDIRECT_URI WEB_OIDC_POST_LOGOUT_REDIRECT_URI
export WEB_OIDC_SILENT_CHECK_SSO_REDIRECT_URI WEB_OIDC_SCOPES WEB_OIDC_MIN_VALIDITY_SECONDS

runtime_directory=/tmp/enterprise-rag-runtime
runtime_file="$runtime_directory/runtime-config.js"
mkdir -p "$runtime_directory"
umask 077
envsubst '${WEB_APP_ENVIRONMENT} ${WEB_AUTH_MODE} ${WEB_OIDC_URL} ${WEB_OIDC_REALM} ${WEB_OIDC_CLIENT_ID} ${WEB_OIDC_REDIRECT_URI} ${WEB_OIDC_POST_LOGOUT_REDIRECT_URI} ${WEB_OIDC_SILENT_CHECK_SSO_REDIRECT_URI} ${WEB_OIDC_SCOPES} ${WEB_OIDC_MIN_VALIDITY_SECONDS}' \
  < /etc/enterprise-rag/runtime-config.js.template > "$runtime_file.tmp"
chmod 0444 "$runtime_file.tmp"
mv "$runtime_file.tmp" "$runtime_file"
