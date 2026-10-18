#!/usr/bin/env bash
# Installation du contrôleur infra-mcp, en une commande (en root, sur le contrôleur) :
#
#   curl -fsSL https://raw.githubusercontent.com/quiche-lorraine/mcp-admin/main/install.sh | sudo bash
#
# Options d'infra-mcp-admin install après « bash -s -- », par exemple :
#   ... | sudo bash -s -- --repo quiche-lorraine/infra --domain mcp.exemple.fr
#
# Ce script ne fait que récupérer infra-mcp-admin et lui passer la main. Une
# fois le repo de config cloné, infra-mcp-admin continue à la version que ce
# repo fige (requirements.txt). INFRA_MCP_REF choisit une autre branche ou un
# commit de mcp-admin pour ce premier script.

set -euo pipefail

main() {
    local ref=${INFRA_MCP_REF:-main}
    local url="https://raw.githubusercontent.com/quiche-lorraine/mcp-admin/$ref/src/infra_mcp/deploy/infra-mcp-admin"
    [[ $(id -u) == 0 ]] || { echo "à lancer en root : ... | sudo bash" >&2; exit 1; }
    command -v curl >/dev/null || { apt-get update -qq && apt-get install -y -qq curl; }
    WORK_DIR=$(mktemp -d)
    trap 'rm -rf "$WORK_DIR"' EXIT
    curl -fsSL -o "$WORK_DIR/infra-mcp-admin" "$url"
    # Le script arrive par un tube : les questions se posent au terminal.
    if [[ -t 0 ]]; then
        bash "$WORK_DIR/infra-mcp-admin" install "$@"
    elif { : </dev/tty; } 2>/dev/null; then
        bash "$WORK_DIR/infra-mcp-admin" install "$@" </dev/tty
    else
        echo "pas de terminal pour les questions : lance la commande depuis une session SSH" >&2
        exit 1
    fi
}

# Tout est lu avant de s'exécuter : sûr même à travers « curl | bash ».
main "$@"
