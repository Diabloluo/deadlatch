# Fictional post-mortem: option direction used to trust a self-reported close

This note explains a design defect that was already fixed in G1. It uses a
**fictional** option order only. It is not a live-account result, not a
customer story, not a PnL claim, and not evidence that Deadlatch intercepted a
real broker order.

## What went wrong

Early direction logic treated an option order's self-reported close side
(`buy_to_close` / `sell_to_close`) as proof that the order reduced risk. A
caller could label an opening sale as a close. The guard then returned PASS
and wrote an audit record, while the intended contract was still an open.

That is a False PASS class of defect: the tool answered "allowed" when the
position evidence did not support a close.

## Fictional reproduction

Inputs are invented. There is no account, screenshot, fill, or customer.

1. Fictional short-put style order: `symbol` is a unique full contract code
   such as `AAA 260918P00190000`, `side` is `buy_to_close`, quantity 1.
2. Portfolio snapshot either has no matching contract, or the matching
   position is the same way the order wants to trade (not an opposite open
   that this order would reduce), or the snapshot is not usable.
3. **Before G1:** the self-reported close was trusted → `PASS / 0`.
4. **After G1:** direction is inferred from the unique contract code, the
   position side, and a sufficient snapshot → `BLOCK / 3`.
5. The caller that honors BLOCK stops and does not send the order onward.
6. `Guard.check()` still appends one sanitized audit record for the BLOCK.

## Fix principle

- Option `symbol` must be the broker's unique full contract code. An
  underlying ticker reused across expiries, strikes, or rights is not enough.
- Open vs close is inferred. Self-reported close sides are not accepted as
  proof.
- A close requires a fresh snapshot, the same unique contract, the opposite
  position direction, and enough quantity.
- Missing, stale, or contradictory snapshot data fail-closed instead of
  guessing.

## Tests and remaining limits

Regression coverage lives in the option-direction tests. Remaining limits are
unchanged: Deadlatch is advisory-only, never places orders, and cannot stop a
caller that ignores BLOCK. The local audit hash chain added in v0.1.2 is
tamper-evident, not a signature and not tamper-proof; without an external
anchor, deleting the last record or the whole visible set is not reliably
detectable.

This document ships with the public candidate so the G1 fix can be explained.
It is not a launch, promotion, live-broker demo, or pricing change.
