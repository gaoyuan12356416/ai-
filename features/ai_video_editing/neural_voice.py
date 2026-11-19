"""Isolated neural speech process. Only spoken text is sent to the TTS provider."""

import asyncio
import json
from pathlib import Path
import sys


async def run(payload, destination):
    import edge_tts

    speech = edge_tts.Communicate(
        payload["text"],
        payload["voice"],
        boundary="WordBoundary",
        rate=payload.get("rate", "+0%"),
        connect_timeout=10,
        receive_timeout=30,
    )
    words = []
    with Path(destination).open("wb") as output:
        async for chunk in speech.stream():
            if chunk["type"] == "audio":
                output.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                words.append(
                    {
                        "start": round(chunk["offset"] / 10000),
                        "end": round((chunk["offset"] + chunk["duration"]) / 10000),
                        "text": chunk["text"],
                    }
                )
    print(json.dumps(words, ensure_ascii=False))


if __name__ == "__main__":
    try:
        asyncio.run(asyncio.wait_for(run(json.load(sys.stdin), sys.argv[1]), 55))
    except Exception as exc:
        # Never echo submitted text or upstream response/connection details.
        print(type(exc).__name__, file=sys.stderr)
        sys.exit(1)
