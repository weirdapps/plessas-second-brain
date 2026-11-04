"""Worked examples for the Teams and WhatsApp system prompts under call shapes v2 and
v2-text (issue 211).

Two threads per route, one Greek and one English, each with the answer the instructions
want and why. They do two jobs: they show the rules at work (a quoted policy is not a
decision, an intention is not an action, a promise is a commitment only with whoever made
it), and they make the system prompt long enough for the model to cache, which the bare
instructions are not. Every name and address is fictional.
"""

import json
from dataclasses import dataclass, field

from src.extract.teams_prompt import THREAD_TEMPLATE


@dataclass(frozen=True)
class ThreadExample:
    """A thread (its THREAD_TEMPLATE fields and messages), the answer, and why. The
    commitments are kept apart: they are shown only to a store that asks for them, and so
    are the topics (issue 281: only when BRAIN_EXTRACT_CHAT_TOPICS asks for them)."""

    title: str
    thread: dict
    chat: tuple[dict, ...]
    answer: dict
    why: str
    commitments: tuple[dict, ...] = field(default_factory=tuple)
    topics: tuple[str, ...] = field(default_factory=tuple)


def render(example: ThreadExample) -> str:
    """The thread as it would appear between the tags of a real call."""
    transcript = "\n".join(
        f"[{m['composed_at']}] {m['sender']}: {m['content']}" for m in example.chat
    )
    t = example.thread
    return THREAD_TEMPLATE.format(
        chat_label=t["chat_label"],
        thread_kind=t["thread_kind"],
        started_at=t["started_at"],
        ended_at=t["ended_at"],
        participants=", ".join(t["participants"]),
        message_count=len(example.chat),
        transcript=transcript,
    )


def answer(example: ThreadExample, *, commitments: bool, topics: bool = False) -> dict:
    """The example's answer in the order the keys are asked: its commitments, then its
    topics, before key_facts when the prompt asks for them (teams_prompt.with_commitments,
    then topic_registry.with_topics)."""
    out: dict = {}
    for key, value in example.answer.items():
        if key == "key_facts" and commitments:
            out["commitments"] = list(example.commitments)
        if key == "key_facts" and topics:
            out["topics"] = list(example.topics)
        out[key] = value
    return out


def examples_text(route: str, *, commitments: bool, topics: bool = False) -> str:
    """The cached block: the route's examples, each thread, its answer, and why."""
    parts = [
        "WORKED EXAMPLES\n"
        "The two examples below show what a good answer looks like. Each thread is shown"
        " as it would appear between the tags, then the answer, then why."
    ]
    for n, example in enumerate(EXAMPLES[route], start=1):
        shown = json.dumps(
            answer(example, commitments=commitments, topics=topics), ensure_ascii=False, indent=2
        )
        parts.append(
            f"Example {n}: {example.title}.\n\n{render(example)}\n\n"
            f"Answer:\n{shown}\n\nWhy: {example.why}"
        )
    return "\n\n".join(parts)


_TEAMS_GR = ThreadExample(
    title="a Greek channel thread that settles a campaign date and quotes a policy",
    thread={
        "chat_label": "Κάρτες / Καμπάνιες",
        "thread_kind": "channel_post",
        "started_at": "2026-03-02T08:10:00Z",
        "ended_at": "2026-03-02T11:45:00Z",
        "participants": ["Νίκος Αντωνίου", "Μαρία Κωστάκη", "Γιώργος Λαμπρόπουλος"],
    },
    chat=(
        {
            "composed_at": "2026-03-02T08:10:00Z",
            "sender": "Νίκος Αντωνίου",
            "content": (
                "Καλημέρα. Για την καμπάνια cashback της άνοιξης έχουμε δύο επιλογές"
                " έναρξης: 16 ή 23 Μαρτίου. Η πολιτική επικοινωνίας λέει ότι «κάθε"
                " προωθητική ενέργεια εγκρίνεται από τη Νομική Υπηρεσία πριν από την"
                " αποστολή», οπότε χρειαζόμαστε και το δικό τους οκ."
            ),
        },
        {
            "composed_at": "2026-03-02T09:02:00Z",
            "sender": "Μαρία Κωστάκη",
            "content": (
                "Το 16 δεν βγαίνει, το creative κλειδώνει στις 13. Πάμε 23 Μαρτίου."
                " Θα δούμε αργότερα αν θα επεκταθούμε και σε χρεωστικές."
            ),
        },
        {
            "composed_at": "2026-03-02T10:30:00Z",
            "sender": "Γιώργος Λαμπρόπουλος",
            "content": (
                "Συμφωνώ για 23. Θα στείλω το κείμενο στη Νομική μέχρι την Πέμπτη 5/3."
                " Το ποσοστό επιστροφής μένει 2% όπως πέρσι."
            ),
        },
        {
            "composed_at": "2026-03-02T11:45:00Z",
            "sender": "Νίκος Αντωνίου",
            "content": "Τέλεια, 23 Μαρτίου λοιπόν.",
        },
    ),
    answer={
        "summary": (
            "Η ομάδα αποφάσισε έναρξη της ανοιξιάτικης καμπάνιας cashback στις 23 Μαρτίου,"
            " επειδή το creative κλειδώνει στις 13. Ο Γιώργος θα στείλει το κείμενο στη"
            " Νομική Υπηρεσία έως τις 5 Μαρτίου."
        ),
        "decisions": [
            {
                "decision": "Έναρξη της ανοιξιάτικης καμπάνιας cashback στις 23 Μαρτίου",
                "decided_by": "Μαρία Κωστάκη",
                "decision_date": "2026-03-02",
            }
        ],
        "action_items": [
            {
                "task": "Αποστολή του κειμένου της καμπάνιας στη Νομική Υπηρεσία για έγκριση",
                "owner": "Γιώργος Λαμπρόπουλος",
                "deadline": "2026-03-05",
            }
        ],
        "key_facts": [
            {"fact": "Το creative της καμπάνιας κλειδώνει στις 13 Μαρτίου"},
            {"fact": "Το ποσοστό επιστροφής της καμπάνιας μένει 2%, όπως πέρσι"},
            {
                "fact": (
                    "Κάθε προωθητική ενέργεια εγκρίνεται από τη Νομική Υπηρεσία πριν από"
                    " την αποστολή (πολιτική επικοινωνίας)"
                )
            },
        ],
        "sentiment": "positive",
        "language": "el",
    },
    topics=("καμπάνια cashback", "Νομική Υπηρεσία"),
    commitments=(
        {
            "commitment": "Θα στείλει το κείμενο στη Νομική Υπηρεσία μέχρι την Πέμπτη 5/3",
            "by": "Γιώργος Λαμπρόπουλος",
            "to": "Νίκος Αντωνίου",
            "due": "2026-03-05",
        },
    ),
    why=(
        "The start date is a choice the participants made, so it is a decision. The"
        " approval rule is quoted policy: a key fact, not a decision. 'Θα δούμε αργότερα'"
        " is an intention without a choice, so it is neither a decision nor an action. The"
        " promise to send the text by Thursday is both an action and, where commitments are"
        " asked, a commitment, with 'by' the one who made it."
    ),
)

_TEAMS_EN = ThreadExample(
    title="an English chat that forwards a vendor email and agrees a test plan",
    thread={
        "chat_label": "Wallet UAT",
        "thread_kind": "chat_session",
        "started_at": "2026-04-14T13:00:00Z",
        "ended_at": "2026-04-14T14:20:00Z",
        "participants": ["Daniel Reed", "Sofia Marin"],
    },
    chat=(
        {
            "composed_at": "2026-04-14T13:00:00Z",
            "sender": "Daniel Reed",
            "content": (
                "Forwarding the vendor's note: 'We have decided to move the token service"
                " cutover to 30 April and will freeze changes from 25 April.' Can we still"
                " run UAT before the freeze?"
            ),
        },
        {
            "composed_at": "2026-04-14T13:40:00Z",
            "sender": "Sofia Marin",
            "content": (
                "Yes if we start on the 20th. Let's run UAT from 20 to 24 April with the"
                " three test cards we already have. I'll book the test environment today."
            ),
        },
        {
            "composed_at": "2026-04-14T14:20:00Z",
            "sender": "Daniel Reed",
            "content": "Agreed, 20-24 April. I'll tell the vendor by tomorrow.",
        },
    ),
    answer={
        "summary": (
            "Daniel and Sofia agreed to run wallet UAT from 20 to 24 April, before the"
            " vendor's change freeze of 25 April and its cutover on 30 April. Sofia books"
            " the test environment; Daniel informs the vendor."
        ),
        "decisions": [
            {
                "decision": "Run UAT from 20 to 24 April with the three existing test cards",
                "decided_by": "Sofia Marin",
                "decision_date": "2026-04-14",
            }
        ],
        "action_items": [
            {
                "task": "Book the UAT test environment",
                "owner": "Sofia Marin",
                "deadline": "2026-04-14",
            },
            {
                "task": "Tell the vendor the UAT dates",
                "owner": "Daniel Reed",
                "deadline": "2026-04-15",
            },
        ],
        "key_facts": [
            {"fact": "The vendor moved the token service cutover to 30 April"},
            {"fact": "The vendor freezes changes from 25 April"},
        ],
        "sentiment": "neutral",
        "language": "en",
    },
    topics=("wallet UAT", "vendor change freeze"),
    commitments=(
        {
            "commitment": "Book the test environment today",
            "by": "Sofia Marin",
            "to": "Daniel Reed",
            "due": "2026-04-14",
        },
        {
            "commitment": "Tell the vendor the UAT dates by tomorrow",
            "by": "Daniel Reed",
            "to": "Sofia Marin",
            "due": "2026-04-15",
        },
    ),
    why=(
        "The vendor's cutover is quoted from a forwarded email: the participants did not"
        " decide it, so it is a key fact. The UAT window is what they chose. 'Today' and"
        " 'tomorrow' are resolved against the date of the message that says them."
    ),
)

_WHATSAPP_GR = ThreadExample(
    title="a Greek family chat that plans a weekend",
    thread={
        "chat_label": "Οικογένεια",
        "thread_kind": "whatsapp_session",
        "started_at": "2026-05-08T17:05:00Z",
        "ended_at": "2026-05-08T18:30:00Z",
        "participants": ["Ελένη", "Κώστας", "Άννα"],
    },
    chat=(
        {
            "composed_at": "2026-05-08T17:05:00Z",
            "sender": "Ελένη",
            "content": "Το Σάββατο πάμε τελικά στη γιαγιά; [photo]",
        },
        {
            "composed_at": "2026-05-08T17:20:00Z",
            "sender": "Κώστας",
            "content": "Ναι, φεύγουμε στις 10 το πρωί. Θα πάρω εγώ το γλυκό από το ζαχαροπλαστείο.",
        },
        {
            "composed_at": "2026-05-08T17:45:00Z",
            "sender": "Άννα",
            "content": "Εγώ έχω προπόνηση ως τις 11, θα έρθω με το λεωφορείο. Ίσως φέρω και τη Δανάη.",
        },
        {
            "composed_at": "2026-05-08T18:30:00Z",
            "sender": "Ελένη",
            "content": "Οκ! Η γιαγιά είπε ότι το μεσημεριανό είναι στη 1.",
        },
    ),
    answer={
        "summary": (
            "Η οικογένεια κανόνισε επίσκεψη στη γιαγιά το Σάββατο, με αναχώρηση στις 10."
            " Ο Κώστας παίρνει το γλυκό· η Άννα έρχεται αργότερα με λεωφορείο."
        ),
        "decisions": [
            {
                "decision": "Επίσκεψη στη γιαγιά το Σάββατο, αναχώρηση στις 10 το πρωί",
                "decided_by": "Κώστας",
                "decision_date": "2026-05-08",
            }
        ],
        "action_items": [
            {
                "task": "Αγορά γλυκού από το ζαχαροπλαστείο για το Σάββατο",
                "owner": "Κώστας",
                "deadline": "2026-05-09",
            }
        ],
        "key_facts": [
            {"fact": "Το μεσημεριανό στη γιαγιά είναι στη 1 το Σάββατο"},
            {"fact": "Η Άννα έχει προπόνηση ως τις 11 και θα έρθει με το λεωφορείο"},
        ],
        "sentiment": "positive",
        "language": "el",
    },
    topics=("επίσκεψη στη γιαγιά", "οικογενειακό Σάββατο"),
    commitments=(
        {
            "commitment": "Θα πάρει το γλυκό από το ζαχαροπλαστείο",
            "by": "Κώστας",
            "to": "Ελένη",
            "due": "2026-05-09",
        },
    ),
    why=(
        "The departure time settles the plan, so it is the decision. 'Ίσως φέρω και τη"
        " Δανάη' is a maybe, not a promise or a task. The photo is shown only as its kind"
        " and adds nothing to extract. Saturday after Friday 8 May is 2026-05-09."
    ),
)

_WHATSAPP_EN = ThreadExample(
    title="an English chat among friends booking a sailing trip",
    thread={
        "chat_label": "Sailing crew",
        "thread_kind": "whatsapp_session",
        "started_at": "2026-06-01T19:00:00Z",
        "ended_at": "2026-06-01T20:10:00Z",
        "participants": ["Mark", "Lena", "Tom"],
    },
    chat=(
        {
            "composed_at": "2026-06-01T19:00:00Z",
            "sender": "Mark",
            "content": (
                "Charter company says the boat is free 12-19 July, deposit 30% by 10 June."
                " Their terms: 'cancellation within 30 days of departure forfeits the"
                " deposit'. In?"
            ),
        },
        {
            "composed_at": "2026-06-01T19:30:00Z",
            "sender": "Lena",
            "content": "In! Let's book 12-19 July. I can do the provisioning list.",
        },
        {
            "composed_at": "2026-06-01T20:10:00Z",
            "sender": "Tom",
            "content": "In too. Mark, pay the deposit and we'll each send you our share by Friday. [voice note]",
        },
    ),
    answer={
        "summary": (
            "The crew agreed to charter the boat for 12-19 July. Mark pays the 30% deposit"
            " by 10 June and the others send him their share; Lena drafts the provisioning"
            " list."
        ),
        "decisions": [
            {
                "decision": "Charter the boat for 12-19 July",
                "decided_by": "Lena",
                "decision_date": "2026-06-01",
            }
        ],
        "action_items": [
            {
                "task": "Pay the 30% charter deposit",
                "owner": "Mark",
                "deadline": "2026-06-10",
            },
            {"task": "Draft the provisioning list", "owner": "Lena", "deadline": None},
            {
                "task": "Send Mark each share of the deposit",
                "owner": "Tom",
                "deadline": "2026-06-05",
            },
        ],
        "key_facts": [
            {"fact": "The charter deposit is 30%, due by 10 June"},
            {
                "fact": (
                    "Charter terms: cancellation within 30 days of departure forfeits the deposit"
                )
            },
        ],
        "sentiment": "positive",
        "language": "en",
    },
    topics=("sailing charter", "boat deposit", "provisioning list"),
    commitments=(
        {
            "commitment": "Do the provisioning list",
            "by": "Lena",
            "to": "Mark",
            "due": None,
        },
        {
            "commitment": "Send Mark our share of the deposit by Friday",
            "by": "Tom",
            "to": "Mark",
            "due": "2026-06-05",
        },
    ),
    why=(
        "The cancellation rule is the charter company's term, quoted: a key fact. Tom's"
        " request that Mark pay is an action Mark owes, not a commitment of Mark's, since"
        " Mark made no promise. The voice note is shown only as its kind."
    ),
)

EXAMPLES: dict[str, tuple[ThreadExample, ...]] = {
    "teams": (_TEAMS_GR, _TEAMS_EN),
    "whatsapp": (_WHATSAPP_GR, _WHATSAPP_EN),
}
