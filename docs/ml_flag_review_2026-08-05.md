# ML Flagging Review — 18 Reviewer-Rejected Conversations

**Date:** 2026-08-05
**Source:** 18 screenshots + Head of Texting misclassification notes
**Method:** every conversation was replayed through the live detectors
(`ai/prefilter/label_validator.py`, `ai/prefilter/tier1_phrases_v2.py`) to identify the
**exact regex** responsible. 16/18 reproduced in `_expected_label()`; the remaining 2 reproduced
in the Tier-1 short-circuit path. One case (#14) has no deterministic trigger — see its entry.

Screenshots are numbered by capture time (14:27 → 14:43).

---

## ✅ Status: Implemented (2026-08-05, same day)

All fixes below are shipped and verified. **17/18 no longer produce the rejected verdict; the
18th (#12 Darren Miller) now produces a *correct* flag** (was a false "Sold" claim closing a live
lead — now a legitimate "Maybe Later" coaching note, a real improvement rather than a bug).

**What shipped:**
- **Pattern fixes** for RC-1 through RC-7, RC-9, RC-10 in `ai/prefilter/label_validator.py` and
  `ai/prefilter/tier1_phrases_v2.py` — see the fix-order table below for what's now live.
- **New structural veto layer**, `ai/prefilter/label_vetoes.py` — three context checks
  (`contact_confirmed_address`, `contact_engaged_on_property`, `sold_refers_to_subject_property`)
  that hold regardless of which regex phrasing triggered the underlying detector, so the *next*
  unseen variant of these mistakes gets caught too, not just the 18 in this review.
- **Defensible-alternative routing** (RC-8): `is_defensible_alternative()` in `label_validator.py`
  + `DEFENSIBLE_ALTERNATIVE_SUFFIX`/`DEFENSIBLE_ALTERNATIVE_CONFIDENCE` in `ai/prefilter/_guards.py`.
  DNC vs Not Interested (no explicit opt-out) and specialist labels (Investor/Realtor/Wholesaler)
  vs generic Not Interested still surface as flags — so label drift stays visible to the Head of
  Texting — but route to `needs_review` confidence instead of counting as a texter error.
- **37 regression tests** added to `tests/test_false_flag_regressions.py`, one per conversation
  (plus 3 unit tests on the new veto functions). Full suite compared against `git stash` baseline:
  **zero new failures** — the pre-existing failures (1 in this file, 48 across `tests/` overall)
  are unchanged before and after.
- **Not yet done:** the mandatory `FALSE-CLEAN ≤ 5%` gate (`python scripts/eval_prefilter.py
  --limit 500`) could not run on this machine — local `conversation_scores` is empty (documented,
  pre-existing blocker). Run it against a populated DB (Railway or a restored dump) before
  deploying. Not committed to git yet.

See the approved plan for full design rationale: `C:\Users\vos\.claude\plans\set-a-plan-and-snazzy-sprout.md`.

---

## Executive summary

The 18 rejections collapse into **10 root causes**, and **3 regexes account for 8 of the 18**:

| # | Root cause | Location | Cases |
|---|---|---|---|
| RC-1 | `who ... is <word>?` treated as Wrong Number | `label_validator.py:108` | 2, 5, 8, 16, 18 |
| RC-2 | "I'm not \<word\>" guesses the word is a person's name | `label_validator.py:110` | 1, 17 |
| RC-3 | "way off base" (a *price* objection) treated as Wrong Number | `label_validator.py:67` | 13 |
| RC-4 | Bare `\bsold\b` matches any use of the word | `label_validator.py:358` | 12, 15 |
| RC-5 | "under contract" classified as Sold | `tier1_phrases_v2.py:_SOLD_SC_PATTERNS` | 4 |
| RC-6 | "haven't owned" classified as Wrong Number | `tier1_phrases_v2.py:_WRONG_NUMBER_PATTERNS` | 7 |
| RC-7 | DNC vocabulary gaps — hostile/removal requests unmatched | `label_validator.py:_DNC` | 3, 18 |
| RC-8 | Generic "Not Interested" overrides a *more specific* texter label | `tier1_phrases_v2.py` Check 8 | 9, 10, 14 |
| RC-9 | False "contact reversal" → Potential | reversal detector | 6, 11 |
| RC-10 | Reversal check runs **before** the condescension guard | ordering | 6 |

**Confirms all three of the Head of Texting's patterns**, and adds a fourth the notes did not
mention: the model overrides valid *specialist* labels (Investor, Decision Maker, Listed) with the
generic "Not Interested".

### Verified DNC vocabulary gaps

The Head's own DNC examples were tested against the live `_DNC` list:

| Phrase | Currently detected? |
|---|---|
| "Don't call me again" / "Never text me again" / "Stop" / "I'm on the Do Not Call list" | ✅ yes |
| "Remove me" / "lose my number" / "take me off your list" / "leave me alone" | ✅ yes |
| **"You should die."** | ❌ **no match** |
| **"Delete this number."** | ❌ **no match** |
| **"Don't bother me again."** | ❌ **no match** |
| **"Get fucked"** | ❌ **no match** (profanity list misses it) |
| **"remove this number off the list"** | ❌ **no match** ("remove me" works, "remove this number" does not) |
| **"Go awayu"** (typo) | ❌ no match — "Go away" works, the typo does not |

---

## Screenshot-by-screenshot review

### Screenshot 1 — Tim McDonald (Kapil21"SC")
**ML said:** Not Interested → should be **Wrong Number**

**Issue.** The contact wrote *"No I'm not dead yet"*. The regex at `label_validator.py:110`
(`\bi'?m\s+not\s+(?!interested|selling|ready|…)[A-Za-z]{3,}\b`) is a **name guesser** — it assumes
any word after "I'm not" that isn't on a short blocklist is a person's name, so it read
"I'm not **dead**" as "I'm not \<Name\>" and returned Wrong Number.

**Feedback.** This is the opposite of a wrong number. Tim answered to his own name, joked about
being alive, and then volunteered *"My next door neighbor passed away"* — an unprompted **referral
lead**. The texter's "Not Interested" is acceptable; "Potential" or a referral label would be
better. Wrong Number is indefensible.

**How to fix.**
1. Invert RC-2's logic — a blocklist can never be complete. Require the captured word to look like
   a name: `[A-Z][a-z]{2,}` **and** not appear in a stoplist of common adjectives/adverbs
   (`dead, sure, necessarily, really, ready, able, going, comfortable, happy, interested…`).
   Better still, only fire when the word matches the **contact's own first name or the name used by
   the texter in the opener** — that is the only case that actually means "you have the wrong person".
2. Add a hard suppressor: if the message contains a life-status idiom
   (`not dead yet`, `still alive`, `above ground`), never classify as Wrong Number.
3. Add "neighbor/relative passed away" to the **referral** signals so this routes to Potential.

---

### Screenshot 2 — Adele Molina (Mez1061"SC")
**ML said:** Not Interested → should be **Wrong Number**

**Issue.** `label_validator.py:108` — `\bwho\s+(the\s+)?(hell\s+)?is\s+\w+\?` — matched
*"Who is this?"*. `\w+` accepts **"this"**, so a generic identity question is treated as a
wrong-number declaration. (Root cause RC-1; this is the Head's Pattern #1.)

**Feedback.** Adele never said the texter reached the wrong person. She asked who was messaging,
got an answer, and then asked *"That's wonderful, Jack. How are you able to get my cellular
number?"* — she is engaged and challenging the source of her number. She is the right person.
Correct label is Not Interested (as the texter had it), or Potential given she kept replying.

**How to fix.**
1. **Narrow the pattern to real names only:**
   `\bwho\s+(the\s+)?(hell\s+)?is\s+(?!this\b|that\b|it\b|u\b|you\b|dis\b)(?=[A-Z])[A-Za-z]{2,}\?`
   — "Who is **Amjad**?" stays a signal; "Who is **this**?" no longer fires.
2. Add an explicit **never-Wrong-Number** list, checked before `_WRONG_NUMBER`:
   `who is this`, `who's this`, `who are you`, `who dis`, `who?`, `how did you get this number`,
   `where did you get my number`, `how do you have my number`.
3. Per the Head's rule: only allow Wrong Number when the contact **confirms** the mismatch —
   require a second signal (`wrong number`, `not me`, `I'm not <the name in the opener>`,
   `don't own`, `never owned`).

---

### Screenshot 3 — Margina Guzman (Kapil21"SC")
**ML said:** Do Not Call → should be **Not Interested**

**Issue.** Two failures at once. The contact wrote *"Get fucked"* and *"Go awayu"*. Neither is
matched by `_DNC_PROFANITY_INSULTS` or `_DNC` (verified — "Go away" matches, the typo "Go awayu"
does not; "Get fucked" is not in the profanity list at all). With no DNC signal, the bare "No"
matched `_NOT_INTERESTED` and Tier-1 Check 8 short-circuited to Not Interested.

**Feedback.** "Get fucked" + "Go away" is hostile and an unambiguous demand to stop contact. The
texter's **Do Not Call is correct** — and per the team's own convention, DNC is the accepted close
for this tone. The ML downgrading it to Not Interested is exactly backwards.

**How to fix.**
1. Add to `_DNC_PROFANITY_INSULTS`: `get\s+f+u+c+k+e+d`, `f+u+c+k\s+(off|you|u)\b`,
   `piss\s+off`, `screw\s+(you|off)`, `go\s+to\s+hell`, `you\s+should\s+die`, `drop\s+dead`.
2. Make dismissal patterns typo-tolerant: `\bgo\s+away\w*\b` (catches "awayu", "awayyy"),
   `\b(leave|lose)\s+me\s+alone\b`, `\bdon'?t\s+bother\s+me\b`.
3. **Rule:** when hostility or profanity is directed at the texter, DNC is always defensible —
   never emit a "should be Not Interested" flag against a texter-assigned DNC. See RC-8 fix.

---

### Screenshot 4 — Kimberly Stephens (Kapil21"SC")
**ML said:** Listed → should be **Sold**

**Issue.** `tier1_phrases_v2.py` `_SOLD_SC_PATTERNS` contains
`\b(already\s+sold|just\s+sold|under\s+contract|sold\s+it)\b`. *"We're under contract."* matched
`under contract` and short-circuited as **Sold**.

**Feedback.** Under contract is **not sold** — the sale has not closed, and a meaningful share of
contracts fall through. It is a live pipeline property. The texter's "Listed" is the better label;
at minimum this is Listed/Pending, not Sold.

**How to fix.**
1. Remove `under\s+contract` from `_SOLD_SC_PATTERNS`.
2. Add it to `_LISTED` in `label_validator.py` alongside a new `pending sale` / `in escrow` /
   `accepted an offer` group.
3. If a distinct outcome is wanted, introduce a **Pending** classification; until then Listed is
   the correct destination. Sold should require completed-sale language
   (`sold it`, `already sold`, `closed on it`, `new owner`, `no longer own`).

---

### Screenshot 5 — Justin Rzepka (Kapil21"SC")
**ML said:** Undefined → should be **Wrong Number**

**Issue.** Identical to #2 — RC-1. The only contact message in the whole thread is *"Who is this?"*,
which matched `label_validator.py:108`.

**Feedback.** A single "Who is this?" carries **no information** about whether the right person was
reached. The texter answered and the contact never replied. The honest label is Undefined (what the
texter chose) or Stopped Responding. Wrong Number asserts a fact the transcript does not contain.

**How to fix.** Apply the RC-1 fixes from #2. Additionally: when the **only** contact message is an
identity question and the contact never replies again, the conversation must classify as
**Undefined / Stopped Responding** — there is not enough evidence for any outcome label.

---

### Screenshot 6 — Christopher Brady (Kapil21"SC")
**ML said:** Not Interested → should be **Potential**
*(ML reason: "detected contact reversal — initial disinterest followed by interest/inquiry")*

**Issue.** Two bugs. (a) The reversal detector read the contact's final message —
*"No, I do not all the houses in my area 500,000 on they're not fixer-upper"* — as renewed interest
because it contains a price and property vocabulary. It is a **refusal** containing a number.
(b) The audit's own bullets say *"Contact responded with mockery/condescension … DO Not Call is an
accepted close for this tone"* — the condescension guard **did** recognise
"What part of no don't you understand?" (verified: `_CONDESCENSION_RE` matches it), yet the reversal
verdict still won. The reversal check runs **before** the guard.

**Feedback.** Christopher said "No", then *"What part of no don't you understand?"*, then refused
again. There is no reversal anywhere in the thread — it escalates from refusal to hostility. Not
Interested is right; Do Not Call would also be defensible. Potential is the worst possible reading
and would put a hostile contact back into the follow-up rotation.

**How to fix.**
1. **Reversal must require a positive signal, not just topic words.** Require an affirmative
   (`yes`, `sure`, `what's your offer`, a question about price/process **without** a negation) and
   **suppress** when the same message contains `no`, `not`, `don't`, `won't`, `never`.
2. **Reorder:** run the condescension/hostility guard **before** the reversal check. Mockery ends
   the conversation — it can never be a reversal.
3. Never allow "Potential" to override a texter's Not Interested when the contact's **final**
   message is a refusal.

---

### Screenshot 7 — Dan Wiseman (Kapil21"SC")
**ML said:** Sold → should be **Wrong Number**

**Issue.** `tier1_phrases_v2.py` `_WRONG_NUMBER_PATTERNS` contains `\bhaven'?t\s+owned\b`, which
matched *"Haven't owned that house for years"*.

**Feedback.** Dan is a **former owner**, not a wrong number. He knew the property, confirmed prior
ownership, and the texter correctly pivoted to a referral close. "Haven't owned it for years" means
the property changed hands — that is **Sold** (the texter's label) or a no-longer-owner outcome. It
is not a misdialled number.

**How to fix.**
1. Move `haven'?t\s+owned`, `no\s+longer\s+own`, `used\s+to\s+own`, `sold\s+it\s+years\s+ago`
   out of `_WRONG_NUMBER_PATTERNS` and into the **Sold / former-owner** group.
2. Keep Wrong Number strictly for **identity** mismatch ("I'm not Dan", "wrong number",
   "never owned that", "don't know that address"). Past-tense ownership is an *ownership* fact, not
   an identity fact.
3. Note the difference: `never owned` → Wrong Number; `haven't owned … for years` → Sold.

---

### Screenshot 8 — Anthony (Kapil21"SC")
**ML said:** Undefined → should be **Wrong Number**

**Issue.** RC-1 again — sole contact message *"Who is this?"* matched `label_validator.py:108`.

**Feedback.** Same as #5. The contact asked who was texting, the texter explained, and no reply
came. Nothing establishes a wrong number. Undefined / Stopped Responding is correct.

**How to fix.** RC-1 fixes from #2, plus the "identity question + no further reply → Undefined" rule
from #5. Note this case is the third identical failure in the batch — fixing this one regex
resolves five of the eighteen rejections.

---

### Screenshot 9 — Dawnisha Gaston (Noah1056"SC")
**ML said:** DNC → should be **Not Interested**

**Issue.** Interesting split: `_expected_label()` actually returns **"Do Not Call"** for this text
(the `I am a Realtor` → `_DNC_RELATIVE_REALTOR` rule fires) — it *agrees* with the texter. But
Tier-1 Check 8 short-circuits on "No thank you" / "No" and emits Not Interested **before**
`label_validator` is consulted, so the earlier, cruder verdict wins.

**Feedback.** The contact declined twice and then disclosed *"I am a Realtor"*. The team's
convention treats a realtor contact as DNC. Whether DNC or Not Interested is preferred, both are
defensible — which means **no flag should have been raised at all**. Flagging the texter here is
noise that trains reviewers to ignore the system.

**How to fix.**
1. **Ordering:** Tier-1 Check 8 must not short-circuit before `_expected_label()` has been given a
   chance to produce a higher-priority label (DNC, Wrong Number, Sold, Listed, Bluffer).
2. **Equivalence rule (highest-value fix in this review):** define DNC ↔ Not Interested as
   **interchangeable** when there is no explicit opt-out, and never flag one as the other. This is
   already written policy in the project's decision log; it is not enforced in this code path.
3. Add `i\s+am\s+a\s+realtor` to the set of labels where the texter's judgment is accepted as-is.

---

### Screenshot 10 — Lonnie Mincy (Noah1056"SC")
**ML said:** Investor → should be **Not Interested**

**Issue.** `_NOT_INTERESTED` matched "No thanks" and "No, I don't", and Tier-1 Check 8
short-circuited to the generic Not Interested — overwriting the texter's more specific **Investor**
label. (RC-8.)

**Feedback.** The contact explicitly self-identified: *"I don't I'm an investor and so are my
friends"*. "Investor" is a **specialisation** of not-interested, and it is far more useful — it
identifies a possible partner/lead source and it is exactly why the texter pivoted to the partner
call. Replacing it with "Not Interested" destroys information and flags a texter who did the right
thing.

**How to fix.**
1. Build a **label-compatibility map**. When the texter's label is a strict specialisation of the
   ML's (`Investor`, `Realtor`, `Decision Maker`, `Wholesaler` ⊂ `Not Interested`), treat the
   texter's label as **correct** and suppress the flag.
2. Add contact-side self-identification patterns (`i'?m\s+an?\s+investor`, `we\s+buy\s+houses`,
   `i\s+flip\s+houses`) that map to **Investor** and outrank generic NI.
3. General principle: the ML should only flag a label when it is **wrong**, never when it is merely
   **less specific** than what the texter chose.

---

### Screenshot 11 — Melody Gregory (Noah1056"SC")
**ML said:** Not Interested → should be **Potential**
*(ML reason: "detected contact reversal")*

**Issue.** RC-9. The reversal detector fired on
*"No. If and when we are ready to sell we will be using our friend who is a realtor"* — the phrase
`ready to sell` reads as future intent, and the conditional `if and when` was ignored. The final
message (*"Our house is not a fixer upper lol. I don't know anyone off hand who is looking to
sell"*) is another refusal.

**Feedback.** Every one of Melody's four messages is a decline. She also named a competing agent —
if anything this is **Listed**-adjacent (has a realtor lined up), certainly not Potential. Calling
this a reversal would push a firmly-declined contact back into follow-up.

**How to fix.**
1. Suppress reversal when the interest phrase is inside a **conditional**:
   `if and when`, `if we ever`, `should we decide`, `in the event`, `if I ever`.
2. Require the reversal signal to appear in a message with **no leading negation** — this message
   literally starts with "No."
3. Route "we will be using our friend who is a realtor" to the existing `_DNC_RELATIVE_REALTOR` /
   Listed logic rather than to interest.
4. Same ordering rule as #6: if the contact's **last** message is a refusal, reversal cannot fire.

---

### Screenshot 12 — Darren Miller (Noah1056"SC")
**ML said:** Not Interested → should be **Sold**

**Issue.** RC-4 — bare `\b(sold|already\s+sold)\b` in `label_validator.py:358` matched the word
"sold" inside *"In 4 months it **could be sold** if I get an outrageous offer"*. The pattern has no
tense or conditionality awareness.

**Feedback.** Nothing has been sold. This is a **hypothetical future sale conditional on price** —
in fact it is a textbook **Above Market Value / warm lead**: the owner named a condition
("outrageous offer"), engaged across five messages, and rejected the range as too low. Labelling it
Sold closes an active opportunity. This is the Head's Pattern #3 in its most costly form.

**How to fix.**
1. **Require completed-sale grammar.** Replace the bare pattern with forms that carry a completed
   action: `\b(already\s+sold|just\s+sold|sold\s+it|has\s+been\s+sold|we\s+sold|i\s+sold\s+(it|the\s+(house|property)))\b`.
2. **Suppress on conditional/future modals** within ~60 characters of "sold":
   `could be sold`, `would be sold`, `might be sold`, `if I get`, `if someone offers`, `will be sold`,
   `it could sell`.
3. Add a **subject-property test** (see #15) — Sold must refer to *this* address.
4. When a price condition is present, prefer **Abv MV / Potential**, never Sold.

---

### Screenshot 13 — Max Kielcz (Noah1056"SC")
**ML said:** Not Interested → should be **Wrong Number**

**Issue.** RC-3. `label_validator.py:67` contains `\bway\s+off\s+base\b` in the **Wrong Number**
list. The contact wrote *"You're way off base with that price."* — an objection to the **offer**,
which the pattern misreads as "you've got the wrong property/person".

**Feedback.** Max is unambiguously the owner: he discussed his property, rejected the
$187,200–$249,600 range, said *"it's too much for you"*, and closed warmly with *"No, sorry. I'll
keep you in mind."* That is a **price disagreement / Above Market Value**, and the friendly close
makes it a **Maybe Later** candidate. Wrong Number is the least accurate label available.

**How to fix.**
1. **Delete `way\s+off\s+base` from `_WRONG_NUMBER` entirely.** In this domain the idiom is
   overwhelmingly about price.
2. Add it to the **Above Market Value / price-objection** signals, next to the existing
   `_BUYER_SIDE_REJECTION_RE` ("too low", "worth more").
3. Guard rule: no Wrong Number verdict when the contact has **discussed price or property
   condition** — engaging with the offer proves they are the right person.
4. Add `I'll keep you in mind`, `keep my number`, `check back later` to **Maybe Later**.

---

### Screenshot 14 — Kathleen Charlton (Thor1068"SC")
**ML said:** Decision Maker, Not interested → should be **Wrong Number**

**Issue.** This one does **not** reproduce in any deterministic pattern — I tested her full text
against `_WRONG_NUMBER`, `_NOT_THIS_PERSON_PATTERNS` and the Tier-1 wrong-number list, and nothing
fires. The verdict therefore came from the **Tier-3 statistical classifier** (or a Tier-2 nearest
neighbour), which produces no explainable trigger. Note the audit's cited evidence —
*"Kathleen Charlton replied: 'Ok so what's your??'"* — is a truncated, semantically empty fragment.

**Feedback.** Kathleen said *"I guess you can ask, but I have no plans of selling or moving"* and
*"That would be a NO!"*. She confirmed she is the owner and simply is not selling. The texter's
"Decision Maker, Not interested" is **exactly right** on both counts. Wrong Number is unsupported by
anything in the transcript.

**How to fix.**
1. **Never let T2/T3 assign Wrong Number.** Identity is a categorical claim that requires explicit
   evidence — restrict Wrong Number to deterministic pattern matches only, and have the statistical
   tiers defer to T4/Groq when they lean that way.
2. Require an **evidence quote** for every label flag. If the flag cannot cite a contact message
   that supports it, suppress the flag (this case's quote demonstrably does not support it).
3. Add "no plans of selling/moving" to `_NOT_INTERESTED` and to the **owner-confirmed** signal set,
   which should block Wrong Number outright.

---

### Screenshot 15 — Joseph Ortenzi (Thor1068"SC")
**ML said:** Decision Maker, Not interested → should be **Sold**

**Issue.** RC-4 — bare `\bsold\b` matched *"I sold off 20 units"*. `_SOLD_NEIGHBOR_CONTEXT` does
suppress neighbour sales and "sold a 2nd/3rd property", but it has no rule for
`sold off N units` / `sold N condos`. This is **verbatim the Head of Texting's example**.

**Feedback.** Joseph is a career rehabber describing his **track record** — "For 18 years I rebabed
houses and a major recession. I sold off 20 units." Those 20 units are not the subject property. He
had already said *"Not really. Love it."* about the property in question: he **owns it and is
keeping it**. He is also a strong referral/investor contact. The texter's label is correct.

**How to fix.**
1. Extend `_SOLD_NEIGHBOR_CONTEXT` (better: rename it `_SOLD_OTHER_PROPERTY_CONTEXT`) with
   quantity and portfolio forms:
   `sold\s+(off\s+)?\d+\s+(units?|condos?|houses?|homes?|properties|doors)`,
   `sold\s+(a\s+few|several|many|dozens|lots)\b`,
   `i'?ve\s+sold\s+\d+`, `flipped\s+\d+`.
2. Add **past-career context** suppressors: `for\s+\d+\s+years\s+I`, `I\s+used\s+to\s+(flip|rehab)`,
   `rehabbed`, `rebabed` (real typo from this transcript).
3. **Subject-property rule (the durable fix):** only assign Sold when the sale is tied to *this*
   property — the message references the subject address, or uses a singular definite reference
   (`it`, `the house`, `my house`, `this property`). A **plural or numeric object**
   ("20 units", "2 condos") can never be the subject property.
4. Positive signal: `Love it` / `not selling` in the same thread should veto Sold.

---

### Screenshot 16 — James Naber (M1026"SC")
**ML said:** Undefined → should be **Wrong Number**

**Issue.** RC-1, fourth occurrence — *"Who is this?"* matched `label_validator.py:108`.

**Feedback.** One identity question, an answer from the texter, no further reply. Nothing indicates
the wrong person was reached. Undefined (the texter's label) or Stopped Responding is correct.

**How to fix.** RC-1 fixes from #2. Given four identical cases in one 18-conversation sample, this
single regex is the highest-frequency defect in the batch — fix it first.

---

### Screenshot 17 — Rachelle Bohannon (M1026"SC")
**ML said:** Potential → should be **Wrong Number**

**Issue.** RC-2. `label_validator.py:110` matched *"I'm not **necessarily** willing to talk on the
phone"* — the name-guesser treated the adverb "necessarily" as a person's name.

**Feedback.** This is the most damaging error in the batch: Rachelle is a **warm lead**. She
addressed the texter by name, said she'd consider it if given information, thanked him, and
**volunteered the subject address**: *"707 Pritz Avenue Dayton Ohio"* — the exact property in the
opener. Confirming the address is the strongest possible proof of a correct number. The texter's
"Potential" is right, and the texter properly sent a handoff message.

**How to fix.**
1. Apply the RC-2 fix from #1 — require name-shaped, capitalised tokens and exclude adverbs/
   adjectives (`necessarily`, `really`, `entirely`, `exactly`, `completely`, `particularly`, `quite`).
   Practical shortcut: **never** match when the word ends in `-ly`.
2. **Address-confirmation veto (high value):** if the contact's text contains the subject property's
   street number or street name, Wrong Number must be impossible. This one rule would have caught
   this case regardless of the regex bug.
3. Add `not necessarily`, `not particularly`, `not really` to the negation exclusion list — these
   are softeners, never identity statements.

---

### Screenshot 18 — Megan Blatnik (Resva1052)
**ML said:** Do Not Call → should be **Wrong Number**

**Issue.** Two failures compounding. (a) RC-1 — *"Who is this?"* matched
`label_validator.py:108` → Wrong Number. (b) RC-7 — her actual closing message,
*"Not interested remove this number off the contract list thanks!!"*, is an **explicit removal
request** but matches **no** `_DNC` pattern (verified: "remove me" is covered, "remove this number"
is not). So the one message that should have decided the label was invisible, and the identity
question decided it instead.

**Feedback.** This is precisely the Head of Texting's Pattern #2. "Remove this number off the list"
is a textbook opt-out — the texter's **Do Not Call is correct**, and DNC has absolute priority in
the documented label hierarchy. Mislabelling an opt-out as Wrong Number is also the highest-risk
error type here, since it can leave a contact who asked to be removed inside the contactable pool.

**How to fix.**
1. Add to `_DNC`: `remove\s+(this|my|the)\s+(number|phone|contact)`,
   `take\s+(this|my)\s+number\s+off`, `delete\s+(this|my)\s+(number|contact)`,
   `off\s+(your|the)\s+(list|contract\s+list)`, `don'?t\s+bother\s+me`.
   These also close three of the Head's listed gaps ("Delete this number", "Don't bother me again").
2. Enforce the documented priority: **DNC is checked before Wrong Number** in `_expected_label()`
   — the ordering is correct in code, so the fix is purely the missing vocabulary above.
3. Apply the RC-1 fix so the identity question stops competing at all.

---

## Recommended fix order

Ranked by cases resolved per unit of work:

| Priority | Fix | Resolves |
|---|---|---|
| **1** | Narrow `who ... is <word>?` to capitalised names + add the never-WN identity-question list | 2, 5, 8, 16, 18 (5) |
| **2** | Expand `_DNC` vocabulary (removal requests, profanity, "don't bother me", typo tolerance) | 3, 18 (2) + closes 5 documented gaps |
| **3** | Require completed-sale grammar + subject-property test for Sold | 12, 15 (2) |
| **4** | Fix the "I'm not \<word\>" name-guesser (no `-ly`, capitalised only, address veto) | 1, 17 (2) |
| **5** | Label-compatibility map — never flag a *less specific* ML label over a texter's specialist label | 9, 10, 14 (3) |
| **6** | Reversal requires a positive signal + no negation + run condescension guard first | 6, 11 (2) |
| **7** | Move `under contract` → Listed; move `haven't owned` → Sold | 4, 7 (2) |
| **8** | Delete `way off base` from Wrong Number; add to price-objection | 13 (1) |
| **9** | Block T2/T3 from assigning Wrong Number; require an evidence quote per label flag | 14 (1) |

**Two cross-cutting guards worth adding regardless of the above:**

- **Address-confirmation veto** — if the contact repeats the subject address, Wrong Number is
  impossible. (Would have caught #17 alone.)
- **Engagement veto** — if the contact discussed price or property condition, Wrong Number is
  impossible. (Would have caught #13, #14, #1.)

---

## Validation requirement before shipping

Any change to `ai/prefilter/` must clear the **FALSE-CLEAN ≤ 5%** gate:

```
python scripts/eval_prefilter.py --limit 500
```

⚠️ This gate **cannot run on the local machine** — the local `conversation_scores` table is empty
(a known, documented blocker). Run it against a populated DB, or restore a dump first. All 18
conversations above should also be added as regression cases in
`tests/test_false_flag_regressions.py`.
