#!/usr/bin/env bash
# Shared helpers for install.sh (also tested via scripts/test_install_lib.sh)

validate_domain() {
  local d="$1"
  [[ "$d" =~ ^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)+$ ]]
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  validate_domain "${1:-}" && echo OK || echo FAIL
fi

check_os() {
  if [[ -f /etc/os-release ]]; then
    # shellcheck source=/dev/null
    source /etc/os-release
    case "${ID:-}" in
      ubuntu|debian) return 0 ;;
    esac
  fi
  return 1
}
