# Sourced by the actions: `python` is a Python 3.10 or newer. Windows runners have it as `python`
# (their `python3` may be a stub), the others as `python3`.
python() {
    if command -v python3 >/dev/null && python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
        python3 "$@"
    else
        command python "$@"
    fi
}
