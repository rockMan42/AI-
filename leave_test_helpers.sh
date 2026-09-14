leave_api() {
  PYTHONPATH=. .venv/bin/python -B scripts/manual_leave_request.py "$@"
}

json_field() {
  .venv/bin/python -B -c \
    'import json,sys; print(json.load(sys.stdin)[sys.argv[1]])' "$1"
}