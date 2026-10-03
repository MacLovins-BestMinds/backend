"""Прогрев демо: прогоняет записи через весь раунд на запущенном сервере, чтобы на показе ответы шли из кэша.

    python tools/demo_warmup.py pitch.m4a --answer answer.m4a --case stoicism

Первый прогон ходит в AI-сервисы и наполняет кэш (.ai_cache); повторный с теми же файлами отвечает мгновенно.
Перед показом запустите дважды и убедитесь, что второй прогон быстрый.
"""

import argparse
import sys
import time
from pathlib import Path

import httpx


def step(name: str, call):
    started = time.perf_counter()
    response = call()
    print(f"  {name:16} HTTP {response.status_code}  {time.perf_counter() - started:5.1f} с")
    if response.status_code >= 400:
        sys.exit(f"  ошибка: {response.text[:300]}")
    return response.json()


def warmup(base: str, pitch: Path, answer: Path | None, case_id: str, nick: str) -> None:
    client = httpx.Client(base_url=base, timeout=180)
    health = client.get("/api/ai/health").json()
    for service, check in health["checks"].items():
        print(f"  {service:16} {'ok' if check['ok'] else 'FAIL'}  {check['detail']}")
    if not health["ok"]:
        sys.exit("AI-сервисы недоступны — прогрев бессмысленен")

    user_id = client.post("/api/game/auth", json={"nick": nick}).json()["user_id"]
    rnd = step(
        "раунд",
        lambda: client.post("/api/game/rounds", json={"user_id": user_id, "mode": "training", "case_id": case_id}),
    )
    rid = rnd["round_id"]
    with pitch.open("rb") as f:
        delivery = step(
            "delivery",
            lambda: client.post(f"/api/ai/rounds/{rid}/delivery", files={"audio": (pitch.name, f, "audio/mp4")}),
        )
    pron = delivery.get("pronunciation") or {}
    print(
        f"    подача {delivery['scores']['delivery']['total']}, содержание {delivery['scores']['content']['total']}, "
        f"произношение {pron.get('overall_score', '—')}"
    )
    questions = step("вопросы жюри", lambda: client.post(f"/api/ai/rounds/{rid}/jury/questions"))["questions"]
    for q in questions:
        mp3 = client.get(q["audio_url"])
        print(f"    [{q['juror']}] {q['text'][:70]}…  mp3 {mp3.status_code}")
    if answer:
        with answer.open("rb") as f:
            files = {"audio": (answer.name, f, "audio/mp4")}
            result = step(
                "ответ жюри",
                lambda: client.post(
                    f"/api/ai/rounds/{rid}/jury/answer", files=files, data={"question_id": questions[0]["id"]}
                ),
            )
        print(f"    {result['score']} — {result['comment']}")
    final = step("итог", lambda: client.post(f"/api/game/rounds/{rid}/finish"))
    print(f"    итог {final['total']}, звание {final['rank']['title']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("pitch", type=Path, help="запись выступления (m4a)")
    parser.add_argument("--answer", type=Path, help="запись ответа на первый вопрос жюри (m4a)")
    parser.add_argument("--case", default="stoicism", help="id темы из content/topics.json")
    parser.add_argument("--nick", default="demo_stage")
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    for attempt in (1, 2):
        print(f"\nПрогон {attempt}{' (должен идти из кэша)' if attempt == 2 else ''}:")
        warmup(args.base, args.pitch, args.answer, args.case, args.nick)


if __name__ == "__main__":
    main()
