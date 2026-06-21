from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable

import httpx
from dotenv import load_dotenv
from openai import AsyncOpenAI, OpenAIError


BASE_URL = "https://elyos-interview-907656039105.europe-west2.run.app"
INSTRUCTIONS = """
Use tools silently because the application shows pending status.
After a tool call, only clean up and restate its output:
- For weather, write one short sentence using only the returned values. Do not
  convert units, infer a forecast, or offer more help.
- For research, restate the summary faithfully. On the next line write
  "Sources: <comma-separated sources>". If a notice exists, add "Note: <notice>"
  on one final line. Do not add a title, context, claims, or follow-up questions.
  The summary is mandatory; never return sources without it.
- For an error, state only the error in natural language.
Never expose raw JSON or internal field names.
""".strip()

def tool(
    name: str,
    description: str,
    argument: str,
    argument_description: str,
) -> dict[str, Any]:
    return {
        "type": "function",
        "name": name,
        "description": description,
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                argument: {
                    "type": "string",
                    "description": argument_description,
                }
            },
            "required": [argument],
            "additionalProperties": False,
        },
    }


TOOLS = [
    tool(
        "get_weather",
        "Get current weather for a concise city and optional country/region.",
        "location",
        "City, optionally followed by country or region. No units or other prose.",
    ),
    tool(
        "research_topic",
        "Research a concise topic in depth. This takes several seconds.",
        "topic",
        "Non-empty topic. The API only processes the first 50 characters.",
    ),
]


def ok(data: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "data": data}


def fail(message: str) -> dict[str, Any]:
    return {"ok": False, "error": message}


def weather_payload(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict) or not isinstance(data.get("location"), str):
        raise ValueError("Weather service returned an invalid response.")

    conditions = data.get("conditions")
    if conditions is None:
        conditions = [
            {
                "temperature_c": data.get("temperature_c"),
                "condition": data.get("condition"),
                "humidity": data.get("humidity"),
            }
        ]

    if not isinstance(conditions, list) or not conditions:
        raise ValueError("Weather service returned no conditions.")

    for item in conditions:
        valid = (
            isinstance(item, dict)
            and isinstance(item.get("temperature_c"), (int, float))
            and isinstance(item.get("condition"), str)
            and isinstance(item.get("humidity"), int)
        )
        if not valid:
            raise ValueError("Weather service returned invalid conditions.")

    result = {"location": data["location"], "conditions": conditions}
    if data.get("note"):
        result["note"] = data["note"]
    return result


def research_payload(data: Any) -> dict[str, Any]:
    required = ("topic", "summary", "sources", "generated_at")
    if not isinstance(data, dict) or not all(key in data for key in required):
        raise ValueError("Research service returned an empty or invalid response.")

    strings = all(
        isinstance(data[key], str) for key in ("topic", "summary", "generated_at")
    )
    if not strings or not data["summary"].strip():
        raise ValueError("Research service returned an empty summary.")

    sources = data["sources"]
    if not isinstance(sources, list) or not all(
        isinstance(source, str) for source in sources
    ):
        raise ValueError("Research service returned invalid sources.")

    result = {"summary": data["summary"], "sources": sources}
    if data.get("cached"):
        result["notice"] = "This result is cached and may be stale."
    if data.get("truncated"):
        processed = data.get("processed_topic")
        result["notice"] = (
            f"The API researched only the first 50 characters: {processed!r}."
        )
    return result


def http_error(response: httpx.Response, service: str) -> dict[str, Any]:
    if response.status_code == 401:
        return fail(f"{service} authentication failed.")
    if response.status_code == 404:
        message = (
            "Location not found."
            if service == "Weather service"
            else "Research service found no result."
        )
        return fail(message)
    if response.status_code == 422:
        return fail(f"{service} rejected the request.")
    return fail(f"{service} failed with HTTP {response.status_code}.")


@dataclass(frozen=True)
class ToolHandler:
    argument: str
    path: str
    timeout: float
    service: str
    payload: Callable[[Any], dict[str, Any]]
    message: str


async def call_tool_api(
    http: httpx.AsyncClient,
    value: str,
    handler: ToolHandler,
) -> dict[str, Any]:
    value = value.strip()
    label = f"{handler.service.removesuffix(' service')} {handler.argument}"
    if not value:
        return fail(f"{label} cannot be empty.")

    try:
        response = await http.get(
            handler.path,
            params={handler.argument: value},
            timeout=handler.timeout,
        )
        if response.is_error:
            return http_error(response, handler.service)
        data = response.json()
        return ok(handler.payload(data))
    except (httpx.TimeoutException, httpx.TransportError):
        return fail(f"{handler.service} did not respond.")
    except json.JSONDecodeError:
        return fail(f"{handler.service} returned invalid JSON.")
    except ValueError as exc:
        return fail(str(exc))


TOOL_HANDLERS = {
    "get_weather": ToolHandler(
        argument="location",
        path="/weather",
        timeout=8,
        service="Weather service",
        payload=weather_payload,
        message="Checking weather for {value}…",
    ),
    "research_topic": ToolHandler(
        argument="topic",
        path="/research",
        timeout=10,
        service="Research service",
        payload=research_payload,
        message="Researching {value}…",
    ),
}


class DelayedStatus:
    def __init__(self, message: str) -> None:
        self.message = message
        self.finished = asyncio.Event()
        self.task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> "DelayedStatus":
        async def show() -> None:
            try:
                await asyncio.wait_for(self.finished.wait(), timeout=0.25)
                return
            except TimeoutError:
                pass

            if not sys.stdout.isatty():
                print(self.message, flush=True)
                await self.finished.wait()
                return

            frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
            index = 0
            try:
                while not self.finished.is_set():
                    print(
                        f"\r\033[2K{frames[index % len(frames)]} {self.message}",
                        end="",
                        flush=True,
                    )
                    index += 1
                    try:
                        await asyncio.wait_for(
                            self.finished.wait(),
                            timeout=0.1,
                        )
                    except TimeoutError:
                        pass
            finally:
                print("\r\033[2K", end="", flush=True)

        self.task = asyncio.create_task(show())
        return self

    async def stop(self) -> None:
        if not self.finished.is_set():
            self.finished.set()
        if self.task:
            await self.task

    async def __aexit__(self, *_: Any) -> None:
        await self.stop()


def tool_output(result: dict[str, Any]) -> str:
    if result["ok"]:
        payload = result["data"]
    else:
        payload = {"error": result["error"]}
    return json.dumps(payload)


async def run_tool(
    call: Any,
    http: httpx.AsyncClient,
) -> dict[str, Any]:
    try:
        arguments = json.loads(call.arguments)
        handler = TOOL_HANDLERS.get(call.name)
        if handler:
            value = arguments[handler.argument]
            async with DelayedStatus(
                f"{handler.message.format(value=value)} (Ctrl+C to cancel)"
            ):
                result = await call_tool_api(http, value, handler)
        else:
            result = fail(f"Unknown tool: {call.name}")
    except (json.JSONDecodeError, KeyError, TypeError):
        result = fail("The model supplied invalid tool arguments.")

    return {
        "type": "function_call_output",
        "call_id": call.call_id,
        "output": tool_output(result),
    }


async def call_llm(
    user_input: str,
    conversation_history: list[Any],
    openai: AsyncOpenAI,
    http: httpx.AsyncClient,
) -> AsyncIterator[str]:
    working = [*conversation_history, {"role": "user", "content": user_input}]

    for _ in range(10):
        completed = None
        emitted_text = False
        async with DelayedStatus("Thinking…") as status:
            stream = await openai.responses.create(
                model=os.getenv("OPENAI_MODEL", "gpt-5-nano"),
                instructions=INSTRUCTIONS,
                input=working,
                tools=TOOLS,
                parallel_tool_calls=False,
                store=False,
                include=["reasoning.encrypted_content"],
                stream=True,
            )
            async with stream:
                async for event in stream:
                    if event.type == "response.output_text.delta":
                        await status.stop()
                        yield event.delta
                        emitted_text = True
                    elif (
                        event.type == "response.output_item.done"
                        and event.item.type == "function_call"
                    ):
                        await status.stop()
                    elif event.type == "response.completed":
                        completed = event.response

        if completed is None:
            raise RuntimeError("OpenAI stream ended without completion.")

        working.extend(
            item.model_dump(exclude_none=True) for item in completed.output
        )
        calls = [
            item for item in completed.output if item.type == "function_call"
        ]
        if not calls:
            conversation_history[:] = working
            return

        if emitted_text:
            yield "\n"
        for call in calls:
            working.append(await run_tool(call, http))

    raise RuntimeError("Tool loop exceeded the supported depth.")


async def get_user_input() -> str:
    loop = asyncio.get_running_loop()
    future = loop.create_future()

    def read_stdin() -> None:
        line = sys.stdin.readline()
        if not future.done():
            future.set_result(line)

    try:
        loop.add_reader(sys.stdin, read_stdin)
    except (AttributeError, NotImplementedError):
        return await asyncio.to_thread(input, "You: ")

    print("You: ", end="", flush=True)
    try:
        line = await future
        if line == "":
            raise EOFError
        return line.rstrip("\n")
    finally:
        loop.remove_reader(sys.stdin)


async def main() -> None:
    load_dotenv()
    required = ("OPENAI_API_KEY", "ELYOS_API_KEY")
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise SystemExit(f"Missing {', '.join(missing)}.")

    history: list[Any] = []
    root_task = asyncio.current_task()
    loop = asyncio.get_running_loop()

    def interrupt() -> None:
        if root_task:
            root_task.cancel()

    signal_installed = False
    try:
        loop.add_signal_handler(signal.SIGINT, interrupt)
        signal_installed = True
    except (AttributeError, NotImplementedError):
        pass

    async with AsyncOpenAI() as openai, httpx.AsyncClient(
        base_url=BASE_URL,
        headers={"X-API-Key": os.environ["ELYOS_API_KEY"]},
    ) as http:
        try:
            while True:
                try:
                    user_input = (await get_user_input()).strip()
                except EOFError:
                    print()
                    return

                if user_input.lower() in {"q", "quit", "exit"}:
                    return
                if not user_input:
                    continue

                started = False
                try:
                    async for chunk in call_llm(
                        user_input,
                        history,
                        openai,
                        http,
                    ):
                        if not started:
                            print("Assistant: ", end="", flush=True)
                            chunk = chunk.lstrip()
                            started = True
                        print(chunk, end="", flush=True)
                    if started:
                        print()
                except asyncio.CancelledError:
                    if root_task:
                        root_task.uncancel()
                    print("\nCancelled.")
                except OpenAIError as exc:
                    print(f"\nOpenAI request failed: {exc}")
                except Exception as exc:
                    print(f"\nRequest failed: {exc}")
        finally:
            if signal_installed:
                loop.remove_signal_handler(signal.SIGINT)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, asyncio.CancelledError):
        print()
