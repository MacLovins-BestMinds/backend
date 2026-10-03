"""Эталонные записи: текст для синтеза и что анализатор ОБЯЗАН найти. [[slnc N]] — пауза N мс в голосе macOS."""


def pause(ms: int) -> str:
    return f" [[slnc {ms}]] "


_FAST = (
    "Stoicism teaches that you cannot control the world around you but you can always control how you react to it,"
    " and that simple idea helps students stay calm before exams and helps workers handle stress at their jobs. "
)
_SLOW = "Stoicism is a philosophy that teaches us to stay calm and focus only on the things we can really control. "

RECORDINGS = {
    "bad_fillers": {
        "rate": 175,
        "expect": {
            "fillers": 11,
            "filler_words": ["um", "so", "like", "uh", "you know", "so", "well", "actually", "kind of", "um", "like"],
        },
        "text": (
            "Um, so today I want to talk about, like, stoicism. Uh, it's a philosophy from, you know, ancient Greece. "
            "So, the main idea is, well, you control your reactions. Actually, it's kind of simple. "
            "Um, you just focus on what you can change, and, like, ignore the rest. That's it."
        ),
    },
    "bad_pauses": {
        "rate": 175,
        "expect": {
            "long_pause_mid_phrase": 3,
            "hesitation_mid_phrase": 3,
            "good_pause_after_sentence": 2,
            "pause_4s_after_sentence_not_penalized": 1,
        },
        "text": (
            "Stoicism is an ancient philosophy" + pause(4000) + "that teaches calm. "
            "It says we should focus"
            + pause(1500)
            + "on what we control."
            + pause(1500)
            + "For example, you cannot control"
            + pause(4000)
            + "the weather. But you can control your reaction."
            + pause(4000)
            + "Students feel stress"
            + pause(1500)
            + "before exams."
            + pause(1500)
            + "Stoicism gives them a simple tool"
            + pause(4000)
            + "to stay calm. It helps them"
            + pause(1500)
            + "focus on preparation."
        ),
    },
    "traps": {
        "rate": 175,
        "expect": {"fillers": 0},
        "text": (
            "Do you know why the Roman emperor Marcus Aurelius kept a diary? "
            "What I mean is that he wrote notes to himself every night. "
            "He literally wrote them in a military camp. He did not like complaining. "
            "Well-known thinkers still quote him today. "
            "So many people read his book, and it is actually one of the best sellers in philosophy."
        ),
    },
    "bad_fast": {"rate": 320, "expect": {"pace": "fast (>180 wpm)"}, "text": _FAST * 7},
    "bad_slow": {"rate": 175, "expect": {"pace": "slow (<100 wpm)"}, "text": pause(350).join((_SLOW * 2).split())},
    "good": {
        "rate": 140,
        "expect": {"fillers": 0, "long_pause": 0, "hesitation": 0, "good_pause_after_sentence": 3},
        "text": (
            "Imagine you fail an important exam."
            + pause(1500)
            + "What do you do next? Stoicism has a clear answer."
            + pause(1500)
            + "It teaches you to separate what you control from what you don't. "
            "You can't change the grade, but you can change how you prepare."
            + pause(1500)
            + "Teachers can use this idea to help students handle stress. That is why Stoicism belongs in the classroom."
        ),
    },
}
