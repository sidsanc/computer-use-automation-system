# Computer-Use Automation System

Work in progress. Setup and the demo path will be documented here.

## Mock target app

```
uv sync
uv run cua serve-mock --variant a --port 5001
uv run cua faults set slow_load 4 --port 5001
uv run cua faults show --port 5001
```
