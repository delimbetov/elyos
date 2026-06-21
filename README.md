# Elyos CLI

## Run

Requires Python 3.11 or newer.

```bash
cd cli
python -m venv .venv
source .venv/bin/activate
python -m pip install .
python cli.py
```

Set these environment variables, either in the shell or a `.env` file:

```text
OPENAI_API_KEY
ELYOS_API_KEY
```

`OPENAI_MODEL` is optional and defaults to `gpt-5-nano`.

## Use

Enter a question at the `You:` prompt. The assistant can fetch weather or research a topic.

Press Ctrl+C to cancel the current response or tool call. Enter `q`, `quit`, or `exit`, or press Ctrl+D, to close the CLI.
