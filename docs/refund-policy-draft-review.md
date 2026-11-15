# Refund Policy — DRAFT EDGE-CASE REVIEW (not published, not merged, not live)

Status: DRAFT ONLY. The /refund page exists on the development branch with a
"DRAFT — pending founder and legal approval" banner and NO footer link. It must
not be published, merged, or shown to customers until Manthan and a legal
reviewer approve every item below.

Draft terms under review (agent proposal, not approved):
7-day full refund on the first paid subscription payment (including a first
annual payment); renewals non-refundable; cancel anytime (service runs to the
paid period's end); refunds processed in 7-10 working days; service-credit
option offered before cash refund.

## Edge cases requiring a decision

1. Definition of "first payment". A 14-day free trial precedes the first paid
   invoice. Confirm: does the 7-day window start at the first PAID payment
   (recommended) or at signup? Wording must say "first successful payment,
   starting the day payment completes".
2. Trial-to-paid conversions with zero usage. A business that pays, uses
   nothing, and refunds may repeat across new accounts. Add an abuse clause:
   one refund per business identity (name + GSTIN/phone), at discretion.
3. Annual first payment. Full refund inside 7 days as drafted. Edge: annual
   buyer at day 6 who has sent 200 recovery messages. Decide: full refund
   regardless (keep it simple, founding-partner goodwill) or pro-rated usage.
   Recommendation: full refund inside 7 days, no pro-rating, worded as
   "founder's discretion on exceptional usage".
4. Growth/Enterprise mid-cycle downgrade vs refund. Cancel-anytime stops
   renewals; it does not refund the paid period. Wording must say cancellation
   "stops future charges" and never "ends billing".
5. Renewal failure after cancellation. If a renewal payment is attempted after
   a cancellation request and succeeds, that charge must be auto-refundable.
   Requires a billing note: cancelled status gates renewals (verify in code
   before launch; billing.py cancel path).
6. Failed-payment recoveries in the refund window. A business that collected
   recovered payments (our product helped recover X) and then refunds: the
   recovered money belongs to the business, not us. No clawback. State it.
7. Taxes. Refunds must reverse GST as invoiced. Wording: "refunds exclude or
   reverse applicable taxes as required by law". Requires GST-registered
   decision before launch.
8. Razorpay fees. A refund returns the customer's full amount; payment-gateway
   fees are not always returned by the processor. Decide: absorb (recommended
   at founding scale) or net-off. Must be stated in writing either way.
9. Refund method. Refund to the original payment instrument via Razorpay
   refunds API. Never cash, never a different account (anti-fraud wording).
10. Chargebacks and disputes. If the customer disputes the charge with their
    bank after requesting a refund, both rails can move. Wording: refund
    requests and bank disputes cannot run in parallel; one cancels the other.
11. Service credits. "Service credit option" needs a value definition (credit =
    equal value, 12-month validity, non-transferable, non-redeemable for cash)
    and must be optional, never forced, to stay consumer-law safe.
12. Refund processing time. "7-10 working days" is processor-dependent. Wording:
    "initiated within 2 working days; bank/posted timelines vary (typically
    5-10 working days)". Do not promise the bank's leg.
13. Statutory rights. Clause: nothing in these terms limits rights under the
    Consumer Protection Act, 2019. Standard, non-negotiable.
14. Currency and geography. INR only, services rendered in India, governed by
    Indian law; jurisdiction named (recommend: courts at the founder's city).
15. Contact and turnaround. A named contact (recursathi@gmail.com today) and a
    2-working-day acknowledgement promise.
16. Data after refund. Refunded account: export offered, data deleted within
    30 days per the privacy page (consistent wording required across pages).

## Wording that must not appear until legal review

- Any absolute "100% money-back guarantee" (state the 7-day scope instead).
- "No questions asked" (keeps discretion away from the abuse clause).
- "Instant refund" (processor timelines).
- Any auto-acceptance of refund requests by the product (human confirmation
  only, consistent with the no-auto-money rule).

## Implementation notes (for the merge, still not live)

- billing.py refund helpers exist in dev; the confirm-payment path never
  auto-refunds: refunds require the founder's admin action.
- The /refund route and footer link must be re-enabled only in the same
  approved change that removes the draft banner.
