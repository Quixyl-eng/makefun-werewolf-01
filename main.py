#!/usr/bin/env python3
"""make.fun werewolf-agent-01 entry: a deterministic, stdlib-only heuristic agent.

The agent holds one seat at each of the trial's games, reached through the env at
MAKEFUN_ENV_URL with the agent/v1 protocol (POST /observe, /game, /act). It makes
no model call: every decision is a pure function of that one game's view and the
trial seed, so the live run and its replay come out byte for byte the same.

Design credit
-------------
The suspicion model is adapted from the MIT-licensed deepwolf copilot
(https://github.com/JuneQQQ/deepwolf, deepwolf/copilot/advisor.py): a prior of
``unknown wolves / unknown living others`` hardened to 0 or 1 for anything the
seat has confirmed, then nudged by public lynch behaviour and renormalised so the
unknown scores sum to the wolves still at large. The ruleset facts come from
google/werewolf_arena (Apache-2.0) and the AGI House 2024 agent (MIT); see README.

The policies below (night actions, bandwagon voting, role-claim handling and the
statement templates) are our own, written for this competition's exact rules:
8 seats, 2 wolves, 1 seer, 1 doctor, 4 villagers; sequential public plurality
votes; a doctor that may not self-protect or repeat last night's target; and wolf
kills that land only on agreement.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.error
import urllib.request

ENV_URL = os.environ.get("MAKEFUN_ENV_URL", "").rstrip("/")
TRIAL_SEED = os.environ.get("MAKEFUN_TRIAL_SEED", "")
TOTAL_WOLVES = 2
MAX_CHARS = 500
HTTP_TIMEOUT_S = 600

# Suspicion-nudge multipliers (deepwolf advisor v1): a seat that helped lynch a
# confirmed wolf looks cleaner, one that helped lynch a confirmed villager worse.
NUDGE_VOTED_WOLF = 0.55
NUDGE_VOTED_VILLAGER = 1.45
NUDGE_SPARED_WOLF = 1.12
NUDGE_SPARED_VILLAGER = 0.92
CLAMP_LO = 0.02
CLAMP_HI = 0.97


# --------------------------------------------------------------- transport
def post(path: str, body: dict) -> dict:
    """POST one JSON object to the env and read one back."""
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        ENV_URL + path,
        data=data,
        headers={"content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8"))


# --------------------------------------------------------------- determinism
def seeded_order(parts: list, seats: list) -> list:
    """A deterministic ordering of seats from the trial seed and a label.

    Used only to break ties between equally ranked candidates, so the choice is
    stable across the live run and the replay (no clock, no unseeded RNG).
    """
    def key(seat: int) -> bytes:
        label = TRIAL_SEED + "|" + "|".join(str(p) for p in parts) + "|" + str(seat)
        return hashlib.sha256(label.encode("utf-8")).digest()

    return sorted(seats, key=key)


def pick(parts: list, candidates: list, score, reverse: bool) -> int | None:
    """The best candidate by ``score`` (desc if reverse), ties broken by seed."""
    if not candidates:
        return None
    ordered = seeded_order(parts, list(candidates))
    best = ordered[0]
    best_score = score(best)
    for seat in ordered[1:]:
        s = score(seat)
        if (s > best_score) if reverse else (s < best_score):
            best, best_score = seat, s
    return best


# --------------------------------------------------------------- view model
class Game:
    """One game's view, parsed into the facts the policies read."""

    def __init__(self, view: dict):
        self.view = view
        you = view["you"]
        self.me = you["seat"]
        self.role = you["role"]
        self.alive_me = you["alive"]
        partner = you.get("partner")
        self.partner = partner["seat"] if partner else None
        self.day = view.get("day", 1)
        self.phase = view.get("phase", "night")
        self.players = view.get("players", [])
        self.seer_results = view.get("seerResults") or []
        self.last_protect = view.get("lastProtect")
        self.wolf_votes = view.get("wolfVotes")
        self.log = view.get("log", [])
        self.living = [p["seat"] for p in self.players if p["alive"]]
        self.revealed = {p["seat"]: p["role"] for p in self.players if p.get("role")}

    # ---- confirmed roles ------------------------------------------------
    def confirmed(self) -> dict:
        """seat -> 'wolf' | 'village', for everything this seat has confirmed."""
        out: dict = {}
        for seat, role in self.revealed.items():
            out[seat] = "wolf" if role == "werewolf" else "village"
        if self.role == "werewolf" and self.partner is not None:
            out[self.partner] = "wolf"
        for r in self.seer_results:
            if r.get("seat") is not None and r.get("werewolf") is not None:
                out[r["seat"]] = "wolf" if r["werewolf"] else "village"
        # If I am the real seer, any *other* seat claiming seer is a wolf.
        if self.role == "seer":
            for seat in self.seer_claimants():
                if seat != self.me and seat not in out:
                    out[seat] = "wolf"
        return out

    # ---- public talk ----------------------------------------------------
    def statements(self) -> list:
        """(seat, lowercased text) for every spoken line, in order."""
        out = []
        for e in self.log:
            if e.get("kind") == "speech" and isinstance(e.get("text"), str):
                out.append((e["seat"], e["text"].lower()))
        return out

    def seer_claimants(self) -> list:
        """Seats that have claimed the seer role, earliest first, de-duplicated."""
        seen: list = []
        markers = ("i am the seer", "i'm the seer", "im the seer", "as the seer",
                   "i checked", "i inspected", "i investigated", "my inspection",
                   "i am seer", "i'm seer", "the real seer")
        for seat, text in self.statements():
            if "seer" in text and any(m in text for m in markers):
                if seat not in seen:
                    seen.append(seat)
        return seen

    def doctor_claimants(self) -> list:
        seen: list = []
        markers = ("i am the doctor", "i'm the doctor", "im the doctor",
                   "i protected", "i saved", "i healed", "as the doctor")
        for seat, text in self.statements():
            if "doctor" in text and any(m in text for m in markers):
                if seat not in seen:
                    seen.append(seat)
        return seen

    # ---- voting history -------------------------------------------------
    def confirmed_lynches(self) -> list:
        """(day, exiled_seat, 'wolf'|'village') for every revealed day exile."""
        out = []
        for e in self.log:
            if e.get("kind") == "exile" and e.get("seat") is not None and e.get("role"):
                out.append((e["day"], e["seat"], "wolf" if e["role"] == "werewolf" else "village"))
        return out

    def voters_for(self, day: int, target: int) -> set:
        out = set()
        for e in self.log:
            if e.get("kind") == "vote" and e.get("day") == day and e.get("target") == target:
                out.add(e["seat"])
        return out

    def votes_today(self) -> dict:
        """seat -> target for votes already cast this day (plurality tracking)."""
        out = {}
        for e in self.log:
            if e.get("kind") == "vote" and e.get("day") == self.day and e.get("target") is not None:
                out[e["seat"]] = e["target"]
        return out

    # ---- suspicion model (adapted from deepwolf advisor) ----------------
    def suspicion(self) -> dict:
        """seat -> P(werewolf) for every living seat other than me."""
        confirmed = self.confirmed()
        others = [s for s in self.living if s != self.me]
        dead_wolves = sum(1 for s, r in self.revealed.items() if r == "werewolf")
        known_living_wolves = [s for s in others if confirmed.get(s) == "wolf"]
        unknown = [s for s in others if s not in confirmed]
        unknown_wolves = max(0, TOTAL_WOLVES - dead_wolves - len(known_living_wolves))

        scores: dict = {}
        for s in others:
            if confirmed.get(s) == "wolf":
                scores[s] = 1.0
            elif confirmed.get(s) == "village":
                scores[s] = 0.0

        if unknown and unknown_wolves > 0:
            weight = {s: 1.0 for s in unknown}
            for day, target, kind in self.confirmed_lynches():
                voters = self.voters_for(day, target)
                wolf_lynched = kind == "wolf"
                for s in unknown:
                    if s in voters:
                        weight[s] *= NUDGE_VOTED_WOLF if wolf_lynched else NUDGE_VOTED_VILLAGER
                    else:
                        weight[s] *= NUDGE_SPARED_WOLF if wolf_lynched else NUDGE_SPARED_VILLAGER
            # A second seer claim is almost surely a wolf: bump later claimants.
            claimants = self.seer_claimants()
            for extra in claimants[1:]:
                if extra in weight:
                    weight[extra] *= 2.5
            total = sum(weight.values()) or 1.0
            for s in unknown:
                raw = unknown_wolves * weight[s] / total
                scores[s] = min(CLAMP_HI, max(CLAMP_LO, raw))
        else:
            for s in unknown:
                scores[s] = 0.0
        return scores

    # ---- plurality bookkeeping -----------------------------------------
    def current_leader(self) -> int | None:
        """The unique seat leading today's vote so far, or None on a tie/empty."""
        tally: dict = {}
        for target in self.votes_today().values():
            tally[target] = tally.get(target, 0) + 1
        if not tally:
            return None
        top = max(tally.values())
        leaders = [s for s, n in tally.items() if n == top]
        return leaders[0] if len(leaders) == 1 else None


# --------------------------------------------------------------- policies
def choose_village_vote(g: Game, options: list) -> int | None:
    """Village-side day vote: lock a known wolf, bandwagon a strong lead, else top suspicion."""
    susp = g.suspicion()
    legal = [s for s in options]
    known_wolf = pick(["vote-wolf", g.day], [s for s in legal if susp.get(s, 0) >= 0.999], lambda s: susp.get(s, 0), True)
    if known_wolf is not None:
        return known_wolf
    leader = g.current_leader()
    if leader in legal and susp.get(leader, 0) >= 0.5:
        # Securing a plurality beats splitting the village across two suspects.
        return leader
    return pick(["vote-top", g.day], legal, lambda s: susp.get(s, 0.0), True)


def choose_wolf_vote(g: Game, options: list) -> int | None:
    """Wolf day vote: never a packmate; push the village's most trusted seat, or join the lead."""
    legal = [s for s in options if s != g.partner]
    if not legal:
        return None
    susp = g.suspicion()
    leader = g.current_leader()
    if leader in legal and leader != g.partner:
        return leader  # ride the bandwagon so a villager, not us, is exiled
    # Otherwise push the seat the village trusts most (lowest suspicion).
    return pick(["wolf-vote", g.day], legal, lambda s: susp.get(s, 0.0), False)


def choose_kill(g: Game, options: list) -> int | None:
    """Wolf night kill. The second wolf copies the first's legal pick to force agreement."""
    legal = list(options)
    if not legal:
        return None
    # If my partner already named a legal target tonight, match it exactly.
    if g.wolf_votes:
        for v in g.wolf_votes:
            if v.get("night") == g.day and v.get("seat") == g.partner and v.get("target") in legal:
                return v["target"]
    susp = g.suspicion()
    # Prefer a claimed seer, then a seer-confirmed villager, then the most trusted.
    claimed_seer = [s for s in g.seer_claimants() if s in legal]
    if claimed_seer:
        return pick(["kill-seer", g.day], claimed_seer, lambda s: s, False)
    confirmed = g.confirmed()
    confirmed_village = [s for s in legal if confirmed.get(s) == "village"]
    if confirmed_village:
        return pick(["kill-conf", g.day], confirmed_village, lambda s: s, False)
    return pick(["kill-trust", g.day], legal, lambda s: susp.get(s, 0.0), False)


def choose_protect(g: Game, options: list) -> int | None:
    """Doctor protect: a claimed seer first, else the most trusted living seat.

    Options already exclude the doctor and last night's target, so the no
    self-heal / no repeat rules are satisfied by construction.
    """
    legal = list(options)
    if not legal:
        return None
    claimed_seer = [s for s in g.seer_claimants() if s in legal]
    if claimed_seer:
        return pick(["protect-seer", g.day], claimed_seer, lambda s: s, False)
    susp = g.suspicion()
    return pick(["protect", g.day], legal, lambda s: susp.get(s, 0.0), False)


def choose_inspect(g: Game, options: list) -> int | None:
    """Seer inspect: the most suspicious unchecked living seat (most informative)."""
    legal = list(options)
    if not legal:
        return None
    susp = g.suspicion()
    return pick(["inspect", g.day], legal, lambda s: susp.get(s, 0.0), True)


def who(g: Game, seat: int) -> str:
    for p in g.players:
        if p["seat"] == seat:
            return f"seat {seat} ({p['name']})"
    return f"seat {seat}"


def choose_statement(g: Game, max_chars: int):
    """A short, legal statement (or None to pass) that steers the vote our way."""
    susp = g.suspicion()
    confirmed = g.confirmed()
    living_others = [s for s in g.living if s != g.me]

    if g.role == "seer":
        found_wolf = pick(["claim", g.day], [s for s in living_others if confirmed.get(s) == "wolf"], lambda s: s, False)
        if found_wolf is not None and g.day >= 2:
            return f"I am the seer. I inspected {who(g, found_wolf)} and they are a werewolf. Vote them out."
        return None

    if g.role in ("doctor", "villager"):
        target = pick(["accuse", g.day], living_others, lambda s: susp.get(s, 0.0), True)
        if target is not None and susp.get(target, 0.0) >= 0.5:
            return f"My read puts the most pressure on {who(g, target)}; I'll vote there unless someone clears them."
        return None

    if g.role == "werewolf":
        target = pick(["wolf-talk", g.day], [s for s in living_others if s != g.partner], lambda s: susp.get(s, 0.0), False)
        if target is not None:
            return f"I think {who(g, target)} has been off; worth a closer look today."
        return None
    return None


# --------------------------------------------------------------- dispatch
def decide(pending: dict) -> dict:
    """Turn one pending decision into an /act body."""
    g = Game(pending["view"])
    kind = pending["decision"]
    options = pending.get("options", [])
    game_n = pending["game"]

    if kind == "speak":
        text = choose_statement(g, pending.get("max_chars", MAX_CHARS))
        if text is None:
            return {"game": game_n, "decision": "speak", "pass": True}
        folded = " ".join(text.split())[:MAX_CHARS]
        return {"game": game_n, "decision": "speak", "text": folded}

    if kind == "vote":
        target = choose_wolf_vote(g, options) if g.role == "werewolf" else choose_village_vote(g, options)
        return {"game": game_n, "decision": "vote", "target": target}

    if kind == "kill":
        return {"game": game_n, "decision": "kill", "target": choose_kill(g, options)}

    if kind == "protect":
        return {"game": game_n, "decision": "protect", "target": choose_protect(g, options)}

    if kind == "inspect":
        return {"game": game_n, "decision": "inspect", "target": choose_inspect(g, options)}

    return {"game": game_n, "decision": kind, "target": None}


def run() -> int:
    if not ENV_URL:
        sys.stderr.write("MAKEFUN_ENV_URL is not set\n")
        return 1
    while True:
        obs = post("/observe", {})
        if obs.get("done"):
            break
        for pending in obs.get("pending", []):
            act = decide(pending)
            try:
                post("/act", act)
            except urllib.error.HTTPError:
                # A refusal changes nothing; fall back so no game stalls.
                fb = {"game": pending["game"], "decision": pending["decision"]}
                if pending["decision"] == "speak":
                    fb["pass"] = True
                else:
                    fb["target"] = None
                post("/act", fb)
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
