# make.fun werewolf-agent-01 entry

A deterministic, standard-library-only Werewolf agent for the make.fun
**werewolf-agent-01** bounty (500 USDC). It needs no model key: every decision is
a pure function of that game's view and the trial seed, so the live run and its
replay match byte for byte.

## Entrypoint

`main.py` at the repo root. The grader runs it as `python3 -P -s /bot/main.py`
(Python 3.13). It speaks the agent/v1 protocol to the env at `MAKEFUN_ENV_URL`:
polls `POST /observe`, decides each pending turn, and answers with `POST /act`,
exiting once the trial is `done`.

## Strategy

- **Suspicion model** (adapted from the deepwolf copilot): a prior of
  `unknown wolves / unknown living others`, hardened to 0 or 1 for everything the
  seat has confirmed (own role, packmate, a seer result, a revealed corpse or
  exile), then nudged by who helped lynch confirmed wolves vs. confirmed
  villagers, renormalised so the unknown scores sum to the wolves still at large.
- **Doctor** never self-heals and never repeats last night's target (the env's
  options already exclude both); it shields a claimed seer, else the most trusted
  living seat.
- **Second wolf copies the first wolf's legal kill** so the night actually lands.
- **A second seer claim is treated as almost surely a wolf**; if we are the real
  seer, any other seer claimant is a wolf.
- **Day votes bandwagon the current unique plurality leader** when that seat is a
  plausible wolf, since votes here are public, sequential and settled by
  plurality.

## Credit and license

This entry is released under the MIT License (see `LICENSE`). The suspicion
scoring is adapted from:

- **deepwolf** — https://github.com/JuneQQQ/deepwolf — MIT License. The prior,
  the confirm-hardening and the lynch-behaviour nudges follow
  `deepwolf/copilot/advisor.py`.

Ruleset and role-strategy references (no code copied):

- **google/werewolf_arena** — https://github.com/google/werewolf_arena —
  Apache-2.0.
- **werewolf-agi-house-2024** — https://github.com/cschubiner/werewolf-agi-house-2024 — MIT.

The night policies, bandwagon voting, role-claim handling and statement templates
are original work for this competition's exact rules.

