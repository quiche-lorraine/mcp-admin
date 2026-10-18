#!/usr/bin/env bash
# Installation du contrôleur infra-mcp, en une commande (sur le contrôleur) :
#
#   sudo bash -c "$(curl -fsSL https://raw.githubusercontent.com/quiche-lorraine/mcp-admin/main/install.sh)"
#
# Options d'infra-mcp-admin install après « -- », par exemple :
#   sudo bash -c "$(curl -fsSL …/install.sh)" -- --repo quiche-lorraine/infra --domain mcp.exemple.fr
#
# Pas de « curl … | sudo bash » : sudo lance alors la commande hors du premier
# plan du terminal, qui suspend tout ce qui le touche (apt, saisie du mot de
# passe vault). Le script le détecte et donne la bonne commande.
#
# Ce script ne fait que récupérer infra-mcp-admin et lui passer la main. Une
# fois le repo de config cloné, infra-mcp-admin continue à la version que ce
# repo fige (requirements.txt). INFRA_MCP_REF choisit une autre branche ou un
# commit de mcp-admin pour ce premier script.

set -euo pipefail

main() {
    local ref=${INFRA_MCP_REF:-main}
    local url="https://raw.githubusercontent.com/quiche-lorraine/mcp-admin/$ref/src/infra_mcp/deploy/infra-mcp-admin"
    local usage="sudo bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/quiche-lorraine/mcp-admin/main/install.sh)\""
    [[ $(id -u) == 0 ]] || { echo "à lancer en root : $usage" >&2; exit 1; }
    if ! { : </dev/tty; } 2>/dev/null; then
        echo "pas de terminal pour les questions : lance la commande depuis une session SSH" >&2
        exit 1
    fi
    # Hors du premier plan du terminal (« curl | sudo bash »), la première saisie
    # ou le premier apt suspendrait le script sans rien dire.
    local pgid tpgid
    pgid=$(ps -o pgid= -p $$ | tr -d ' ')
    tpgid=$(ps -o tpgid= -p $$ | tr -d ' ')
    if [[ $pgid != "$tpgid" ]]; then
        echo "lancé hors du premier plan du terminal (curl … | sudo bash ?), lance plutôt :" >&2
        echo "  $usage" >&2
        exit 1
    fi
    if ! command -v curl >/dev/null; then
        export DEBIAN_FRONTEND=noninteractive NEEDRESTART_SUSPEND=1 NEEDRESTART_MODE=l
        apt-get update -qq && apt-get install -y -qq curl </dev/null >/dev/null
    fi
    WORK_DIR=$(mktemp -d)
    trap 'rm -rf "$WORK_DIR"' EXIT
    curl -fsSL -o "$WORK_DIR/infra-mcp-admin" "$url"
    # Script arrivé par un tube (curl | bash, en root) : questions au terminal.
    bash "$WORK_DIR/infra-mcp-admin" install "$@" </dev/tty
}

# Tout est lu avant de s'exécuter, même à travers un tube.
main "$@"
