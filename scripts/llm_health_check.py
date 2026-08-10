"""Make a safe, billable connectivity check to Yandex AI Studio.

It never prints the API key, request body, response text, or provider error body.
"""

from __future__ import annotations

import asyncio

from openai import APIStatusError, AsyncOpenAI, OpenAIError

from app.config import settings


async def main() -> int:
    if not settings.yandex_ai_api_key:
        print("LLM health-check: API key missing")
        return 2

    client = AsyncOpenAI(
        api_key=settings.yandex_ai_api_key,
        base_url="https://ai.api.cloud.yandex.net/v1",
        project=settings.yandex_cloud_folder_id,
        default_headers={"x-data-logging-enabled": "false"},
        timeout=8.0,
        max_retries=0,
    )
    try:
        response = await client.responses.create(
            model=f"gpt://{settings.yandex_cloud_folder_id}/{settings.yandex_ai_model}",
            instructions="Ответь одним словом: ок",
            input="проверка",
            max_output_tokens=1500,
        )
        print("LLM health-check: ok" if response.output_text else "LLM health-check: empty response")
        return 0 if response.output_text else 1
    except APIStatusError as error:
        print(f"LLM health-check: HTTP {error.status_code}")
        return 1
    except OpenAIError as error:
        print(f"LLM health-check: {type(error).__name__}")
        return 1
    finally:
        await client.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
