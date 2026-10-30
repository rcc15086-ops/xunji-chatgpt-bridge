# Xunji read-only MCP bridge

A remote MCP server that provides ChatGPT-compatible read access to Xunji training, body, food, and plan data.

## Security
- Xunji keys are stored only in Render environment variables.
- No secrets are committed to this repository.
- Every MCP request requires a separate Bearer token.
- This service implements read/query endpoints only. It cannot write training, food, body records, or templates.
- The waist field uses Xunji's historical spelling: `weist`.

## MCP tools
- `get_training_record(datestr, include_full_data)`
- `get_body_records(start_date, end_date, types, include_latest, include_records, limit, offset)`
- `get_food_records(start_date, end_date, include_detail)`
- `list_training_plans()`
- `get_training_plan(plan_ref, start_date, end_date, include_movements)`

## Deploy
Render runs `pip install -r requirements.txt` then `uvicorn server:app --host 0.0.0.0 --port $PORT`.
The health check is `/health`; MCP endpoint is `/mcp`.
