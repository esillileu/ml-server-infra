if [[ $- == *i* && -n ${SSH_CONNECTION:-} ]]; then
    if [[ -x ${HOME}/.local/bin/svc-ln ]]; then
        "${HOME}/.local/bin/svc-ln" status 2>/dev/null || true
    fi
fi
